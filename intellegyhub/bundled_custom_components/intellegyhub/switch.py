from __future__ import annotations

import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN, OUTPUTS, XPORT_MODE_DO
from .entity import (
    IntellegyHubExtensionPowerEntity,
    IntellegyHubGpioEntity,
    IntellegyHubOneWirePowerEntity,
    IntellegyHubXDo8Entity,
    IntellegyHubXPortEntity,
)
from .xport_entities import setup_xport_dynamic_platform

LOGGER = logging.getLogger(__name__)
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    manager = hass.data[DOMAIN][entry.entry_id]
    known_xdo8: set[str] = set()

    def xdo8_module_ids() -> set[str]:
        return {
            module_id
            for module in manager.extensions.get("modules", [])
            if isinstance(module_id := module.get("id"), str)
            and (module.get("kind") == "relay_output" or module_id.startswith("xdo8_"))
        }

    def extension_entities(module_ids: set[str]) -> list[IntellegyHubXDo8RelaySwitch]:
        entities = []
        for module_id in sorted(module_ids):
            entities.extend(IntellegyHubXDo8RelaySwitch(manager, module_id, channel) for channel in range(1, 9))
        return entities

    async_add_entities(
        [
            *(IntellegyHubOutputSwitch(manager, output_id) for output_id in OUTPUTS),
            IntellegyHubExtensionPowerSwitch(manager),
            IntellegyHubOneWirePowerSwitch(manager),
        ]
    )
    setup_xport_dynamic_platform(
        entry,
        manager,
        async_add_entities,
        Platform.SWITCH,
        "do",
        lambda channel: IntellegyHubXPortDoSwitch(manager, channel),
    )

    def check_extension_entities() -> None:
        current_module_ids = xdo8_module_ids()
        known_xdo8.intersection_update(current_module_ids)
        new_module_ids = current_module_ids - known_xdo8
        LOGGER.warning(
            "IntellegyHUB xDO-8 switch discovery current=%s known=%s new=%s",
            sorted(current_module_ids),
            sorted(known_xdo8),
            sorted(new_module_ids),
        )
        new_entities = extension_entities(new_module_ids)
        if new_entities:
            async_add_entities(new_entities)
            known_xdo8.update(new_module_ids)

    entry.async_on_unload(manager.async_add_listener(check_extension_entities))
    check_extension_entities()


class IntellegyHubOutputSwitch(IntellegyHubGpioEntity, SwitchEntity):
    _attr_translation_key = "host_output"

    def __init__(self, manager, output_id: str) -> None:
        super().__init__(manager)
        self.output_id = output_id
        self._attr_unique_id = OUTPUTS[output_id]["unique_id"]
        self._attr_name = OUTPUTS[output_id]["name"]

    @property
    def is_on(self) -> bool:
        return bool(self.manager.outputs.get(self.output_id, False))

    async def async_turn_on(self, **kwargs) -> None:
        await self.manager.async_set_output(self.output_id, True)

    async def async_turn_off(self, **kwargs) -> None:
        await self.manager.async_set_output(self.output_id, False)


class IntellegyHubXPortDoSwitch(IntellegyHubXPortEntity, SwitchEntity):
    _attr_translation_key = "xport_do"

    def __init__(self, manager, channel: int) -> None:
        super().__init__(manager, channel)
        self._attr_unique_id = f"intellegyhub_xport_x{channel}_do"
        self._attr_name = "DO"

    @property
    def available(self) -> bool:
        return self.available_for_modes({XPORT_MODE_DO})

    @property
    def is_on(self) -> bool:
        return bool(self.channel_state.get("value", 0))

    async def async_turn_on(self, **kwargs) -> None:
        self.assert_mode_available({XPORT_MODE_DO})
        await self.manager.async_set_xport_value(self.channel, 1)

    async def async_turn_off(self, **kwargs) -> None:
        self.assert_mode_available({XPORT_MODE_DO})
        await self.manager.async_set_xport_value(self.channel, 0)


class IntellegyHubExtensionPowerSwitch(IntellegyHubExtensionPowerEntity, SwitchEntity):
    _attr_unique_id = "intellegyhub_extension_bus_power"
    _attr_name = "Expansion Bus Power"

    @property
    def is_on(self) -> bool:
        return bool(self.manager.extensions.get("power", {}).get("on", False))

    async def async_turn_on(self, **kwargs) -> None:
        await self.manager.async_set_extension_power(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self.manager.async_set_extension_power(False)


class IntellegyHubOneWirePowerSwitch(IntellegyHubOneWirePowerEntity, SwitchEntity):
    _attr_unique_id = "intellegyhub_onewire_bus_power"
    _attr_name = "1-Wire Bus Power"

    @property
    def is_on(self) -> bool:
        return bool(self.manager.onewire.get("power", {}).get("on", False))

    async def async_turn_on(self, **kwargs) -> None:
        await self.manager.async_set_onewire_power(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self.manager.async_set_onewire_power(False)


class IntellegyHubXDo8RelaySwitch(IntellegyHubXDo8Entity, SwitchEntity):
    _attr_translation_key = "xdo8_relay"

    def __init__(self, manager, module_id: str, channel: int) -> None:
        super().__init__(manager, module_id, channel)
        self._attr_unique_id = f"intellegyhub_{module_id}_relay_{channel}"
        self._attr_name = f"Relay {channel}"

    @property
    def is_on(self) -> bool:
        relays = self.module_state.get("relays", [])
        if len(relays) < self.channel:
            return False
        return bool(relays[self.channel - 1])

    async def async_turn_on(self, **kwargs) -> None:
        await self.manager.async_set_extension_relay(self.module_id, self.channel, True)

    async def async_turn_off(self, **kwargs) -> None:
        await self.manager.async_set_extension_relay(self.module_id, self.channel, False)
