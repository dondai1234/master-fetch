"""Adversarial tests for the smart proxy rotation system (search_proxy.py).

Tests cover:
- ProxyPool round-robin rotation (per-call, cycling through all)
- Health tracking (cooldown on failure, recovery after cooldown)
- Config file management (add, remove, clear, list)
- Env var parsing (single + comma-separated)
- Merging env var + config file (dedup, order, max cap)
- Backwards compatibility (single proxy = pool of 1)
- Edge cases (invalid scheme, empty, duplicate, overflow)
- Redaction (credentials hidden in display)
- Metasearch integration (proxy selected per call, health marked)
"""

import os
import time
import json
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock
import pytest

from master_fetch.search_proxy import (
    ProxyPool, _validate_proxy, _read_config_file, _read_env_var,
    load_proxies, save_proxies, add_proxy, remove_proxy, clear_proxies,
    list_proxies, redact_proxy, get_proxy_pool, get_next_proxy, reset_pool,
    MAX_PROXIES, PROXY_COOLDOWN,
)


# ─── ProxyPool rotation ────────────────────────────────────────────

class TestProxyPoolRotation:

    def test_round_robin_cycles_through_all_proxies(self):
        pool = ProxyPool(["http://p1:80", "http://p2:80", "http://p3:80"])
        results = [pool.get_proxy() for _ in range(7)]
        assert results == [
            "http://p1:80", "http://p2:80", "http://p3:80",
            "http://p1:80", "http://p2:80", "http://p3:80",
            "http://p1:80",
        ]

    def test_single_proxy_always_returns_same(self):
        pool = ProxyPool(["socks5://1.2.3.4:1080"])
        assert pool.get_proxy() == "socks5://1.2.3.4:1080"
        assert pool.get_proxy() == "socks5://1.2.3.4:1080"

    def test_empty_pool_raises(self):
        with pytest.raises(ValueError, match="at least one"):
            ProxyPool([])

    def test_size_property(self):
        pool = ProxyPool(["http://a:80", "http://b:80"])
        assert pool.size == 2


# ─── ProxyPool health tracking ─────────────────────────────────────

class TestProxyPoolHealth:

    def test_mark_failed_cools_proxy(self):
        pool = ProxyPool(["http://p1:80", "http://p2:80"])
        pool.mark_failed("http://p1:80")
        assert pool.get_proxy() == "http://p2:80"
        assert pool.get_proxy() == "http://p2:80"

    def test_mark_success_clears_cooldown(self):
        pool = ProxyPool(["http://p1:80", "http://p2:80"])
        pool.mark_failed("http://p1:80")
        assert pool.get_proxy() == "http://p2:80"
        pool.mark_success("http://p1:80")
        assert pool.get_proxy() == "http://p1:80"

    def test_all_cooled_returns_none(self):
        pool = ProxyPool(["http://p1:80"])
        pool.mark_failed("http://p1:80")
        assert pool.get_proxy() is None

    def test_all_cooled_multiple_proxies_returns_none(self):
        pool = ProxyPool(["http://p1:80", "http://p2:80", "http://p3:80"])
        pool.mark_failed("http://p1:80")
        pool.mark_failed("http://p2:80")
        pool.mark_failed("http://p3:80")
        assert pool.get_proxy() is None

    def test_cooldown_expires_and_proxy_returns(self):
        pool = ProxyPool(["http://p1:80"])
        pool.mark_failed("http://p1:80")
        assert pool.get_proxy() is None
        pool._state["http://p1:80"]["cooled_until"] = time.time() - 1
        assert pool.get_proxy() == "http://p1:80"

    def test_mark_failed_unknown_proxy_does_not_crash(self):
        pool = ProxyPool(["http://p1:80"])
        pool.mark_failed("http://unknown:80")
        assert pool.get_proxy() == "http://p1:80"

    def test_status_reports_cooled_state(self):
        pool = ProxyPool(["http://p1:80", "http://p2:80"])
        pool.mark_failed("http://p1:80")
        statuses = pool.status()
        assert len(statuses) == 2
        assert statuses[0]["proxy"] == "http://p1:80"
        assert statuses[0]["cooled"] is True
        assert statuses[0]["cooled_remaining"] > 0
        assert statuses[1]["cooled"] is False


