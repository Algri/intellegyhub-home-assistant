from __future__ import annotations

from homeassistant.components.light import ATTR_BRIGHTNESS, ATTR_RGB_COLOR, ATTR_RGBW_COLOR, ColorMode, LightEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    XPORT_GROUP_MODE_RGBW,
    XPORT_GROUP_MODE_RGB_PLUS_W,
    XPORT_GROUP_MODE_W_PLUS_W_PLUS_W_PLUS_W,
    XPORT_GROUP_MODE_TWO_W_PLUS_TWO_W,
    XPORT_GROUP_MODE_TWO_W_PLUS_W_PLUS_W,
    XPORT_GROUP_MODE_W_PLUS_W_PLUS_TWO_W,
    XPORT_GROUP_MODE_FOUR_W,
    XPORT_MODE_PWM,
    normalize_xport_profile,
)
from .entity import IntellegyHubGpioEntity, IntellegyHubXPortEntity, IntellegyHubXPortGroupEntity
from .xport_entities import setup_xport_dynamic_platform


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    manager = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([IntellegyHubBuzzerVolumeLight(manager)])
    setup_xport_rgbw_light(entry, manager, async_add_entities)
    setup_xport_rgb_plus_w_lights(entry, manager, async_add_entities)
    setup_xport_w_plus_w_lights(entry, manager, async_add_entities)
    setup_xport_linked_white_lights(entry, manager, async_add_entities)
    setup_xport_dynamic_platform(
        entry,
        manager,
        async_add_entities,
        Platform.LIGHT,
        "pwm_light",
        lambda channel: IntellegyHubXPortPwmLight(manager, channel),
    )


def setup_xport_rgbw_light(entry: ConfigEntry, manager, async_add_entities: AddEntitiesCallback) -> None:
    entity: IntellegyHubXPortRgbwLight | None = None

    @callback
    def sync_entity() -> None:
        nonlocal entity
        enabled = normalize_xport_profile(manager.xport.get("group_mode")) == XPORT_GROUP_MODE_RGBW
        if enabled and entity is None:
            entity = IntellegyHubXPortRgbwLight(manager)
            async_add_entities([entity])
        elif not enabled and entity is not None:
            stale = entity
            entity = None
            manager.hass.async_create_task(
                stale.async_remove(force_remove=True),
                "intellegyhub_remove_xport_rgbw",
            )

    entry.async_on_unload(manager.async_add_listener(sync_entity))
    sync_entity()


def setup_xport_rgb_plus_w_lights(entry: ConfigEntry, manager, async_add_entities: AddEntitiesCallback) -> None:
    entities: dict[str, LightEntity] = {}

    @callback
    def sync_entities() -> None:
        desired = {"rgb", "white"} if normalize_xport_profile(manager.xport.get("group_mode")) == XPORT_GROUP_MODE_RGB_PLUS_W else set()
        for key in sorted(desired - set(entities)):
            entity = IntellegyHubXPortRgbLight(manager) if key == "rgb" else IntellegyHubXPortWhiteLight(manager)
            entities[key] = entity
            async_add_entities([entity])
        for key in sorted(set(entities) - desired):
            entity = entities.pop(key)
            manager.hass.async_create_task(
                entity.async_remove(force_remove=True),
                f"intellegyhub_remove_xport_rgb_plus_w_{key}",
            )

    entry.async_on_unload(manager.async_add_listener(sync_entities))
    sync_entities()


def setup_xport_w_plus_w_lights(entry: ConfigEntry, manager, async_add_entities: AddEntitiesCallback) -> None:
    entities: dict[int, IntellegyHubXPortWhiteChannelLight] = {}

    @callback
    def sync_entities() -> None:
        desired = (
            set(range(1, 5))
            if normalize_xport_profile(manager.xport.get("group_mode")) == XPORT_GROUP_MODE_W_PLUS_W_PLUS_W_PLUS_W
            else set()
        )
        for channel in sorted(desired - set(entities)):
            entity = IntellegyHubXPortWhiteChannelLight(manager, channel)
            entities[channel] = entity
            async_add_entities([entity])
        for channel in sorted(set(entities) - desired):
            entity = entities.pop(channel)
            manager.hass.async_create_task(
                entity.async_remove(force_remove=True),
                f"intellegyhub_remove_xport_w_plus_w_x{channel}",
            )

    entry.async_on_unload(manager.async_add_listener(sync_entities))
    sync_entities()


