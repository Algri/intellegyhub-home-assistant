from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, XPORT_GROUP_MODE_RGBW, XPORT_GROUP_MODE_RGB_PLUS_W, XPORT_MODE_PWM, normalize_xport_profile
from .entity import IntellegyHubGpioEntity, IntellegyHubXPortEntity, IntellegyHubXPortGroupEntity
from .xport_entities import setup_xport_dynamic_platform

try:
    from homeassistant.helpers.entity import EntityCategory
except ImportError:
    EntityCategory = None

ENTITY_CATEGORY_CONFIG = EntityCategory.CONFIG if EntityCategory is not None else "config"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    manager = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            IntellegyHubBuzzerFrequencyNumber(manager),
            IntellegyHubBuzzerDurationNumber(manager),
            IntellegyHubBuzzerVolumeNumber(manager),
        ]
    )
    setup_xport_dynamic_platform(
        entry,
        manager,
        async_add_entities,
        Platform.NUMBER,
        "pwm",
        lambda channel: IntellegyHubXPortPwmNumber(manager, channel),
    )
    setup_xport_rgbw_numbers(entry, manager, async_add_entities)


RGBW_CHANNELS = {
    1: ("red", "X1 Red"),
    2: ("green", "X2 Green"),
    3: ("blue", "X3 Blue"),
    4: ("white", "X4 White"),
}

RGB_PLUS_W_CHANNELS = {
    1: ("rgb_red", "X1 Red"),
    2: ("rgb_green", "X2 Green"),
    3: ("rgb_blue", "X3 Blue"),
    4: ("w_white", "X4 White"),
}


def setup_xport_rgbw_numbers(entry: ConfigEntry, manager, async_add_entities: AddEntitiesCallback) -> None:
    known: dict[tuple[str, int], IntellegyHubXPortRgbwChannelNumber] = {}

    @callback
    def sync_entities() -> None:
        group_mode = normalize_xport_profile(manager.xport.get("group_mode"))
        if group_mode == XPORT_GROUP_MODE_RGBW:
            desired = {(XPORT_GROUP_MODE_RGBW, channel) for channel in RGBW_CHANNELS}
        elif group_mode == XPORT_GROUP_MODE_RGB_PLUS_W:
            desired = {(XPORT_GROUP_MODE_RGB_PLUS_W, channel) for channel in RGB_PLUS_W_CHANNELS}
        else:
            desired = set()
        for key in sorted(desired - set(known)):
            group_mode, channel = key
            entity = IntellegyHubXPortRgbwChannelNumber(manager, channel, group_mode)
            known[key] = entity
            async_add_entities([entity])
        for key in sorted(set(known) - desired):
            entity = known.pop(key)
            manager.hass.async_create_task(
                entity.async_remove(force_remove=True),
                f"intellegyhub_remove_xport_group_channel_{entity._attr_unique_id}",
            )

    entry.async_on_unload(manager.async_add_listener(sync_entities))
    sync_entities()


class IntellegyHubXPortPwmNumber(IntellegyHubXPortEntity, NumberEntity):
    _attr_translation_key = "xport_pwm"
    _attr_icon = "mdi:lightbulb"
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "%"
    _attr_mode = NumberMode.SLIDER

    def __init__(self, manager, channel: int) -> None:
        super().__init__(manager, channel)
        self._attr_unique_id = f"intellegyhub_xport_x{channel}_pwm"
        self._attr_name = f"X{channel} PWM"

    @property
    def available(self) -> bool:
        return self.available_for_modes({XPORT_MODE_PWM})

    @property
    def native_value(self):
        return round(float(self.channel_state.get("value", 0)) * 100)

    async def async_set_native_value(self, value: float) -> None:
        self.assert_mode_available({XPORT_MODE_PWM})
        await self.manager.async_set_xport_value(self.channel, value / 100)


