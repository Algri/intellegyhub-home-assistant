from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    XPORT_CONFIGURATION_OPTIONS,
    XPORT_PROFILE_LED_DIMMER,
    XPORT_PROFILE_OPTIONS,
    XPORT_GROUP_MODE_RGBW,
    XPORT_GROUP_MODE_RGB_PLUS_W,
    XPORT_GROUP_MODE_W_PLUS_W_PLUS_W_PLUS_W,
    XPORT_PROFILE_UNIVERSAL_IO,
    XPORT_MODE_LABEL_OPTIONS,
    XPORT_MODE_LABELS,
    XPORT_MODE_VALUES_BY_LABEL,
    normalize_xport_profile,
)
from .entity import IntellegyHubGpioEntity, IntellegyHubXPortEntity

XPORT_CONFIGURATION_LABELS = {
    XPORT_GROUP_MODE_RGBW: "RGBW",
    XPORT_GROUP_MODE_RGB_PLUS_W: "RGB + W",
    XPORT_GROUP_MODE_W_PLUS_W_PLUS_W_PLUS_W: "W + W + W + W",
}
XPORT_CONFIGURATION_VALUES_BY_LABEL = {label: value for value, label in XPORT_CONFIGURATION_LABELS.items()}


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    manager = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([IntellegyHubXPortProfileSelect(manager), IntellegyHubXPortConfigurationSelect(manager)])
    known: dict[int, IntellegyHubXPortModeSelect] = {}

    @callback
    def sync_entities() -> None:
        desired = (
            set()
            if normalize_xport_profile(manager.xport.get("group_mode")) in XPORT_CONFIGURATION_OPTIONS
            else set(range(1, 5))
        )
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


class IntellegyHubXPortProfileSelect(IntellegyHubGpioEntity, SelectEntity):
    _attr_translation_key = "xport_profile"
    _attr_unique_id = "intellegyhub_xport_profile"
    _attr_name = "X-PORT 1 Profile"
    _attr_options = XPORT_PROFILE_OPTIONS
    _attr_has_entity_name = False

    @property
    def available(self) -> bool:
        return self.manager.connected and self.manager.xport.get("availability") == "Available"

    @property
    def current_option(self) -> str | None:
        group_mode = normalize_xport_profile(self.manager.xport.get("group_mode"))
        return XPORT_PROFILE_UNIVERSAL_IO if group_mode == XPORT_PROFILE_UNIVERSAL_IO else XPORT_PROFILE_LED_DIMMER

    async def async_select_option(self, option: str) -> None:
        if option not in XPORT_PROFILE_OPTIONS:
            raise ValueError(f"Unsupported X-Port profile: {option}")
        if option == XPORT_PROFILE_LED_DIMMER:
            group_mode = normalize_xport_profile(self.manager.xport.get("group_mode"))
            await self.manager.async_set_xport_profile(
                group_mode if group_mode in XPORT_CONFIGURATION_OPTIONS else XPORT_GROUP_MODE_RGBW
            )
            return
        await self.manager.async_set_xport_profile(XPORT_PROFILE_UNIVERSAL_IO)


class IntellegyHubXPortConfigurationSelect(IntellegyHubGpioEntity, SelectEntity):
    _attr_translation_key = "xport_configuration"
    _attr_unique_id = "intellegyhub_xport_configuration"
    _attr_name = "X-PORT 2 Mode"
    _attr_options = list(XPORT_CONFIGURATION_VALUES_BY_LABEL)
    _attr_has_entity_name = False

    @property
    def available(self) -> bool:
        return (
            self.manager.connected
            and self.manager.xport.get("availability") == "Available"
            and normalize_xport_profile(self.manager.xport.get("group_mode")) in XPORT_CONFIGURATION_OPTIONS
        )

    @property
    def current_option(self) -> str | None:
        group_mode = normalize_xport_profile(self.manager.xport.get("group_mode"))
        return XPORT_CONFIGURATION_LABELS.get(group_mode)

    async def async_select_option(self, option: str) -> None:
        if option not in XPORT_CONFIGURATION_VALUES_BY_LABEL:
            raise ValueError(f"Unsupported X-Port configuration: {option}")
        await self.manager.async_set_xport_profile(XPORT_CONFIGURATION_VALUES_BY_LABEL[option])
