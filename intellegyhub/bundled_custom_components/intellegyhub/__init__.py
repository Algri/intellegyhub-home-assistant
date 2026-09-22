from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers import entity_registry as er

from .api import IntellegyHubApiClient
from .const import CONF_URL, DOMAIN
from .coordinator import IntellegyHubGpioManager

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
    _repair_dashboard_entity_names(hass, entry.entry_id)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, ENTITY_PLATFORMS)
    manager = hass.data[DOMAIN].pop(entry.entry_id)
    await manager.async_stop()
    return unload_ok


def _repair_dashboard_entity_names(hass: HomeAssistant, entry_id: str) -> None:
    """Clear stale Hardware Host name overrides left by older integration builds."""
    entity_registry = er.async_get(hass)
    update_entity = getattr(entity_registry, "async_update_entity", None)
    if update_entity is None:
        return

    entries = getattr(entity_registry, "entities", {})
    for registry_entry in list(getattr(entries, "values", lambda: [])()):
        if getattr(registry_entry, "platform", None) != DOMAIN:
            continue
        if getattr(registry_entry, "config_entry_id", entry_id) != entry_id:
            continue
        entity_id = getattr(registry_entry, "entity_id", None)
        if entity_id is None:
            continue
        saved_name = getattr(registry_entry, "name", None) or getattr(registry_entry, "name_by_user", None)
        if isinstance(saved_name, str) and saved_name.startswith("Hardware Host "):
            update_entity(entity_id, name=None)
