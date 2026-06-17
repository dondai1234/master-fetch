"""Tests for security.py — input validation and SSRF protection."""

import pytest
from master_fetch.security import (
    validate_url,
    validate_css_selector,
    validate_headers,
    validate_proxy,
    validate_timeout,
    validate_search_query,
    redact_api_key,
    SecurityError,
    MAX_URL_LENGTH,
    MAX_CSS_SELECTOR_LENGTH,
)


class TestValidateUrl:
    """URL validation with SSRF protection."""

    def test_valid_http_url(self):
        assert validate_url("https://example.com") == "https://example.com"

    def test_valid_https_with_path(self):
        result = validate_url("https://example.com/path?q=1")
        assert result == "https://example.com/path?q=1"

    def test_valid_url_with_port(self):
        assert validate_url("https://example.com:8080/path") == "https://example.com:8080/path"

    def test_strips_whitespace(self):
        assert validate_url("  https://example.com  ") == "https://example.com"

    def test_rejects_empty_string(self):
        with pytest.raises(SecurityError, match="non-empty string"):
            validate_url("")

    def test_rejects_none(self):
        with pytest.raises(SecurityError, match="non-empty string"):
            validate_url(None)

    def test_rejects_file_scheme(self):
        with pytest.raises(SecurityError, match="not allowed"):
            validate_url("file:///etc/passwd")

    def test_rejects_javascript_scheme(self):
        with pytest.raises(SecurityError, match="not allowed"):
            validate_url("javascript:alert(1)")

    def test_rejects_data_scheme(self):
        with pytest.raises(SecurityError, match="not allowed"):
            validate_url("data:text/html,<script>alert(1)</script>")

    def test_rejects_gopher_scheme(self):
        with pytest.raises(SecurityError, match="not allowed"):
            validate_url("gopher://evil.com/1")

    def test_rejects_ftp_scheme(self):
        with pytest.raises(SecurityError, match="not allowed"):
            validate_url("ftp://evil.com/malware.exe")

    def test_rejects_loopback_ipv4(self):
        with pytest.raises(SecurityError, match="internal/private IP"):
            validate_url("http://127.0.0.1/admin")

    def test_rejects_private_10(self):
        with pytest.raises(SecurityError, match="internal/private IP"):
            validate_url("http://10.0.0.1/api")

    def test_rejects_private_192(self):
        with pytest.raises(SecurityError, match="internal/private IP"):
            validate_url("http://192.168.1.1/")

    def test_rejects_private_172(self):
        with pytest.raises(SecurityError, match="internal/private IP"):
            validate_url("http://172.16.0.1/")

    def test_rejects_localhost(self):
        with pytest.raises(SecurityError, match="internal service"):
            validate_url("http://localhost:3000/api")

    def test_rejects_cloud_metadata(self):
        with pytest.raises(SecurityError, match="internal"):
            validate_url("http://169.254.169.254/latest/meta-data/")

    def test_rejects_link_local(self):
        with pytest.raises(SecurityError, match="internal/private IP"):
            validate_url("http://169.254.0.1/")

    def test_rejects_zero_ip(self):
        with pytest.raises(SecurityError, match="internal"):
            validate_url("http://0.0.0.0/")

    def test_rejects_ipv6_loopback(self):
        with pytest.raises(SecurityError, match="internal/private IP"):
            validate_url("http://[::1]:8080/path")

    def test_rejects_ipv6_link_local(self):
        with pytest.raises(SecurityError, match="internal/private IP"):
            validate_url("http://[fe80::1]/")

    def test_rejects_oversized_url(self):
        huge = "https://example.com/" + "a" * 9000
        with pytest.raises(SecurityError, match="exceeds maximum length"):
            validate_url(huge)

    def test_allows_internal_with_flag(self):
        # When allow_internal=True, internal IPs are OK
        assert validate_url("http://127.0.0.1/test", allow_internal=True) == "http://127.0.0.1/test"

    def test_rejects_no_hostname(self):
        with pytest.raises(SecurityError, match="no valid hostname"):
            validate_url("http://")

    def test_rejects_malformed(self):
        with pytest.raises(SecurityError, match="Only http and https"):
            validate_url("not a url at all")

    def test_rejects_non_http_scheme_no_colon(self):
        with pytest.raises(SecurityError, match="Only http and https"):
            validate_url("ws://example.com")


class TestValidateCssSelector:
    """CSS selector validation."""

    def test_valid_selector(self):
        assert validate_css_selector("div.content > p") == "div.content > p"

    def test_none_is_ok(self):
        assert validate_css_selector(None) is None

    def test_strips_whitespace(self):
        assert validate_css_selector("  div  ") == "div"

    def test_rejects_non_string(self):
        with pytest.raises(SecurityError, match="must be a string"):
            validate_css_selector(123)

    def test_rejects_oversized(self):
        huge = "div > " * 3000
        with pytest.raises(SecurityError, match="exceeds maximum length"):
            validate_css_selector(huge)

    def test_rejects_script_injection(self):
        with pytest.raises(SecurityError, match="dangerous"):
            validate_css_selector("<script>alert(1)</script>")

    def test_rejects_style_injection(self):
        with pytest.raises(SecurityError, match="dangerous"):
            validate_css_selector("<style>body{color:red}</style>")

    def test_rejects_javascript_protocol(self):
        with pytest.raises(SecurityError, match="dangerous"):
            validate_css_selector("a[href='javascript:alert(1)']")


