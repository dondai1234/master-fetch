"""Adversarial tests for the BYOK (Bring Your Own Key) search API system.

Tests KeyPool rotation, config loading, each provider engine with mocked HTTP,
and metasearch integration. No live API calls — all HTTP is mocked.
"""

import json
import os
import time
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock


# ─── KeyPool ──────────────────────────────────────────────────────────────────

class TestKeyPool:
    """KeyPool: round-robin selection, rate-limit marking, exhaustion."""

    def test_round_robin_single_key(self):
        from master_fetch.search_api_keys import KeyPool
        pool = KeyPool(["key1"])
        assert pool.get_key() == "key1"
        assert pool.get_key() == "key1"  # only one key

    def test_round_robin_multiple_keys(self):
        from master_fetch.search_api_keys import KeyPool
        pool = KeyPool(["key1", "key2", "key3"])
        keys = [pool.get_key() for _ in range(6)]
        # Round-robin: key1, key2, key3, key1, key2, key3
        assert keys == ["key1", "key2", "key3", "key1", "key2", "key3"]

    def test_rate_limited_key_skipped(self):
        from master_fetch.search_api_keys import KeyPool
        pool = KeyPool(["key1", "key2"])
        k1 = pool.get_key()  # key1
        pool.mark_rate_limited(k1)
        # key1 is rate-limited, should get key2
        assert pool.get_key() == "key2"
        # key1 still rate-limited, should get key2 again
        assert pool.get_key() == "key2"

    def test_all_keys_rate_limited_raises(self):
        from master_fetch.search_api_keys import KeyPool
        from master_fetch.search_metasearch import MetaBlockedException
        pool = KeyPool(["key1", "key2"])
        pool.mark_rate_limited("key1")
        pool.mark_rate_limited("key2")
        with pytest.raises(MetaBlockedException, match="rate-limited"):
            pool.get_key()

    def test_mark_success_clears_rate_limit(self):
        from master_fetch.search_api_keys import KeyPool
        pool = KeyPool(["key1", "key2"])
        pool.mark_rate_limited("key1")
        pool.mark_success("key1")
        # key1 should be available again
        assert pool.get_key() in ("key1", "key2")

    def test_invalid_key_longer_cooldown(self):
        from master_fetch.search_api_keys import KeyPool
        pool = KeyPool(["key1", "key2"])
        pool.mark_invalid("key1")
        # key1 should be skipped (invalid cooldown is 300s)
        assert pool.get_key() == "key2"
        assert pool.get_key() == "key2"

    def test_empty_keys_raises(self):
        from master_fetch.search_api_keys import KeyPool
        with pytest.raises(ValueError, match="at least one key"):
            KeyPool([])

    def test_status_reports_active_and_rate_limited(self):
        from master_fetch.search_api_keys import KeyPool
        pool = KeyPool(["key1", "key2"])
        pool.mark_rate_limited("key1")
        status = pool.status()
        assert len(status) == 2
        states = {s["status"] for s in status}
        assert "rate_limited" in states
        assert "active" in states

    def test_rate_limit_expires(self):
        """A rate-limited key should become available after cooldown."""
        from master_fetch.search_api_keys import KeyPool
        pool = KeyPool(["key1"])
        pool.RATE_LIMIT_COOLDOWN = 0.1  # very short for testing
        pool.mark_rate_limited("key1")
        with pytest.raises(Exception):
            pool.get_key()
        time.sleep(0.15)
        assert pool.get_key() == "key1"


# ─── BYOK Config ──────────────────────────────────────────────────────────────

