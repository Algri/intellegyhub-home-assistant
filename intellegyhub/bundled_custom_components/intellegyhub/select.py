from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    XPORT_GROUP_MODE_RGBW,
    XPORT_MODE_LABEL_OPTIONS,
    XPORT_MODE_LABELS,
    XPORT_MODE_VALUES_BY_LABEL,
)
from .entity import IntellegyHubXPortEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    manager = hass.data[DOMAIN][entry.entry_id]
    known: dict[int, IntellegyHubXPortModeSelect] = {}

    @callback
    def sync_entities() -> None:
        desired = set() if manager.xport.get("group_mode") == XPORT_GROUP_MODE_RGBW else set(range(1, 5))
        for channel in sorted(desired - set(known)):
            entity = IntellegyHubXPortModeSelect(manager, channel)
            known[channel] = entity
            async_add_entities([entity])
        for channel in sorted(set(known) - desired):
            entity = known.pop(channel)
            manager.hass.async_create_task(
                entity.async_remove(force_remove=True),
                f"intellegyhub_remove_xport_x{channel}_mode",
            )

    entry.async_on_unload(manager.async_add_listener(sync_entities))
    sync_entities()


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
