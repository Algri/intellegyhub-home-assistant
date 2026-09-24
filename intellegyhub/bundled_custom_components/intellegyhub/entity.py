from __future__ import annotations

import re

from homeassistant.helpers.entity import DeviceInfo, Entity
from homeassistant.helpers import device_registry as dr
from homeassistant.exceptions import HomeAssistantError

from .const import DEVICE_IDENTIFIER, DOMAIN
from .coordinator import IntellegyHubGpioManager


class IntellegyHubGpioEntity(Entity):
    _attr_has_entity_name = False

    def __init__(self, manager: IntellegyHubGpioManager) -> None:
        self.manager = manager
        self._remove_listener = None

    @property
    def available(self) -> bool:
        return self.manager.connected

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, DEVICE_IDENTIFIER)},
            name="Controls",
            manufacturer="IntellegyHub",
            model="IntellegyHUB Controller",
        )

    async def async_added_to_hass(self) -> None:
        self._remove_listener = self.manager.async_add_listener(self.async_write_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        if self._remove_listener:
            self._remove_listener()


class IntellegyHubXPortEntity(IntellegyHubGpioEntity):
    def __init__(self, manager: IntellegyHubGpioManager, channel: int) -> None:
        super().__init__(manager)
        self.channel = channel

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, f"xport_{self.channel}")},
            name=f"X-Port X{self.channel}",
            manufacturer="IntellegyHub",
            model="X-Port",
        )

    @property
    def channel_state(self) -> dict:
        return self.manager.xport_channel(self.channel)

    @property
    def xport_available(self) -> bool:
        return self.manager.connected and self.manager.xport.get("availability") == "Available"

    def available_for_modes(self, modes: set[str]) -> bool:
        return self.xport_available and self.channel_state.get("confirmed_mode") in modes

    def assert_mode_available(self, modes: set[str]) -> None:
        if not self.available_for_modes(modes):
            confirmed_mode = self.channel_state.get("confirmed_mode", "unknown")
            raise HomeAssistantError(
                f"X-Port X{self.channel} is in {confirmed_mode} mode; this entity is not active"
            )


class IntellegyHubExtensionPowerEntity(IntellegyHubGpioEntity):
    @property
    def available(self) -> bool:
        return self.manager.connected and bool(self.manager.extensions.get("power", {}).get("available", False))


class IntellegyHubOneWirePowerEntity(IntellegyHubGpioEntity):
    @property
    def available(self) -> bool:
        return self.manager.connected and bool(self.manager.onewire.get("power", {}).get("available", False))


def _parent_device_id(entity: Entity, manager: IntellegyHubGpioManager) -> str | None:
    hass = getattr(entity, "hass", None)
    if hass is None:
        return None
    try:
        return dr.async_get_device_id_by_identifier(
            hass,
            (DOMAIN, DEVICE_IDENTIFIER),
            config_entry_id=manager.entry_id,
        )
    except ValueError:
        return None


def _onewire_bridge_title(bridge: dict) -> str:
    name = bridge.get("name") or "1-Wire Bridge"
    match = re.fullmatch(r"1-Wire Bridge\s+(\d+)", str(name))
    if match:
        return f"1-Wire Bus{match.group(1)}"
    return str(name)


def _xbus_slot(address: object) -> str:
    try:
        return str(int(str(address), 16) - 0x20)
    except (TypeError, ValueError):
        return "?"


def _xbus_module_title(address: object, model: str) -> str:
    return f"X-Bus{_xbus_slot(address)} {model}"


def _xbus_module_model(address: object, model: str) -> str:
    return f"{model} {address or 'unknown'}"


class IntellegyHubXDo8Entity(IntellegyHubGpioEntity):
    _attr_has_entity_name = True

    def __init__(self, manager: IntellegyHubGpioManager, module_id: str, channel: int) -> None:
        super().__init__(manager)
        self.module_id = module_id
        self.channel = channel

    @property
    def module_state(self) -> dict:
        return self.manager.extension_module(self.module_id)

    @property
    def available(self) -> bool:
        return (
            self.manager.connected
            and bool(self.manager.extensions.get("power", {}).get("on", False))
            and bool(self.module_state.get("available", False))
        )

    @property
    def device_info(self) -> DeviceInfo:
        module = self.module_state
        address = module.get("address", "unknown")
        return DeviceInfo(
            identifiers={(DOMAIN, self.module_id)},
            name=_xbus_module_title(address, "xDO-8"),
            manufacturer="IntellegyHub",
            model=_xbus_module_model(address, "xDO-8"),
            via_device_id=_parent_device_id(self, self.manager),
        )


class IntellegyHubXDi16Entity(IntellegyHubGpioEntity):
    _attr_has_entity_name = True

    def __init__(self, manager: IntellegyHubGpioManager, module_id: str, channel: int) -> None:
        super().__init__(manager)
        self.module_id = module_id
        self.channel = channel

    @property
    def module_state(self) -> dict:
        return self.manager.extension_module(self.module_id)

    @property
    def available(self) -> bool:
        return (
            self.manager.connected
            and bool(self.manager.extensions.get("power", {}).get("on", False))
            and bool(self.module_state.get("available", False))
        )

    @property
    def device_info(self) -> DeviceInfo:
        module = self.module_state
        address = module.get("address", "unknown")
        return DeviceInfo(
            identifiers={(DOMAIN, self.module_id)},
            name=_xbus_module_title(address, "xDI-16"),
            manufacturer="IntellegyHub",
            model=_xbus_module_model(address, "xDI-16"),
            via_device_id=_parent_device_id(self, self.manager),
        )


class IntellegyHubOneWireBridgeEntity(IntellegyHubGpioEntity):
    _attr_has_entity_name = True

    def __init__(self, manager: IntellegyHubGpioManager, bridge_id: str) -> None:
        super().__init__(manager)
        self.bridge_id = bridge_id

    @property
    def bridge_state(self) -> dict:
        return self.manager.onewire_bridge(self.bridge_id)

    @property
    def device_info(self) -> DeviceInfo:
        bridge = self.bridge_state
        return DeviceInfo(
            identifiers={(DOMAIN, self.bridge_id)},
            name=_onewire_bridge_title(bridge),
            manufacturer="IntellegyHub",
            model=bridge.get("chip") or bridge.get("model") or "1-Wire Bridge",
            via_device_id=_parent_device_id(self, self.manager),
        )


class IntellegyHubOneWireSensorEntity(IntellegyHubOneWireBridgeEntity):
    def __init__(self, manager: IntellegyHubGpioManager, sensor_id: str) -> None:
        sensor = manager.onewire_sensor(sensor_id)
        super().__init__(manager, sensor.get("bridge_id") or "onewire_unknown")
        self.sensor_id = sensor_id

    @property
    def sensor_state(self) -> dict:
        return self.manager.onewire_sensor(self.sensor_id)

    @property
    def available(self) -> bool:
        return (
            self.manager.connected
            and bool(self.manager.onewire.get("power", {}).get("on", False))
            and bool(self.sensor_state.get("available", False))
        )
