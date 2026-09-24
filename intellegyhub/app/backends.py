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

ButtonCallback = Callable[[str, bool], Awaitable[None]]
OUTPUTS = {
    "ste": {"name": "STE LED", "gpio": 19, "active_low": False},
    "err": {"name": "ERR LED", "gpio": 20, "active_low": False},
    "net": {"name": "NET LED", "gpio": 21, "active_low": False},
    "user_led": {"name": "USR LED", "gpio": 22, "active_low": False},
}
BUTTONS = {
    "power": {"name": "Power", "gpio": 17, "active_low": True},
    "fn1": {"name": "FN1", "gpio": 27, "active_low": True},
    "fn2": {"name": "FN2", "gpio": 26, "active_low": True},
}


@dataclass
class HardwareState:
    led_on: bool = False
    button_pressed: bool = False
    outputs: dict[str, bool] | None = None
    buttons: dict[str, bool] | None = None

    def __post_init__(self) -> None:
        if self.outputs is None:
            self.outputs = {output_id: False for output_id in OUTPUTS}
        if self.buttons is None:
            self.buttons = {button_id: False for button_id in BUTTONS}
        self.button_pressed = bool(self.buttons.get("fn2", self.button_pressed))


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
    async def read_buttons(self) -> dict[str, bool]: ...

    @abstractmethod
    async def stop(self) -> None: ...


class MockGpioBackend(HardwareBackend):
    def __init__(self) -> None:
        self.led_on = False
        self.outputs = {output_id: False for output_id in OUTPUTS}
        self.buttons = {button_id: False for button_id in BUTTONS}
        self.button_pressed = False
        self._callback: ButtonCallback | None = None

    async def start(self, on_button_changed: ButtonCallback) -> HardwareState:
        self._callback = on_button_changed
        self.led_on = False
        self.outputs = {output_id: False for output_id in OUTPUTS}
        self.buttons = {button_id: False for button_id in BUTTONS}
        self.button_pressed = self.buttons["fn2"]
        return HardwareState(
            led_on=False,
            button_pressed=self.button_pressed,
            outputs=dict(self.outputs),
            buttons=dict(self.buttons),
        )

    async def set_led(self, on: bool) -> bool:
        return await self.set_output("user_led", on)

    async def set_output(self, output_id: str, on: bool) -> bool:
        if output_id not in OUTPUTS:
            raise ValueError(f"Unknown output: {output_id}")
        self.outputs[output_id] = on
        self.led_on = self.outputs["user_led"]
        return self.outputs[output_id]

    async def read_button(self) -> bool:
        return self.buttons["fn2"]

    async def read_buttons(self) -> dict[str, bool]:
        return dict(self.buttons)

    async def simulate_button(self, pressed: bool, button_id: str = "fn2") -> None:
        if button_id not in BUTTONS:
            raise ValueError(f"Unknown button: {button_id}")
        self.buttons[button_id] = pressed
        self.button_pressed = self.buttons["fn2"]
        if self._callback:
            await self._callback(button_id, pressed)

    async def stop(self) -> None:
        self.led_on = False
        self.outputs = {output_id: False for output_id in OUTPUTS}
        self.buttons = {button_id: False for button_id in BUTTONS}


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
        buttons = await self.read_buttons()
        pressed = buttons["fn2"]
        self._thread = threading.Thread(target=self._watch_edges, name="intellegy-gpio-edge", daemon=True)
        self._thread.start()
        return HardwareState(led_on=outputs["user_led"], button_pressed=pressed, outputs=outputs, buttons=buttons)

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
        button_config = {}
        for button_id, definition in BUTTONS.items():
            gpio = self.config.button_gpio if button_id == "fn2" else int(definition["gpio"])
            button_config[gpio] = settings_cls(
                    direction=direction.INPUT,
                    edge_detection=edge.BOTH,
                    bias=bias_map[self.config.button_bias],
                )
        self._button_request = self._chip.request_lines(
            consumer="intellegyhub-gpio-buttons",
            config=button_config,
        )

    def _physical_output_value(self, output_id: str, logical_on: bool) -> Any:
        import gpiod

        definition = OUTPUTS[output_id]
        active_low = self.config.led_active_low if output_id == "user_led" else bool(definition["active_low"])
        electrical = not logical_on if active_low else logical_on
        return gpiod.line.Value.ACTIVE if electrical else gpiod.line.Value.INACTIVE

    def _logical_pressed_from_value(self, button_id: str, value: Any) -> bool:
        import gpiod

        electrical_active = value == gpiod.line.Value.ACTIVE
        active_low = self.config.button_active_low if button_id == "fn2" else bool(BUTTONS[button_id]["active_low"])
        return not electrical_active if active_low else electrical_active

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
        return (await self.read_buttons())["fn2"]

    async def read_buttons(self) -> dict[str, bool]:
        if self._button_request is None:
            raise RuntimeError("Button line is not initialized")
        result = {}
        for button_id, definition in BUTTONS.items():
            gpio = self.config.button_gpio if button_id == "fn2" else int(definition["gpio"])
            value = await asyncio.to_thread(self._button_request.get_value, gpio)
            result[button_id] = self._logical_pressed_from_value(button_id, value)
        return result

    def _watch_edges(self) -> None:
        while not self._stop.is_set():
            try:
                if not self._button_request.wait_edge_events(timeout=0.2):
                    continue
                events = self._button_request.read_edge_events()
                time.sleep(self.config.button_debounce_ms / 1000)
                now = time.monotonic()
                if now - self._last_emit < self.config.button_debounce_ms / 1000:
                    continue
                self._last_emit = now
                changed_offsets = {_event_line_offset(event) for event in events}
                for button_id, definition in BUTTONS.items():
                    gpio = self.config.button_gpio if button_id == "fn2" else int(definition["gpio"])
                    if gpio not in changed_offsets:
                        continue
                    pressed = self._logical_pressed_from_value(button_id, self._button_request.get_value(gpio))
                    if self._loop and self._callback:
                        asyncio.run_coroutine_threadsafe(self._callback(button_id, pressed), self._loop)
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


def _event_line_offset(event: Any) -> int | None:
    line_offset = getattr(event, "line_offset", None)
    return line_offset() if callable(line_offset) else line_offset