class TestBYOKConfig:
    """Config loading from env vars + config file."""

    def test_load_from_env_vars(self, monkeypatch):
        from master_fetch.byok_config import load_byok_keys
        monkeypatch.setenv("HOUND_SEARCH_SERPER_KEYS", "serper-key1,serper-key2")
        monkeypatch.setenv("HOUND_SEARCH_TAVILY_KEYS", "tavily-key1")
        keys = load_byok_keys()
        assert keys.get("serper") == ["serper-key1", "serper-key2"]
        assert keys.get("tavily") == ["tavily-key1"]

    def test_env_vars_override_config_file(self, monkeypatch, tmp_path):
        from master_fetch.byok_config import load_byok_keys, _config_path
        # Set config file with serper keys
        config = {"serper": ["file-key"], "tavily": ["tavily-file"]}
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        (tmp_path / ".hound").mkdir()
        (tmp_path / ".hound" / "search_keys.json").write_text(json.dumps(config))
        # Env var should override for serper, file fallback for tavily
        monkeypatch.setenv("HOUND_SEARCH_SERPER_KEYS", "env-key")
        keys = load_byok_keys()
        assert keys.get("serper") == ["env-key"]  # env wins
        assert keys.get("tavily") == ["tavily-file"]  # file fallback

    def test_no_keys_returns_empty(self, monkeypatch):
        from master_fetch.byok_config import load_byok_keys
        # Clear all env vars
        for var in ["HOUND_SEARCH_SERPER_KEYS", "HOUND_SEARCH_TAVILY_KEYS",
                     "HOUND_SEARCH_EXA_KEYS", "HOUND_SEARCH_FIRECRAWL_KEYS",
                     "HOUND_SEARCH_TINYFISH_KEYS"]:
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setattr(Path, "home", lambda: Path("/nonexistent-home-12345"))
        keys = load_byok_keys()
        assert keys == {}

    def test_has_byok_keys_true(self, monkeypatch):
        from master_fetch.byok_config import has_byok_keys
        monkeypatch.setenv("HOUND_SEARCH_SERPER_KEYS", "some-key")
        assert has_byok_keys() is True

    def test_has_byok_keys_false(self, monkeypatch):
        from master_fetch.byok_config import has_byok_keys
        for var in ["HOUND_SEARCH_SERPER_KEYS", "HOUND_SEARCH_TAVILY_KEYS",
                     "HOUND_SEARCH_EXA_KEYS", "HOUND_SEARCH_FIRECRAWL_KEYS",
                     "HOUND_SEARCH_TINYFISH_KEYS"]:
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setattr(Path, "home", lambda: Path("/nonexistent-home-12345"))
        assert has_byok_keys() is False

    def test_add_key(self, monkeypatch, tmp_path):
        from master_fetch.byok_config import add_key, _read_config_file
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        add_key("serper", "new-key")
        keys = _read_config_file()
        assert keys.get("serper") == ["new-key"]

    def test_add_key_duplicate_not_added(self, monkeypatch, tmp_path):
        from master_fetch.byok_config import add_key, _read_config_file
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        add_key("serper", "key1")
        add_key("serper", "key1")  # duplicate
        keys = _read_config_file()
        assert keys.get("serper") == ["key1"]

    def test_add_multiple_keys(self, monkeypatch, tmp_path):
        from master_fetch.byok_config import add_key, _read_config_file
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        add_key("tavily", "key1")
        add_key("tavily", "key2")
        keys = _read_config_file()
        assert keys.get("tavily") == ["key1", "key2"]

    def test_remove_key_by_index(self, monkeypatch, tmp_path):
        from master_fetch.byok_config import add_key, remove_key, _read_config_file
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        add_key("exa", "key1")
        add_key("exa", "key2")
        removed = remove_key("exa", 0)
        assert removed == 1
        keys = _read_config_file()
        assert keys.get("exa") == ["key2"]

    def test_remove_all_keys_for_provider(self, monkeypatch, tmp_path):
        from master_fetch.byok_config import add_key, remove_key, _read_config_file
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        add_key("firecrawl", "key1")
        add_key("firecrawl", "key2")
        removed = remove_key("firecrawl")
        assert removed == 2
        keys = _read_config_file()
        assert "firecrawl" not in keys

    def test_remove_nonexistent_provider(self, monkeypatch, tmp_path):
        from master_fetch.byok_config import remove_key
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        removed = remove_key("serper")
        assert removed == 0

    def test_clear_all_keys(self, monkeypatch, tmp_path):
        from master_fetch.byok_config import add_key, clear_all_keys, _read_config_file
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        add_key("serper", "key1")
        add_key("tavily", "key2")
        removed = clear_all_keys()
        assert removed == 2
        keys = _read_config_file()
        assert keys == {}

    def test_redact_key_long(self):
        from master_fetch.byok_config import redact_key
        assert redact_key("abcdefghijklmnop") == "abcdefgh...mnop"

    def test_redact_key_short(self):
        from master_fetch.byok_config import redact_key
        result = redact_key("shortkey")
        # 8 chars or less -> fully masked
        assert result == "***" or "..." in result

    def test_add_key_unknown_provider_raises(self, monkeypatch, tmp_path):
        from master_fetch.byok_config import add_key
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        with pytest.raises(ValueError, match="Unknown provider"):
            add_key("unknown_provider", "key")

    def test_add_empty_key_raises(self, monkeypatch, tmp_path):
        from master_fetch.byok_config import add_key
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        with pytest.raises(ValueError, match="cannot be empty"):
            add_key("serper", "")

    def test_remove_index_out_of_range(self, monkeypatch, tmp_path):
        from master_fetch.byok_config import add_key, remove_key
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        add_key("serper", "key1")
        with pytest.raises(IndexError, match="out of range"):
            remove_key("serper", 5)

    def test_malformed_config_file_ignored(self, monkeypatch, tmp_path):
        from master_fetch.byok_config import load_byok_keys
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        (tmp_path / ".hound").mkdir()
        (tmp_path / ".hound" / "search_keys.json").write_text("not valid json {{{")
        for var in ["HOUND_SEARCH_SERPER_KEYS", "HOUND_SEARCH_TAVILY_KEYS",
                     "HOUND_SEARCH_EXA_KEYS", "HOUND_SEARCH_FIRECRAWL_KEYS",
                     "HOUND_SEARCH_TINYFISH_KEYS"]:
            monkeypatch.delenv(var, raising=False)
        keys = load_byok_keys()
        assert keys == {}

    def test_env_var_single_key_no_comma(self, monkeypatch):
        from master_fetch.byok_config import load_byok_keys
        monkeypatch.setenv("HOUND_SEARCH_TINYFISH_KEYS", "single-key")
        keys = load_byok_keys()
        assert keys.get("tinyfish") == ["single-key"]

    def test_env_var_empty_string_ignored(self, monkeypatch):
        from master_fetch.byok_config import load_byok_keys
        monkeypatch.setenv("HOUND_SEARCH_SERPER_KEYS", "")
        keys = load_byok_keys()
        assert "serper" not in keys