# ─── Validation ────────────────────────────────────────────────────

class TestProxyValidation:

    def test_valid_http(self):
        assert _validate_proxy("http://1.2.3.4:8080") == "http://1.2.3.4:8080"

    def test_valid_https(self):
        assert _validate_proxy("https://1.2.3.4:443") == "https://1.2.3.4:443"

    def test_valid_socks5(self):
        assert _validate_proxy("socks5://1.2.3.4:1080") == "socks5://1.2.3.4:1080"

    def test_valid_socks5h(self):
        assert _validate_proxy("socks5h://1.2.3.4:1080") == "socks5h://1.2.3.4:1080"

    def test_valid_with_auth(self):
        assert _validate_proxy("http://user:pass@1.2.3.4:8080") == "http://user:pass@1.2.3.4:8080"

    def test_strips_whitespace(self):
        assert _validate_proxy("  http://1.2.3.4:8080  ") == "http://1.2.3.4:8080"

    def test_rejects_empty(self):
        assert _validate_proxy("") is None
        assert _validate_proxy("   ") is None

    def test_rejects_none(self):
        assert _validate_proxy(None) is None

    def test_rejects_non_string(self):
        assert _validate_proxy(123) is None

    def test_rejects_invalid_scheme(self):
        assert _validate_proxy("ftp://1.2.3.4:21") is None
        assert _validate_proxy("1.2.3.4:8080") is None


# ─── Config file management ────────────────────────────────────────

class TestConfigFile:

    @pytest.fixture
    def temp_config(self, tmp_path, monkeypatch):
        config_file = tmp_path / "search_proxies.json"
        monkeypatch.setattr("master_fetch.search_proxy._config_path", lambda: config_file)
        return config_file

    def test_add_and_list_proxy(self, temp_config):
        add_proxy("http://1.2.3.4:8080")
        assert list_proxies() == ["http://1.2.3.4:8080"]
        assert temp_config.exists()
        data = json.loads(temp_config.read_text())
        assert data == {"proxies": ["http://1.2.3.4:8080"]}

    def test_add_multiple_proxies(self, temp_config):
        add_proxy("http://p1:80")
        add_proxy("socks5://p2:1080")
        assert list_proxies() == ["http://p1:80", "socks5://p2:1080"]

    def test_add_duplicate_rejected(self, temp_config):
        add_proxy("http://1.2.3.4:8080")
        with pytest.raises(ValueError, match="already configured"):
            add_proxy("http://1.2.3.4:8080")

    def test_add_invalid_rejected(self, temp_config):
        with pytest.raises(ValueError, match="Invalid proxy"):
            add_proxy("ftp://bad:21")

    def test_add_over_max_rejected(self, temp_config):
        for i in range(MAX_PROXIES):
            add_proxy(f"http://10.0.0.{i}:8080")
        with pytest.raises(ValueError, match="Proxy pool is full"):
            add_proxy("http://10.0.0.99:8080")

    def test_remove_by_index(self, temp_config):
        add_proxy("http://p1:80")
        add_proxy("http://p2:80")
        add_proxy("http://p3:80")
        removed = remove_proxy(1)
        assert removed == "http://p2:80"
        assert list_proxies() == ["http://p1:80", "http://p3:80"]

    def test_remove_out_of_range(self, temp_config):
        add_proxy("http://p1:80")
        with pytest.raises(IndexError, match="out of range"):
            remove_proxy(5)

    def test_remove_empty_list(self, temp_config):
        with pytest.raises(IndexError, match="No proxies"):
            remove_proxy(0)

    def test_clear_proxies(self, temp_config):
        add_proxy("http://p1:80")
        add_proxy("http://p2:80")
        count = clear_proxies()
        assert count == 2
        assert list_proxies() == []

    def test_clear_empty(self, temp_config):
        assert clear_proxies() == 0


# ─── Env var parsing ───────────────────────────────────────────────

