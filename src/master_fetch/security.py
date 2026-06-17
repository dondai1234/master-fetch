"""Input validation and security utilities for Hound.

URL validation with SSRF protection, input sanitization,
and safe defaults for all external-facing parameters.
"""

import ipaddress
import re
from typing import Optional
from urllib.parse import urlparse

# Maximum URL length to prevent DoS via oversized URLs
MAX_URL_LENGTH = 8192

# Blocked URL schemes (SSRF, local file access, etc.)
_BLOCKED_SCHEMES = frozenset({
    "file", "ftp", "gopher", "data", "javascript", "vbscript",
    "about", "chrome", "chrome-extension", "jar",
})

# Hostnames that must never be targeted (bypass IP checks via name resolution)
_BLOCKED_HOSTNAMES = frozenset({
    "localhost", "metadata.google.internal", "169.254.169.254",
    "0.0.0.0", "0",  # "0" from bare IPv6 without brackets (e.g. http://0:0:0:...)
})

# DNS rebinding service suffixes (nip.io, sslip.io, etc.)
# These services resolve subdomains to arbitrary IPs, enabling SSRF bypass.
_DNS_REBINDING_SUFFIXES = (
    ".nip.io", ".sslip.io", ".xip.io", ".nip.name",
    ".1u.ms",  # resolves to loopback
)

# Private/reserved IP ranges that must never be targeted
_PRIVATE_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),       # Loopback
    ipaddress.ip_network("10.0.0.0/8"),        # Private
    ipaddress.ip_network("172.16.0.0/12"),     # Private
    ipaddress.ip_network("192.168.0.0/16"),    # Private
    ipaddress.ip_network("169.254.0.0/16"),    # Link-local
    ipaddress.ip_network("0.0.0.0/8"),         # Current network
    ipaddress.ip_network("224.0.0.0/4"),       # Multicast
    ipaddress.ip_network("240.0.0.0/4"),       # Reserved
    ipaddress.ip_network("::1/128"),            # IPv6 loopback (compressed)
    ipaddress.ip_network("::ffff:0:0/96"),     # IPv4-mapped IPv6 — extract mapped v4 for re-check
    ipaddress.ip_network("fc00::/7"),           # IPv6 unique local
    ipaddress.ip_network("fe80::/10"),          # IPv6 link-local
    ipaddress.ip_network("::/128"),             # IPv6 unspecified
]

# Max CSS selector length to prevent ReDoS
MAX_CSS_SELECTOR_LENGTH = 4096

# Max header count and value length
MAX_HEADER_COUNT = 50
MAX_HEADER_VALUE_LENGTH = 8192


class SecurityError(ValueError):
    """Raised when input fails security validation."""
    pass


def _normalize_ip_notation(host: str) -> str | None:
    """Resolve alternate IP notations that curl/libcurl would resolve.

    curl_cffi uses libcurl, which accepts many IP address formats that
    Python's ipaddress module rejects. This normalizes them so we can
    check against private network ranges.

    Handles:
    - Octal notation: 0177.0.0.1 → 127.0.0.1
    - Hex notation: 0x7f000001 → 127.0.0.1, 0x7f.1 → 127.0.0.1
    - Decimal integer: 2130706433 → 127.0.0.1
    - Short-form: 127.1 → 127.0.0.1

    Returns dotted-decimal string like '127.0.0.1' or None.
    """
    cleaned = host.strip('[]')

    # Case: pure decimal integer (2130706433) or packed octal (017700000001)
    if cleaned.isdigit():
        try:
            # Leading zero → curl treats as octal: 017700000001 → 127.0.0.1
            if len(cleaned) > 1 and cleaned[0] == '0' and all(c in '01234567' for c in cleaned):
                val = int(cleaned, 8)
            else:
                val = int(cleaned)
            if val <= 0xFFFFFFFF:
                return f"{(val >> 24) & 0xFF}.{(val >> 16) & 0xFF}.{(val >> 8) & 0xFF}.{val & 0xFF}"
        except (ValueError, OverflowError):
            pass
        return None

    # Case: dotted notation (127.0.0.1, 0177.0.0.1, 0x7f.1, 127.1)
    parts = cleaned.split('.')
    if not (1 <= len(parts) <= 4):
        return None

    try:
        resolved = []
        for p in parts:
            if not p:
                return None
            # Leading zero → octal (curl behavior): '0177' → 127
            if len(p) > 1 and p[0] == '0' and all(c in '01234567' for c in p):
                resolved.append(int(p, 8))
            # Hex prefix: '0x7f' → 127
            elif p.lower().startswith('0x'):
                resolved.append(int(p, 0))
            else:
                resolved.append(int(p))

        # Expand short-form to 4 octets (curl/inet_aton behavior).
        # 2-part: a.b → a.0.0.b
        # 3-part: a.b.c → a.b.0.c
        # 4-part: a.b.c.d → a.b.c.d (no expansion needed)
        if len(resolved) == 2:
            resolved = [resolved[0], 0, 0, resolved[1]]
        elif len(resolved) == 3:
            resolved = [resolved[0], resolved[1], 0, resolved[2]]

        if any(o < 0 or o > 255 for o in resolved):
            return None
        return '.'.join(str(o) for o in resolved)
    except (ValueError, OverflowError):
        return None


