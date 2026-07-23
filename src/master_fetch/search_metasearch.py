"""Hound metasearch engine layer.

Vendored + stripped from ddgs (https://github.com/deedy5/ddgs), MIT-licensed,
(c) Pragmatic School / deedy5. Adapted for hound-mcp: text search only,
async-native parallel aggregation with early-return-on-quorum, no CLI / API
server / MCP / images / videos / news / books / extract / cache / network bloat.
See the ddgs LICENSE notice in NOTICE.ddgs.txt for full attribution.

Backends (all keyless, no API key, no account): duckduckgo, brave, google,
grokipedia, mojeek, startpage, wikipedia, yahoo, yandex. They run in PARALLEL;
a backend that CAPTCHAs / rate-limits / has no topic-match simply yields
nothing and the others carry - so search is robust without any single point of
failure. This is the robustness hound's hand-rolled 3-engine scraper never had.

Transport: primp (Rust HTTP client with browser TLS/header impersonation) for
most backends; httpx (HTTP/2 + randomized cipher/SETTINGS frame) for DuckDuckGo.
HOUND_SEARCH_PROXY env var (http/https/socks5) is the power-user rotating-proxy
escape hatch for per-IP throttling - the one thing no scraper, browser or not,
can escape from a single IP.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import ssl
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cached_property
from random import SystemRandom
from time import time
from types import TracebackType
from typing import Any, ClassVar, Optional, TypeVar
from urllib.parse import parse_qs, quote, unquote_plus, urlparse

import h2
import httpcore
import httpx
import primp
from fake_useragent import UserAgent
from lxml import html
from lxml.etree import HTMLParser as LHTMLParser

logger = logging.getLogger(__name__)
random = SystemRandom()

T = TypeVar("T")

_PROXY = os.environ.get("HOUND_SEARCH_PROXY") or None
if _PROXY:
    _PROXY = _PROXY.strip()
    if not _PROXY:
        _PROXY = None
    else:
        # Validate scheme at import so a typo doesn't silently kill every backend.
        _scheme = urlparse(_PROXY).scheme.lower()
        if _scheme not in ("http", "https", "socks5", "socks5h"):
            logger.warning(
                "HOUND_SEARCH_PROXY has unsupported scheme '%s' (expected http, "
                "https, socks5, or socks5h). Ignoring proxy, using direct connection.",
                _scheme or "(none)",
            )
            _PROXY = None
# Per-engine + overall deadline. Engines run in parallel + we early-return on
# quorum, so a healthy search is ~1-2s; this bounds a fully-throttled one.
_SEARCH_DEADLINE = float(os.environ.get("HOUND_SEARCH_DEADLINE", "8") or "8")
_ua = UserAgent()


# ─── exceptions ──────────────────────────────────────────────────────────────
class MetaSearchException(Exception):
    """Base metasearch error."""


class MetaTimeoutException(MetaSearchException):
    """A backend or the whole search timed out."""


class MetaBlockedException(MetaSearchException):
    """A backend refused us (CAPTCHA / 403 / rate-limit). The caller should
    circuit-open that backend for a cooldown so we don't keep hammering a host
    that is actively blocking our IP (which risks escalating to a longer IP ban)."""


# ─── transport: primp (browser-impersonated TLS) ─────────────────────────────
class _PrimpResponse:
    """Thin wrapper over a primp response (status, text, content)."""

    __slots__ = ("_resp", "content", "status_code", "text")

    def __init__(self, resp: Any) -> None:
        self._resp = resp
        self.status_code = resp.status_code
        self.content = resp.content
        self.text = resp.text


class _PrimpClient:
    """primp-based HTTP client with random browser impersonation (anti-bot)."""

    def __init__(self, proxy: str | None = None, timeout: int | None = 10, *,
                 verify: bool = True, impersonate: str = "random") -> None:
        self.client = primp.Client(
            proxy=proxy,
            timeout=timeout,
            impersonate=impersonate,
            impersonate_os="random",
            verify=verify,
        )

    def request(self, *args: Any, **kwargs: Any) -> _PrimpResponse:
        try:
            return _PrimpResponse(self.client.request(*args, **kwargs))
        except primp.TimeoutError as ex:
            raise MetaTimeoutException(str(ex)) from ex
        except Exception as ex:
            raise MetaSearchException(f"{type(ex).__name__}: {ex!r}") from ex

    def get(self, url: str, *args: Any, **kwargs: Any) -> _PrimpResponse:
        return self.request("GET", url, *args, **kwargs)

    def post(self, url: str, *args: Any, **kwargs: Any) -> _PrimpResponse:
        return self.request("POST", url, *args, **kwargs)


# ─── transport: httpx (HTTP/2 + randomized fingerprint, for DuckDuckGo) ──────
_DEFAULT_CIPHERS = [  # cloudflare-recommended + modern + compatible + legacy
    "TLS_AES_128_GCM_SHA256", "TLS_AES_256_GCM_SHA384", "TLS_CHACHA20_POLY1305_SHA256",
    "ECDHE-ECDSA-AES128-GCM-SHA256", "ECDHE-ECDSA-CHACHA20-POLY1305", "ECDHE-RSA-AES128-GCM-SHA256",
    "ECDHE-RSA-CHACHA20-POLY1305", "ECDHE-ECDSA-AES256-GCM-SHA384", "ECDHE-RSA-AES256-GCM-SHA384",
    "ECDHE-ECDSA-AES128-GCM-SHA256", "ECDHE-ECDSA-CHACHA20-POLY1305", "ECDHE-RSA-AES128-GCM-SHA256",
    "ECDHE-RSA-CHACHA20-POLY1305", "ECDHE-ECDSA-AES256-GCM-SHA384", "ECDHE-RSA-AES256-GCM-SHA384",
    "ECDHE-ECDSA-AES128-SHA256", "ECDHE-RSA-AES128-SHA256", "ECDHE-ECDSA-AES256-SHA384",
    "ECDHE-RSA-AES256-SHA384", "ECDHE-ECDSA-AES128-SHA", "ECDHE-RSA-AES128-SHA", "AES128-GCM-SHA256",
    "AES128-SHA256", "AES128-SHA", "ECDHE-RSA-AES256-SHA", "AES256-GCM-SHA384", "AES256-SHA256",
    "AES256-SHA", "DES-CBC3-SHA",
]  # fmt: skip


def _random_ssl_context(verify: bool = True) -> ssl.SSLContext:
    ctx = ssl.create_default_context(cafile=verify if isinstance(verify, str) else None)
    shuffled = random.sample(_DEFAULT_CIPHERS[9:], len(_DEFAULT_CIPHERS) - 9)
    ctx.set_ciphers(":".join(_DEFAULT_CIPHERS[:9] + shuffled))
    commands = [
        None,
        lambda c: setattr(c, "maximum_version", ssl.TLSVersion.TLSv1_2),
        lambda c: setattr(c, "minimum_version", ssl.TLSVersion.TLSv1_3),
        lambda c: setattr(c, "options", c.options | ssl.OP_NO_TICKET),
    ]
    cmd = random.choice(commands)
    if cmd:
        cmd(ctx)
    return ctx


class _H2Patch:
    """Randomize HTTP/2 SETTINGS frame to dodge JA3/JA4 fingerprinting (DuckDuckGo)."""

    def __enter__(self) -> None:
        def _send_connection_init(self: httpcore._sync.http2.HTTP2Connection, request: httpcore.Request) -> None:
            self._h2_state.local_settings = h2.settings.Settings(
                client=True,
                initial_values={
                    h2.settings.SettingCodes.INITIAL_WINDOW_SIZE: random.randint(100, 200),
                    h2.settings.SettingCodes.HEADER_TABLE_SIZE: random.randint(4000, 5000),
                    h2.settings.SettingCodes.MAX_FRAME_SIZE: random.randint(16384, 65535),
                    h2.settings.SettingCodes.MAX_CONCURRENT_STREAMS: random.randint(100, 200),
                    h2.settings.SettingCodes.MAX_HEADER_LIST_SIZE: random.randint(65500, 66500),
                    h2.settings.SettingCodes.ENABLE_CONNECT_PROTOCOL: random.randint(0, 1),
                    h2.settings.SettingCodes.ENABLE_PUSH: random.randint(0, 1),
                },
            )
            self._h2_state.initiate_connection()
            self._h2_state.increment_flow_control_window(2**24)
            self._write_outgoing_data(request)

        self._orig = httpcore._sync.http2.HTTP2Connection._send_connection_init
        httpcore._sync.http2.HTTP2Connection._send_connection_init = _send_connection_init  # type: ignore[method-assign]

    def __exit__(self, *exc: Any) -> None:
        httpcore._sync.http2.HTTP2Connection._send_connection_init = self._orig  # type: ignore[method-assign]


class _HttpxResponse:
    __slots__ = ("content", "status_code", "text")

    def __init__(self, status_code: int, content: bytes, text: str) -> None:
        self.status_code = status_code
        self.content = content
        self.text = text


class _HttpxClient:
    """httpx client for DuckDuckGo (primp had issues with DDG upstream)."""

    def __init__(self, headers: dict[str, str] | None = None, proxy: str | None = None,
                 timeout: int | None = 10, *, verify: bool = True) -> None:
        self.client = httpx.Client(
            headers=headers, proxy=proxy, timeout=timeout,
            verify=_random_ssl_context(verify=verify) if verify else False,
            follow_redirects=False, http2=True,
        )

    def request(self, *args: Any, **kwargs: Any) -> _HttpxResponse:
        with _H2Patch():
            try:
                resp = self.client.request(*args, **kwargs)
                return _HttpxResponse(resp.status_code, resp.content, resp.text)
            except Exception as ex:
                if "timed out" in f"{ex}":
                    raise MetaTimeoutException(f"Request timed out: {ex!r}") from ex
                raise MetaSearchException(f"{type(ex).__name__}: {ex!r}") from ex


# ─── result type ─────────────────────────────────────────────────────────────
@dataclass
class TextResult:
    """A single text search result from a backend."""

    title: str = ""
    href: str = ""
    body: str = ""


# ─── base search engine (text-only, XPath-driven) ────────────────────────────
class BaseSearchEngine:
    """Abstract base: build_payload -> fetch -> extract via XPath -> post-process."""

    name: ClassVar[str]
    category: ClassVar[str] = "text"
    provider: ClassVar[str]
    disabled: ClassVar[bool] = False
    priority: ClassVar[float] = 1.0

    search_url: str
    search_method: ClassVar[str] = "GET"
    headers_update: ClassVar[Mapping[str, str]] = {}
    items_xpath: ClassVar[str]
    elements_xpath: ClassVar[Mapping[str, str]]

    def __init__(self, proxy: str | None = None, timeout: int | None = None, *, verify: bool = True) -> None:
        self.http_client = _PrimpClient(proxy=proxy, timeout=timeout, verify=verify)
        self.http_client.client.headers_update(self.headers_update)
        self.results: list[Any] = []

    @property
    def result_type(self) -> type:
        return TextResult

    def build_payload(self, query: str, region: str, safesearch: str,
                      timelimit: str | None, page: int, **kwargs: str) -> dict[str, Any]:
        raise NotImplementedError

    def request(self, *args: Any, **kwargs: Any) -> str | None:
        resp = self.http_client.request(*args, **kwargs)
        if resp.status_code in (403, 503):
            # Bot challenge / access denied -> circuit-open this backend rather
            # than treat it as a normal empty result (which would retry every call
            # and risk escalating the block). Empty/timeout stay non-fatal.
            raise MetaBlockedException(f"HTTP {resp.status_code}")
        return resp.text if resp.status_code == 200 else None

    @cached_property
    def parser(self) -> LHTMLParser:
        return LHTMLParser(remove_blank_text=True, remove_comments=True,
                           remove_pis=True, collect_ids=False)

    def extract_tree(self, html_text: str) -> html.Element:
        return html.fromstring(html_text, parser=self.parser)

    def pre_process_html(self, html_text: str) -> str:
        return html_text

    def extract_results(self, html_text: str) -> list[Any]:
        html_text = self.pre_process_html(html_text)
        tree = self.extract_tree(html_text)
        results = []
        for item in tree.xpath(self.items_xpath):
            result = self.result_type()
            for key, value in self.elements_xpath.items():
                data = " ".join("".join(item.xpath(value)).split())
                result.__setattr__(key, data)
            results.append(result)
        return results

    def post_extract_results(self, results: list[Any]) -> list[Any]:
        return results

    def search(self, query: str, region: str = "us-en", safesearch: str = "moderate",
               timelimit: str | None = None, page: int = 1, **kwargs: str) -> list[Any] | None:
        payload = self.build_payload(query=query, region=region, safesearch=safesearch,
                                     timelimit=timelimit, page=page, **kwargs)
        if self.search_method == "GET":
            html_text = self.request(self.search_method, self.search_url, params=payload)
        else:
            html_text = self.request(self.search_method, self.search_url, data=payload)
        if not html_text:
            return None
        return self.post_extract_results(self.extract_results(html_text))


# ─── DuckDuckGo (httpx transport) ────────────────────────────────────────────
class Duckduckgo(BaseSearchEngine):
    name = "duckduckgo"
    provider = "bing"
    search_url = "https://html.duckduckgo.com/html/"
    search_method = "POST"
    items_xpath = "//div[contains(@class, 'body')]"
    elements_xpath: ClassVar[Mapping[str, str]] = {
        "title": ".//h2//text()", "href": "./a/@href", "body": "./a//text()",
    }
    headers: ClassVar[dict[str, str]] = {}

    def __init__(self, proxy: str | None = None, timeout: int | None = None, *, verify: bool = True) -> None:
        # DDG uses the httpx transport (primp had issues upstream).
        self.headers = {"User-Agent": _ua.random}
        self.http_client = _HttpxClient(headers=self.headers, proxy=proxy, timeout=timeout, verify=verify)  # type: ignore[assignment]
        self.results: list[Any] = []

    def build_payload(self, query: str, region: str, safesearch: str,  # noqa: ARG002
                      timelimit: str | None, page: int = 1, **kwargs: str) -> dict[str, Any]:
        payload = {"q": query, "b": "", "l": region}
        if page > 1:
            payload["s"] = f"{10 + (page - 2) * 15}"
        if timelimit:
            payload["df"] = timelimit
        return payload

    def request(self, *args: Any, **kwargs: Any) -> str | None:
        # httpx transport: kwargs use method= instead of positional method.
        method = args[0] if args else kwargs.pop("method", "GET")
        url = args[1] if len(args) > 1 else kwargs.pop("url", "")
        resp = self.http_client.request(method=method, url=url, **kwargs)  # type: ignore[attr-defined]
        if resp.status_code in (403, 503):
            raise MetaBlockedException(f"HTTP {resp.status_code}")
        return resp.text if resp.status_code == 200 else None

    def post_extract_results(self, results: list[Any]) -> list[Any]:
        return [r for r in results if not r.href.startswith("https://duckduckgo.com/y.js?")]


# ─── Bing (disabled upstream; kept off by default - DDG/Yahoo serve its index) ─
def _unwrap_bing_url(raw_url: str) -> str | None:
    parsed = urlparse(raw_url)
    u_vals = parse_qs(parsed.query).get("u", [])
    if not u_vals:
        return None
    u = u_vals[0]
    if len(u) <= 2:
        return None
    b64 = u[2:]
    return base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4)).decode()


class Bing(BaseSearchEngine):
    disabled = True  # DDG + Yahoo already serve Bing's index; direct Bing is redundant.
    name = "bing"
    provider = "bing"
    search_url = "https://www.bing.com/search"
    search_method = "GET"
    items_xpath = "//li[contains(@class, 'b_algo')]"
    elements_xpath: ClassVar[Mapping[str, str]] = {
        "title": ".//h2/a//text()", "href": ".//h2/a/@href", "body": ".//p//text()",
    }

    def build_payload(self, query: str, region: str, safesearch: str,  # noqa: ARG002
                      timelimit: str | None, page: int = 1, **kwargs: str) -> dict[str, Any]:
        country, lang = region.lower().split("-")
        payload = {"q": query, "pq": query, "cc": lang}
        self.http_client.client.set_cookies(  # type: ignore[attr-defined]
            "https://www.bing.com",
            {"_EDGE_CD": f"m={lang}-{country}&u={lang}-{country}",
             "_EDGE_S": f"mkt={lang}-{country}&ui={lang}-{country}"},
        )
        if timelimit:
            d = int(time() // 86400)
            code = f"ez5_{d - 365}_{d}" if timelimit == "y" else "ez" + {"d": "1", "w": "2", "m": "3"}[timelimit]
            payload["filters"] = f'ex1:"{code}"'
        if page > 1:
            payload["first"] = f"{(page - 1) * 10}"
            payload["FORM"] = f"PERE{page - 2 if page > 2 else ''}"
        return payload

    def post_extract_results(self, results: list[Any]) -> list[Any]:
        out = []
        for r in results:
            if r.href.startswith("https://www.bing.com/aclick?"):
                continue
            if r.href.startswith("https://www.bing.com/ck/a?"):
                r.href = _unwrap_bing_url(r.href) or r.href
            out.append(r)
        return out


# ─── Brave ───────────────────────────────────────────────────────────────────
class Brave(BaseSearchEngine):
    name = "brave"
    provider = "brave"
    search_url = "https://search.brave.com/search"
    search_method = "GET"
    items_xpath = "//div[@data-type='web']"
    elements_xpath: ClassVar[Mapping[str, str]] = {
        "title": ".//div[(contains(@class,'title') or contains(@class,'sitename-container')) and position()=last()]//text()",
        "href": ".//a[div[contains(@class, 'title')]]/@href",
        "body": ".//div[contains(@class, 'snippet')]//div[contains(@class, 'content')]//text()",
    }

    def build_payload(self, query: str, region: str, safesearch: str,
                      timelimit: str | None, page: int = 1, **kwargs: str) -> dict[str, Any]:
        payload = {"q": query, "source": "web"}
        country, _lang = region.lower().split("-")
        cookies = {country: country, "useLocation": "0"}
        if safesearch != "moderate":
            cookies["safesearch"] = "strict" if safesearch == "on" else "off"
        self.http_client.client.set_cookies("https://search.brave.com", cookies)  # type: ignore[attr-defined]
        if timelimit:
            payload["tf"] = {"d": "pd", "w": "pw", "m": "pm", "y": "py"}[timelimit]
        if page > 1:
            payload["offset"] = f"{page - 1}"
        return payload


# ─── Google (Android UA + CONSENT cookie; often CAPTCHAs under load) ─────────
def _google_ua() -> str:
    devices = (
        ("5.0", "SM-G900P Build/LRX21T", 39, 60),
        ("6.0", "Nexus 5 Build/MRA58N", 39, 60),
        ("8.0", "Pixel 2 Build/OPD3.170816.012", 39, 60),
    )
    av, dev, cmin, cmax = random.choice(devices)
    cmaj = random.randint(cmin, cmax)
    ua = (f"Mozilla/5.0 (Linux; Android {av}; {dev}) AppleWebKit/537.36 "
          f"(KHTML, like Gecko) Chrome/{cmaj}.0.{random.randint(1000, 9999)}.{random.randint(1000, 1999)} "
          f"Mobile Safari/537.36")
    return ua + bytes.fromhex("4e53544e5756").decode()


class Google(BaseSearchEngine):
    name = "google"
    provider = "google"
    search_url = "https://www.google.com/search"
    search_method = "GET"
    headers_update: ClassVar[dict[str, str]] = {}
    items_xpath = "//div[@data-hveid][.//h3]"
    elements_xpath: ClassVar[Mapping[str, str]] = {
        "title": ".//h3//text()", "href": ".//a[.//h3]/@href", "body": "./div/div[last()]//text()",
    }

    def __init__(self, proxy: str | None = None, timeout: int | None = None, *, verify: bool = True) -> None:
        self.headers_update = {"User-Agent": _google_ua()}  # type: ignore[misc]
        super().__init__(proxy=proxy, timeout=timeout, verify=verify)

    def build_payload(self, query: str, region: str, safesearch: str,
                      timelimit: str | None, page: int = 1, **kwargs: str) -> dict[str, Any]:
        self.http_client.client.set_cookies("google.com", {"CONSENT": "YES+"})  # type: ignore[attr-defined]
        start = (page - 1) * 10
        country, lang = region.split("-")
        payload = {"q": query, "filter": {"on": "2", "moderate": "1", "off": "0"}[safesearch.lower()],
                   "start": str(start), "hl": f"{lang}-{country.upper()}", "lr": f"lang_{lang}",
                   "cr": f"country{country.upper()}"}
        if timelimit:
            payload["tbs"] = f"qdr:{timelimit}"
        return payload

    def post_extract_results(self, results: list[Any]) -> list[Any]:
        out = []
        for r in results:
            if r.href.startswith("/url?q="):
                r.href = r.href.split("?q=")[1].split("&")[0]
            if r.title and r.href.startswith("http"):
                out.append(r)
        return out


# ─── Startpage (Google-index, privacy frontend; needs an sc token) ───────────
class Startpage(BaseSearchEngine):
    name = "startpage"
    provider = "google"
    search_url = "https://www.startpage.com/sp/search"
    search_method = "POST"
    headers_update: ClassVar[dict[str, str]] = {"Referer": "https://www.startpage.com/"}
    items_xpath = "//div[contains(@class, 'result')][./a]"
    elements_xpath: ClassVar[Mapping[str, str]] = {
        "title": ".//h2//text()", "href": "./a/@href", "body": ".//p//text()",
    }

    def get_sc(self) -> str:
        resp_text = self.http_client.request("GET", "https://www.startpage.com/").text  # type: ignore[attr-defined]
        tree = self.extract_tree(resp_text)
        sc = tree.xpath('//form[@id="search"]//input[@name="sc"]/@value')
        self._sc = sc[0] if sc else ""
        return self._sc

    def build_payload(self, query: str, region: str, safesearch: str,
                      timelimit: str | None, page: int = 1, **kwargs: str) -> dict[str, Any]:
        country, lang = region.lower().split("-")
        payload: dict[str, Any] = {
            "query": query, "cat": "web", "t": "device", "sc": self.get_sc(),
            "lui": "english", "language": "english", "abp": "1", "abd": "0", "abe": "0",
            "qsr": f"{lang}_{country.upper()}",
            "qadf": {"on": "heavy", "moderate": "moderate", "off": "none"}[safesearch.lower()],
            "segment": "organic",
        }
        if page > 1:
            payload["page"] = str(page)
        if timelimit:
            payload["with_date"] = timelimit
        return payload


# ─── Grokipedia (keyless JSON API; encyclopedic/topic queries) ───────────────
class Grokipedia(BaseSearchEngine):
    name = "grokipedia"
    provider = "grokipedia"
    priority = 1.9
    search_url = "https://grokipedia.com/api/typeahead"
    search_method = "GET"

    def build_payload(self, query: str, region: str, safesearch: str,  # noqa: ARG002
                      timelimit: str | None, page: int = 1,  # noqa: ARG002
                      **kwargs: str) -> dict[str, Any]:
        return {"query": query, "limit": "1"}

    def extract_results(self, html_text: str) -> list[Any]:
        data = json.loads(html_text)
        items = data.get("results", [])
        if not items:
            return []
        r = TextResult()
        r.title = items[0].get("title", "").strip("_")
        body = items[0].get("snippet", "")
        r.body = body.split("\n\n", 1)[1] if "\n\n" in body else body
        r.href = f"https://grokipedia.com/page/{items[0]['slug']}"
        return [r]


# ─── Wikipedia (opensearch API; encyclopedic/topic queries) ──────────────────
class Wikipedia(BaseSearchEngine):
    name = "wikipedia"
    provider = "wikipedia"
    priority = 2.0
    search_url = "https://{lang}.wikipedia.org/w/api.php?action=opensearch&search={query}"
    search_method = "GET"

    def build_payload(self, query: str, region: str, safesearch: str,  # noqa: ARG002
                      timelimit: str | None, page: int = 1,  # noqa: ARG002
                      **kwargs: str) -> dict[str, Any]:
        _country, lang = region.lower().split("-")
        self.search_url = (f"https://{lang}.wikipedia.org/w/api.php?action=opensearch"
                           f"&profile=fuzzy&limit=1&search={quote(query)}")
        self.lang = lang
        return {}

    def extract_results(self, html_text: str) -> list[Any]:
        data = json.loads(html_text)
        if not data[1]:
            return []
        r = TextResult()
        r.title = data[1][0]
        r.href = data[3][0]
        resp = self.request("GET", f"https://{self.lang}.wikipedia.org/w/api.php?action=query"
                            f"&format=json&prop=extracts&titles={quote(r.title)}&explaintext=0&exintro=0&redirects=1")
        if resp:
            pages = json.loads(resp).get("query", {}).get("pages", {})
            r.body = next(iter(pages.values())).get("extract", "")
        if "may refer to:" in r.body:
            return []
        return [r]


# ─── Yahoo (Bing-index from a different server; RU= redirect decode) ─────────
def _yahoo_extract_url(u: str) -> str:
    t = u.split("/RU=", 1)[1]
    return unquote_plus(t.split("/RK=", 1)[0].split("/RS=", 1)[0])


class Yahoo(BaseSearchEngine):
    name = "yahoo"
    provider = "bing"
    search_url = "https://search.yahoo.com/search"
    search_method = "GET"
    items_xpath = "//div[contains(@class, 'relsrch')]"
    elements_xpath: ClassVar[Mapping[str, str]] = {
        "title": ".//div[contains(@class, 'Title')]//h3//text()",
        "href": ".//div[contains(@class, 'Title')]//a/@href",
        "body": ".//div[contains(@class, 'Text')]//text()",
    }

    def build_payload(self, query: str, region: str, safesearch: str,  # noqa: ARG002
                      timelimit: str | None, page: int = 1,  # noqa: ARG002
                      **kwargs: str) -> dict[str, Any]:
        from secrets import token_urlsafe
        self.search_url = (f"https://search.yahoo.com/search;_ylt={token_urlsafe(24 * 3 // 4)}"
                           f";_ylu={token_urlsafe(47 * 3 // 4)}")
        payload = {"p": query}
        if page > 1:
            payload["b"] = f"{(page - 1) * 7 + 1}"
        if timelimit:
            payload["btf"] = timelimit
        return payload

    def post_extract_results(self, results: list[Any]) -> list[Any]:
        out = []
        for r in results:
            if r.href.startswith("https://www.bing.com/aclick?"):
                continue
            if "/RU=" in r.href:
                r.href = _yahoo_extract_url(r.href)
            out.append(r)
        return out


# ─── Mojeek (independent index) ──────────────────────────────────────────────
class Mojeek(BaseSearchEngine):
    name = "mojeek"
    provider = "mojeek"
    search_url = "https://www.mojeek.com/search"
    search_method = "GET"
    items_xpath = "//ul[contains(@class, 'results')]/li"
    elements_xpath: ClassVar[Mapping[str, str]] = {
        "title": ".//h2//text()", "href": ".//h2/a/@href", "body": ".//p[@class='s']//text()",
    }

    def build_payload(self, query: str, region: str, safesearch: str,
                      timelimit: str | None,  # noqa: ARG002
                      page: int = 1, **kwargs: str) -> dict[str, Any]:
        country, lang = region.lower().split("-")
        self.http_client.client.set_cookies("https://www.mojeek.com", {"arc": country, "lb": lang})  # type: ignore[attr-defined]
        payload = {"q": query}
        if safesearch == "on":
            payload["safe"] = "1"
        if page > 1:
            payload["s"] = f"{(page - 1) * 10 + 1}"
        return payload


# ─── Yandex ──────────────────────────────────────────────────────────────────
class Yandex(BaseSearchEngine):
    name = "yandex"
    provider = "yandex"
    search_url = "https://yandex.com/search/site/"
    search_method = "GET"
    items_xpath = "//li[contains(@class, 'serp-item')]"
    elements_xpath: ClassVar[Mapping[str, str]] = {
        "title": ".//h3//text()", "href": ".//h3/a/@href", "body": ".//div[contains(@class, 'text')]//text()",
    }

    def build_payload(self, query: str, region: str, safesearch: str,  # noqa: ARG002
                      timelimit: str | None,  # noqa: ARG002
                      page: int = 1, **kwargs: str) -> dict[str, Any]:
        payload = {"text": query, "web": "1", "searchid": f"{random.randint(1000000, 9999999)}"}
        if page > 1:
            payload["p"] = f"{page - 1}"
        return payload


# ─── Qwant (keyless JSON API; safari-pinned — chrome/edge get 403-captcha) ─────
class Qwant(BaseSearchEngine):
    name = "qwant"
    provider = "qwant"  # own independent index (European)
    search_url = "https://api.qwant.com/v3/search/web"
    search_method = "GET"
    # JSON API -> no XPath; overrides extract_results. SearXNG's proven param set.

    def __init__(self, proxy: str | None = None, timeout: int | None = None, *, verify: bool = True) -> None:
        # Qwant 403-captchas chrome/edge TLS fingerprints; pin to safari.
        self.http_client = _PrimpClient(proxy=proxy, timeout=timeout, verify=verify, impersonate="safari")
        self.http_client.client.headers_update({"Accept": "application/json"})
        self.results: list[Any] = []

    def build_payload(self, query: str, region: str, safesearch: str,
                      timelimit: str | None,  # noqa: ARG002
                      page: int = 1, **kwargs: str) -> dict[str, Any]:
        # hound region is "us-en" (country-lang) -> Qwant locale "en_US".
        country, lang = region.lower().split("-")
        locale = f"{lang}_{country.upper()}"
        ss_map = {"on": 2, "moderate": 1, "off": 0}
        args = {
            "q": query,
            "count": 10,            # count must be exactly 10 (other values -> 400)
            "locale": locale,
            "offset": (page - 1) * 10,
            "tgp": random.randint(1, 3),        # "test group" — value is ignored, must be present
            "device": "desktop",
            "safesearch": ss_map.get(safesearch, 1),
            "display": True,
            "llm": True,
        }
        # Shuffle param order to resist fingerprinting (SearXNG's trick).
        items = list(args.items())
        random.shuffle(items)
        # primp requires every param value to be a str (unlike urlencode which
        # coerces). Bools -> lowercase 'true'/'false' (standard JSON-API style).
        def _str(v):
            if isinstance(v, bool):
                return "true" if v else "false"
            return str(v)
        return {k: _str(v) for k, v in items}

    def extract_results(self, html_text: str) -> list[Any]:
        try:
            data = json.loads(html_text)
        except Exception:
            return []
        if data.get("status") != "success":
            err = data.get("data", {}) or {}
            # captcha / rate-limit (error_code 24) -> block signal (circuit-open).
            if err.get("error_data", {}).get("captchaUrl") or err.get("error_code") == 24:
                raise MetaBlockedException("qwant captcha/rate-limit")
            return []  # other API error -> no results, not a block
        mainline = data.get("data", {}).get("result", {}).get("items", {}).get("mainline", []) or []
        out: list[Any] = []
        for row in mainline:
            if row.get("type") != "web":
                continue  # skip ads / images / videos / news rows
            for item in row.get("items", []) or []:
                href = item.get("url", "")
                title = item.get("title", "")
                if not href or not title:
                    continue
                r = TextResult()
                r.title = title
                r.href = href
                r.body = item.get("desc", "") or ""
                out.append(r)
        return out


# ─── registry ────────────────────────────────────────────────────────────────
# All enabled text backends. Bing is disabled (DDG + Yahoo already serve its
# index). Qwant is a real independent JSON-API backend (v8.1). Order = rough
# preference; the aggregator runs them all in parallel.
_TEXT_ENGINES: dict[str, type[BaseSearchEngine]] = {
    "duckduckgo": Duckduckgo,
    "brave": Brave,
    "google": Google,
    "startpage": Startpage,
    "grokipedia": Grokipedia,
    "wikipedia": Wikipedia,
    "yahoo": Yahoo,
    "mojeek": Mojeek,
    "yandex": Yandex,
    "qwant": Qwant,
}
# Map hound's public engine names -> metasearch backends.
_HOUND_TO_BACKEND = {
    "duckduckgo": "duckduckgo", "bing": "yahoo",  # bing -> yahoo (same index, diff server)
    "qwant": "qwant", "yahoo": "yahoo", "wikipedia": "wikipedia",
    "brave": "brave", "google": "google", "mojeek": "mojeek", "yandex": "yandex",
    "startpage": "startpage", "grokipedia": "grokipedia",
}

# Specialized JSON-API backends (lazy registration to avoid circular import:
# api_backends imports from search_metasearch, so we register on first use
# instead of at module load time when search_metasearch may not be fully loaded).

def _register_api_backends() -> None:
    """Register specialized JSON-API backends lazily. Called from metasearch()
    on first use and from tests. Safe to call multiple times (idempotent).
    Checks _TEXT_ENGINES directly (not a flag) so re-registration works even
    if a test saved/restored the dict."""
    if "semantic_scholar" in _TEXT_ENGINES:
        return
    try:
        from master_fetch.api_backends import (
            SemanticScholarEngine,
            GitHubSearchEngine,
            HackerNewsEngine,
        )
        _TEXT_ENGINES["semantic_scholar"] = SemanticScholarEngine
        _TEXT_ENGINES["github_api"] = GitHubSearchEngine
        _TEXT_ENGINES["hackernews"] = HackerNewsEngine
        _HOUND_TO_BACKEND["semantic_scholar"] = "semantic_scholar"
        _HOUND_TO_BACKEND["github_api"] = "github_api"
        _HOUND_TO_BACKEND["hackernews"] = "hackernews"
    except Exception as ex:
        logger.debug("api_backends not loaded: %r", ex)


def _register_byok_backends() -> None:
    """Register BYOK (Bring Your Own Key) search backends lazily.

    Called from metasearch() on first use. Registers API-backed engines
    (serper, tavily, exa, firecrawl, tinyfish) when user-provided keys are
    configured (via env vars or ~/.hound/search_keys.json). These engines
    become the PRIMARY search sources; hound's keyless local engines
    remain as fallback. Safe to call multiple times (idempotent via
    _TEXT_ENGINES check). Refreshes key pools so newly-added keys are
    picked up without restart.
    """
    try:
        from master_fetch.search_api_keys import get_byok_engines
        engines = get_byok_engines()
        for name, cls in engines.items():
            if name not in _TEXT_ENGINES:
                _TEXT_ENGINES[name] = cls
                _HOUND_TO_BACKEND[name] = name
    except Exception as ex:
        logger.debug("byok_backends not loaded: %r", ex)


_DEFAULT_BACKENDS = ["duckduckgo", "brave", "mojeek", "yahoo", "yandex", "startpage", "google", "qwant"]


# ─── circuit breaker (per-backend block cooldown) ───────────────────────────
# A backend that CAPTCHAs / 403s / rate-limits us is skipped for a cooldown so
# we don't keep firing requests at a host that is actively blocking our IP
# (which risks escalating to a longer IP-level ban, and wastes quorum slots
# waiting on a backend that will not contribute). Empty results and timeouts
# are transient and do NOT trip the breaker. Cleared on the next success.
_CIRCUIT_COOLDOWN = 60.0  # seconds
_BACKEND_HEALTH: dict[str, float] = {}  # name -> block-until timestamp


def _is_circuit_open(name: str) -> bool:
    return _BACKEND_HEALTH.get(name, 0.0) > time()


def _record_block(name: str) -> None:
    _BACKEND_HEALTH[name] = time() + _CIRCUIT_COOLDOWN


def _record_success(name: str) -> None:
    _BACKEND_HEALTH.pop(name, None)


def _reset_circuit_breaker() -> None:
    """Test hook: clear all circuit-breaker state."""
    _BACKEND_HEALTH.clear()


def _resolve_backends(engines: Optional[list[str]]) -> list[str]:
    """Map hound engine names (or 'auto'/None) to ddgs backend names, dropping dups/unknowns.

    When engines is None:
    - If BYOK keys are configured, returns ONLY the first BYOK provider
      (no local keyless engines, to avoid IP rate limiting).
    - Otherwise, returns the default local backend pool.
    """
    if not engines:
        # BYOK mode: if user has configured API keys, use ONLY the first
        # BYOK provider. No local keyless engines.
        try:
            from master_fetch.search_api_keys import get_byok_engines
            byok_names = list(get_byok_engines().keys())
            if byok_names:
                return [byok_names[0]]
        except Exception:
            pass
        # Normal mode: default local pool.
        return list(_DEFAULT_BACKENDS)
    out: list[str] = []
    for e in engines:
        b = _HOUND_TO_BACKEND.get(e)
        if b and b not in out:
            out.append(b)
    return out or list(_DEFAULT_BACKENDS)


_SEARCH_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "ref", "ref_src", "source", "_ga", "mc_cid", "mc_eid",
    "igshid", "si",  # YouTube/social share trackers
}

_GITHUB_REPO_HOSTS = {"github.com", "www.github.com"}

_GITHUB_RESERVED_ROUTES = frozenset({
    "about",
    "apps",
    "codespaces",
    "collections",
    "dashboard",
    "explore",
    "features",
    "issues",
    "login",
    "marketplace",
    "new",
    "notifications",
    "orgs",
    "pricing",
    "pulls",
    "search",
    "security",
    "settings",
    "sponsors",
    "topics",
    "trending",
})


def _normalize_url(url: str) -> str:
    """Normalize a URL for cross-backend dedup.

    Strips tracking/analytics query params (utm_*, fbclid, gclid, ref, ...) but
    KEEPS real query params, so two results that differ only in a tracking tag
    collapse to one, while genuinely distinct pages (e.g. ?page=2 vs ?page=3)
    stay distinct. Also lowercases scheme+host and strips non-root trailing slash.
    GitHub repository owner and name are case-insensitive, so their first two
    nonempty path segments are lowercased for github.com and www.github.com;
    later path segments remain unchanged because branches and file paths can be
    case-sensitive. GitHub system routes (topics, settings, explore, ...) are
    excluded from path folding because they are not repositories and case can
    carry meaning. Credential-bearing URLs skip GitHub-specific path folding so
    opaque userinfo is never part of a newly introduced canonical collision.
    www.github.com retains its distinct host rather than adding a separate
    host-alias canonicalization policy.
    """
    if not url:
        return ""
    u = url.strip()
    if u.startswith("//"):
        u = "https:" + u
    p = urlparse(u)
    scheme = (p.scheme or "https").lower()
    host = p.netloc.lower()
    path = p.path.rstrip("/") if len(p.path) > 1 else p.path
    if p.hostname in _GITHUB_REPO_HOSTS and p.username is None and p.password is None:
        segments = path.split("/")
        repo_segment_indexes = [i for i, segment in enumerate(segments) if segment][:2]
        if (
            len(repo_segment_indexes) == 2
            and segments[repo_segment_indexes[0]].lower() not in _GITHUB_RESERVED_ROUTES
        ):
            for i in repo_segment_indexes:
                segments[i] = segments[i].lower()
            path = "/".join(segments)
    if p.query:
        kept = [kv for kv in p.query.split("&")
                if kv and kv.split("=", 1)[0].lower() not in _SEARCH_TRACKING_PARAMS]
        query = "&".join(kept)
    else:
        query = ""
    return f"{scheme}://{host}{path}{('?' + query) if query else ''}"


# ─── async metasearch aggregator ─────────────────────────────────────────────
async def metasearch(
    query: str,
    max_results: int = 10,
    *,
    region: str = "us-en",
    safesearch: str = "moderate",
    timelimit: Optional[str] = None,
    page: int = 1,
    engines: Optional[list[str]] = None,
    query_map: dict[str, str] | None = None,
    site: Optional[str] = None,
    exclude_sites: Optional[list[str]] = None,
) -> tuple[list[dict[str, str]], dict[str, str]]:
    """Run the backends in PARALLEL and return (results, per-backend-status).

    results: list of {title, href, body, backend} deduped by normalized URL,
    preserving first-seen order (the backend that delivered it is its `backend`).
    status: {backend: "ok" | "empty" | "error:..."} for every backend tried.

    Early-return-on-quorum: once enough unique results have landed we cancel the
    laggards, so a healthy search returns in ~1-2s while a throttled one still
    finishes within the deadline from whichever backends got through.
    """
    _register_api_backends()  # lazy-load specialized JSON-API backends on first use
    _register_byok_backends()  # lazy-load BYOK (user-provided API key) backends
    backends = _resolve_backends(engines)
    status: dict[str, str] = {}
    # One engine instance per backend (cheap; primp/httpx clients are light).
    # Circuit breaker: skip backends that recently blocked us (CAPTCHA/403/rate-
    # limit) for a cooldown, so we don't keep firing at a host that is actively
    # blocking our IP. They show up in status as 'circuit_open' (-> engine_blocked).
    instances: dict[str, BaseSearchEngine] = {}
    for b in backends:
        cls = _TEXT_ENGINES.get(b)
        if not cls or cls.disabled:
            continue
        if _is_circuit_open(b):
            status[b] = "circuit_open"
            continue
        try:
            instances[b] = cls(proxy=_PROXY, timeout=int(_SEARCH_DEADLINE), verify=True)
        except Exception as ex:  # construction failure (e.g. primp missing) -> skip
            logger.debug("engine %s init failed: %r", b, ex)
            status[b] = f"init_error:{type(ex).__name__}"

    # If every engine failed to construct (bad proxy, missing deps, etc),
    # surface a clear error instead of silently returning 0 results.
    if not instances:
        proxy_note = f" (proxy in use: {_PROXY})" if _PROXY else ""
        raise MetaSearchException(
            f"No search engines could start{proxy_note}. "
            f"Engine status: {status}. "
            f"Check HOUND_SEARCH_PROXY and installed dependencies."
        )

    seen: dict[str, dict[str, Any]] = {}
    order: list[dict[str, str]] = []
    # Diversity quorum: wait for at least MIN_ENGINES backends to contribute
    # (not just enough results from one) so a single backend's bias/rate-limit
    # can't dominate - the cross-backend diversity is the robustness. A soft
    # fallback returns at SOFT_DEADLINE once we have enough results even if some
    # backends are dead/captcha'd (don't wait the full deadline for them).
    min_engines = min(3, len(instances))
    soft_deadline = 4.0
    # When fewer engines are running (BYOK mode with 1-3 engines), the +4
    # padding for neural rerank candidates is unnecessary. A single BYOK
    # provider returns clean results; we don't need extra from dead engines.
    quorum_results = max_results if len(instances) <= 3 else max_results + 4

    async def _run(name: str, eng: BaseSearchEngine) -> tuple[str, list[Any]]:
        # Per-engine query: if a query_map is provided (v12 multi-query
        # fan-out), each engine searches its assigned variant. Falls back to
        # the main query for engines not in the map (backward-compatible).
        q = (query_map or {}).get(name, query)
        res = await asyncio.to_thread(
            eng.search, q, region, safesearch, timelimit, page,
            site=site, exclude_sites=exclude_sites,
        )
        return name, (res or [])

    tasks = {asyncio.ensure_future(_run(n, e)): n for n, e in instances.items()}
    pending = set(tasks)
    deadline = time() + _SEARCH_DEADLINE
    start = time()
    engines_ok = 0

    while pending and time() < deadline:
        timeout = max(0.1, deadline - time())
        try:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED, timeout=timeout)
        except Exception:
            break
        if not done:
            break
        for t in done:
            name = tasks[t]
            try:
                _, res = t.result()
            except MetaBlockedException:
                # Backend refused us (CAPTCHA/403/rate-limit) -> circuit-open it
                # for the cooldown so we stop hammering it.
                _record_block(name)
                status[name] = "blocked"
                continue
            except BaseException as ex:  # CancelledError is BaseException in py3.11+
                status[name] = f"error:{type(ex).__name__}"
                continue
            added = 0
            touched = False  # returned a valid result that matched an existing key (dupe)
            for r in res:
                if not getattr(r, "href", None) or not getattr(r, "title", None):
                    continue
                key = _normalize_url(r.href)
                if not key:
                    continue
                if key in seen:
                    # another backend already returned this URL -> record the
                    # agreement (cross-backend consensus authority signal).
                    seen[key]["backends"].add(name)
                    # Snippet merging: combine snippets from multiple engines.
                    # Zero-latency enrichment: the agent gets more info per result
                    # without fetching the page. Different engines often extract
                    # different sentences from the same page.
                    new_body = (getattr(r, "body", "") or "").strip()
                    if new_body:
                        existing = seen[key].get("body", "")
                        if new_body not in existing and len(existing) < 600:
                            seen[key]["body"] = (existing + " " + new_body).strip()[:600]
                    touched = True
                    continue
                entry = {"title": r.title, "href": r.href, "body": getattr(r, "body", "") or "",
                         "backend": name, "backends": {name}}
                seen[key] = entry
                order.append(entry)
                added += 1
            if added:
                engines_ok += 1
            # 'ok' = contributed a valid result (new OR a dupe that confirms
            # consensus). 'empty' = returned nothing usable. (A backend whose
            # only result was a dupe still contributed - it confirmed the URL.)
            if added or touched:
                status[name] = "ok"
                _record_success(name)  # backend is healthy -> clear any prior block
            else:
                status[name] = "empty"
        # early-return: enough engines contributed enough results, OR enough
        # results after the soft deadline (don't hold for dead backends).
        # Secondary check: if we have >= quorum_results and soft_deadline passed,
        # return even without full quorum (don't wait the full deadline for dead
        # backends). Note: previously returned at max_results after soft_deadline,
        # which returned too early with results from just 1-2 fast engines,
        # cancelling slower engines that would have returned better results.
        elapsed = time() - start
        if len(order) >= quorum_results and (
            engines_ok >= min_engines or elapsed >= soft_deadline
        ):
            for pt in pending:
                pt.cancel()
            for pt in list(pending):
                nm = tasks[pt]
                if nm not in status:
                    status[nm] = "preempted"  # cancelled because enough backends delivered
                try:
                    await pt
                except BaseException:
                    pass
            pending = set()
            break

    # cancel + record any still-pending (timed out) backends
    for pt in pending:
        pt.cancel()
    for pt in list(pending):
        name = tasks[pt]
        if name not in status:
            status[name] = "timeout"
        try:
            await pt
        except BaseException:
            pass

    # freeze backends sets to sorted lists for the caller
    for e in order:
        e["backends"] = sorted(e["backends"])

    # Sort: BYOK engines first, then general engines, then API backends.
    # BYOK backends (serper, tavily, exa, firecrawl, tinyfish) are user-provided
    # API keys and take priority as the primary search source.
    # General HTML engines are the fallback when BYOK is not configured.
    # API backends (GitHub, Semantic Scholar, HN) are specialized indexes that
    # may not match query intent as well as general engines.
    _BYOK_BACKEND_NAMES = {"serper", "tavily", "exa", "firecrawl", "tinyfish"}
    _API_BACKEND_NAMES = {"semantic_scholar", "github_api", "hackernews"}
    def _sort_key(e: dict[str, str]) -> int:
        b = e.get("backend", "")
        if b in _BYOK_BACKEND_NAMES:
            return 0  # BYOK first
        if b in _API_BACKEND_NAMES:
            return 2  # specialized API backends last
        return 1  # general HTML engines
    order.sort(key=_sort_key)

    return order, status
