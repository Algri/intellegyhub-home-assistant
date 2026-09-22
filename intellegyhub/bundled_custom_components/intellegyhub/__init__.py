from __future__ import annotations

import logging
import re

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
    _apply_compact_entity_dashboard_names(hass, entry.entry_id)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, ENTITY_PLATFORMS)
    manager = hass.data[DOMAIN].pop(entry.entry_id)
    await manager.async_stop()
    return unload_ok


STATIC_ENTITY_DASHBOARD_NAMES: dict[str, str] = {
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


XPORT_ENTITY_SUFFIX_NAMES = {
    "mode": "Mode",
    "do": "DO",
    "di": "DI",
    "ai": "AI",
    "counter": "Counter",
    "pwm": "PWM",
    "counter_reset": "Reset Counter",
}


def _apply_compact_entity_dashboard_names(hass: HomeAssistant, entry_id: str) -> None:
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
        desired_name = _compact_dashboard_name(unique_id)
        if entity_id is None or desired_name is None:
            continue
        if getattr(registry_entry, "name", None) != desired_name:
            update_entity(entity_id, name=desired_name)


def _compact_dashboard_name(unique_id: object) -> str | None:
    if not isinstance(unique_id, str):
        return None
    if unique_id in STATIC_ENTITY_DASHBOARD_NAMES:
        return STATIC_ENTITY_DASHBOARD_NAMES[unique_id]

    xport_match = re.fullmatch(r"intellegyhub_xport_x([1-4])_(.+)", unique_id)
    if xport_match:
        channel, suffix = xport_match.groups()
        suffix_name = XPORT_ENTITY_SUFFIX_NAMES.get(suffix)
        return f"X{channel} {suffix_name}" if suffix_name is not None else None

    relay_match = re.fullmatch(r"intellegyhub_(xdo8_.+)_relay_(\d+)", unique_id)
    if relay_match:
        module_id, channel = relay_match.groups()
        return f"{_module_label(module_id)} Relay {channel}"

    input_match = re.fullmatch(r"intellegyhub_(xdi16_.+)_input_(\d+)", unique_id)
    if input_match:
        module_id, channel = input_match.groups()
        return f"{_module_label(module_id)} Input {channel}"

    bridge_match = re.fullmatch(r"intellegyhub_(onewire_.+)_connected", unique_id)
    if bridge_match:
        return f"{_onewire_bridge_label(bridge_match.group(1))} Bridge"

    sensor_match = re.fullmatch(r"intellegyhub_(ds18b20_.+)_temperature", unique_id)
    if sensor_match:
        return f"{_ds18b20_label(sensor_match.group(1))} Temperature"

    return None


def _module_label(module_id: str) -> str:
    address_match = re.search(r"_addr([0-9a-fA-F]{2})", module_id)
    address = f" 0x{address_match.group(1).lower()}" if address_match else ""
    if module_id.startswith("xdo8_"):
        return f"xDO-8{address}"
    if module_id.startswith("xdi16_"):
        return f"xDI-16{address}"
    return module_id


def _onewire_bridge_label(bridge_id: str) -> str:
    address_match = re.search(r"_addr([0-9a-fA-F]{2})", bridge_id)
    if not address_match:
        return "1-Wire"
    address = address_match.group(1).lower()
    if address == "1a":
        return "1-Wire Bridge 1"
    if address == "1b":
        return "1-Wire Bridge 2"
    return f"1-Wire 0x{address}"


def _ds18b20_label(sensor_id: str) -> str:
    rom_match = re.search(r"_([0-9a-fA-F]{16})$", sensor_id)
    if rom_match:
        return f"DS18B20 {rom_match.group(1).lower()}"
    return "DS18B20"
