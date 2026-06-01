"""Robots.txt compliance for Master Fetch.

Respects robots.txt Disallow rules with caching per domain.
If robots.txt is unreachable (timeout, blocked), allows by default.
"""

import logging
from urllib.robotparser import RobotFileParser
from urllib.request import Request, urlopen
from urllib.error import URLError
from urllib.parse import urlparse
from time import time

logger = logging.getLogger("master-fetch.robots")

# Cache robots.txt parsers per domain: {domain: (RobotFileParser, fetch_time)}
_robots_cache: dict[str, tuple[RobotFileParser, float]] = {}
_ROBOTS_CACHE_TTL = 3600  # 1 hour
_FETCH_TIMEOUT = 10  # seconds

DEFAULT_USER_AGENT = "MasterFetch/1.0 (web fetcher for AI agents; https://github.com/dondai1234/master-fetch)"


def _domain_from_url(url: str) -> str:
    """Extract domain from URL. Returns '' for invalid URLs."""
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""


def _get_robots_parser(domain: str, user_agent: str = "*") -> RobotFileParser | None:
    """Fetch and parse robots.txt for a domain. Caches result.

    Returns None if robots.txt is unreachable (allow by default).
    Returns RobotFileParser if successfully fetched.
    """
    now_ts = time()

    # Check cache
    if domain in _robots_cache:
        parser, fetched_at = _robots_cache[domain]
        if now_ts - fetched_at < _ROBOTS_CACHE_TTL:
            return parser
        # Expired — remove and re-fetch
        del _robots_cache[domain]

    robots_url = f"https://{domain}/robots.txt"
    try:
        req = Request(robots_url, headers={"User-Agent": DEFAULT_USER_AGENT})
        with urlopen(req, timeout=_FETCH_TIMEOUT) as response:
            raw = response.read().decode("utf-8", errors="replace")

        parser = RobotFileParser()
        parser.parse(raw.splitlines())
        _robots_cache[domain] = (parser, now_ts)
        logger.debug(f"Fetched robots.txt for {domain}")
        return parser

    except URLError as e:
        logger.debug(f"robots.txt unreachable for {domain}: {e}")
        return None
    except Exception as e:
        logger.debug(f"Failed to parse robots.txt for {domain}: {e}")
        return None


def is_allowed(url: str, user_agent: str = "*") -> bool:
    """Check if a URL is allowed per robots.txt.

    Returns True if:
    - robots.txt is unreachable (allow by default)
    - robots.txt allows this URL
    - URL is invalid (malformed)

    Returns False only if robots.txt explicitly disallows this URL.
    """
    domain = _domain_from_url(url)
    if not domain:
        return True  # Malformed URL — allow

    parser = _get_robots_parser(domain, user_agent)
    if parser is None:
        return True  # Can't reach robots.txt — allow

    try:
        return parser.can_fetch(user_agent, url)
    except Exception:
        return True  # Parse error — allow


def clear_robots_cache() -> None:
    """Clear the robots.txt cache."""
    _robots_cache.clear()
    logger.info("Robots.txt cache cleared")
