from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import WebSocket

from .backends import HardwareBackend, HardwareState
from .buzzer import BuzzerManager
from .carrier import CarrierManager
from .extensions import ExtensionHardware, ExtensionManager
from .onewire import OneWireHardware, OneWireManager
from .xport import XPortManager

LOGGER = logging.getLogger(__name__)


class AppRuntime:
    def __init__(
        self,
        backend: HardwareBackend,
        startup_buzzer_enabled: bool = False,
        startup_buzzer_frequency: int = 2000,
        startup_buzzer_duration_ms: int = 200,
        onewire_poll_intervals: dict[str, int] | None = None,
    ) -> None:
        self.backend = backend
        self.carrier = CarrierManager()
        self.buzzer = BuzzerManager()
        self.xport = XPortManager()
        self.extensions = ExtensionManager(hardware=ExtensionHardware(self.carrier))
        self.onewire = OneWireManager(hardware=OneWireHardware(self.carrier), poll_intervals=onewire_poll_intervals)
        self.state = HardwareState()
        self.ready = False
        self.error: str | None = "startup pending"
        self._lock = asyncio.Lock()
        self._clients: set[WebSocket] = set()
        self._startup_buzzer_enabled = startup_buzzer_enabled
        self._startup_buzzer_frequency = startup_buzzer_frequency
        self._startup_buzzer_duration_ms = startup_buzzer_duration_ms

    async def start(self) -> None:
        try:
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

    async def stop(self) -> None:
        self.ready = False
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
        return {
            "led": {"on": self.state.led_on},
            "button": {"pressed": self.state.button_pressed},
            "carrier": self.carrier.snapshot(),
            "xport": self.xport.snapshot(),
            "extensions": self.extensions.snapshot(),
            "onewire": self.onewire.snapshot(),
        }

    async def async_snapshot(self) -> dict[str, Any]:
        await self.carrier.refresh_faults()
        return self.snapshot()

    async def set_led(self, on: bool) -> bool:
        async with self._lock:
            confirmed = await self.backend.set_led(on)
            if self.state.led_on != confirmed:
                self.state.led_on = confirmed
                await self.broadcast({"type": "led_changed", "on": confirmed})
            return confirmed

    async def handle_button_changed(self, pressed: bool) -> None:
        async with self._lock:
            if self.state.button_pressed == pressed:
                return
            self.state.button_pressed = pressed
            await self.broadcast({"type": "button_changed", "pressed": pressed})

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
