from __future__ import annotations

import asyncio
import os
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from .config import AppConfig

ButtonCallback = Callable[[bool], Awaitable[None]]


@dataclass
class HardwareState:
    led_on: bool = False
    button_pressed: bool = False


class HardwareBackend(ABC):
    @abstractmethod
    async def start(self, on_button_changed: ButtonCallback) -> HardwareState: ...

    @abstractmethod
    async def set_led(self, on: bool) -> bool: ...

    @abstractmethod
    async def read_button(self) -> bool: ...

    @abstractmethod
    async def stop(self) -> None: ...


class MockGpioBackend(HardwareBackend):
    def __init__(self) -> None:
        self.led_on = False
        self.button_pressed = False
        self._callback: ButtonCallback | None = None

    async def start(self, on_button_changed: ButtonCallback) -> HardwareState:
        self._callback = on_button_changed
        self.led_on = False
        return HardwareState(led_on=False, button_pressed=self.button_pressed)

    async def set_led(self, on: bool) -> bool:
        self.led_on = on
        return self.led_on

    async def read_button(self) -> bool:
        return self.button_pressed

    async def simulate_button(self, pressed: bool) -> None:
        self.button_pressed = pressed
        if self._callback:
            await self._callback(pressed)

    async def stop(self) -> None:
        self.led_on = False


class RealGpiodBackend(HardwareBackend):
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self._chip: Any = None
        self._led_request: Any = None
        self._button_request: Any = None
        self._callback: ButtonCallback | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_emit = 0.0

    async def start(self, on_button_changed: ButtonCallback) -> HardwareState:
        if not os.path.exists(self.config.chip_path):
            raise RuntimeError(f"{self.config.chip_path} not available")
        self._callback = on_button_changed
        self._loop = asyncio.get_running_loop()
        await asyncio.to_thread(self._open_lines)
        await self.set_led(False)
        pressed = await self.read_button()
        self._thread = threading.Thread(target=self._watch_edges, name="intellegy-gpio-edge", daemon=True)
        self._thread.start()
        return HardwareState(led_on=False, button_pressed=pressed)

    def _open_lines(self) -> None:
        import gpiod

        self._chip = gpiod.Chip(self.config.chip_path)
        settings_cls = gpiod.LineSettings
        direction = gpiod.line.Direction
        edge = gpiod.line.Edge
        bias = gpiod.line.Bias
        value = gpiod.line.Value
        bias_map = {
            "pull_up": bias.PULL_UP,
            "pull_down": bias.PULL_DOWN,
            "disabled": bias.DISABLED,
        }
        self._led_request = self._chip.request_lines(
            consumer="intellegyhub-gpio-led",
            config={
                self.config.led_gpio: settings_cls(
                    direction=direction.OUTPUT,
                    output_value=self._physical_value(False),
                )
            },
        )
        self._button_request = self._chip.request_lines(
            consumer="intellegyhub-gpio-button",
            config={
                self.config.button_gpio: settings_cls(
                    direction=direction.INPUT,
                    edge_detection=edge.BOTH,
                    bias=bias_map[self.config.button_bias],
                )
            },
        )
    def _physical_value(self, logical_on: bool) -> Any:
        import gpiod

        electrical = not logical_on if self.config.led_active_low else logical_on
        return gpiod.line.Value.ACTIVE if electrical else gpiod.line.Value.INACTIVE

    def _logical_pressed_from_value(self, value: Any) -> bool:
        import gpiod

        electrical_active = value == gpiod.line.Value.ACTIVE
        return not electrical_active if self.config.button_active_low else electrical_active

    async def set_led(self, on: bool) -> bool:
        if self._led_request is None:
            raise RuntimeError("LED line is not initialized")
        await asyncio.to_thread(self._led_request.set_value, self.config.led_gpio, self._physical_value(on))
        return on

    async def read_button(self) -> bool:
        if self._button_request is None:
            raise RuntimeError("Button line is not initialized")
        value = await asyncio.to_thread(self._button_request.get_value, self.config.button_gpio)
        return self._logical_pressed_from_value(value)

    def _watch_edges(self) -> None:
        while not self._stop.is_set():
            try:
                if not self._button_request.wait_edge_events(timeout=0.2):
                    continue
                self._button_request.read_edge_events()
                time.sleep(self.config.button_debounce_ms / 1000)
                pressed = self._logical_pressed_from_value(self._button_request.get_value(self.config.button_gpio))
                now = time.monotonic()
                if now - self._last_emit < self.config.button_debounce_ms / 1000:
                    continue
                self._last_emit = now
                if self._loop and self._callback:
                    asyncio.run_coroutine_threadsafe(self._callback(pressed), self._loop)
            except Exception:
                time.sleep(1)

    async def stop(self) -> None:
        self._stop.set()
        if self._thread:
            await asyncio.to_thread(self._thread.join, 2)
        if self._led_request:
            try:
                await self.set_led(False)
            except Exception:
                pass
            self._led_request.release()
        if self._button_request:
            self._button_request.release()
        if self._chip:
            self._chip.close()