def validate_url(url: str, allow_internal: bool = False) -> str:
    """Validate and sanitize a URL. Returns the validated URL.

    Protects against:
    - SSRF attacks (internal IPs, localhost, cloud metadata endpoints)
    - File:// and other dangerous schemes
    - Oversized URLs (DoS)
    - Malformed URLs
    - URL parsing confusion attacks (backslash in authority, bracketed hosts)
    - CVE-2024-11168: urllib.parse bracket-host SSRF bypass
    - CVE-2025-0454-style: backslash-@ authority confusion between parsers

    Args:
        url: The URL to validate.
        allow_internal: If True, allow private/internal IPs (for testing only).

    Returns:
        The validated URL string.

    Raises:
        SecurityError: If the URL fails validation.
    """
    if not url or not isinstance(url, str):
        raise SecurityError("URL must be a non-empty string")

    url = url.strip()

    if not url:
        raise SecurityError("URL must be a non-empty string")

    if len(url) > MAX_URL_LENGTH:
        raise SecurityError(
            f"URL exceeds maximum length of {MAX_URL_LENGTH} characters"
        )

    # CRITICAL: Reject URLs containing backslash in authority section.
    # Backslash causes parsing confusion between urlparse (treats \\ as part
    # of userinfo/netloc) and urllib3/requests (treats \\ as path delimiter).
    # This enables SSRF: http://evil.com\\@127.0.0.1/ passes urlparse validation
    # (hostname=127.0.0.1) but fetches from evil.com.
    # See: corCTF 2025 "Python URL Parsing Confusion"; CVE-2025-0454
    if "\\" in url:
        raise SecurityError("URL contains backslash character (potential SSRF bypass)")

    try:
        parsed = urlparse(url)
    except Exception as e:
        raise SecurityError(f"Malformed URL: {e}")

    # Block dangerous schemes
    scheme = (parsed.scheme or "").lower()
    if scheme in _BLOCKED_SCHEMES:
        raise SecurityError(f"URL scheme '{scheme}' is not allowed")

    if scheme not in ("http", "https"):
        raise SecurityError(f"Only http and https URLs are supported, got: {scheme}")

    # Block bracketed hosts that aren't valid IPv6 — CVE-2024-11168
    # urlparse in Python <3.12.7 improperly validated bracketed hosts, allowing
    # arbitrary content in brackets to be treated as valid hostnames.
    netloc = (parsed.netloc or "").lower()
    if "[" in netloc or "]" in netloc:
        if not (netloc.startswith("[") and "]" in netloc):
            raise SecurityError("URL contains malformed bracketed host (potential SSRF bypass)")
        # Extract the bracketed part and verify it's a valid IPv6 address
        bracket_content = netloc[1:netloc.index("]")]
        try:
            ipaddress.IPv6Address(bracket_content)
        except ipaddress.AddressValueError:
            # Allow IPvFuture (v[0-9]+\..+) but nothing else
            if not re.match(r'^v[0-9]+\..+$', bracket_content, re.IGNORECASE):
                raise SecurityError("URL contains invalid bracketed host (potential SSRF bypass)")

    hostname = parsed.hostname
    if not hostname:
        raise SecurityError("URL has no valid hostname")

    # Check for internal IPs (only if hostname is an IP)
    if not allow_internal:
        addr = None
        is_ip = False
        try:
            addr = ipaddress.ip_address(hostname)
            is_ip = True
        except ValueError:
            pass  # Not a standard IP — try alternate notations below

        # Handle alternate IP notations that ipaddress rejects but curl resolves
        # (octal, hex, decimal integer, short-form). This is critical: Python's
        # ipaddress module rejects leading zeros (CVE-2021-29921), but curl_cffi
        # (libcurl) resolves them. An attacker could use http://0177.0.0.1 to
        # bypass SSRF protection and reach the loopback interface.
        if not is_ip:
            normalized = _normalize_ip_notation(hostname)
            if normalized is not None:
                try:
                    addr = ipaddress.ip_address(normalized)
                    is_ip = True
                except ValueError:
                    pass  # Not resolvable to a valid IP after normalization

        if is_ip:
            # Check IPv4-mapped IPv6: extract the mapped IPv4 and re-check it
            if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
                ipv4_addr = addr.ipv4_mapped
                for network in _PRIVATE_NETWORKS:
                    if isinstance(network, ipaddress.IPv4Network) and ipv4_addr in network:
                        raise SecurityError(
                            f"URL targets internal/private IP (IPv4-mapped {hostname} → {ipv4_addr} in {network})"
                        )
            for network in _PRIVATE_NETWORKS:
                try:
                    if addr in network:
                        raise SecurityError(
                            f"URL targets internal/private IP ({hostname} in {network})"
                        )
                except TypeError:
                    pass  # IPv4/IPv6 type mismatch, skip
        else:
            # Block known internal hostnames (cloud metadata, localhost, DNS rebinding services)
            hostname_lower = hostname.lower()
            if hostname_lower in _BLOCKED_HOSTNAMES:
                raise SecurityError(f"URL targets internal service: {hostname}")
            # Block DNS rebinding services that resolve to internal IPs
            if hostname_lower.endswith(_DNS_REBINDING_SUFFIXES):
                raise SecurityError(f"URL uses DNS rebinding service: {hostname}")

    return url