class IntellegyHubXPortRgbwChannelNumber(IntellegyHubXPortGroupEntity, NumberEntity):
    _attr_translation_key = "xport_rgbw_channel"
    _attr_icon = "mdi:lightbulb"
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "%"
    _attr_mode = NumberMode.SLIDER

    def __init__(self, manager, channel: int, group_mode: str = XPORT_GROUP_MODE_RGBW) -> None:
        super().__init__(manager)
        self.channel = channel
        self.group_mode = normalize_xport_profile(group_mode)
        channels = RGB_PLUS_W_CHANNELS if self.group_mode == XPORT_GROUP_MODE_RGB_PLUS_W else RGBW_CHANNELS
        slug, name = channels[channel]
        prefix = "intellegyhub_xport" if self.group_mode == XPORT_GROUP_MODE_RGB_PLUS_W else "intellegyhub_xport_rgbw"
        self._attr_unique_id = f"{prefix}_{slug}"
        self._attr_name = name

    @property
    def group_identifier(self) -> str:
        if self.group_mode == XPORT_GROUP_MODE_RGB_PLUS_W:
            return "xport_w_dimmer" if self.channel == 4 else "xport_rgb_dimmer"
        return "xport_rgbw_dimmer"

    @property
    def group_name(self) -> str:
        if self.group_mode == XPORT_GROUP_MODE_RGB_PLUS_W:
            return "X-Port W Dimmer" if self.channel == 4 else "X-Port RGB Dimmer"
        return "X-Port RGBW Dimmer"

    @property
    def group_model(self) -> str:
        if self.group_mode == XPORT_GROUP_MODE_RGB_PLUS_W:
            return "W Dimmer" if self.channel == 4 else "RGB Dimmer"
        return "RGBW Dimmer"

    @property
    def available(self) -> bool:
        return self.xport_available and normalize_xport_profile(self.manager.xport.get("group_mode")) == self.group_mode

    @property
    def native_value(self):
        return round(float(self.manager.xport_channel(self.channel).get("value", 0)) * 100)

    async def async_set_native_value(self, value: float) -> None:
        if not self.available:
            raise HomeAssistantError("X-Port LED Dimmer is not active")
        await self.manager.async_set_xport_group_channel_value(self.channel, value / 100)


class IntellegyHubBuzzerVolumeNumber(IntellegyHubGpioEntity, NumberEntity):
    _attr_translation_key = "buzzer_volume"
    _attr_unique_id = "intellegyhub_buzzer_volume"
    _attr_name = "Buzzer Volume"
    _attr_icon = "mdi:volume-high"
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "%"
    _attr_mode = NumberMode.SLIDER
    _attr_entity_category = ENTITY_CATEGORY_CONFIG

    @property
    def native_value(self):
        return int(self.manager.buzzer.get("volume_percent", 50))

    async def async_set_native_value(self, value: float) -> None:
        await self.manager.async_set_buzzer_volume(round(value))


class IntellegyHubBuzzerFrequencyNumber(IntellegyHubGpioEntity, NumberEntity):
    _attr_translation_key = "buzzer_frequency"
    _attr_unique_id = "intellegyhub_buzzer_frequency"
    _attr_name = "Buzzer Frequency"
    _attr_icon = "mdi:sine-wave"
    _attr_native_min_value = 300
    _attr_native_max_value = 2800
    _attr_native_step = 10
    _attr_native_unit_of_measurement = "Hz"
    _attr_mode = NumberMode.SLIDER
    _attr_entity_category = ENTITY_CATEGORY_CONFIG

    @property
    def native_value(self):
        return int(self.manager.buzzer.get("frequency", 2000))

    async def async_set_native_value(self, value: float) -> None:
        await self.manager.async_set_buzzer_settings(frequency=round(value))


class IntellegyHubBuzzerDurationNumber(IntellegyHubGpioEntity, NumberEntity):
    _attr_translation_key = "buzzer_duration"
    _attr_unique_id = "intellegyhub_buzzer_duration"
    _attr_name = "Buzzer Duration"
    _attr_icon = "mdi:timer-outline"
    _attr_native_min_value = 10
    _attr_native_max_value = 1000
    _attr_native_step = 10
    _attr_native_unit_of_measurement = "ms"
    _attr_mode = NumberMode.SLIDER
    _attr_entity_category = ENTITY_CATEGORY_CONFIG

    @property
    def native_value(self):
        return int(self.manager.buzzer.get("duration_ms", 300))

    async def async_set_native_value(self, value: float) -> None:
        await self.manager.async_set_buzzer_settings(duration_ms=round(value))
