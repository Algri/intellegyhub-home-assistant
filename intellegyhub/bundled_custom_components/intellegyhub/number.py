from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, XPORT_MODE_PWM
from .entity import IntellegyHubGpioEntity, IntellegyHubXPortEntity
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


class IntellegyHubXPortPwmNumber(IntellegyHubXPortEntity, NumberEntity):
    _attr_translation_key = "xport_pwm"
    _attr_icon = "mdi:brightness-percent"
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