class TestValidateHeaders:
    """Custom header validation."""

    def test_valid_headers(self):
        result = validate_headers({"X-Custom": "value", "Accept": "text/html"})
        assert result == {"X-Custom": "value", "Accept": "text/html"}

    def test_none_is_ok(self):
        assert validate_headers(None) is None

    def test_rejects_newline_in_name(self):
        with pytest.raises(SecurityError, match="newline"):
            validate_headers({"X-Evil\nHeader": "value"})

    def test_rejects_newline_in_value(self):
        with pytest.raises(SecurityError, match="newline"):
            validate_headers({"X-Header": "value\r\nInjected: true"})

    def test_rejects_forbidden_host(self):
        with pytest.raises(SecurityError, match="managed internally"):
            validate_headers({"Host": "evil.com"})

    def test_rejects_forbidden_content_length(self):
        with pytest.raises(SecurityError, match="managed internally"):
            validate_headers({"content-length": "1000"})

    def test_rejects_too_many_headers(self):
        with pytest.raises(SecurityError, match="Too many custom headers"):
            validate_headers({f"X-Header-{i}": "value" for i in range(100)})

    def test_rejects_non_string_name(self):
        with pytest.raises(SecurityError, match="must be non-empty"):
            validate_headers({123: "value"})

    def test_none_value_is_empty_string(self):
        result = validate_headers({"X-Empty": None})
        assert result == {"X-Empty": ""}


class TestValidateProxy:
    """Proxy configuration validation."""

    def test_valid_http_proxy(self):
        assert validate_proxy("http://proxy:8080") == "http://proxy:8080"

    def test_valid_socks5_proxy(self):
        assert validate_proxy("socks5://proxy:1080") == "socks5://proxy:1080"

    def test_none_is_ok(self):
        assert validate_proxy(None) is None

    def test_rejects_invalid_scheme(self):
        with pytest.raises(SecurityError, match="scheme must be"):
            validate_proxy("ftp://proxy:21")

    def test_valid_dict_proxy(self):
        result = validate_proxy({"server": "http://proxy:8080"})
        assert result == {"server": "http://proxy:8080"}

    def test_dict_with_credentials(self):
        result = validate_proxy({
            "server": "http://proxy:8080",
            "username": "user",
            "password": "pass",
        })
        assert result["username"] == "user"

    def test_dict_missing_server(self):
        with pytest.raises(SecurityError, match="must include 'server'"):
            validate_proxy({"username": "user"})

    def test_dict_unknown_key(self):
        with pytest.raises(SecurityError, match="Unknown proxy config"):
            validate_proxy({"server": "http://proxy:8080", "evil": "value"})

    def test_dict_socks5_server_accepted(self):
        """Dict proxy with a socks5 server should be accepted, matching the
        string form. Regression: previously validate_proxy called validate_url
        on the server, which only permits http/https, so socks5 dicts were
        rejected while socks5 strings were accepted.
        """
        result = validate_proxy({"server": "socks5://proxy:1080"})
        assert result == {"server": "socks5://proxy:1080"}

    def test_dict_socks5h_server_accepted(self):
        result = validate_proxy({"server": "socks5h://proxy:1080"})
        assert result["server"] == "socks5h://proxy:1080"

    def test_dict_local_proxy_server_allowed(self):
        """Local/internal proxy hosts must still be accepted (proxies are often
        on localhost). Regression: the fix must not re-introduce an IP block.
        """
        result = validate_proxy({"server": "http://127.0.0.1:8080"})
        assert result["server"] == "http://127.0.0.1:8080"

    def test_dict_rejects_bad_scheme(self):
        with pytest.raises(SecurityError, match="scheme must be"):
            validate_proxy({"server": "ftp://proxy:21"})


class TestValidateTimeout:
    """Timeout validation."""

    def test_valid_timeout(self):
        assert validate_timeout(30000) == 30000

    def test_rejects_negative(self):
        with pytest.raises(SecurityError, match="must be positive"):
            validate_timeout(-1)

    def test_rejects_zero(self):
        with pytest.raises(SecurityError, match="must be positive"):
            validate_timeout(0)

    def test_rejects_exceeds_max(self):
        with pytest.raises(SecurityError, match="exceeds maximum"):
            validate_timeout(200_000)

    def test_rejects_non_number(self):
        with pytest.raises(SecurityError, match="must be a number"):
            validate_timeout("30000")


class TestValidateSearchQuery:
    """Search query validation."""

    def test_valid_query(self):
        assert validate_search_query("python tutorial") == "python tutorial"

    def test_strips_whitespace(self):
        assert validate_search_query("  hello world  ") == "hello world"

    def test_rejects_empty(self):
        with pytest.raises(SecurityError, match="non-empty string"):
            validate_search_query("")

    def test_rejects_oversized(self):
        with pytest.raises(SecurityError, match="exceeds maximum"):
            validate_search_query("a" * 3000)

    def test_strips_control_chars(self):
        result = validate_search_query("hello\x00world")
        assert "\x00" not in result
        assert "hello" in result


class TestRedactApiKey:
    """API key redaction in error messages."""

    def test_redacts_tinyfish_key(self):
        result = redact_api_key("Failed with key sk-tinyfish-abc123def456")
        assert "sk-tinyfish-abc123def456" not in result
        assert "[REDACTED]" in result

    def test_redacts_generic_sk_key(self):
        result = redact_api_key("Key sk-12345678901234567890 is invalid")
        assert "[API_KEY_REDACTED]" in result

    def test_preserves_normal_text(self):
        text = "This is a normal error message"
        assert redact_api_key(text) == text
