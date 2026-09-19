from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, UNIQUE_ID_BUTTON, XPORT_MODE_DI
from .entity import IntellegyHubGpioEntity, IntellegyHubOneWireBridgeEntity, IntellegyHubXDi16Entity, IntellegyHubXPortEntity
from .xport_entities import setup_xport_dynamic_platform

BRIDGE_DEVICE_CLASS_CONNECTIVITY = getattr(BinarySensorDeviceClass, "CONNECTIVITY", "connectivity")
XDI16_INPUT_ICON = "mdi:toggle-switch-outline"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    manager = hass.data[DOMAIN][entry.entry_id]
    known_onewire_bridges: set[str] = set()
    known_xdi16: set[str] = set()
    async_add_entities([IntellegyHubButtonSensor(manager)])
    setup_xport_dynamic_platform(
        entry,
        manager,
        async_add_entities,
        Platform.BINARY_SENSOR,
        "di",
        lambda channel: IntellegyHubXPortDiSensor(manager, channel),
    )

    def onewire_bridge_ids() -> set[str]:
        return {
            bridge_id
            for bridge in manager.onewire.get("bridges", [])
            if isinstance(bridge_id := bridge.get("id"), str)
        }

    def check_onewire_bridge_entities() -> None:
        current_bridge_ids = onewire_bridge_ids()
        known_onewire_bridges.intersection_update(current_bridge_ids)
        new_bridge_ids = current_bridge_ids - known_onewire_bridges
        new_entities = [
            IntellegyHubOneWireBridgeConnectivitySensor(manager, bridge_id)
            for bridge_id in sorted(new_bridge_ids)
        ]
        if new_entities:
            async_add_entities(new_entities)
            known_onewire_bridges.update(new_bridge_ids)

    def xdi16_module_ids() -> set[str]:
        return {
            module_id
            for module in manager.extensions.get("modules", [])
            if module.get("kind") == "digital_input" and isinstance(module_id := module.get("id"), str)
        }

    def check_xdi16_entities() -> None:
        current_module_ids = xdi16_module_ids()
        known_xdi16.intersection_update(current_module_ids)
        new_module_ids = current_module_ids - known_xdi16
        new_entities = [
            IntellegyHubXDi16InputSensor(manager, module_id, channel)
            for module_id in sorted(new_module_ids)
            for channel in range(1, 17)
        ]
        if new_entities:
            async_add_entities(new_entities)
            known_xdi16.update(new_module_ids)

    entry.async_on_unload(manager.async_add_listener(check_onewire_bridge_entities))
    entry.async_on_unload(manager.async_add_listener(check_xdi16_entities))
    _reset_xdi16_registry_entries(hass, manager)
    check_onewire_bridge_entities()
    check_xdi16_entities()


def _reset_xdi16_registry_entries(hass: HomeAssistant, manager) -> None:
    entity_registry = er.async_get(hass)
    remove_entity = getattr(entity_registry, "async_remove", None)
    if remove_entity is None:
        return
    module_ids = {
        module_id
        for module in manager.extensions.get("modules", [])
        if module.get("kind") == "digital_input" and isinstance(module_id := module.get("id"), str)
    }
    for module_id in sorted(module_ids):
        for channel in range(1, 17):
            unique_id = f"intellegyhub_{module_id}_input_{channel}"
            entity_id = entity_registry.async_get_entity_id("binary_sensor", DOMAIN, unique_id)
            if entity_id is None:
                continue
            try:
                remove_entity(entity_id)
            except Exception:
                continue


class IntellegyHubButtonSensor(IntellegyHubGpioEntity, BinarySensorEntity):
    _attr_unique_id = UNIQUE_ID_BUTTON
    _attr_translation_key = "button"
    _attr_device_class = BinarySensorDeviceClass.OCCUPANCY

    @property
    def is_on(self) -> bool:
        return self.manager.button_pressed


class IntellegyHubXPortDiSensor(IntellegyHubXPortEntity, BinarySensorEntity):
    _attr_translation_key = "xport_di"
    _attr_device_class = BinarySensorDeviceClass.OCCUPANCY

    def __init__(self, manager, channel: int) -> None:
        super().__init__(manager, channel)
        self._attr_unique_id = f"intellegyhub_xport_x{channel}_di"
        self._attr_name = "DI"

    @property
    def available(self) -> bool:
        return self.available_for_modes(XPORT_MODE_DI)

    @property
    def is_on(self) -> bool:
        return bool(self.channel_state.get("value", 0))


class IntellegyHubXDi16InputSensor(IntellegyHubXDi16Entity, BinarySensorEntity):
    _attr_translation_key = "xdi16_input"
    _attr_icon = XDI16_INPUT_ICON

    def __init__(self, manager, module_id: str, channel: int) -> None:
        super().__init__(manager, module_id, channel)
        self._attr_unique_id = f"intellegyhub_{module_id}_input_{channel}"
        self._attr_name = f"Input {channel}"

    @property
    def device_class(self) -> str | None:
        return None

    @property
    def icon(self) -> str:
        return XDI16_INPUT_ICON

    @property
    def is_on(self) -> bool:
        inputs = self.module_state.get("inputs", [])
        if len(inputs) < self.channel:
            return False
        return bool(inputs[self.channel - 1])


class IntellegyHubOneWireBridgeConnectivitySensor(IntellegyHubOneWireBridgeEntity, BinarySensorEntity):
    _attr_translation_key = "onewire_bridge"
    _attr_device_class = BRIDGE_DEVICE_CLASS_CONNECTIVITY

    def __init__(self, manager, bridge_id: str) -> None:
        super().__init__(manager, bridge_id)
        self._attr_unique_id = f"intellegyhub_{bridge_id}_connected"
        self._attr_name = "Bridge"

    @property
    def available(self) -> bool:
        return self.manager.connected and bool(self.manager.onewire.get("power", {}).get("on", False))

    @property
    def is_on(self) -> bool:
        bridge = self.bridge_state
        return bool(bridge.get("available", False)) and bool(bridge.get("enabled", True))

    @property
    def extra_state_attributes(self) -> dict:
        bridge = self.bridge_state
        return {
            "bus": bridge.get("bus"),
            "address": bridge.get("address"),
            "chip": bridge.get("chip"),
            "polling_enabled": bridge.get("enabled"),
            "error": bridge.get("error"),
            "sensors": bridge.get("sensors", []),
        }