def setup_xport_linked_white_lights(entry: ConfigEntry, manager, async_add_entities: AddEntitiesCallback) -> None:
    entities: dict[tuple[str, tuple[int, ...]], IntellegyHubXPortLinkedWhiteLight] = {}

    @callback
    def sync_entities() -> None:
        group_mode = normalize_xport_profile(manager.xport.get("group_mode"))
        desired = {
            (group_mode, channels)
            for channels in linked_white_light_groups(group_mode)
        }
        for key in sorted(desired - set(entities), key=lambda item: item[1]):
            group_mode, channels = key
            entity = IntellegyHubXPortLinkedWhiteLight(manager, group_mode, channels)
            entities[key] = entity
            async_add_entities([entity])
        for key in sorted(set(entities) - desired, key=lambda item: item[1]):
            entity = entities.pop(key)
            manager.hass.async_create_task(
                entity.async_remove(force_remove=True),
                f"intellegyhub_remove_xport_linked_white_{entity._attr_unique_id}",
            )

    entry.async_on_unload(manager.async_add_listener(sync_entities))
    sync_entities()


def linked_white_light_groups(group_mode: str) -> tuple[tuple[int, ...], ...]:
    if group_mode == XPORT_GROUP_MODE_FOUR_W:
        return ((1, 2, 3, 4),)
    if group_mode == XPORT_GROUP_MODE_TWO_W_PLUS_TWO_W:
        return ((1, 2), (3, 4))
    if group_mode == XPORT_GROUP_MODE_TWO_W_PLUS_W_PLUS_W:
        return ((1, 2), (3,), (4,))
    if group_mode == XPORT_GROUP_MODE_W_PLUS_W_PLUS_TWO_W:
        return ((1,), (2,), (3, 4))
    return ()


class IntellegyHubXPortPwmLight(IntellegyHubXPortEntity, LightEntity):
    _attr_translation_key = "xport_pwm"
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}
    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_icon = "mdi:lightbulb"

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


class IntellegyHubXPortRgbwLight(IntellegyHubXPortGroupEntity, LightEntity):
    _attr_translation_key = "xport_rgbw"
    _attr_unique_id = "intellegyhub_xport_rgbw"
    _attr_suggested_object_id = "intellegyhub_xport_rgbw"
    _attr_name = "Dimmer"
    _attr_supported_color_modes = {ColorMode.RGBW}
    _attr_color_mode = ColorMode.RGBW
    _attr_icon = "mdi:lightbulb"

    @property
    def available(self) -> bool:
        return self.xport_available and normalize_xport_profile(self.manager.xport.get("group_mode")) == XPORT_GROUP_MODE_RGBW

    @property
    def is_on(self) -> bool:
        return any(value > 0 for value in self._channel_values())

    @property
    def brightness(self) -> int | None:
        level = max(self._channel_values())
        if level <= 0:
            return None
        return max(1, min(255, round(level * 255)))

    @property
    def rgbw_color(self) -> tuple[int, int, int, int] | None:
        return tuple(max(0, min(255, round(value * 255))) for value in self._channel_values())

    async def async_turn_on(self, **kwargs) -> None:
        if not self.available:
            raise HomeAssistantError("X-Port RGBW Dimmer is not active")
        values = self._channel_values()
        rgbw = kwargs.get(ATTR_RGBW_COLOR)
        brightness = kwargs.get(ATTR_BRIGHTNESS)
        if rgbw is not None:
            values = tuple(max(0.0, min(1.0, int(part) / 255)) for part in rgbw)
        elif not any(values):
            values = (1.0, 1.0, 1.0, 0.0)
        if brightness is not None:
            scale = max(0.0, min(1.0, int(brightness) / 255))
            current = max(values)
            if current > 0:
                values = tuple(max(0.0, min(1.0, value / current * scale)) for value in values)
            else:
                values = (scale, scale, scale, 0.0)
        await self.manager.async_set_xport_rgbw(values)

    async def async_turn_off(self, **kwargs) -> None:
        if not self.available:
            raise HomeAssistantError("X-Port RGBW Dimmer is not active")
        await self.manager.async_set_xport_rgbw((0.0, 0.0, 0.0, 0.0))

    def _channel_values(self) -> tuple[float, float, float, float]:
        return tuple(
            max(0.0, min(1.0, float(self.manager.xport_channel(channel).get("value", 0))))
            for channel in range(1, 5)
        )