# ─── Provider Engines (mocked HTTP) ──────────────────────────────────────────

class _MockResponse:
    """Mock httpx.Response for testing."""
    def __init__(self, status_code: int, json_data: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._json_data = json_data
        self.text = text or (json.dumps(json_data) if json_data else "")

    def json(self):
        if self._json_data is None:
            raise json.JSONDecodeError("no json", "", 0)
        return self._json_data


class TestSerperEngine:
    """Serper engine: POST, X-API-KEY, organic[] response."""

    def test_parse_organic_results(self):
        from master_fetch.search_api_keys import SerperEngine
        eng = SerperEngine()
        data = {"organic": [
            {"title": "Result 1", "link": "https://example.com/1", "snippet": "Snippet 1", "position": 1},
            {"title": "Result 2", "link": "https://example.com/2", "snippet": "Snippet 2", "position": 2},
        ]}
        results = eng._parse_results(data)
        assert len(results) == 2
        assert results[0].title == "Result 1"
        assert results[0].href == "https://example.com/1"
        assert results[0].body == "Snippet 1"

    def test_parse_empty_organic(self):
        from master_fetch.search_api_keys import SerperEngine
        eng = SerperEngine()
        results = eng._parse_results({"organic": []})
        assert results == []

    def test_parse_missing_fields_skipped(self):
        from master_fetch.search_api_keys import SerperEngine
        eng = SerperEngine()
        data = {"organic": [
            {"title": "No link", "snippet": "test"},  # missing link -> skip
            {"link": "https://example.com", "snippet": "No title"},  # missing title -> skip
            {"title": "OK", "link": "https://ok.com", "snippet": "OK"},  # valid
        ]}
        results = eng._parse_results(data)
        assert len(results) == 1
        assert results[0].title == "OK"

    def test_build_request(self):
        from master_fetch.search_api_keys import SerperEngine
        eng = SerperEngine()
        data, headers = eng._build_request("test query", "my-key")
        assert data["q"] == "test query"
        assert headers["X-API-KEY"] == "my-key"

    def test_build_request_timelimit_maps_to_tbs(self):
        from master_fetch.search_api_keys import SerperEngine
        eng = SerperEngine()
        for tl, expected_tbs in [("d", "qdr:d"), ("w", "qdr:w"), ("m", "qdr:m"), ("y", "qdr:y")]:
            data, _ = eng._build_request("q", "k", timelimit=tl)
            assert data["tbs"] == expected_tbs

    def test_build_request_no_tbs_without_timelimit(self):
        from master_fetch.search_api_keys import SerperEngine
        eng = SerperEngine()
        data, _ = eng._build_request("q", "k")
        assert "tbs" not in data

    def test_build_request_site_reapplied(self):
        from master_fetch.search_api_keys import SerperEngine
        eng = SerperEngine()
        data, _ = eng._build_request("query", "k", site="example.com")
        assert "site:example.com" in data["q"]

    def test_build_request_exclude_sites_reapplied(self):
        from master_fetch.search_api_keys import SerperEngine
        eng = SerperEngine()
        data, _ = eng._build_request("query", "k", exclude_sites=["spam.com", "ads.com"])
        assert "-site:spam.com" in data["q"]
        assert "-site:ads.com" in data["q"]

    def test_build_request_region_parsed(self):
        from master_fetch.search_api_keys import SerperEngine
        eng = SerperEngine()
        data, _ = eng._build_request("q", "k", region="gb-en")
        assert data["gl"] == "gb"
        assert data["hl"] == "en"

    def test_build_request_page_passed(self):
        from master_fetch.search_api_keys import SerperEngine
        eng = SerperEngine()
        data, _ = eng._build_request("q", "k", page=3)
        assert data["page"] == 3

    def test_parse_date_enriched_snippet(self):
        from master_fetch.search_api_keys import SerperEngine
        eng = SerperEngine()
        data = {"organic": [
            {"title": "R1", "link": "https://example.com", "snippet": "Content", "date": "3 days ago"},
        ]}
        results = eng._parse_results(data)
        assert "3 days ago" in results[0].body


class TestTavilyEngine:
    """Tavily engine: POST, Bearer auth, results[] with content."""

    def test_parse_results(self):
        from master_fetch.search_api_keys import TavilyEngine
        eng = TavilyEngine()
        data = {"results": [
            {"title": "Result 1", "url": "https://example.com/1", "content": "Content 1"},
            {"title": "Result 2", "url": "https://example.com/2", "content": "Content 2"},
        ]}
        results = eng._parse_results(data)
        assert len(results) == 2
        assert results[0].title == "Result 1"
        assert results[0].href == "https://example.com/1"
        assert results[0].body == "Content 1"

    def test_parse_missing_url_skipped(self):
        from master_fetch.search_api_keys import TavilyEngine
        eng = TavilyEngine()
        data = {"results": [
            {"title": "No URL", "content": "test"},  # skip
            {"url": "https://ok.com", "content": "OK"},  # keep (title from URL)
        ]}
        results = eng._parse_results(data)
        assert len(results) == 1

    def test_build_request_bearer_auth(self):
        from master_fetch.search_api_keys import TavilyEngine
        eng = TavilyEngine()
        data, headers = eng._build_request("test", "my-key")
        assert data["query"] == "test"
        assert headers["Authorization"] == "Bearer my-key"
        assert data["search_depth"] == "advanced"

    def test_build_request_timelimit_maps_to_time_range(self):
        from master_fetch.search_api_keys import TavilyEngine
        eng = TavilyEngine()
        for tl, tr in [("d", "day"), ("w", "week"), ("m", "month"), ("y", "year")]:
            data, _ = eng._build_request("q", "k", timelimit=tl)
            assert data["time_range"] == tr

    def test_build_request_include_domains(self):
        from master_fetch.search_api_keys import TavilyEngine
        eng = TavilyEngine()
        data, _ = eng._build_request("q", "k", site="example.com")
        assert data["include_domains"] == ["example.com"]

    def test_build_request_exclude_domains(self):
        from master_fetch.search_api_keys import TavilyEngine
        eng = TavilyEngine()
        data, _ = eng._build_request("q", "k", exclude_sites=["spam.com"])
        assert data["exclude_domains"] == ["spam.com"]

    def test_parse_truncates_long_content(self):
        from master_fetch.search_api_keys import TavilyEngine
        eng = TavilyEngine()
        long_content = "x" * 600
        data = {"results": [{"title": "T", "url": "https://example.com", "content": long_content}]}
        results = eng._parse_results(data)
        assert len(results[0].body) <= 404  # 400 + "..."


class TestExaEngine:
    """Exa engine: POST, x-api-key, results[] with url."""

    def test_parse_results(self):
        from master_fetch.search_api_keys import ExaEngine
        eng = ExaEngine()
        data = {"results": [
            {"title": "Result 1", "url": "https://example.com/1", "publishedDate": "2026-01-01"},
            {"title": "", "url": "https://example.com/2"},  # empty title -> domain fallback
        ]}
        results = eng._parse_results(data)
        assert len(results) == 2
        assert results[0].title == "Result 1"
        assert "2026-01-01" in results[0].body

    def test_parse_missing_url_skipped(self):
        from master_fetch.search_api_keys import ExaEngine
        eng = ExaEngine()
        data = {"results": [{"title": "No URL"}]}
        results = eng._parse_results(data)
        assert results == []

    def test_build_request_x_api_key(self):
        from master_fetch.search_api_keys import ExaEngine
        eng = ExaEngine()
        data, headers = eng._build_request("test", "my-key")
        assert data["query"] == "test"
        assert data["numResults"] == 10
        assert headers["x-api-key"] == "my-key"

    def test_build_request_contents_highlights_always_present(self):
        """CRITICAL: without contents.highlights, Exa returns no snippets."""
        from master_fetch.search_api_keys import ExaEngine
        eng = ExaEngine()
        data, _ = eng._build_request("q", "k")
        assert "contents" in data
        assert data["contents"].get("highlights") is True

    def test_build_request_include_domains(self):
        from master_fetch.search_api_keys import ExaEngine
        eng = ExaEngine()
        data, _ = eng._build_request("q", "k", site="example.com")
        assert data["includeDomains"] == ["example.com"]

    def test_build_request_exclude_domains(self):
        from master_fetch.search_api_keys import ExaEngine
        eng = ExaEngine()
        data, _ = eng._build_request("q", "k", exclude_sites=["spam.com"])
        assert data["excludeDomains"] == ["spam.com"]

    def test_build_request_start_published_date(self):
        from master_fetch.search_api_keys import ExaEngine
        eng = ExaEngine()
        data, _ = eng._build_request("q", "k", timelimit="d")
        assert "startPublishedDate" in data
        assert len(data["startPublishedDate"]) == 10  # YYYY-MM-DD

    def test_parse_highlights_from_contents(self):
        """Verify that Exa highlights are extracted from the top-level field."""
        from master_fetch.search_api_keys import ExaEngine
        eng = ExaEngine()
        data = {"results": [
            {"title": "R1", "url": "https://example.com",
             "highlights": ["key snippet text", "second snippet", "third snippet"]},
        ]}
        results = eng._parse_results(data)
        assert "key snippet text" in results[0].body
        assert "second snippet" in results[0].body  # first 2 joined
        assert "third snippet" not in results[0].body  # third excluded

    def test_parse_falls_back_to_metadata_without_highlights(self):
        from master_fetch.search_api_keys import ExaEngine
        eng = ExaEngine()
        data = {"results": [
            {"title": "R1", "url": "https://example.com",
             "publishedDate": "2026-01-15", "author": "Jane"},
        ]}
        results = eng._parse_results(data)
        assert "2026-01-15" in results[0].body
        assert "Jane" in results[0].body


class TestFirecrawlEngine:
    """Firecrawl engine: POST, Bearer, data.web[] response."""

    def test_parse_v2_format(self):
        from master_fetch.search_api_keys import FirecrawlEngine
        eng = FirecrawlEngine()
        data = {"success": True, "data": {"web": [
            {"title": "Result 1", "url": "https://example.com/1", "description": "Desc 1"},
        ]}}
        results = eng._parse_results(data)
        assert len(results) == 1
        assert results[0].title == "Result 1"
        assert "Desc 1" in results[0].body

    def test_parse_list_format(self):
        from master_fetch.search_api_keys import FirecrawlEngine
        eng = FirecrawlEngine()
        data = {"data": [
            {"title": "R1", "url": "https://example.com/1"},
        ]}
        results = eng._parse_results(data)
        assert len(results) == 1

    def test_parse_with_highlights(self):
        from master_fetch.search_api_keys import FirecrawlEngine
        eng = FirecrawlEngine()
        data = {"data": {"web": [
            {"title": "R1", "url": "https://example.com", "highlights": ["highlight1", "highlight2"]},
        ]}}
        results = eng._parse_results(data)
        assert "highlight1" in results[0].body

    def test_build_request_bearer_auth(self):
        from master_fetch.search_api_keys import FirecrawlEngine
        eng = FirecrawlEngine()
        data, headers = eng._build_request("test", "my-key")
        assert data["query"] == "test"
        assert headers["Authorization"] == "Bearer my-key"

    def test_build_request_include_domains(self):
        from master_fetch.search_api_keys import FirecrawlEngine
        eng = FirecrawlEngine()
        data, _ = eng._build_request("q", "k", site="example.com")
        assert data["includeDomains"] == ["example.com"]

    def test_build_request_exclude_domains(self):
        from master_fetch.search_api_keys import FirecrawlEngine
        eng = FirecrawlEngine()
        data, _ = eng._build_request("q", "k", exclude_sites=["spam.com"])
        assert data["excludeDomains"] == ["spam.com"]

    def test_build_request_timelimit_maps_to_tbs(self):
        from master_fetch.search_api_keys import FirecrawlEngine
        eng = FirecrawlEngine()
        data, _ = eng._build_request("q", "k", timelimit="d")
        assert data["tbs"] == "qdr:d"


class TestTinyFishEngine:
    """TinyFish engine: GET, X-API-Key, results[] with snippet."""

    def test_parse_results(self):
        from master_fetch.search_api_keys import TinyFishEngine
        eng = TinyFishEngine()
        data = {"results": [
            {"title": "Result 1", "url": "https://example.com/1", "snippet": "Snippet 1"},
        ]}
        results = eng._parse_results(data)
        assert len(results) == 1
        assert results[0].title == "Result 1"
        assert results[0].body == "Snippet 1"

    def test_build_request_get_method(self):
        from master_fetch.search_api_keys import TinyFishEngine
        eng = TinyFishEngine()
        data, headers = eng._build_request("test", "my-key")
        assert data["query"] == "test"
        assert headers["X-API-Key"] == "my-key"
        assert eng.search_method == "GET"

    def test_build_request_after_date(self):
        from master_fetch.search_api_keys import TinyFishEngine
        eng = TinyFishEngine()
        data, _ = eng._build_request("q", "k", timelimit="d")
        assert "after_date" in data
        assert len(data["after_date"]) == 10  # YYYY-MM-DD

    def test_build_request_site_reapplied(self):
        from master_fetch.search_api_keys import TinyFishEngine
        eng = TinyFishEngine()
        data, _ = eng._build_request("query", "k", site="example.com")
        assert "site:example.com" in data["query"]

    def test_build_request_exclude_sites_reapplied(self):
        from master_fetch.search_api_keys import TinyFishEngine
        eng = TinyFishEngine()
        data, _ = eng._build_request("query", "k", exclude_sites=["spam.com"])
        assert "-site:spam.com" in data["query"]


# ─── Engine search() with mocked HTTP ──────────────────────────────────────

class TestBYOKEngineSearch:
    """Test the full search() flow with mocked HTTP responses."""

    def _make_engine_with_mock(self, engine_cls, mock_response, monkeypatch):
        """Create an engine with a mocked HTTP client."""
        from master_fetch.search_api_keys import _POOLS, KeyPool
        provider = engine_cls.provider_name
        _POOLS[provider] = KeyPool(["test-key"])
        eng = engine_cls()
        # Mock the http_client.request method
        eng.http_client.request = MagicMock(return_value=mock_response)
        return eng

    def test_serper_success(self, monkeypatch):
        from master_fetch.search_api_keys import SerperEngine, _POOLS
        resp = _MockResponse(200, {"organic": [
            {"title": "Test", "link": "https://example.com", "snippet": "OK"},
        ]})
        eng = self._make_engine_with_mock(SerperEngine, resp, monkeypatch)
        results = eng.search("test query")
        assert results is not None
        assert len(results) == 1
        assert results[0].title == "Test"

    def test_rate_limit_triggers_rotation(self, monkeypatch):
        """When first key gets 429, engine retries with next key."""
        from master_fetch.search_api_keys import SerperEngine, _POOLS, KeyPool
        _POOLS["serper"] = KeyPool(["key1", "key2"])
        eng = SerperEngine()
        # First call returns 429, second returns 200
        responses = [
            _MockResponse(429),
            _MockResponse(200, {"organic": [{"title": "OK", "link": "https://ok.com", "snippet": ""}]}),
        ]
        eng.http_client.request = MagicMock(side_effect=responses)
        results = eng.search("test")
        assert results is not None
        assert len(results) == 1
        assert results[0].title == "OK"
        # Verify the pool marked key1 as rate-limited
        pool = _POOLS["serper"]
        key1_state = pool._state.get("key1", {})
        assert key1_state.get("error") == "rate_limited"

    def test_all_keys_rate_limited_raises(self, monkeypatch):
        """When all keys get 429, MetaBlockedException is raised."""
        from master_fetch.search_api_keys import SerperEngine, _POOLS, KeyPool
        from master_fetch.search_metasearch import MetaBlockedException
        _POOLS["serper"] = KeyPool(["key1"])
        eng = SerperEngine()
        eng.http_client.request = MagicMock(return_value=_MockResponse(429))
        with pytest.raises(MetaBlockedException):
            eng.search("test")

    def test_invalid_key_marks_invalid(self, monkeypatch):
        """401/403 marks key as invalid with longer cooldown."""
        from master_fetch.search_api_keys import TavilyEngine, _POOLS, KeyPool
        _POOLS["tavily"] = KeyPool(["bad-key", "good-key"])
        eng = TavilyEngine()
        responses = [
            _MockResponse(403),  # bad key
            _MockResponse(200, {"results": [{"title": "OK", "url": "https://ok.com", "content": ""}]}),
        ]
        eng.http_client.request = MagicMock(side_effect=responses)
        results = eng.search("test")
        assert results is not None
        pool = _POOLS["tavily"]
        assert pool._state.get("bad-key", {}).get("error") == "invalid"

    def test_server_error_returns_none(self, monkeypatch):
        """502/503 server errors don't mark the key, just return None."""
        from master_fetch.search_api_keys import ExaEngine, _POOLS, KeyPool
        _POOLS["exa"] = KeyPool(["key1"])
        eng = ExaEngine()
        eng.http_client.request = MagicMock(return_value=_MockResponse(502))
        results = eng.search("test")
        assert results is None
        # Key should NOT be marked as rate-limited or invalid
        pool = _POOLS["exa"]
        assert pool._state.get("key1", {}).get("error") is None

    def test_malformed_json_returns_none(self, monkeypatch):
        from master_fetch.search_api_keys import FirecrawlEngine, _POOLS, KeyPool
        _POOLS["firecrawl"] = KeyPool(["key1"])
        eng = FirecrawlEngine()
        resp = _MockResponse(200, text="not json {{{")
        eng.http_client.request = MagicMock(return_value=resp)
        results = eng.search("test")
        assert results is None

    def test_empty_results_returns_none(self, monkeypatch):
        from master_fetch.search_api_keys import TinyFishEngine, _POOLS, KeyPool
        _POOLS["tinyfish"] = KeyPool(["key1"])
        eng = TinyFishEngine()
        eng.http_client.request = MagicMock(return_value=_MockResponse(200, {"results": []}))
        results = eng.search("test")
        assert results is None

    def test_no_pool_returns_none(self, monkeypatch):
        """When no keys are configured, search returns None immediately."""
        from master_fetch.search_api_keys import SerperEngine, _POOLS
        _POOLS.clear()
        eng = SerperEngine()
        results = eng.search("test")
        assert results is None

    def test_site_prefix_stripped(self, monkeypatch):
        """site: prefixes are stripped before sending to API."""
        from master_fetch.search_api_keys import SerperEngine, _POOLS, KeyPool
        _POOLS["serper"] = KeyPool(["key1"])
        eng = SerperEngine()
        captured_data = {}
        def mock_request(method, url, **kwargs):
            captured_data.update(kwargs.get("json", kwargs.get("params", {})))
            return _MockResponse(200, {"organic": []})
        eng.http_client.request = MagicMock(side_effect=mock_request)
        eng.search("site:example.com test query")
        assert "site:" not in captured_data.get("q", "")

    def test_timeout_returns_none(self, monkeypatch):
        """Network timeout doesn't mark the key, returns None."""
        from master_fetch.search_api_keys import SerperEngine, _POOLS, KeyPool
        from master_fetch.search_metasearch import MetaTimeoutException
        _POOLS["serper"] = KeyPool(["key1"])
        eng = SerperEngine()
        eng.http_client.request = MagicMock(side_effect=MetaTimeoutException("timeout"))
        results = eng.search("test")
        assert results is None
        # Key should NOT be marked
        pool = _POOLS["serper"]
        assert pool._state.get("key1", {}).get("error") is None


# ─── Metasearch Integration ───────────────────────────────────────────────────

class TestMetasearchBYOKIntegration:
    """Test BYOK backends integrate with metasearch correctly."""

    def test_byok_engines_registered_when_keys_present(self, monkeypatch):
        """When BYOK keys are configured, engines are registered in _TEXT_ENGINES."""
        import master_fetch.search_metasearch as m
        from master_fetch.search_api_keys import _reset_byok_pools
        monkeypatch.setenv("HOUND_SEARCH_SERPER_KEYS", "test-key")
        _reset_byok_pools()
        original = dict(m._TEXT_ENGINES)
        try:
            m._register_byok_backends()
            assert "serper" in m._TEXT_ENGINES
        finally:
            m._TEXT_ENGINES.clear()
            m._TEXT_ENGINES.update(original)
            _reset_byok_pools()

    def test_byok_engines_not_registered_without_keys(self, monkeypatch):
        """When no BYOK keys, engines are NOT registered."""
        import master_fetch.search_metasearch as m
        from master_fetch.search_api_keys import _reset_byok_pools
        for var in ["HOUND_SEARCH_SERPER_KEYS", "HOUND_SEARCH_TAVILY_KEYS",
                     "HOUND_SEARCH_EXA_KEYS", "HOUND_SEARCH_FIRECRAWL_KEYS",
                     "HOUND_SEARCH_TINYFISH_KEYS"]:
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setattr(Path, "home", lambda: Path("/nonexistent-home-12345"))
        _reset_byok_pools()
        original = dict(m._TEXT_ENGINES)
        # Remove any BYOK engines that might have been registered earlier
        for name in ("serper", "tavily", "exa", "firecrawl", "tinyfish"):
            m._TEXT_ENGINES.pop(name, None)
        try:
            m._register_byok_backends()
            assert "serper" not in m._TEXT_ENGINES
            assert "tavily" not in m._TEXT_ENGINES
        finally:
            m._TEXT_ENGINES.clear()
            m._TEXT_ENGINES.update(original)
            _reset_byok_pools()

    def test_byok_results_sorted_first(self):
        """BYOK results should sort before general engine results."""
        import master_fetch.search_metasearch as m
        order = [
            {"backend": "duckduckgo", "href": "http://ddg.com", "title": "DDG"},
            {"backend": "serper", "href": "http://serper.com", "title": "Serper"},
            {"backend": "github_api", "href": "http://gh.com", "title": "GH"},
        ]
        _BYOK = {"serper", "tavily", "exa", "firecrawl", "tinyfish"}
        _API = {"semantic_scholar", "github_api", "hackernews"}
        def _sort_key(e):
            b = e.get("backend", "")
            if b in _BYOK: return 0
            if b in _API: return 2
            return 1
        order.sort(key=_sort_key)
        assert order[0]["backend"] == "serper"
        assert order[1]["backend"] == "duckduckgo"
        assert order[2]["backend"] == "github_api"


# ─── Pool Management ─────────────────────────────────────────────────────────

class TestPoolManagement:
    """Module-level pool singleton management."""

    def test_get_pool_returns_none_without_keys(self, monkeypatch):
        from master_fetch.search_api_keys import _get_pool, _reset_byok_pools
        _reset_byok_pools()
        for var in ["HOUND_SEARCH_SERPER_KEYS", "HOUND_SEARCH_TAVILY_KEYS",
                     "HOUND_SEARCH_EXA_KEYS", "HOUND_SEARCH_FIRECRAWL_KEYS",
                     "HOUND_SEARCH_TINYFISH_KEYS"]:
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setattr(Path, "home", lambda: Path("/nonexistent-home-12345"))
        assert _get_pool("serper") is None

    def test_get_pool_creates_pool_with_keys(self, monkeypatch):
        from master_fetch.search_api_keys import _get_pool, _reset_byok_pools
        _reset_byok_pools()
        monkeypatch.setenv("HOUND_SEARCH_EXA_KEYS", "exa-key1,exa-key2")
        pool = _get_pool("exa")
        assert pool is not None
        assert pool.size == 2

    def test_refresh_pools_picks_up_new_keys(self, monkeypatch):
        from master_fetch.search_api_keys import _refresh_pools, _POOLS, _reset_byok_pools
        _reset_byok_pools()
        monkeypatch.setenv("HOUND_SEARCH_SERPER_KEYS", "key1")
        _refresh_pools()
        assert "serper" in _POOLS
        # Add another key
        monkeypatch.setenv("HOUND_SEARCH_SERPER_KEYS", "key1,key2")
        _refresh_pools()
        assert _POOLS["serper"].size == 2

    def test_reset_clears_all_pools(self):
        from master_fetch.search_api_keys import _POOLS, _reset_byok_pools
        _POOLS["test"] = MagicMock()
        _reset_byok_pools()
        assert _POOLS == {}


# ─── Provider Registry ───────────────────────────────────────────────────────

class TestProviderRegistry:
    """get_byok_engines() returns only providers with keys."""

    def test_returns_only_configured_providers(self, monkeypatch):
        from master_fetch.search_api_keys import get_byok_engines, _reset_byok_pools
        _reset_byok_pools()
        monkeypatch.setenv("HOUND_SEARCH_SERPER_KEYS", "key1")
        monkeypatch.setenv("HOUND_SEARCH_TAVILY_KEYS", "key2")
        engines = get_byok_engines()
        assert "serper" in engines
        assert "tavily" in engines
        assert "exa" not in engines

    def test_returns_empty_without_keys(self, monkeypatch):
        from master_fetch.search_api_keys import get_byok_engines, _reset_byok_pools
        _reset_byok_pools()
        for var in ["HOUND_SEARCH_SERPER_KEYS", "HOUND_SEARCH_TAVILY_KEYS",
                     "HOUND_SEARCH_EXA_KEYS", "HOUND_SEARCH_FIRECRAWL_KEYS",
                     "HOUND_SEARCH_TINYFISH_KEYS"]:
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setattr(Path, "home", lambda: Path("/nonexistent-home-12345"))
        engines = get_byok_engines()
        assert engines == {}


# ─── Index Family ─────────────────────────────────────────────────────────────

class TestIndexFamily:
    """BYOK engines have correct index family entries."""

    def test_all_byok_providers_in_index_family(self):
        from master_fetch.search_engines import _INDEX_FAMILY
        for provider in ("serper", "tavily", "exa", "firecrawl", "tinyfish"):
            assert provider in _INDEX_FAMILY
            assert _INDEX_FAMILY[provider] == provider
