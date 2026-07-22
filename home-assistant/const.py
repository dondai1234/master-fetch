"""Constants for Hound MCP integration."""

from homeassistant.const import CONF_URL, CONF_PORT

DOMAIN = "hound_mcp"
DEFAULT_URL = "http://localhost"
DEFAULT_PORT = 8765

CONF_NAME = "name"
CONF_TIMEOUT = "timeout"
DEFAULT_TIMEOUT = 30

# Available tools
TOOLS = [
    "smart_fetch",
    "fetch",
    "search",
    "crawl",
    "extract_article",
    "screenshot",
    "open_session",
    "close_session",
]