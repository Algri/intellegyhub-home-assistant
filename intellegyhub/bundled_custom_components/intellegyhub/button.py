from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, XPORT_MODE_COUNTER
from .entity import IntellegyHubXPortEntity
from .xport_entities import setup_xport_dynamic_platform


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    manager = hass.data[DOMAIN][entry.entry_id]
    setup_xport_dynamic_platform(
        entry,
        manager,
        async_add_entities,
        Platform.BUTTON,
        "counter_reset",
        lambda channel: IntellegyHubXPortCounterResetButton(manager, channel),
    )


class IntellegyHubXPortCounterResetButton(IntellegyHubXPortEntity, ButtonEntity):
    _attr_translation_key = "xport_counter_reset"

    def __init__(self, manager, channel: int) -> None:
        super().__init__(manager, channel)
        self._attr_unique_id = f"intellegyhub_xport_x{channel}_counter_reset"
        self._attr_name = "Reset Counter"

    @property
    def available(self) -> bool:
        return self.available_for_modes(XPORT_MODE_COUNTER)

    async def async_press(self) -> None:
        self.assert_mode_available(XPORT_MODE_COUNTER)
        await self.manager.async_reset_xport_counter(self.channel)
