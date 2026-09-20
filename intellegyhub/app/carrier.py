from __future__ import annotations

import asyncio
import errno
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

I2C_SLAVE = 0x0703


@dataclass
class FaultLineState:
    id: str
    name: str
    gpio: int
    active: bool
    available: bool
    error: str | None = None


class CarrierHardware:
    bus = 10
    address = 0x20
    gpiochip_path = "/dev/gpiochip0"

    _iodira = 0x00
    _iodirb = 0x01
    _gpioa = 0x12
    _gpiob = 0x13

    bit_onewire_power = 14
    bit_xbus_power = 15
    fault_onewire_gpio = 16
    fault_xbus_gpio = 23

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._gpio_a = 0
        self._gpio_b = 0
        self._available = False

    async def initialize(self) -> bool:
        if _is_mock_platform():
            self._available = False
            return False
        async with self._lock:
            self._available = await asyncio.to_thread(self._initialize_sync)
            return self._available

    async def set_bit(self, bit: int, on: bool) -> bool:
        if _is_mock_platform():
            return on
        async with self._lock:
            if not self._available:
                self._available = await asyncio.to_thread(self._initialize_sync)
            if bit < 8:
                if on:
                    self._gpio_a |= 1 << bit
                else:
                    self._gpio_a &= ~(1 << bit)
            else:
                offset = bit - 8
                if on:
                    self._gpio_b |= 1 << offset
                else:
                    self._gpio_b &= ~(1 << offset)
            await asyncio.to_thread(self._write_outputs)
            return on

    async def bit_on(self, bit: int) -> bool:
        if _is_mock_platform():
            return False
        async with self._lock:
            if not self._available:
                self._available = await asyncio.to_thread(self._initialize_sync)
            if bit < 8:
                return bool(self._gpio_a & (1 << bit))
            return bool(self._gpio_b & (1 << (bit - 8)))

    async def fault_lines(self) -> list[FaultLineState]:
        if _is_mock_platform():
            return [
                FaultLineState("onewire_power_fault", "1-Wire Power Fault", self.fault_onewire_gpio, False, False, "mock"),
                FaultLineState("xbus_power_fault", "X-Bus Power Fault", self.fault_xbus_gpio, False, False, "mock"),
            ]
        return await asyncio.to_thread(self._read_fault_lines)

    def _initialize_sync(self) -> bool:
        if not self._probe_register(self._iodirb, 1):
            return False
        try:
            self._gpio_a = self._i2c_read_register(self._gpioa, 1)[0]
            self._gpio_b = self._i2c_read_register(self._gpiob, 1)[0]
        except OSError:
            self._gpio_a = 0
            self._gpio_b = 0
        self._i2c_write(bytes([self._iodira, 0x00]))
        self._i2c_write(bytes([self._iodirb, 0x00]))
        self._write_outputs()
        return True

    def _write_outputs(self) -> None:
        self._i2c_write(bytes([self._gpioa, self._gpio_a & 0xFF]))
        self._i2c_write(bytes([self._gpiob, self._gpio_b & 0xFF]))

    def _read_fault_lines(self) -> list[FaultLineState]:
        return [
            self._read_fault_line("onewire_power_fault", "1-Wire Power Fault", self.fault_onewire_gpio),
            self._read_fault_line("xbus_power_fault", "X-Bus Power Fault", self.fault_xbus_gpio),
        ]

    def _read_fault_line(self, fault_id: str, name: str, gpio: int) -> FaultLineState:
        try:
            import gpiod

            settings = gpiod.LineSettings(
                direction=gpiod.line.Direction.INPUT,
                bias=gpiod.line.Bias.DISABLED,
            )
            logical_active_low = False
            if hasattr(settings, "active_low"):
                settings.active_low = True
                logical_active_low = True
            with gpiod.Chip(self.gpiochip_path) as chip:
                request = chip.request_lines(
                    consumer=f"intellegyhub-{fault_id}",
                    config={gpio: settings},
                )
                try:
                    value = request.get_value(gpio)
                finally:
                    request.release()
            active = value == (gpiod.line.Value.ACTIVE if logical_active_low else gpiod.line.Value.INACTIVE)
            return FaultLineState(fault_id, name, gpio, active, True)
        except Exception as exc:
            return FaultLineState(fault_id, name, gpio, False, False, str(exc))

    def _probe_register(self, register: int, length: int) -> bool:
        try:
            self._i2c_read_register(register, length)
            return True
        except FileNotFoundError:
            return False
        except OSError as exc:
            if exc.errno in {errno.ENXIO, errno.EREMOTEIO, errno.EIO, errno.ETIMEDOUT}:
                return False
            raise

    def _i2c_write(self, payload: bytes) -> None:
        import fcntl

        with Path(f"/dev/i2c-{self.bus}").open("rb+", buffering=0) as device:
            fcntl.ioctl(device, I2C_SLAVE, self.address)
            os.write(device.fileno(), payload)

    def _i2c_read_register(self, register: int, length: int) -> bytes:
        import fcntl

        with Path(f"/dev/i2c-{self.bus}").open("rb+", buffering=0) as device:
            fcntl.ioctl(device, I2C_SLAVE, self.address)
            os.write(device.fileno(), bytes([register]))
            return os.read(device.fileno(), length)


