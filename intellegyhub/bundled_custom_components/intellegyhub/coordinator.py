from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .api import IntellegyHubApiClient
from .const import DOMAIN
from .xport_entities import XPORT_ENTITY_KINDS, xport_desired_unique_ids, xport_platform_value

LOGGER = logging.getLogger(__name__)


class IntellegyHubGpioManager:
    def __init__(self, hass: HomeAssistant, client: IntellegyHubApiClient, entry_id: str) -> None:
        self.hass = hass
        self.client = client
        self.entry_id = entry_id
        self.connected = False
        self.led_on = False
        self.button_pressed = False
        self.carrier: dict = {}
        self.xport: dict = {}
        self.extensions: dict = {}
        self.onewire: dict = {}
        self._listeners: list[Callable[[], None]] = []
        self._task: asyncio.Task | None = None
        self._resync_task: asyncio.Task | None = None
        self._stopped = asyncio.Event()
        self._remove_stop_listener: Callable[[], None] | None = None

    async def async_start(self) -> None:
        try:
            await self._sync_snapshot()
            self.connected = True
        except Exception as exc:
            LOGGER.warning("Initial IntellegyHUB backend snapshot failed: %s", exc)
            self.connected = False
        self._notify()
        self._remove_stop_listener = self.hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STOP,
            lambda event: self.hass.async_create_task(self.async_stop(), "intellegyhub_stop"),
        )
        self._task = self._create_background_task(self._run(), "intellegyhub_listener")
        self._resync_task = self._create_background_task(self._run_periodic_resync(), "intellegyhub_resync")

    async def async_stop(self) -> None:
        self._stopped.set()
        if self._remove_stop_listener:
            self._remove_stop_listener()
            self._remove_stop_listener = None
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._resync_task:
            self._resync_task.cancel()
            try:
                await self._resync_task
            except asyncio.CancelledError:
                pass
            self._resync_task = None

    async def async_set_led(self, on: bool) -> None:
        confirmed = await self.client.set_led(on)
        self.led_on = confirmed
        self.connected = True
        self._notify()

    async def async_set_xport_mode(self, channel: int, mode: str) -> None:
        result = await self.client.set_xport_mode(channel, mode, self.xport_channel(channel).get("revision", 0) + 1)
        self._apply_xport_channel(result.get("channel"))
        self._remove_stale_xport_registry_entries()
        self.connected = True
        self._notify()

    async def async_set_xport_value(self, channel: int, value: float) -> None:
        result = await self.client.set_xport_value(channel, value)
        self._apply_xport_channel(result.get("channel"))
        self.connected = True
        self._notify()

    async def async_reset_xport_counter(self, channel: int) -> None:
        result = await self.client.reset_xport_counter(channel)
        self._apply_xport_channel(result.get("channel"))
        self.connected = True
        self._notify()

    async def async_set_extension_power(self, on: bool) -> None:
        self.extensions = await self.client.set_extension_power(on)
        self.connected = True
        self._notify()

    async def async_scan_extensions(self) -> None:
        self.extensions = await self.client.scan_extensions()
        self.connected = True
        self._notify()

    async def async_set_extension_relay(self, module_id: str, channel: int, on: bool) -> None:
        result = await self.client.set_extension_relay(module_id, channel, on)
        self._apply_extension_module(result.get("module"))
        self.connected = True
        self._notify()

    async def async_delete_extension_module(self, module_id: str) -> None:
        result = await self.client.delete_extension_module(module_id)
        self.extensions = result.get("extensions", self.extensions)
        self.connected = True
        self._notify()

    async def async_set_onewire_power(self, on: bool) -> None:
        self.onewire = await self.client.set_onewire_power(on)
        self.connected = True
        self._notify()

    async def async_scan_onewire(self) -> None:
        self.onewire = await self.client.scan_onewire()
        self.connected = True
        self._notify()

    async def async_refresh_onewire(self) -> None:
        self.onewire = await self.client.refresh_onewire()
        self.connected = True
        self._notify()

    async def async_delete_onewire_sensor(self, sensor_id: str) -> None:
        result = await self.client.delete_onewire_sensor(sensor_id)
        self.onewire = result.get("onewire", self.onewire)
        self.connected = True
        self._notify()

    def xport_channel(self, channel: int) -> dict:
        for item in self.xport.get("channels", []):
            if item.get("channel") == channel:
                return item
        return {}

    def extension_module(self, module_id: str) -> dict:
        for item in self.extensions.get("modules", []):
            if item.get("id") == module_id:
                return item
        return {}

    def onewire_sensor(self, sensor_id: str) -> dict:
        for item in self.onewire.get("sensors", []):
            if item.get("id") == sensor_id:
                return item
        return {}

    def onewire_bridge(self, bridge_id: str) -> dict:
        for item in self.onewire.get("bridges", []):
            if item.get("id") == bridge_id:
                return item
        return {"id": bridge_id, "name": bridge_id}

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(listener)

        @callback
        def remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return remove

    async def _sync_snapshot(self) -> None:
        payload = await self.client.state()
        self.led_on = payload["led"]["on"]
        self.button_pressed = payload["button"]["pressed"]
        self.carrier = payload.get("carrier", {})
        self.xport = payload.get("xport", {})
        self.extensions = payload.get("extensions", {})
        self.onewire = payload.get("onewire", {})
        self._remove_stale_extension_registry_entries()
        self._remove_stale_onewire_registry_entries()
        self._remove_stale_xport_registry_entries()

    async def _run_periodic_resync(self) -> None:
        while not self._stopped.is_set():
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=10)
                return
            except asyncio.TimeoutError:
                pass
            try:
                await self._sync_snapshot()
                self.connected = True
                self._notify()
            except Exception as exc:
                LOGGER.debug("Periodic IntellegyHUB resync failed: %s", exc)

    async def _run(self) -> None:
        backoffs = [1, 2, 5, 10]
        index = 0
        while not self._stopped.is_set():
            try:
                if not self.connected:
                    await self._sync_snapshot()
                    self.connected = True
                    self._notify()
                async for event in self.client.listen():
                    index = 0
                    self._handle_event(event)
                raise ConnectionError("WebSocket disconnected")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOGGER.debug("Backend listener disconnected: %s", exc)
                self.connected = False
                self._notify()
                delay = backoffs[min(index, len(backoffs) - 1)]
                index += 1
                try:
                    await asyncio.wait_for(self._stopped.wait(), timeout=delay)
                    return
                except asyncio.TimeoutError:
                    pass
                try:
                    await self._sync_snapshot()
                    self.connected = True
                    self._notify()
                except Exception as sync_exc:
                    LOGGER.debug("Backend resync failed: %s", sync_exc)

    @callback
    def _handle_event(self, event: dict) -> None:
        event_type = event.get("type")
        if event_type == "state":
            self.led_on = bool(event["led"]["on"])
            self.button_pressed = bool(event["button"]["pressed"])
            self.carrier = event.get("carrier", self.carrier)
            self.xport = event.get("xport", self.xport)
            self.extensions = event.get("extensions", self.extensions)
            self.onewire = event.get("onewire", self.onewire)
            self.connected = True
            self._remove_stale_xport_registry_entries()
        elif event_type == "led_changed" and isinstance(event.get("on"), bool):
            self.led_on = event["on"]
        elif event_type == "button_changed" and isinstance(event.get("pressed"), bool):
            self.button_pressed = event["pressed"]
        elif event_type == "xport_channel_changed":
            self._apply_xport_channel(event.get("channel"))
            self._remove_stale_xport_registry_entries()
        elif event_type == "extensions_changed":
            self.extensions = event.get("extensions", self.extensions)
            self._remove_stale_extension_registry_entries()
        elif event_type == "extension_module_changed":
            self._apply_extension_module(event.get("module"))
        elif event_type == "extension_module_removed" and isinstance(event.get("module_id"), str):
            self._remove_extension_module(event["module_id"])
            self._remove_extension_registry_entries(event["module_id"])
        elif event_type == "extension_power_changed" and isinstance(event.get("on"), bool):
            self.extensions.setdefault("power", {})["on"] = event["on"]
        elif event_type == "onewire_changed":
            self.onewire = event.get("onewire", self.onewire)
            self._remove_stale_onewire_registry_entries()
        elif event_type == "onewire_sensor_removed" and isinstance(event.get("sensor_id"), str):
            self._remove_onewire_sensor(event["sensor_id"])
            self._remove_onewire_registry_entries(event["sensor_id"])
        elif event_type == "onewire_power_changed" and isinstance(event.get("on"), bool):
            self.onewire.setdefault("power", {})["on"] = event["on"]
        else:
            LOGGER.debug("Ignoring unknown backend event: %s", event)
            return
        self._notify()

    @callback
    def _apply_xport_channel(self, channel: dict | None) -> None:
        if not isinstance(channel, dict):
            return
        channels = list(self.xport.get("channels", []))
        for index, item in enumerate(channels):
            if item.get("channel") == channel.get("channel"):
                channels[index] = channel
                break
        else:
            channels.append(channel)
        self.xport["channels"] = sorted(channels, key=lambda item: item.get("channel", 0))

    @callback
    def _apply_extension_module(self, module: dict | None) -> None:
        if not isinstance(module, dict):
            return
        modules = list(self.extensions.get("modules", []))
        for index, item in enumerate(modules):
            if item.get("id") == module.get("id"):
                modules[index] = module
                break
        else:
            modules.append(module)
        self.extensions["modules"] = sorted(modules, key=lambda item: item.get("id", ""))

    @callback
    def _remove_extension_module(self, module_id: str) -> None:
        modules = [
            module
            for module in self.extensions.get("modules", [])
            if module.get("id") != module_id
        ]
        self.extensions["modules"] = sorted(modules, key=lambda item: item.get("id", ""))

    @callback
    def _remove_onewire_sensor(self, sensor_id: str) -> None:
        sensors = [
            sensor
            for sensor in self.onewire.get("sensors", [])
            if sensor.get("id") != sensor_id
        ]
        self.onewire["sensors"] = sorted(sensors, key=lambda item: item.get("id", ""))

    @callback
    def _remove_extension_registry_entries(self, module_id: str) -> None:
        entity_registry = er.async_get(self.hass)
        if module_id.startswith("xdo8_"):
            for channel in range(1, 9):
                unique_id = f"intellegyhub_{module_id}_relay_{channel}"
                entity_id = entity_registry.async_get_entity_id("switch", DOMAIN, unique_id)
                if entity_id is not None:
                    entity_registry.async_remove(entity_id)
        if module_id.startswith("xdi16_"):
            for channel in range(1, 17):
                unique_id = f"intellegyhub_{module_id}_input_{channel}"
                entity_id = entity_registry.async_get_entity_id("binary_sensor", DOMAIN, unique_id)
                if entity_id is not None:
                    entity_registry.async_remove(entity_id)

        device_registry = dr.async_get(self.hass)
        device = device_registry.async_get_device_by_identifier((DOMAIN, module_id), self.entry_id)
        if device is not None:
            device_registry.async_remove_device(device.id)

    @callback
    def _remove_stale_extension_registry_entries(self) -> None:
        known_module_ids = {
            module.get("id")
            for module in self.extensions.get("modules", [])
            if isinstance(module.get("id"), str)
        }
        entity_registry = er.async_get(self.hass)
        entries = getattr(entity_registry, "entities", {})
        for entry in list(getattr(entries, "values", lambda: [])()):
            unique_id = getattr(entry, "unique_id", "")
            entity_id = getattr(entry, "entity_id", None)
            platform = getattr(entry, "platform", None)
            if platform != DOMAIN or entity_id is None:
                continue
            module_id = _xbus_module_id_from_unique_id(unique_id)
            if module_id is not None and module_id not in known_module_ids:
                entity_registry.async_remove(entity_id)

        device_registry = dr.async_get(self.hass)
        devices = getattr(device_registry, "devices", {})
        for device in list(getattr(devices, "values", lambda: [])()):
            identifiers = getattr(device, "identifiers", set())
            for domain, identifier in identifiers:
                if domain == DOMAIN and isinstance(identifier, str) and identifier.startswith(("xdo8_", "xdi16_")):
                    if identifier not in known_module_ids:
                        device_registry.async_remove_device(device.id)
                    break

    @callback
    def _remove_onewire_registry_entries(self, sensor_id: str) -> None:
        entity_registry = er.async_get(self.hass)
        unique_id = f"intellegyhub_{sensor_id}_temperature"
        entity_id = entity_registry.async_get_entity_id("sensor", DOMAIN, unique_id)
        if entity_id is not None:
            entity_registry.async_remove(entity_id)

    @callback
    def _remove_stale_onewire_registry_entries(self) -> None:
        known_sensor_ids = {
            sensor.get("id")
            for sensor in self.onewire.get("sensors", [])
            if sensor.get("added") is True and isinstance(sensor.get("id"), str)
        }
        entity_registry = er.async_get(self.hass)
        entries = getattr(entity_registry, "entities", {})
        for entry in list(getattr(entries, "values", lambda: [])()):
            unique_id = getattr(entry, "unique_id", "")
            entity_id = getattr(entry, "entity_id", None)
            platform = getattr(entry, "platform", None)
            if platform != DOMAIN or entity_id is None:
                continue
            sensor_id = _onewire_sensor_id_from_temperature_unique_id(unique_id)
            if sensor_id is not None and sensor_id not in known_sensor_ids:
                entity_registry.async_remove(entity_id)

        device_registry = dr.async_get(self.hass)
        devices = getattr(device_registry, "devices", {})
        for device in list(getattr(devices, "values", lambda: [])()):
            identifiers = getattr(device, "identifiers", set())
            for domain, identifier in identifiers:
                if domain == DOMAIN and isinstance(identifier, str) and identifier.startswith("ds18b20_"):
                    device_registry.async_remove_device(device.id)
                    break

    @callback
    def _remove_stale_xport_registry_entries(self) -> None:
        desired: set[tuple[str, str]] = set()
        for channel in range(1, 5):
            mode = self.xport_channel(channel).get("confirmed_mode")
            desired.update(xport_desired_unique_ids(channel, mode))

        entity_registry = er.async_get(self.hass)
        if not hasattr(entity_registry, "async_get_entity_id"):
            return
        for channel in range(1, 5):
            for platform, suffix in XPORT_ENTITY_KINDS:
                unique_id = f"intellegyhub_xport_x{channel}_{suffix}"
                if (xport_platform_value(platform), unique_id) in desired:
                    continue
                entity_id = entity_registry.async_get_entity_id(platform, DOMAIN, unique_id)
                if entity_id is not None:
                    entity_registry.async_remove(entity_id)

    @callback
    def _notify(self) -> None:
        for listener in list(self._listeners):
            listener()

    def _create_background_task(self, coro, name: str) -> asyncio.Task:
        create_background = getattr(self.hass, "async_create_background_task", None)
        if create_background is not None:
            try:
                return create_background(coro, name, eager_start=True)
            except TypeError:
                return create_background(coro, name)
        return self.hass.async_create_task(coro, name)


def _xbus_module_id_from_unique_id(unique_id: str) -> str | None:
    prefix = "intellegyhub_"
    if not unique_id.startswith(prefix):
        return None
    body = unique_id[len(prefix):]
    if "_relay_" in body:
        module_id, channel = body.rsplit("_relay_", 1)
        expected_prefix = "xdo8_"
        max_channel = 8
    elif "_input_" in body:
        module_id, channel = body.rsplit("_input_", 1)
        expected_prefix = "xdi16_"
        max_channel = 16
    else:
        return None
    if not module_id.startswith(expected_prefix):
        return None
    try:
        channel_number = int(channel)
    except ValueError:
        return None
    if channel_number < 1 or channel_number > max_channel:
        return None
    return module_id


def _onewire_sensor_id_from_temperature_unique_id(unique_id: str) -> str | None:
    prefix = "intellegyhub_"
    suffix = "_temperature"
    if not unique_id.startswith(prefix) or not unique_id.endswith(suffix):
        return None
    sensor_id = unique_id[len(prefix):-len(suffix)]
    if not sensor_id.startswith("ds18b20_"):
        return None
    return sensor_id