def validate_css_selector(selector: Optional[str]) -> Optional[str]:
    """Validate a CSS selector for injection/DoS safety.

    Args:
        selector: The CSS selector string, or None.

    Returns:
        The validated selector, or None.

    Raises:
        SecurityError: If the selector fails validation.
    """
    if selector is None:
        return None

    if not isinstance(selector, str):
        raise SecurityError("CSS selector must be a string or None")

    selector = selector.strip()

    if not selector:
        return None  # Empty/whitespace-only selectors have no effect

    if len(selector) > MAX_CSS_SELECTOR_LENGTH:
        raise SecurityError(
            f"CSS selector exceeds maximum length of {MAX_CSS_SELECTOR_LENGTH}"
        )

    # Block selectors that could cause excessive backtracking (ReDoS)
    # Nested pseudo-classes, deeply nested combinators, etc.
    depth = selector.count(">") + selector.count(" ") + selector.count("+") + selector.count("~")
    if depth > 100:
        raise SecurityError("CSS selector nesting depth too high")

    # Block inline script/style injection attempts
    dangerous = ["<script", "<style", "javascript:", "expression("]
    lowered = selector.lower()
    for d in dangerous:
        if d in lowered:
            raise SecurityError(f"CSS selector contains potentially dangerous content")

    return selector


def validate_headers(headers: Optional[dict]) -> Optional[dict]:
    """Validate custom HTTP headers for injection/prevention.

    Args:
        headers: Dict of header name -> value, or None.

    Returns:
        The validated headers dict, or None.

    Raises:
        SecurityError: If headers fail validation.
    """
    if headers is None:
        return None

    if not isinstance(headers, dict):
        raise SecurityError("Headers must be a dictionary or None")

    validated = {}
    for name, value in headers.items():
        if not isinstance(name, str) or not name.strip():
            raise SecurityError("Header names must be non-empty strings")
        if value is not None and not isinstance(value, str):
            raise SecurityError(f"Header '{name}' value must be a string or None")

        name = name.strip()
        value = (value or "").strip()

        if len(value) > MAX_HEADER_VALUE_LENGTH:
            raise SecurityError(
                f"Header '{name}' value exceeds max length of {MAX_HEADER_VALUE_LENGTH}"
            )

        # Block header injection via newline characters in name or value
        if "\n" in name or "\r" in name or "\n" in value or "\r" in value:
            raise SecurityError(
                f"Header '{name}' contains newline characters (header injection)"
            )

        # Block forbidden headers that could interfere with Scrapling
        forbidden = {"host", "content-length", "transfer-encoding", "connection"}
        if name.lower() in forbidden:
            raise SecurityError(
                f"Header '{name}' is managed internally and cannot be overridden"
            )

        validated[name] = value

    if len(validated) > MAX_HEADER_COUNT:
        raise SecurityError(
            f"Too many custom headers ({len(validated)}, max {MAX_HEADER_COUNT})"
        )

    return validated