class CarrierManager:
    def __init__(self, hardware: CarrierHardware | None = None) -> None:
        self.hardware = hardware or CarrierHardware()
        self.available = False
        self.error: str | None = "startup pending"
        self.faults: dict[str, FaultLineState] = {}
        self._publisher: Callable[[dict[str, Any]], Any] | None = None
        self._fault_task: asyncio.Task | None = None
        self._stopped = asyncio.Event()

    def set_publisher(self, publisher: Callable[[dict[str, Any]], Any]) -> None:
        self._publisher = publisher

    async def start(self) -> None:
        try:
            self._stopped.clear()
            self.available = await self.hardware.initialize()
            self.faults = {fault.id: fault for fault in await self.hardware.fault_lines()}
            self.error = None if self.available else "carrier MCP23017 unavailable"
            self._fault_task = asyncio.create_task(self._monitor_faults(), name="intellegyhub_carrier_faults")
        except Exception as exc:
            self.available = False
            self.error = str(exc)

    async def stop(self) -> None:
        self._stopped.set()
        if self._fault_task:
            self._fault_task.cancel()
            try:
                await self._fault_task
            except asyncio.CancelledError:
                pass
            self._fault_task = None

    async def set_xbus_power(self, on: bool) -> bool:
        return await self.hardware.set_bit(CarrierHardware.bit_xbus_power, on)

    async def xbus_power_on(self) -> bool:
        return await self.hardware.bit_on(CarrierHardware.bit_xbus_power)

    async def set_onewire_power(self, on: bool) -> bool:
        return await self.hardware.set_bit(CarrierHardware.bit_onewire_power, on)

    async def onewire_power_on(self) -> bool:
        return await self.hardware.bit_on(CarrierHardware.bit_onewire_power)

    async def refresh_faults(self) -> dict[str, Any]:
        self.faults = {fault.id: fault for fault in await self.hardware.fault_lines()}
        return self.snapshot()

    async def _monitor_faults(self) -> None:
        while not self._stopped.is_set():
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=0.25)
                return
            except asyncio.TimeoutError:
                pass
            try:
                previous = self._fault_signature()
                await self.refresh_faults()
                if previous != self._fault_signature():
                    await self._publish({"type": "carrier_changed", "carrier": self.snapshot()})
            except Exception:
                continue

    def _fault_signature(self) -> tuple[tuple[str, bool, bool, str | None], ...]:
        return tuple(
            sorted((fault.id, fault.active, fault.available, fault.error) for fault in self.faults.values())
        )

    async def _publish(self, message: dict[str, Any]) -> None:
        if self._publisher is None:
            return
        result = self._publisher(message)
        if hasattr(result, "__await__"):
            await result

    def snapshot(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "controller": {"bus": 10, "address": "0x20", "chip": "MCP23017"},
            "faults": [asdict(fault) for fault in self.faults.values()],
            "error": self.error,
        }


def _is_mock_platform() -> bool:
    return (
        sys.platform == "win32"
        or os.environ.get("INTELLEGY_GPIO_MOCK", "").lower() in {"1", "true", "yes"}
        or os.environ.get("INTELLEGY_MOCK_GPIO", "").lower() in {"1", "true", "yes"}
    )
