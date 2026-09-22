from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import IntellegyHubApiClient
from .const import BUTTONS, CARRIER_OUTPUTS, CONF_URL, DOMAIN, OUTPUTS
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
    _apply_host_entity_dashboard_names(hass, entry.entry_id)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, ENTITY_PLATFORMS)
    manager = hass.data[DOMAIN].pop(entry.entry_id)
    await manager.async_stop()
    return unload_ok


HOST_ENTITY_DASHBOARD_NAMES: dict[str, str] = {
    **{item["unique_id"]: item["name"] for item in OUTPUTS.values()},
    **{item["unique_id"]: item["name"] for item in CARRIER_OUTPUTS.values()},
    **{item["unique_id"]: item["name"] for item in BUTTONS.values()},
    "intellegyhub_extension_bus_power": "Expansion Bus Power",
    "intellegyhub_onewire_bus_power": "1-Wire Bus Power",
    "intellegyhub_onewire_power_fault": "1-Wire Power Fault",
    "intellegyhub_xbus_power_fault": "X-Bus Power Fault",
    "intellegyhub_carrier_board_temperature": "Board Temperature",
    "intellegyhub_carrier_rail_vin": "Input Voltage",
    "intellegyhub_carrier_rail_5v": "+5 V Rail",
    "intellegyhub_carrier_rail_3v3": "+3.3 V Rail",
    "intellegyhub_buzzer_frequency": "Buzzer Frequency",
    "intellegyhub_buzzer_duration": "Buzzer Duration",
    "intellegyhub_buzzer_volume": "Buzzer Volume",
    "intellegyhub_buzzer_play": "Buzzer Play",
}


def _apply_host_entity_dashboard_names(hass: HomeAssistant, entry_id: str) -> None:
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
        unique_id = getattr(registry_entry, "unique_id", None)
        desired_name = HOST_ENTITY_DASHBOARD_NAMES.get(unique_id)
        if entity_id is None or desired_name is None:
            continue
        if getattr(registry_entry, "name", None) != desired_name:
            update_entity(entity_id, name=desired_name)