class TestEnvVarParsing:

    def test_single_proxy_env_var(self, monkeypatch):
        monkeypatch.setenv("HOUND_SEARCH_PROXY", "http://1.2.3.4:8080")
        monkeypatch.setattr("master_fetch.search_proxy._config_path",
                             lambda: Path("/nonexistent_proxy_test"))
        assert _read_env_var() == ["http://1.2.3.4:8080"]

    def test_comma_separated_env_var(self, monkeypatch):
        monkeypatch.setenv("HOUND_SEARCH_PROXY", "http://p1:80,socks5://p2:1080,http://p3:80")
        assert _read_env_var() == ["http://p1:80", "socks5://p2:1080", "http://p3:80"]

    def test_env_var_strips_whitespace(self, monkeypatch):
        monkeypatch.setenv("HOUND_SEARCH_PROXY", "  http://1.2.3.4:8080  ,  socks5://5.6.7.8:1080  ")
        assert _read_env_var() == ["http://1.2.3.4:8080", "socks5://5.6.7.8:1080"]

    def test_env_var_empty(self, monkeypatch):
        monkeypatch.delenv("HOUND_SEARCH_PROXY", raising=False)
        assert _read_env_var() == []

    def test_env_var_invalid_scheme_skipped(self, monkeypatch):
        monkeypatch.setenv("HOUND_SEARCH_PROXY", "ftp://bad:21,http://good:80")
        assert _read_env_var() == ["http://good:80"]


# ─── Merging env var + config file ─────────────────────────────────

class TestProxyMerging:

    def test_env_and_config_merged_deduped(self, monkeypatch, tmp_path):
        config_file = tmp_path / "proxies.json"
        config_file.write_text(json.dumps({"proxies": ["http://p1:80", "http://p2:80"]}))
        monkeypatch.setattr("master_fetch.search_proxy._config_path", lambda: config_file)
        monkeypatch.setenv("HOUND_SEARCH_PROXY", "http://p1:80,http://p3:80")
        result = load_proxies()
        assert result == ["http://p1:80", "http://p3:80", "http://p2:80"]

    def test_env_only(self, monkeypatch, tmp_path):
        config_file = tmp_path / "proxies.json"
        monkeypatch.setattr("master_fetch.search_proxy._config_path", lambda: config_file)
        monkeypatch.setenv("HOUND_SEARCH_PROXY", "http://p1:80")
        assert load_proxies() == ["http://p1:80"]

    def test_config_only(self, monkeypatch, tmp_path):
        config_file = tmp_path / "proxies.json"
        config_file.write_text(json.dumps({"proxies": ["socks5://p1:1080"]}))
        monkeypatch.setattr("master_fetch.search_proxy._config_path", lambda: config_file)
        monkeypatch.delenv("HOUND_SEARCH_PROXY", raising=False)
        assert load_proxies() == ["socks5://p1:1080"]

    def test_none_configured(self, monkeypatch, tmp_path):
        config_file = tmp_path / "proxies.json"
        monkeypatch.setattr("master_fetch.search_proxy._config_path", lambda: config_file)
        monkeypatch.delenv("HOUND_SEARCH_PROXY", raising=False)
        assert load_proxies() == []

    def test_max_proxies_cap(self, monkeypatch, tmp_path):
        config_file = tmp_path / "proxies.json"
        config_file.write_text(json.dumps({"proxies": [f"http://p{i}:80" for i in range(MAX_PROXIES + 5)]}))
        monkeypatch.setattr("master_fetch.search_proxy._config_path", lambda: config_file)
        monkeypatch.delenv("HOUND_SEARCH_PROXY", raising=False)
        result = load_proxies()
        assert len(result) == MAX_PROXIES


# ─── Redaction ─────────────────────────────────────────────────────

class TestRedaction:

    def test_redact_with_credentials(self):
        assert redact_proxy("http://user:pass@1.2.3.4:8080") == "http://***:***@1.2.3.4:8080"

    def test_no_credentials_not_redacted(self):
        assert redact_proxy("http://1.2.3.4:8080") == "http://1.2.3.4:8080"

    def test_socks5_with_credentials(self):
        assert redact_proxy("socks5://user:pass@5.6.7.8:1080") == "socks5://***:***@5.6.7.8:1080"


# ─── Pool singleton ────────────────────────────────────────────────

