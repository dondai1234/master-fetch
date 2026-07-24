"""Smart proxy pool for search engine rotation.

Rotates search engine requests across multiple proxies so no single IP
gets rate-limited. Round-robin per search call: each search uses one proxy,
the next search uses the next proxy, cycling through the pool. Unhealthy
proxies (connection errors) are cooled for 60s and skipped.

Config sources (merged, deduped):
  1. CLI: ``hound proxy add/list/remove/clear``
  2. Config file: ``~/.hound/search_proxies.json``
     ``{"proxies": ["http://user:pass@ip:port", ...]}``
  3. Env var: ``HOUND_SEARCH_PROXY`` (comma-separated for multiple)

A single proxy string in the env var is fully backward-compatible (pool of 1).
Max 20 proxies. State is in-memory only (resets on restart).
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

MAX_PROXIES = 20
PROXY_COOLDOWN = 60.0  # seconds — same as engine circuit breaker
_VALID_SCHEMES = ("http", "https", "socks5", "socks5h")


def _config_path() -> Path:
    """Return the path to ~/.hound/search_proxies.json."""
    return Path.home() / ".hound" / "search_proxies.json"


def _validate_proxy(proxy: str) -> str | None:
    """Validate scheme + return stripped proxy, or None if invalid."""
    if not isinstance(proxy, str):
        return None
    p = proxy.strip()
    if not p:
        return None
    scheme = urlparse(p).scheme.lower()
    if scheme not in _VALID_SCHEMES:
        logger.warning("Skipping proxy with unsupported scheme '%s' (expected %s)",
                        scheme or "(none)", ", ".join(_VALID_SCHEMES))
        return None
    return p


def _read_config_file() -> list[str]:
    """Read proxies from the config file. Returns [] if missing or malformed."""
    path = _config_path()
    if not path.exists():
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return []
        raw = data.get("proxies", [])
        if not isinstance(raw, list):
            return []
        return [p for p in (_validate_proxy(x) for x in raw) if p]
    except (json.JSONDecodeError, OSError) as ex:
        logger.warning("Failed to read search_proxies.json: %r", ex)
        return []


def _read_env_var() -> list[str]:
    """Read proxies from HOUND_SEARCH_PROXY env var (comma-separated)."""
    raw = os.environ.get("HOUND_SEARCH_PROXY", "").strip()
    if not raw:
        return []
    return [p for p in (_validate_proxy(x) for x in raw.split(",")) if p]


def load_proxies() -> list[str]:
    """Load proxies from env var + config file, deduped, order preserved.

    Env var proxies come first (priority), then config file proxies.
    Duplicates are removed (case-sensitive URL match).
    """
    env = _read_env_var()
    file = _read_config_file()
    seen: set[str] = set()
    merged: list[str] = []
    for p in env + file:
        if p not in seen:
            seen.add(p)
            merged.append(p)
    return merged[:MAX_PROXIES]


def save_proxies(proxies: list[str]) -> None:
    """Write proxies to the config file. Creates ~/.hound/ if needed."""
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    validated = [p for p in (_validate_proxy(x) for x in proxies) if p]
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"proxies": validated}, f, indent=2)


def add_proxy(proxy: str) -> int:
    """Add a proxy to the config file. Returns total count after adding.

    Raises ValueError if the proxy is invalid or the pool is full.
    """
    p = _validate_proxy(proxy)
    if not p:
        raise ValueError(
            f"Invalid proxy '{proxy}'. Expected format: http://ip:port, "
            f"https://ip:port, socks5://ip:port (or with user:pass@)."
        )
    existing = _read_config_file()
    if p in existing:
        raise ValueError(f"Proxy already configured: {p}")
    if len(existing) >= MAX_PROXIES:
        raise ValueError(f"Proxy pool is full ({MAX_PROXIES} max). Remove one first.")
    existing.append(p)
    save_proxies(existing)
    return len(existing)


def remove_proxy(index: int) -> str:
    """Remove a proxy by index from the config file. Returns the removed proxy.

    Raises IndexError if the index is out of range.
    """
    existing = _read_config_file()
    if not existing:
        raise IndexError("No proxies configured.")
    if index < 0 or index >= len(existing):
        raise IndexError(f"Index {index} out of range (0-{len(existing) - 1}).")
    removed = existing.pop(index)
    save_proxies(existing)
    return removed


def clear_proxies() -> int:
    """Remove all proxies from the config file. Returns count removed."""
    existing = _read_config_file()
    if existing:
        save_proxies([])
    return len(existing)


def list_proxies() -> list[str]:
    """List all proxies from the config file (not env var)."""
    return _read_config_file()


def redact_proxy(proxy: str) -> str:
    """Redact credentials in a proxy URL for display."""
    try:
        parsed = urlparse(proxy)
        if parsed.username or parsed.password:
            # Replace user:pass@ with ***@ 
            netloc = f"***:***@{parsed.hostname}"
            if parsed.port:
                netloc += f":{parsed.port}"
            return f"{parsed.scheme}://{netloc}"
        return proxy
    except Exception:
        return proxy


class ProxyPool:
    """Manages multiple proxies with round-robin rotation + health tracking.

    Round-robin per search call: each call gets the next proxy. If a proxy
    produces connection errors (all engines failed), it's cooled for 60s.
    If all proxies are cooled, ``get_proxy`` returns None (direct connection).

    State is in-memory only (not persisted). Resets on restart.
    """

    PROXY_COOLDOWN = 60.0

    def __init__(self, proxies: list[str]) -> None:
        if not proxies:
            raise ValueError("ProxyPool requires at least one proxy")
        self._proxies = list(proxies)
        self._state: dict[str, dict[str, float | str]] = {p: {} for p in self._proxies}
        self._idx = 0  # round-robin pointer

    @property
    def size(self) -> int:
        return len(self._proxies)

    def get_proxy(self) -> str | None:
        """Return the next available (non-cooled) proxy.

        Returns None if all proxies are cooled (caller falls back to direct).
        """
        now = time.time()
        for i in range(len(self._proxies)):
            proxy = self._proxies[(self._idx + i) % len(self._proxies)]
            state = self._state.get(proxy, {})
            until = state.get("cooled_until", 0)
            if isinstance(until, (int, float)) and until < now:
                self._idx = (self._idx + i + 1) % len(self._proxies)
                return proxy
        # All cooled → direct connection (None = no proxy).
        return None

    def mark_failed(self, proxy: str) -> None:
        """Cool a proxy that produced connection errors."""
        self._state.setdefault(proxy, {})["cooled_until"] = time.time() + self.PROXY_COOLDOWN

    def mark_success(self, proxy: str) -> None:
        """Clear cooldown for a proxy that returned results."""
        self._state.setdefault(proxy, {}).pop("cooled_until", None)

    def status(self) -> list[dict[str, str | float | bool]]:
        """Return per-proxy status for diagnostics."""
        now = time.time()
        result = []
        for p in self._proxies:
            state = self._state.get(p, {})
            until = state.get("cooled_until", 0)
            cooled = isinstance(until, (int, float)) and until > now
            result.append({
                "proxy": redact_proxy(p),
                "cooled": cooled,
                "cooled_remaining": max(0, until - now) if cooled else 0,
            })
        return result


# ── Module-level singleton ──────────────────────────────────────────

_pool: ProxyPool | None = None


def get_proxy_pool() -> ProxyPool | None:
    """Return the shared ProxyPool, or None if no proxies configured."""
    global _pool
    proxies = load_proxies()
    if not proxies:
        _pool = None
        return None
    if _pool is None or _pool.size != len(proxies) or _pool._proxies != proxies:
        _pool = ProxyPool(proxies)
    return _pool


def get_next_proxy() -> str | None:
    """Get the next proxy for a search call. Returns None if no pool / all cooled."""
    pool = get_proxy_pool()
    if pool is None:
        return None
    return pool.get_proxy()


def reset_pool() -> None:
    """Force re-creation of the pool on next access (for tests / config changes)."""
    global _pool
    _pool = None
