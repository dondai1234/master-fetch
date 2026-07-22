"""Config flow for Hound MCP integration."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
import voluptuous as vol

from .const import (
    DOMAIN,
    DEFAULT_URL,
    DEFAULT_PORT,
    CONF_NAME,
    CONF_TIMEOUT,
    DEFAULT_TIMEOUT,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME, default="Hound MCP"): str,
        vol.Required("url", default=DEFAULT_URL): str,
        vol.Required("port", default=DEFAULT_PORT): int,
        vol.Optional(CONF_TIMEOUT, default=DEFAULT_TIMEOUT): int,
    }
)


async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Validate the user input allows us to connect."""
    url = f"{data['url']}:{data['port']}/mcp"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=data.get(CONF_TIMEOUT, DEFAULT_TIMEOUT))) as response:
                if response.status == 200:
                    return {"title": data[CONF_NAME]}
                else:
                    raise ValueError(f"Server returned status {response.status}")
    except aiohttp.ClientError as err:
        raise ValueError(f"Cannot connect to Hound MCP at {url}: {err}")


class HoundMcpConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Hound MCP."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                info = await validate_input(self.hass, user_input)
                return self.async_create_entry(title=info["title"], data=user_input)
            except ValueError as err:
                errors["base"] = "cannot_connect"
                _LOGGER.error("Connection error: %s", err)

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )