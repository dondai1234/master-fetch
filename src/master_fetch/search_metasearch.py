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
# Per-engine + overall deadline. Engines run in parallel + we early-return on
# quorum, so a healthy search is ~1-2s; this bounds a fully-throttled one.
_SEARCH_DEADLINE = float(os.environ.get("HOUND_SEARCH_DEADLINE", "8") or "8")
_ua = UserAgent()


# ─── exceptions ──────────────────────────────────────────────────────────────
class MetaSearchException(Exception):
    """Base metasearch error."""


class MetaTimeoutException(MetaSearchException):
    """A backend or the whole search timed out."""


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

    def __init__(self, proxy: str | None = None, timeout: int | None = 10, *, verify: bool = True) -> None:
        self.client = primp.Client(
            proxy=proxy,
            timeout=timeout,
            impersonate="random",
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


# ─── registry ────────────────────────────────────────────────────────────────
# All enabled text backends. Bing is disabled (DDG + Yahoo already serve its
# index). Order = rough preference; the aggregator runs them all in parallel.
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
}
# Map hound's public engine names -> ddgs backends (qwant has no ddgs backend).
_HOUND_TO_BACKEND = {
    "duckduckgo": "duckduckgo", "bing": "yahoo",  # bing -> yahoo (same index, diff server)
    "qwant": "duckduckgo", "yahoo": "yahoo", "wikipedia": "wikipedia",
    "brave": "brave", "google": "google", "mojeek": "mojeek", "yandex": "yandex",
    "startpage": "startpage", "grokipedia": "grokipedia",
}
_DEFAULT_BACKENDS = ["duckduckgo", "brave", "mojeek", "yahoo", "yandex", "startpage", "google"]


def _resolve_backends(engines: Optional[list[str]]) -> list[str]:
    """Map hound engine names (or 'auto'/None) to ddgs backend names, dropping dups/unknowns."""
    if not engines:
        return list(_DEFAULT_BACKENDS)
    out: list[str] = []
    for e in engines:
        b = _HOUND_TO_BACKEND.get(e)
        if b and b not in out:
            out.append(b)
    return out or list(_DEFAULT_BACKENDS)


def _normalize_url(url: str) -> str:
    """Light URL normalization for dedup (strip trailing slash on non-root, scheme lowercase)."""
    if not url:
        return ""
    u = url.strip()
    if u.startswith("//"):
        u = "https:" + u
    p = urlparse(u)
    scheme = (p.scheme or "https").lower()
    host = p.netloc.lower()
    path = p.path.rstrip("/") if len(p.path) > 1 else p.path
    return f"{scheme}://{host}{path}"


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
) -> tuple[list[dict[str, str]], dict[str, str]]:
    """Run the backends in PARALLEL and return (results, per-backend-status).

    results: list of {title, href, body, backend} deduped by normalized URL,
    preserving first-seen order (the backend that delivered it is its `backend`).
    status: {backend: "ok" | "empty" | "error:..."} for every backend tried.

    Early-return-on-quorum: once enough unique results have landed we cancel the
    laggards, so a healthy search returns in ~1-2s while a throttled one still
    finishes within the deadline from whichever backends got through.
    """
    backends = _resolve_backends(engines)
    # One engine instance per backend (cheap; primp/httpx clients are light).
    instances: dict[str, BaseSearchEngine] = {}
    for b in backends:
        cls = _TEXT_ENGINES.get(b)
        if not cls or cls.disabled:
            continue
        try:
            instances[b] = cls(proxy=_PROXY, timeout=int(_SEARCH_DEADLINE), verify=True)
        except Exception as ex:  # construction failure (e.g. primp missing) -> skip
            logger.debug("engine %s init failed: %r", b, ex)

    status: dict[str, str] = {}
    seen: dict[str, dict[str, Any]] = {}
    order: list[dict[str, str]] = []
    # Diversity quorum: wait for at least MIN_ENGINES backends to contribute
    # (not just enough results from one) so a single backend's bias/rate-limit
    # can't dominate - the cross-backend diversity is the robustness. A soft
    # fallback returns at SOFT_DEADLINE once we have enough results even if some
    # backends are dead/captcha'd (don't wait the full deadline for them).
    min_engines = min(3, len(instances))
    soft_deadline = 2.0
    quorum_results = max_results + 4  # a little extra for the neural reranker

    async def _run(name: str, eng: BaseSearchEngine) -> tuple[str, list[Any]]:
        # engine.search is sync (blocking HTTP) -> offload to a thread.
        res = await asyncio.to_thread(
            eng.search, query, region, safesearch, timelimit, page,
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
            except BaseException as ex:  # CancelledError is BaseException in py3.11+
                status[name] = f"error:{type(ex).__name__}"
                continue
            added = 0
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
                    continue
                entry = {"title": r.title, "href": r.href, "body": getattr(r, "body", "") or "",
                         "backend": name, "backends": {name}}
                seen[key] = entry
                order.append(entry)
                added += 1
            if added:
                engines_ok += 1
            status[name] = "ok" if added else ("empty" if res else "empty")
        # early-return: enough engines contributed enough results, OR enough
        # results after the soft deadline (don't hold for dead backends).
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
    return order, status
