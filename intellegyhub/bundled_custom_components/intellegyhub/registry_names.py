from __future__ import annotations

import re

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import BUTTONS, CARRIER_OUTPUTS, DOMAIN, OUTPUTS

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


def apply_compact_entity_dashboard_names(hass: HomeAssistant, entry_id: str) -> None:
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
        desired_name = compact_dashboard_name(unique_id)
        if entity_id is None or desired_name is None:
            continue
        if getattr(registry_entry, "name", None) != desired_name:
            update_entity(entity_id, name=desired_name)


def compact_dashboard_name(unique_id: object) -> str | None:
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
        return f"Relay {relay_match.group(2)}"

    input_match = re.fullmatch(r"intellegyhub_(xdi16_.+)_input_(\d+)", unique_id)
    if input_match:
        return f"Input {input_match.group(2)}"

    bridge_match = re.fullmatch(r"intellegyhub_(onewire_.+)_connected", unique_id)
    if bridge_match:
        return "Bridge"

    sensor_match = re.fullmatch(r"intellegyhub_(ds18b20_.+)_temperature", unique_id)
    if sensor_match:
        return "Temperature"

    return None

