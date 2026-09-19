from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform, UnitOfElectricPotential
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, XPORT_MODE_AI, XPORT_MODE_COUNTER
from .entity import IntellegyHubOneWireSensorEntity, IntellegyHubXPortEntity
from .xport_entities import setup_xport_dynamic_platform

try:
    from homeassistant.const import UnitOfTemperature
except ImportError:
    UnitOfTemperature = None

UNIT_CELSIUS = UnitOfTemperature.CELSIUS if UnitOfTemperature is not None else "°C"
SENSOR_DEVICE_CLASS_TEMPERATURE = getattr(SensorDeviceClass, "TEMPERATURE", "temperature")


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    manager = hass.data[DOMAIN][entry.entry_id]
    known_onewire: set[str] = set()
    setup_xport_dynamic_platform(
        entry,
        manager,
        async_add_entities,
        Platform.SENSOR,
        "ai",
        lambda channel: IntellegyHubXPortAiSensor(manager, channel),
    )
    setup_xport_dynamic_platform(
        entry,
        manager,
        async_add_entities,
        Platform.SENSOR,
        "counter",
        lambda channel: IntellegyHubXPortCounterSensor(manager, channel),
    )

    def onewire_sensor_ids() -> set[str]:
        return {
            sensor_id
            for sensor in manager.onewire.get("sensors", [])
            if sensor.get("added") is True and isinstance(sensor_id := sensor.get("id"), str)
        }

    def check_onewire_entities() -> None:
        current_sensor_ids = onewire_sensor_ids()
        known_onewire.intersection_update(current_sensor_ids)
        new_sensor_ids = current_sensor_ids - known_onewire
        new_entities = [
            IntellegyHubDs18b20TemperatureSensor(manager, sensor_id)
            for sensor_id in sorted(new_sensor_ids)
        ]
        if new_entities:
            async_add_entities(new_entities)
            known_onewire.update(new_sensor_ids)

    entry.async_on_unload(manager.async_add_listener(check_onewire_entities))
    check_onewire_entities()


class IntellegyHubXPortAiSensor(IntellegyHubXPortEntity, SensorEntity):
    _attr_translation_key = "xport_ai"
    _attr_device_class = SensorDeviceClass.VOLTAGE
    _attr_native_unit_of_measurement = UnitOfElectricPotential.VOLT
    _attr_suggested_display_precision = 3
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, manager, channel: int) -> None:
        super().__init__(manager, channel)
        self._attr_unique_id = f"intellegyhub_xport_x{channel}_ai"
        self._attr_name = "AI"

    @property
    def available(self) -> bool:
        return self.available_for_modes({XPORT_MODE_AI})

    @property
    def native_value(self):
        return self.channel_state.get("value")


class IntellegyHubXPortCounterSensor(IntellegyHubXPortEntity, SensorEntity):
    _attr_translation_key = "xport_counter"
    _attr_native_unit_of_measurement = "pulses"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(self, manager, channel: int) -> None:
        super().__init__(manager, channel)
        self._attr_unique_id = f"intellegyhub_xport_x{channel}_counter"
        self._attr_name = "Counter"

    @property
    def available(self) -> bool:
        return self.available_for_modes(XPORT_MODE_COUNTER)

    @property
    def native_value(self):
        return self.channel_state.get("counter")


class IntellegyHubDs18b20TemperatureSensor(IntellegyHubOneWireSensorEntity, SensorEntity):
    _attr_translation_key = "ds18b20_temperature"
    _attr_device_class = SENSOR_DEVICE_CLASS_TEMPERATURE
    _attr_native_unit_of_measurement = UNIT_CELSIUS
    _attr_suggested_display_precision = 2
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, manager, sensor_id: str) -> None:
        super().__init__(manager, sensor_id)
        self._attr_unique_id = f"intellegyhub_{sensor_id}_temperature"
        self._attr_name = "Temperature"

    @property
    def native_value(self):
        return self.sensor_state.get("temperature_c")

    @property
    def extra_state_attributes(self) -> dict:
        sensor = self.sensor_state
        return {
            "rom": sensor.get("rom"),
            "bridge": sensor.get("address"),
            "bus": sensor.get("bus"),
            "family": sensor.get("family"),
        }