class IntellegyHubXPortRgbLight(IntellegyHubXPortGroupEntity, LightEntity):
    group_identifier = "xport_rgb_dimmer"
    group_name = "X-Port RGB Dimmer"
    group_model = "RGB Dimmer"
    _attr_translation_key = "xport_rgb"
    _attr_unique_id = "intellegyhub_xport_rgb"
    _attr_suggested_object_id = "intellegyhub_xport_rgb"
    _attr_name = "Dimmer"
    _attr_supported_color_modes = {ColorMode.RGB}
    _attr_color_mode = ColorMode.RGB
    _attr_icon = "mdi:lightbulb"

    @property
    def available(self) -> bool:
        return self.xport_available and normalize_xport_profile(self.manager.xport.get("group_mode")) == XPORT_GROUP_MODE_RGB_PLUS_W

    @property
    def is_on(self) -> bool:
        return self.brightness is not None and self.brightness > 0

    @property
    def brightness(self) -> int:
        _, brightness = self.manager.xport_rgb_state()
        return max(0, min(255, int(brightness)))

    @property
    def rgb_color(self) -> tuple[int, int, int] | None:
        color, _ = self.manager.xport_rgb_state()
        return color

    async def async_turn_on(self, **kwargs) -> None:
        if not self.available:
            raise HomeAssistantError("X-Port RGB Dimmer is not active")
        rgb = kwargs.get(ATTR_RGB_COLOR)
        brightness = kwargs.get(ATTR_BRIGHTNESS)
        await self.manager.async_set_xport_rgb_state(rgb_color=rgb, brightness=brightness)

    async def async_turn_off(self, **kwargs) -> None:
        if not self.available:
            raise HomeAssistantError("X-Port RGB Dimmer is not active")
        await self.manager.async_set_xport_rgb_state(brightness=0)


class IntellegyHubXPortWhiteLight(IntellegyHubXPortGroupEntity, LightEntity):
    group_identifier = "xport_w_dimmer"
    group_name = "X-Port W Dimmer"
    group_model = "W Dimmer"
    _attr_translation_key = "xport_w"
    _attr_unique_id = "intellegyhub_xport_w"
    _attr_suggested_object_id = "intellegyhub_xport_w"
    _attr_name = "Dimmer"
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}
    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_icon = "mdi:lightbulb"

    @property
    def available(self) -> bool:
        return self.xport_available and normalize_xport_profile(self.manager.xport.get("group_mode")) == XPORT_GROUP_MODE_RGB_PLUS_W

    @property
    def is_on(self) -> bool:
        return self._value > 0

    @property
    def brightness(self) -> int | None:
        if self._value <= 0:
            return None
        return max(1, min(255, round(self._value * 255)))

    async def async_turn_on(self, **kwargs) -> None:
        if not self.available:
            raise HomeAssistantError("X-Port W Dimmer is not active")
        brightness = kwargs.get(ATTR_BRIGHTNESS)
        if brightness is None:
            value = self._value or 1.0
        else:
            value = max(0.0, min(1.0, int(brightness) / 255))
        await self.manager.async_set_xport_group_channel_value(4, value)

    async def async_turn_off(self, **kwargs) -> None:
        if not self.available:
            raise HomeAssistantError("X-Port W Dimmer is not active")
        await self.manager.async_set_xport_group_channel_value(4, 0.0)

    @property
    def _value(self) -> float:
        return max(0.0, min(1.0, float(self.manager.xport_channel(4).get("value", 0))))


