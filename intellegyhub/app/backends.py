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
OUTPUTS = {
    "user_led": {"name": "USR", "gpio": 22, "active_low": False},
    "ste": {"name": "STE", "gpio": 19, "active_low": False},
    "err": {"name": "ERR", "gpio": 20, "active_low": False},
    "net": {"name": "NET", "gpio": 21, "active_low": False},
}


@dataclass
class HardwareState:
    led_on: bool = False
    button_pressed: bool = False
    outputs: dict[str, bool] | None = None

    def __post_init__(self) -> None:
        if self.outputs is None:
            self.outputs = {output_id: False for output_id in OUTPUTS}


class HardwareBackend(ABC):
    @abstractmethod
    async def start(self, on_button_changed: ButtonCallback) -> HardwareState: ...

    @abstractmethod
    async def set_led(self, on: bool) -> bool: ...

    @abstractmethod
    async def set_output(self, output_id: str, on: bool) -> bool: ...

    @abstractmethod
    async def read_button(self) -> bool: ...

    @abstractmethod
    async def stop(self) -> None: ...


class MockGpioBackend(HardwareBackend):
    def __init__(self) -> None:
        self.led_on = False
        self.outputs = {output_id: False for output_id in OUTPUTS}
        self.button_pressed = False
        self._callback: ButtonCallback | None = None

    async def start(self, on_button_changed: ButtonCallback) -> HardwareState:
        self._callback = on_button_changed
        self.led_on = False
        self.outputs = {output_id: False for output_id in OUTPUTS}
        return HardwareState(led_on=False, button_pressed=self.button_pressed, outputs=dict(self.outputs))

    async def set_led(self, on: bool) -> bool:
        return await self.set_output("user_led", on)

    async def set_output(self, output_id: str, on: bool) -> bool:
        if output_id not in OUTPUTS:
            raise ValueError(f"Unknown output: {output_id}")
        self.outputs[output_id] = on
        self.led_on = self.outputs["user_led"]
        return self.outputs[output_id]

    async def read_button(self) -> bool:
        return self.button_pressed

    async def simulate_button(self, pressed: bool) -> None:
        self.button_pressed = pressed
        if self._callback:
            await self._callback(pressed)

    async def stop(self) -> None:
        self.led_on = False
        self.outputs = {output_id: False for output_id in OUTPUTS}


class RealGpiodBackend(HardwareBackend):
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self._chip: Any = None
        self._output_request: Any = None
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
        outputs = {}
        for output_id in OUTPUTS:
            outputs[output_id] = await self.set_output(output_id, False)
        pressed = await self.read_button()
        self._thread = threading.Thread(target=self._watch_edges, name="intellegy-gpio-edge", daemon=True)
        self._thread.start()
        return HardwareState(led_on=outputs["user_led"], button_pressed=pressed, outputs=outputs)

    def _open_lines(self) -> None:
        import gpiod

        self._chip = gpiod.Chip(self.config.chip_path)
        settings_cls = gpiod.LineSettings
        direction = gpiod.line.Direction
        edge = gpiod.line.Edge
        bias = gpiod.line.Bias
        bias_map = {
            "pull_up": bias.PULL_UP,
            "pull_down": bias.PULL_DOWN,
            "disabled": bias.DISABLED,
        }
        output_config = {}
        for output_id, definition in OUTPUTS.items():
            gpio = self.config.led_gpio if output_id == "user_led" else int(definition["gpio"])
            output_config[gpio] = settings_cls(
                direction=direction.OUTPUT,
                output_value=self._physical_output_value(output_id, False),
            )
        self._output_request = self._chip.request_lines(
            consumer="intellegyhub-gpio-outputs",
            config=output_config,
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

    def _physical_output_value(self, output_id: str, logical_on: bool) -> Any:
        import gpiod

        definition = OUTPUTS[output_id]
        active_low = self.config.led_active_low if output_id == "user_led" else bool(definition["active_low"])
        electrical = not logical_on if active_low else logical_on
        return gpiod.line.Value.ACTIVE if electrical else gpiod.line.Value.INACTIVE

    def _logical_pressed_from_value(self, value: Any) -> bool:
        import gpiod

        electrical_active = value == gpiod.line.Value.ACTIVE
        return not electrical_active if self.config.button_active_low else electrical_active

    async def set_led(self, on: bool) -> bool:
        return await self.set_output("user_led", on)

    async def set_output(self, output_id: str, on: bool) -> bool:
        if output_id not in OUTPUTS:
            raise ValueError(f"Unknown output: {output_id}")
        if self._output_request is None:
            raise RuntimeError("Output lines are not initialized")
        gpio = self.config.led_gpio if output_id == "user_led" else int(OUTPUTS[output_id]["gpio"])
        await asyncio.to_thread(self._output_request.set_value, gpio, self._physical_output_value(output_id, on))
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
        if self._output_request:
            try:
                for output_id in OUTPUTS:
                    await self.set_output(output_id, False)
            except Exception:
                pass
            self._output_request.release()
        if self._button_request:
            self._button_request.release()
        if self._chip:
            self._chip.close()
