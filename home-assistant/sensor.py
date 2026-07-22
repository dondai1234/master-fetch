"""Sensor platform for Hound MCP integration."""

from __future__ import annotations

import logging
from typing import Any, cast

import aiohttp
from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, CONF_TIMEOUT, DEFAULT_TIMEOUT

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the sensor platform."""
    url = f"{entry.data['url']}:{entry.data['port']}/mcp"
    timeout = entry.data.get(CONF_TIMEOUT, DEFAULT_TIMEOUT)

    async_add_entities([
        HoundStatusSensor(entry, url, timeout),
        HoundVersionSensor(entry, url, timeout),
    ])


class HoundMcpSensor(SensorEntity):
    """Base class for Hound MCP sensors."""

    def __init__(self, entry: ConfigEntry, url: str, timeout: int) -> None:
        """Initialize the sensor."""
        self._entry = entry
        self._url = url
        self._timeout = timeout
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": entry.data["name"],
            "manufacturer": "Bishesh Bhandari",
            "model": "hound-mcp",
        }

    async def _fetch_mcp(self, method: str = "tools/list") -> dict[str, Any]:
        """Fetch data from MCP server."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self._url,
                    json={"jsonrpc": "2.0", "id": 1, "method": method},
                    timeout=aiohttp.ClientTimeout(total=self._timeout),
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        return data.get("result", {})
        except aiohttp.ClientError as err:
            _LOGGER.warning("Failed to fetch from %s: %s", self._url, err)
        return {}


class HoundStatusSensor(HoundMcpSensor):
    """Sensor for Hound MCP connection status."""

    _attr_name = "Hound MCP Status"
    _attr_unique_id = f"{DOMAIN}_status"

    async def async_update(self) -> None:
        """Update the sensor."""
        result = await self._fetch_mcp("tools/list")
        self._attr_native_value = "connected" if result else "disconnected"


class HoundVersionSensor(HoundMcpSensor):
    """Sensor for Hound MCP version."""

    _attr_name = "Hound MCP Version"
    _attr_unique_id = f"{DOMAIN}_version"

    async def async_update(self) -> None:
        """Update the sensor."""
        result = await self._fetch_mcp("initialize")
        server_info = result.get("serverInfo", {})
        self._attr_native_value = server_info.get("version", "unknown")