class IntellegyHubXPortWhiteChannelLight(IntellegyHubXPortGroupEntity, LightEntity):
    group_identifier = "xport_w_dimmer"
    group_name = "X-Port W Dimmer"
    group_model = "W Dimmer"
    _attr_translation_key = "xport_w_channel"
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}
    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_icon = "mdi:lightbulb"

    def __init__(self, manager, channel: int) -> None:
        super().__init__(manager)
        self.channel = channel
        self._attr_unique_id = f"intellegyhub_xport_w_x{channel}"
        self._attr_suggested_object_id = f"intellegyhub_xport_w_x{channel}"
        self._attr_name = f"X{channel} White"

    @property
    def available(self) -> bool:
        return (
            self.xport_available
            and normalize_xport_profile(self.manager.xport.get("group_mode"))
            == XPORT_GROUP_MODE_W_PLUS_W_PLUS_W_PLUS_W
        )

    @property
    def is_on(self) -> bool:
        return self._value > 0

    @property
    def brightness(self) -> int | None:
        if self._value <= 0:
            return None
        return max(1, min(255, round(self._value * 255)))

    async def async_turn_on(self, **kwargs) -> None:
        if not self.available:
            raise HomeAssistantError("X-Port W Dimmer is not active")
        brightness = kwargs.get(ATTR_BRIGHTNESS)
        if brightness is None:
            value = self._value or 1.0
        else:
            value = max(0.0, min(1.0, int(brightness) / 255))
        await self.manager.async_set_xport_group_channel_value(self.channel, value)

    async def async_turn_off(self, **kwargs) -> None:
        if not self.available:
            raise HomeAssistantError("X-Port W Dimmer is not active")
        await self.manager.async_set_xport_group_channel_value(self.channel, 0.0)

    @property
    def _value(self) -> float:
        return max(0.0, min(1.0, float(self.manager.xport_channel(self.channel).get("value", 0))))


class IntellegyHubXPortLinkedWhiteLight(IntellegyHubXPortGroupEntity, LightEntity):
    _attr_translation_key = "xport_w_channel"
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}
    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_icon = "mdi:lightbulb"

    def __init__(self, manager, group_mode: str, channels: tuple[int, ...]) -> None:
        super().__init__(manager)
        self.group_mode = normalize_xport_profile(group_mode)
        self.channels = channels
        suffix = "_".join(f"x{channel}" for channel in channels)
        self.group_identifier = f"xport_w_{suffix}_dimmer"
        self.group_name = f"X-Port {self._channels_label} W Dimmer"
        self.group_model = "W Dimmer"
        self._attr_unique_id = f"intellegyhub_xport_w_{suffix}"
        self._attr_suggested_object_id = f"intellegyhub_xport_w_{suffix}"
        self._attr_name = f"{self._channels_label} White" if len(channels) > 1 else f"X{channels[0]} White"

    @property
    def available(self) -> bool:
        return self.xport_available and normalize_xport_profile(self.manager.xport.get("group_mode")) == self.group_mode

    @property
    def is_on(self) -> bool:
        return self._value > 0

    @property
    def brightness(self) -> int | None:
        if self._value <= 0:
            return None
        return max(1, min(255, round(self._value * 255)))

    async def async_turn_on(self, **kwargs) -> None:
        if not self.available:
            raise HomeAssistantError("X-Port W Dimmer is not active")
        brightness = kwargs.get(ATTR_BRIGHTNESS)
        if brightness is None:
            value = self._value or 1.0
        else:
            value = max(0.0, min(1.0, int(brightness) / 255))
        await self.manager.async_set_xport_group_channel_value(self.channels[0], value)

    async def async_turn_off(self, **kwargs) -> None:
        if not self.available:
            raise HomeAssistantError("X-Port W Dimmer is not active")
        await self.manager.async_set_xport_group_channel_value(self.channels[0], 0.0)

    @property
    def _value(self) -> float:
        values = [
            max(0.0, min(1.0, float(self.manager.xport_channel(channel).get("value", 0))))
            for channel in self.channels
        ]
        return max(values, default=0.0)

    @property
    def _channels_label(self) -> str:
        if len(self.channels) == 1:
            return f"X{self.channels[0]}"
        return f"X{self.channels[0]}-X{self.channels[-1]}"


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
            volume = int(
                self.manager.buzzer.get(
                    "last_volume_percent",
                    self.manager.buzzer.get("volume_percent", 50),
                )
            )
            if volume <= 0:
                volume = 50
        else:
            volume = max(1, min(100, round(int(brightness) * 100 / 255)))
        await self.manager.async_set_buzzer_volume(volume)

    async def async_turn_off(self, **kwargs) -> None:
        await self.manager.async_set_buzzer_volume(0)