def validate_proxy(proxy: Optional[str | dict]) -> Optional[str | dict]:
    """Validate proxy configuration.

    Args:
        proxy: Proxy URL string, dict with server/username/password, or None.

    Returns:
        The validated proxy config.

    Raises:
        SecurityError: If proxy config fails validation.
    """
    if proxy is None:
        return None

    if isinstance(proxy, str):
        proxy = proxy.strip()
        if not proxy:
            return None
        # Basic URL validation for proxy
        try:
            parsed = urlparse(proxy)
            if parsed.scheme not in ("http", "https", "socks5", "socks5h"):
                raise SecurityError(
                    f"Proxy scheme must be http, https, socks5, or socks5h, got: {parsed.scheme}"
                )
        except Exception as e:
            raise SecurityError(f"Invalid proxy URL: {e}")
        return proxy

    if isinstance(proxy, dict):
        allowed_keys = {"server", "username", "password"}
        extra = set(proxy.keys()) - allowed_keys
        if extra:
            raise SecurityError(f"Unknown proxy config keys: {extra}")

        server = proxy.get("server", "").strip()
        if not server:
            raise SecurityError("Proxy dict must include 'server' key")

        # Validate the server URL with the same scheme set accepted for string
        # proxies (http/https/socks5/socks5h). Previously this called validate_url,
        # which only permits http/https — so a dict proxy with a socks5 server was
        # rejected even though the string form was accepted.
        try:
            parsed = urlparse(server)
            if parsed.scheme not in ("http", "https", "socks5", "socks5h"):
                raise SecurityError(
                    f"Proxy scheme must be http, https, socks5, or socks5h, got: {parsed.scheme}"
                )
        except SecurityError:
            raise
        except Exception as e:
            raise SecurityError(f"Invalid proxy server URL: {e}")
        return proxy

    raise SecurityError("Proxy must be a URL string, dict, or None")


def validate_timeout(timeout: int | float) -> int | float:
    """Validate timeout value.

    Args:
        timeout: Timeout in milliseconds (browser) or seconds (HTTP).

    Returns:
        The validated timeout.

    Raises:
        SecurityError: On invalid timeout.
    """
    if not isinstance(timeout, (int, float)):
        raise SecurityError("Timeout must be a number")
    if timeout <= 0:
        raise SecurityError("Timeout must be positive")
    if timeout > 120_000:  # 120 seconds max for browser
        raise SecurityError("Timeout exceeds maximum of 120000ms (2 minutes)")
    return timeout


def validate_search_query(query: str) -> str:
    """Validate and sanitize a search query.

    Args:
        query: The search query string.

    Returns:
        The sanitized query.

    Raises:
        SecurityError: On invalid query.
    """
    if not query or not isinstance(query, str):
        raise SecurityError("Search query must be a non-empty string")

    query = query.strip()

    if not query:
        raise SecurityError("Search query is empty after stripping")

    if len(query) > 2048:
        raise SecurityError("Search query exceeds maximum length of 2048 characters")

    # Strip control characters (0x00-0x1F except tab) and delete (0x7F)
    query = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', query)

    return query


def redact_api_key(text: str) -> str:
    """Redact API keys and proxy credentials from error messages/logs.

    Detects TinyFish API key format, generic API key patterns,
    and proxy URLs with embedded credentials.
    """
    # TinyFish keys: sk-tinyfish- followed by alphanumeric
    text = re.sub(r'sk-tinyfish-[a-zA-Z0-9]+', 'sk-tinyfish-[REDACTED]', text)
    # Generic key patterns (sk-, pk-, api_)
    text = re.sub(r'(?:sk|pk|api_key)[-_][a-zA-Z0-9]{20,}', '[API_KEY_REDACTED]', text)
    # Proxy URLs with embedded credentials: http://user:pass@host
    # Catches the full credential portion before @
    text = re.sub(
        r'(https?://)[^:@\s]+:[^@\s]+@',
        r'\1[CREDENTIALS_REDACTED]@',
        text,
    )
    # socks5://user:pass@host
    text = re.sub(
        r'(socks5h?://)[^:@\s]+:[^@\s]+@',
        r'\1[CREDENTIALS_REDACTED]@',
        text,
    )
    return text