class TestPoolSingleton:

    def test_get_proxy_pool_none_when_empty(self, monkeypatch, tmp_path):
        monkeypatch.setattr("master_fetch.search_proxy._config_path", lambda: tmp_path / "none.json")
        monkeypatch.delenv("HOUND_SEARCH_PROXY", raising=False)
        reset_pool()
        assert get_proxy_pool() is None
        assert get_next_proxy() is None

    def test_get_pool_caches_until_config_changes(self, monkeypatch, tmp_path):
        config_file = tmp_path / "proxies.json"
        config_file.write_text(json.dumps({"proxies": ["http://p1:80"]}))
        monkeypatch.setattr("master_fetch.search_proxy._config_path", lambda: config_file)
        monkeypatch.delenv("HOUND_SEARCH_PROXY", raising=False)
        reset_pool()
        pool1 = get_proxy_pool()
        pool2 = get_proxy_pool()
        assert pool1 is pool2
        config_file.write_text(json.dumps({"proxies": ["http://p1:80", "http://p2:80"]}))
        pool3 = get_proxy_pool()
        assert pool3 is not pool1
        assert pool3.size == 2

    def test_rotation_across_calls(self, monkeypatch, tmp_path):
        config_file = tmp_path / "proxies.json"
        config_file.write_text(json.dumps({"proxies": ["http://p1:80", "http://p2:80", "http://p3:80"]}))
        monkeypatch.setattr("master_fetch.search_proxy._config_path", lambda: config_file)
        monkeypatch.delenv("HOUND_SEARCH_PROXY", raising=False)
        reset_pool()
        assert get_next_proxy() == "http://p1:80"
        assert get_next_proxy() == "http://p2:80"
        assert get_next_proxy() == "http://p3:80"
        assert get_next_proxy() == "http://p1:80"


# ─── Metasearch integration ────────────────────────────────────────

class TestMetasearchProxyIntegration:

    def test_metasearch_uses_no_proxy_when_unconfigured(self, monkeypatch, tmp_path):
        monkeypatch.setattr("master_fetch.search_proxy._config_path", lambda: tmp_path / "none.json")
        monkeypatch.delenv("HOUND_SEARCH_PROXY", raising=False)
        reset_pool()
        from master_fetch.search_metasearch import _get_search_proxy
        assert _get_search_proxy() is None

    def test_metasearch_rotates_proxy_per_call(self, monkeypatch, tmp_path):
        config_file = tmp_path / "proxies.json"
        config_file.write_text(json.dumps({"proxies": ["http://p1:80", "http://p2:80"]}))
        monkeypatch.setattr("master_fetch.search_proxy._config_path", lambda: config_file)
        monkeypatch.delenv("HOUND_SEARCH_PROXY", raising=False)
        reset_pool()
        from master_fetch.search_metasearch import _get_search_proxy
        assert _get_search_proxy() == "http://p1:80"
        assert _get_search_proxy() == "http://p2:80"
        assert _get_search_proxy() == "http://p1:80"

    def test_metasearch_sets_module_level_proxy(self, monkeypatch, tmp_path):
        config_file = tmp_path / "proxies.json"
        config_file.write_text(json.dumps({"proxies": ["http://p1:80"]}))
        monkeypatch.setattr("master_fetch.search_proxy._config_path", lambda: config_file)
        monkeypatch.delenv("HOUND_SEARCH_PROXY", raising=False)
        reset_pool()
        import master_fetch.search_metasearch as sm
        sm._PROXY = "stale_value"
        proxy = sm._get_search_proxy()
        assert proxy == "http://p1:80"
        assert sm._PROXY == "http://p1:80"

    def test_all_cooled_falls_back_to_direct(self, monkeypatch, tmp_path):
        config_file = tmp_path / "proxies.json"
        config_file.write_text(json.dumps({"proxies": ["http://p1:80"]}))
        monkeypatch.setattr("master_fetch.search_proxy._config_path", lambda: config_file)
        monkeypatch.delenv("HOUND_SEARCH_PROXY", raising=False)
        reset_pool()
        pool = get_proxy_pool()
        pool.mark_failed("http://p1:80")
        from master_fetch.search_metasearch import _get_search_proxy
        assert _get_search_proxy() is None
