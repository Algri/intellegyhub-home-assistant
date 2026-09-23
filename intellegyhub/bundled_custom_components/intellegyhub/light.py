from __future__ import annotations

from homeassistant.components.light import ATTR_BRIGHTNESS, ColorMode, LightEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, XPORT_MODE_PWM
from .entity import IntellegyHubGpioEntity, IntellegyHubXPortEntity
from .xport_entities import setup_xport_dynamic_platform


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    manager = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([IntellegyHubBuzzerVolumeLight(manager)])
    setup_xport_dynamic_platform(
        entry,
        manager,
        async_add_entities,
        Platform.LIGHT,
        "pwm_light",
        lambda channel: IntellegyHubXPortPwmLight(manager, channel),
    )


class IntellegyHubXPortPwmLight(IntellegyHubXPortEntity, LightEntity):
    _attr_translation_key = "xport_pwm"
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}
    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_icon = "mdi:floor-lamp-outline"

    def __init__(self, manager, channel: int) -> None:
        super().__init__(manager, channel)
        self._attr_unique_id = f"intellegyhub_xport_x{channel}_pwm_light"
        self._attr_name = f"X{channel} PWM"

    @property
    def available(self) -> bool:
        return self.available_for_modes({XPORT_MODE_PWM})

    @property
    def is_on(self) -> bool:
        return float(self.channel_state.get("value", 0)) > 0

    @property
    def brightness(self) -> int | None:
        value = max(0.0, min(1.0, float(self.channel_state.get("value", 0))))
        if value <= 0:
            return None
        return max(1, min(255, round(value * 255)))

    async def async_turn_on(self, **kwargs) -> None:
        self.assert_mode_available({XPORT_MODE_PWM})
        brightness = kwargs.get(ATTR_BRIGHTNESS)
        if brightness is None:
            value = float(self.channel_state.get("value", 0))
            if value <= 0:
                value = 1.0
        else:
            value = max(0.0, min(1.0, int(brightness) / 255))
        await self.manager.async_set_xport_value(self.channel, value)

    async def async_turn_off(self, **kwargs) -> None:
        self.assert_mode_available({XPORT_MODE_PWM})
        await self.manager.async_set_xport_value(self.channel, 0)


class IntellegyHubBuzzerVolumeLight(IntellegyHubGpioEntity, LightEntity):
    _attr_translation_key = "buzzer_volume_light"
    _attr_unique_id = "intellegyhub_buzzer_volume_light"
    _attr_suggested_object_id = "intellegyhub_buzzer_volume_light"
    _attr_name = "Buzzer Volume"
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}
    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_icon = "mdi:volume-high"

    @property
    def is_on(self) -> bool:
        return int(self.manager.buzzer.get("volume_percent", 0)) > 0

    @property
    def brightness(self) -> int | None:
        volume = max(0, min(100, int(self.manager.buzzer.get("volume_percent", 0))))
        if volume <= 0:
            return None
        return max(1, min(255, round(volume * 255 / 100)))

    async def async_turn_on(self, **kwargs) -> None:
        brightness = kwargs.get(ATTR_BRIGHTNESS)
        if brightness is None:
            volume = int(self.manager.buzzer.get("volume_percent", 0))
            if volume <= 0:
                volume = 50
        else:
            volume = max(1, min(100, round(int(brightness) * 100 / 255)))
        await self.manager.async_set_buzzer_volume(volume)

    async def async_turn_off(self, **kwargs) -> None:
        await self.manager.async_set_buzzer_volume(0)
