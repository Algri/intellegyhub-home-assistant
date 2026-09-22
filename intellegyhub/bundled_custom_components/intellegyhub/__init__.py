from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import IntellegyHubApiClient
from .const import CONF_URL, DOMAIN
from .coordinator import IntellegyHubGpioManager
from .registry_names import apply_compact_entity_dashboard_names

LOGGER = logging.getLogger(__name__)
ENTITY_PLATFORMS: tuple[Platform, ...] = (
    Platform.SWITCH,
    Platform.BINARY_SENSOR,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.NUMBER,
    Platform.BUTTON,
)
async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    LOGGER.warning("IntellegyHUB loading platforms: %s", [platform.value for platform in ENTITY_PLATFORMS])
    client = IntellegyHubApiClient(async_get_clientsession(hass), entry.data[CONF_URL])
    manager = IntellegyHubGpioManager(hass, client, entry.entry_id)
    await manager.async_start()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = manager
    await hass.config_entries.async_forward_entry_setups(entry, ENTITY_PLATFORMS)
    apply_compact_entity_dashboard_names(hass, entry.entry_id)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, ENTITY_PLATFORMS)
    manager = hass.data[DOMAIN].pop(entry.entry_id)
    await manager.async_stop()
    return unload_ok
