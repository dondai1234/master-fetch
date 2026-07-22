"""Hound MCP integration for Home Assistant.

Provides web research capabilities to Home Assistant through the hound-mcp server.
Connects to a running hound instance (Docker or local) via HTTP transport.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity import DeviceInfo

from .const import DOMAIN, DEFAULT_URL, DEFAULT_PORT

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Hound MCP from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    # Store the config data
    hass.data[DOMAIN][entry.entry_id] = entry.data

    # Setup platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Reload entry on option updates
    entry.async_on_unload(entry.add_update_listener(async_update_options))

    return True


async def async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Update options."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok


def get_device_info(hass: HomeAssistant, entry: ConfigEntry) -> DeviceInfo:
    """Get device info for Hound MCP."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.data.get(CONF_NAME, "Hound MCP"),
        manufacturer="Bishesh Bhandari",
        model="hound-mcp",
        entry_type=DeviceEntryType.SERVICE,
    )