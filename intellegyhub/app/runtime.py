from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import WebSocket

from .backends import BUTTONS, OUTPUTS, HardwareBackend, HardwareState
from .buzzer import BuzzerManager, BuzzerSettingsStore
from .carrier import CarrierManager
from .extensions import ExtensionHardware, ExtensionManager
from .onewire import OneWireHardware, OneWireManager
from .supervisor import shutdown_host
from .ui_settings import UiSettings, UiSettingsStore, UiTheme
from .xport import XPortManager

LOGGER = logging.getLogger(__name__)


class AppRuntime:
    def __init__(
        self,
        backend: HardwareBackend,
        startup_buzzer_enabled: bool = False,
        startup_buzzer_frequency: int = 2000,
        startup_buzzer_duration_ms: int = 200,
        shutdown_buzzer_enabled: bool = True,
        shutdown_buzzer_frequency: int = 2000,
        shutdown_buzzer_duration_ms: int = 200,
        shutdown_buzzer_volume_percent: int = 50,
        carrier_monitoring_poll_interval_seconds: int = 30,
        onewire_poll_intervals: dict[str, int] | None = None,
        power_button_shutdown_enabled: bool = True,
        power_button_shutdown_hold_seconds: float = 1.0,
        shutdown_host_callback: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.backend = backend
        self.carrier = CarrierManager(monitoring_interval_seconds=carrier_monitoring_poll_interval_seconds)
        self.buzzer = BuzzerManager()
        self.buzzer_store = BuzzerSettingsStore()
        self.xport = XPortManager()
        self.extensions = ExtensionManager(hardware=ExtensionHardware(self.carrier))
        self.onewire = OneWireManager(hardware=OneWireHardware(self.carrier), poll_intervals=onewire_poll_intervals)
        self.ui_store = UiSettingsStore()
        self.ui_settings = UiSettings()
        self.state = HardwareState()
        self.ready = False
        self.error: str | None = "startup pending"
        self._lock = asyncio.Lock()
        self._clients: set[WebSocket] = set()
        self._startup_buzzer_enabled = startup_buzzer_enabled
        self._startup_buzzer_frequency = startup_buzzer_frequency
        self._startup_buzzer_duration_ms = startup_buzzer_duration_ms
        self._shutdown_buzzer_enabled = shutdown_buzzer_enabled
        self._shutdown_buzzer_frequency = shutdown_buzzer_frequency
        self._shutdown_buzzer_duration_ms = shutdown_buzzer_duration_ms
        self._shutdown_buzzer_volume_percent = shutdown_buzzer_volume_percent
        self._started_at = time.monotonic()
        self._power_button_shutdown_enabled = power_button_shutdown_enabled
        self._power_button_shutdown_hold_seconds = power_button_shutdown_hold_seconds
        self._shutdown_host_callback = shutdown_host_callback or shutdown_host
        self._power_shutdown_task: asyncio.Task[None] | None = None
        self._power_shutdown_requested = False

    async def start(self) -> None:
        try:
            await self.ui_store.initialize()
            self.ui_settings = await self.ui_store.load()
            await self.buzzer_store.initialize()
            self.buzzer.apply_settings(await self.buzzer_store.load())
            self.state = await self.backend.start(self.handle_button_changed)
            self.carrier.set_publisher(self.broadcast)
            self.xport.set_publisher(self.broadcast)
            self.extensions.set_publisher(self.broadcast)
            self.onewire.set_publisher(self.broadcast)
            await self.carrier.start()
            await self.xport.start()
            await self.extensions.start()
            await self.onewire.start()
            self.ready = True
            self.error = None
            await self._play_startup_buzzer()
        except Exception as exc:
            self.ready = False
            self.error = str(exc)
            LOGGER.exception("Hardware startup failed")

    async def _play_startup_buzzer(self) -> None:
        if not self._startup_buzzer_enabled:
            return
        saved_settings = self.buzzer.settings()
        try:
            await self.buzzer.play_pwm_once(
                self._startup_buzzer_frequency,
                self._startup_buzzer_duration_ms,
                0.5,
            )
            LOGGER.info(
                "Startup buzzer played: frequency=%sHz duration_ms=%s",
                self._startup_buzzer_frequency,
                self._startup_buzzer_duration_ms,
            )
        except Exception as exc:
            LOGGER.warning("Startup buzzer failed: %s", exc)
        finally:
            self.buzzer.apply_settings(saved_settings)

    async def _play_power_shutdown_buzzer(self) -> None:
        if not self._shutdown_buzzer_enabled or self._shutdown_buzzer_volume_percent <= 0:
            return
        saved_settings = self.buzzer.settings()
        try:
            await self.buzzer.play_pwm_once(
                self._shutdown_buzzer_frequency,
                self._shutdown_buzzer_duration_ms,
                volume_percent=self._shutdown_buzzer_volume_percent,
            )
            await self.broadcast({"type": "power_button_shutdown_buzzer_played"})
            LOGGER.info(
                "Power button shutdown buzzer played: frequency=%sHz duration_ms=%s volume=%s%%",
                self._shutdown_buzzer_frequency,
                self._shutdown_buzzer_duration_ms,
                self._shutdown_buzzer_volume_percent,
            )
        except Exception as exc:
            LOGGER.warning("Power button shutdown buzzer failed: %s", exc)
        finally:
            self.buzzer.apply_settings(saved_settings)

    async def stop(self) -> None:
        self.ready = False
        if self._power_shutdown_task:
            self._power_shutdown_task.cancel()
            try:
                await self._power_shutdown_task
            except asyncio.CancelledError:
                pass
            self._power_shutdown_task = None
        await self.buzzer.stop()
        await self.onewire.stop()
        await self.extensions.stop()
        await self.xport.stop()
        await self.carrier.stop()
        await self.backend.stop()
        clients = list(self._clients)
        for ws in clients:
            try:
                await ws.close()
            except Exception:
                pass
        self._clients.clear()

    def snapshot(self) -> dict[str, Any]:
        outputs = self.state.outputs or {output_id: False for output_id in OUTPUTS}
        buttons = self.state.buttons or {button_id: False for button_id in BUTTONS}
        return {
            "led": {"on": outputs["user_led"]},
            "outputs": {
                output_id: {
                    "id": output_id,
                    "name": str(definition["name"]),
                    "gpio": int(definition["gpio"]),
                    "on": bool(outputs.get(output_id, False)),
                }
                for output_id, definition in OUTPUTS.items()
            },
            "button": {"pressed": bool(buttons.get("fn2", self.state.button_pressed))},
            "buttons": {
                button_id: {
                    "id": button_id,
                    "name": str(definition["name"]),
                    "gpio": int(definition["gpio"]),
                    "pressed": bool(buttons.get(button_id, False)),
                }
                for button_id, definition in BUTTONS.items()
            },
            "carrier": self.carrier.snapshot(),
            "app": {
                "uptime_seconds": max(0, int(time.monotonic() - self._started_at)),
                "power_button_shutdown_enabled": self._power_button_shutdown_enabled,
                "power_button_shutdown_hold_seconds": self._power_button_shutdown_hold_seconds,
                "power_button_shutdown_requested": self._power_shutdown_requested,
                "shutdown_buzzer_enabled": self._shutdown_buzzer_enabled,
                "shutdown_buzzer_frequency": self._shutdown_buzzer_frequency,
                "shutdown_buzzer_duration_ms": self._shutdown_buzzer_duration_ms,
                "shutdown_buzzer_volume_percent": self._shutdown_buzzer_volume_percent,
            },
            "xport": self.xport.snapshot(),
            "extensions": self.extensions.snapshot(),
            "onewire": self.onewire.snapshot(),
            "buzzer": self.buzzer.status(),
            "ui": self.ui_settings.snapshot(),
        }

    async def async_snapshot(self) -> dict[str, Any]:
        await self.carrier.refresh_faults()
        await self.carrier.refresh_monitoring()
        return self.snapshot()

    async def set_led(self, on: bool) -> bool:
        return await self.set_output("user_led", on)

    async def set_output(self, output_id: str, on: bool) -> bool:
        if output_id not in OUTPUTS:
            raise ValueError(f"Unknown output: {output_id}")
        async with self._lock:
            confirmed = await self.backend.set_output(output_id, on)
            if self.state.outputs is None:
                self.state.outputs = {item: False for item in OUTPUTS}
            previous = bool(self.state.outputs.get(output_id, False))
            self.state.outputs[output_id] = confirmed
            self.state.led_on = bool(self.state.outputs["user_led"])
            if previous != confirmed:
                if output_id == "user_led":
                    await self.broadcast({"type": "led_changed", "on": confirmed})
                event = {
                    "type": "output_changed",
                    "output": {
                        "id": output_id,
                        "name": str(OUTPUTS[output_id]["name"]),
                        "gpio": int(OUTPUTS[output_id]["gpio"]),
                        "on": confirmed,
                    },
                }
                await self.broadcast(event)
            return confirmed

    async def set_buzzer_volume(self, volume_percent: int) -> dict:
        return await self.set_buzzer_settings(volume_percent=volume_percent)

    async def set_buzzer_settings(
        self,
        frequency: int | None = None,
        duration_ms: int | None = None,
        volume_percent: int | None = None,
    ) -> dict:
        status = await self.buzzer.set_settings(frequency, duration_ms, volume_percent)
        await self.buzzer_store.save(self.buzzer.settings())
        await self.broadcast({"type": "buzzer_changed", "buzzer": status})
        return status

    async def set_ui_theme(self, theme: UiTheme) -> dict[str, str]:
        self.ui_settings = UiSettings(theme=theme)
        await self.ui_store.save(self.ui_settings)
        payload = self.ui_settings.snapshot()
        await self.broadcast({"type": "ui_changed", "ui": payload})
        return payload

    async def handle_button_changed(self, button_id: str, pressed: bool) -> None:
        if button_id not in BUTTONS:
            return
        async with self._lock:
            if self.state.buttons is None:
                self.state.buttons = {item: False for item in BUTTONS}
            if bool(self.state.buttons.get(button_id, False)) == pressed:
                return
            self.state.buttons[button_id] = pressed
            self.state.button_pressed = bool(self.state.buttons.get("fn2", False))
            await self.broadcast(
                {
                    "type": "button_changed",
                    "id": button_id,
                    "name": str(BUTTONS[button_id]["name"]),
                    "gpio": int(BUTTONS[button_id]["gpio"]),
                    "pressed": pressed,
                }
            )
        self._handle_power_button_shutdown_hold(button_id, pressed)

    def _handle_power_button_shutdown_hold(self, button_id: str, pressed: bool) -> None:
        if button_id != "power":
            return
        if not self._power_button_shutdown_enabled:
            return
        if pressed:
            if self._power_shutdown_task and not self._power_shutdown_task.done():
                return
            self._power_shutdown_task = asyncio.create_task(self._request_shutdown_after_power_hold())
            return
        if self._power_shutdown_task and not self._power_shutdown_task.done():
            self._power_shutdown_task.cancel()

    async def _request_shutdown_after_power_hold(self) -> None:
        try:
            await asyncio.sleep(self._power_button_shutdown_hold_seconds)
            if not bool((self.state.buttons or {}).get("power", False)):
                return
            self._power_shutdown_requested = True
            event = {
                "type": "power_button_shutdown_requested",
                "hold_seconds": self._power_button_shutdown_hold_seconds,
            }
            await self.broadcast(event)
            LOGGER.warning(
                "Power button held for %.1fs; requesting Supervisor host shutdown",
                self._power_button_shutdown_hold_seconds,
            )
            await self._play_power_shutdown_buzzer()
            await self._shutdown_host_callback()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOGGER.exception("Power button shutdown request failed")
            await self.broadcast({"type": "power_button_shutdown_failed", "error": str(exc)})

    async def add_client(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._clients.add(websocket)
        await websocket.send_json({"type": "state", **self.snapshot()})

    def remove_client(self, websocket: WebSocket) -> None:
        self._clients.discard(websocket)

    async def broadcast(self, message: dict[str, Any]) -> None:
        stale = []
        for websocket in list(self._clients):
            try:
                await websocket.send_json(message)
            except Exception:
                stale.append(websocket)
        for websocket in stale:
            self.remove_client(websocket)
