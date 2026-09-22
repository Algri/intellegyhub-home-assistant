from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, XPORT_MODE_LABEL_OPTIONS, XPORT_MODE_LABELS, XPORT_MODE_VALUES_BY_LABEL
from .entity import IntellegyHubXPortEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    manager = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([IntellegyHubXPortModeSelect(manager, channel) for channel in range(1, 5)])


class IntellegyHubXPortModeSelect(IntellegyHubXPortEntity, SelectEntity):
    _attr_translation_key = "xport_mode"
    _attr_options = XPORT_MODE_LABEL_OPTIONS

    def __init__(self, manager, channel: int) -> None:
        super().__init__(manager, channel)
        self._attr_unique_id = f"intellegyhub_xport_x{channel}_mode"
        self._attr_name = f"X{channel} Mode"

    @property
    def available(self) -> bool:
        return self.xport_available

    @property
    def current_option(self) -> str | None:
        mode = self.channel_state.get("confirmed_mode")
        return XPORT_MODE_LABELS.get(mode) if mode else None

    async def async_select_option(self, option: str) -> None:
        await self.manager.async_set_xport_mode(self.channel, XPORT_MODE_VALUES_BY_LABEL[option])
