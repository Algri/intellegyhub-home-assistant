from __future__ import annotations

import asyncio
import errno
import os
import sys
import time
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


@dataclass
class CarrierMetricState:
    id: str
    name: str
    value: float | None
    unit: str
    bus: int
    address: str
    channel: int | None = None
    available: bool = False
    error: str | None = None
    last_read_utc: str | None = None


@dataclass(frozen=True)
class CarrierOutputDefinition:
    id: str
    name: str
    bit: int
    port: str
    pin: str


@dataclass
class CarrierOutputState:
    id: str
    name: str
    on: bool
    bus: int
    address: str
    chip: str
    port: str
    pin: str


def _carrier_output(output_id: str, name: str, bit: int, port: str, pin: str) -> CarrierOutputDefinition:
    return CarrierOutputDefinition(output_id, name, bit, port, pin)


CARRIER_OUTPUTS: dict[str, CarrierOutputDefinition] = {
    "rs485_ch1_termination": _carrier_output("rs485_ch1_termination", "RS-485 CH1 120РћВ© Termination", 10, "GPIOB", "GPB2"),
    "rs485_ch2_termination": _carrier_output("rs485_ch2_termination", "RS-485 CH2 120РћВ© Termination", 13, "GPIOB", "GPB5"),
    "xmod1_reset": _carrier_output("xmod1_reset", "XMOD1 Reset", 8, "GPIOB", "GPB0"),
    "xmod1_flash_enable": _carrier_output("xmod1_flash_enable", "XMOD1 Flash / Enable", 9, "GPIOB", "GPB1"),
    "xmod2_reset": _carrier_output("xmod2_reset", "XMOD2 Reset", 11, "GPIOB", "GPB3"),
    "xmod2_flash_enable": _carrier_output("xmod2_flash_enable", "XMOD2 Flash / Enable", 12, "GPIOB", "GPB4"),
    "usb12_reset": _carrier_output("usb12_reset", "USB1/2 Reset", 1, "GPIOA", "GPA1"),
    "usb3_reset": _carrier_output("usb3_reset", "USB3 Reset", 2, "GPIOA", "GPA2"),
    "usb4_reset": _carrier_output("usb4_reset", "USB4 Reset", 3, "GPIOA", "GPA3"),
    "usb_hub_reset": _carrier_output("usb_hub_reset", "USB Hub Reset", 4, "GPIOA", "GPA4"),
}


class CarrierHardware:
    bus = 10
    address = 0x20
    temperature_address = 0x18
    rails_address = 0x48
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

    async def outputs(self) -> dict[str, bool]:
        if _is_mock_platform():
            return {output_id: False for output_id in CARRIER_OUTPUTS}
        async with self._lock:
            if not self._available:
                self._available = await asyncio.to_thread(self._initialize_sync)
            return {
                output_id: self._bit_on_unlocked(definition.bit)
                for output_id, definition in CARRIER_OUTPUTS.items()
            }

    async def bit_on(self, bit: int) -> bool:
        if _is_mock_platform():
            return False
        async with self._lock:
            if not self._available:
                self._available = await asyncio.to_thread(self._initialize_sync)
            return self._bit_on_unlocked(bit)

    def _bit_on_unlocked(self, bit: int) -> bool:
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

    async def monitoring_metrics(self) -> dict[str, Any]:
        if _is_mock_platform():
            return self._monitoring_error_snapshot("mock")
        return await asyncio.to_thread(self._read_monitoring_metrics)

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

    def _read_monitoring_metrics(self) -> dict[str, Any]:
        now = _utc_timestamp()
        temperature = self._read_temperature_metric(now)
        rails = [
            self._read_ads1115_metric("vin", "Input Voltage", 0, 12.0, now),
            self._read_ads1115_metric("5v", "+5 V Rail", 1, 1.0, now),
            self._read_ads1115_metric("3v3", "+3.3 V Rail", 2, 1.0, now),
        ]
        return {"temperature": asdict(temperature), "rails": [asdict(item) for item in rails]}

    def _monitoring_error_snapshot(self, error: str) -> dict[str, Any]:
        return {
            "temperature": asdict(
                CarrierMetricState(
                    "board_temperature",
                    "Board Temperature",
                    None,
                    "C",
                    self.bus,
                    f"0x{self.temperature_address:02x}",
                    available=False,
                    error=error,
                )
            ),
            "rails": [
                asdict(CarrierMetricState("vin", "Input Voltage", None, "V", self.bus, f"0x{self.rails_address:02x}", 0, False, error)),
                asdict(CarrierMetricState("5v", "+5 V Rail", None, "V", self.bus, f"0x{self.rails_address:02x}", 1, False, error)),
                asdict(CarrierMetricState("3v3", "+3.3 V Rail", None, "V", self.bus, f"0x{self.rails_address:02x}", 2, False, error)),
            ],
        }

    def _read_temperature_metric(self, last_read_utc: str) -> CarrierMetricState:
        try:
            raw = self._i2c_read_register_for_address(self.temperature_address, 0x05, 2)
            word = (raw[0] << 8) | raw[1]
            value = word & 0x0FFF
            if word & 0x1000:
                value -= 0x1000
            temperature_c = value / 16.0
            return CarrierMetricState(
                "board_temperature",
                "Board Temperature",
                round(temperature_c, 3),
                "C",
                self.bus,
                f"0x{self.temperature_address:02x}",
                available=True,
                last_read_utc=last_read_utc,
            )
        except Exception as exc:
            return CarrierMetricState(
                "board_temperature",
                "Board Temperature",
                None,
                "C",
                self.bus,
                f"0x{self.temperature_address:02x}",
                available=False,
                error=str(exc),
            )

    def _read_ads1115_metric(
        self,
        metric_id: str,
        name: str,
        channel: int,
        scale: float,
        last_read_utc: str,
    ) -> CarrierMetricState:
        try:
            raw_voltage = self._read_ads1115_voltage(channel)
            return CarrierMetricState(
                metric_id,
                name,
                round(raw_voltage * scale, 3),
                "V",
                self.bus,
                f"0x{self.rails_address:02x}",
                channel,
                True,
                None,
                last_read_utc,
            )
        except Exception as exc:
            return CarrierMetricState(
                metric_id,
                name,
                None,
                "V",
                self.bus,
                f"0x{self.rails_address:02x}",
                channel,
                False,
                str(exc),
            )

    def _read_ads1115_voltage(self, channel: int) -> float:
        if channel < 0 or channel > 3:
            raise ValueError("ADS1115 channel must be between 0 and 3")
        mux = 0x4000 + (channel << 12)
        config = 0x8000 | mux | 0x0100 | 0x0080 | 0x0003
        self._i2c_write_for_address(self.rails_address, bytes([0x01, (config >> 8) & 0xFF, config & 0xFF]))
        time.sleep(0.01)
        raw = self._i2c_read_register_for_address(self.rails_address, 0x00, 2)
        value = (raw[0] << 8) | raw[1]
        if value & 0x8000:
            value -= 0x10000
        return value * 6.144 / 32768.0

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
        self._i2c_write_for_address(self.address, payload)

    def _i2c_write_for_address(self, address: int, payload: bytes) -> None:
        import fcntl

        with Path(f"/dev/i2c-{self.bus}").open("rb+", buffering=0) as device:
            fcntl.ioctl(device, I2C_SLAVE, address)
            os.write(device.fileno(), payload)

    def _i2c_read_register(self, register: int, length: int) -> bytes:
        return self._i2c_read_register_for_address(self.address, register, length)

    def _i2c_read_register_for_address(self, address: int, register: int, length: int) -> bytes:
        import fcntl

        with Path(f"/dev/i2c-{self.bus}").open("rb+", buffering=0) as device:
            fcntl.ioctl(device, I2C_SLAVE, address)
            os.write(device.fileno(), bytes([register]))
            return os.read(device.fileno(), length)


class CarrierManager:
    def __init__(self, hardware: CarrierHardware | None = None, monitoring_interval_seconds: int = 30) -> None:
        self.hardware = hardware or CarrierHardware()
        self.monitoring_interval_seconds = monitoring_interval_seconds
        self.available = False
        self.error: str | None = "startup pending"
        self.faults: dict[str, FaultLineState] = {}
        self.monitoring: dict[str, Any] = _monitoring_error_snapshot("startup pending")
        self.outputs: dict[str, bool] = {output_id: False for output_id in CARRIER_OUTPUTS}
        self._publisher: Callable[[dict[str, Any]], Any] | None = None
        self._fault_task: asyncio.Task | None = None
        self._monitoring_task: asyncio.Task | None = None
        self._stopped = asyncio.Event()

    def set_publisher(self, publisher: Callable[[dict[str, Any]], Any]) -> None:
        self._publisher = publisher

    async def start(self) -> None:
        try:
            self._stopped.clear()
            self.available = await self.hardware.initialize()
            self.outputs = await self.hardware.outputs()
            self.faults = {fault.id: fault for fault in await self.hardware.fault_lines()}
            self.monitoring = await self.hardware.monitoring_metrics()
            self.error = None if self.available else "carrier MCP23017 unavailable"
            self._fault_task = asyncio.create_task(self._monitor_faults(), name="intellegyhub_carrier_faults")
            self._monitoring_task = asyncio.create_task(self._monitor_metrics(), name="intellegyhub_carrier_monitoring")
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
        if self._monitoring_task:
            self._monitoring_task.cancel()
            try:
                await self._monitoring_task
            except asyncio.CancelledError:
                pass
            self._monitoring_task = None

    async def set_xbus_power(self, on: bool) -> bool:
        return await self.hardware.set_bit(CarrierHardware.bit_xbus_power, on)

    async def xbus_power_on(self) -> bool:
        return await self.hardware.bit_on(CarrierHardware.bit_xbus_power)

    async def set_onewire_power(self, on: bool) -> bool:
        return await self.hardware.set_bit(CarrierHardware.bit_onewire_power, on)

    async def onewire_power_on(self) -> bool:
        return await self.hardware.bit_on(CarrierHardware.bit_onewire_power)

    async def set_output(self, output_id: str, on: bool) -> CarrierOutputState:
        if output_id not in CARRIER_OUTPUTS:
            raise ValueError(f"Unknown carrier output: {output_id}")
        definition = CARRIER_OUTPUTS[output_id]
        confirmed = await self.hardware.set_bit(definition.bit, on)
        previous = self.outputs.get(output_id)
        self.outputs[output_id] = confirmed
        state = self._output_state(output_id, confirmed)
        if previous != confirmed:
            await self._publish({"type": "carrier_output_changed", "output": asdict(state), "carrier": self.snapshot()})
        return state

    async def refresh_outputs(self) -> dict[str, bool]:
        self.outputs = await self.hardware.outputs()
        return dict(self.outputs)

    async def refresh_faults(self) -> dict[str, Any]:
        self.faults = {fault.id: fault for fault in await self.hardware.fault_lines()}
        return self.snapshot()

    async def refresh_monitoring(self) -> dict[str, Any]:
        self.monitoring = await self.hardware.monitoring_metrics()
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

    async def _monitor_metrics(self) -> None:
        while not self._stopped.is_set():
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=self.monitoring_interval_seconds)
                return
            except asyncio.TimeoutError:
                pass
            try:
                previous = self._monitoring_signature()
                await self.refresh_monitoring()
                if previous != self._monitoring_signature():
                    await self._publish({"type": "carrier_changed", "carrier": self.snapshot()})
            except Exception:
                continue

    def _monitoring_signature(self) -> str:
        return repr(self.monitoring)

    async def _publish(self, message: dict[str, Any]) -> None:
        if self._publisher is None:
            return
        result = self._publisher(message)
        if hasattr(result, "__await__"):
            await result

    def snapshot(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "identity": {
                "product": "IHC-1400",
                "model": "IntellegyHUB Controller",
                "serial_number": "IH1400-00001234",
                "hardware_revision": "1.4 Rev.A",
                "software_version": "0.5.129",
            },
            "controller": {"bus": 10, "address": "0x20", "chip": "MCP23017"},
            "faults": [asdict(fault) for fault in self.faults.values()],
            "outputs": [asdict(self._output_state(output_id, on)) for output_id, on in self.outputs.items()],
            "monitoring": self.monitoring,
            "monitoring_interval_seconds": self.monitoring_interval_seconds,
            "error": self.error,
        }

    def _output_state(self, output_id: str, on: bool) -> CarrierOutputState:
        definition = CARRIER_OUTPUTS[output_id]
        return CarrierOutputState(
            definition.id,
            definition.name,
            bool(on),
            CarrierHardware.bus,
            f"0x{CarrierHardware.address:02x}",
            "MCP23017",
            definition.port,
            definition.pin,
        )


def _is_mock_platform() -> bool:
    return (
        sys.platform == "win32"
        or os.environ.get("INTELLEGY_GPIO_MOCK", "").lower() in {"1", "true", "yes"}
        or os.environ.get("INTELLEGY_MOCK_GPIO", "").lower() in {"1", "true", "yes"}
    )


def _monitoring_error_snapshot(error: str) -> dict[str, Any]:
    return {
        "temperature": asdict(
            CarrierMetricState(
                "board_temperature",
                "Board Temperature",
                None,
                "C",
                10,
                "0x18",
                available=False,
                error=error,
            )
        ),
        "rails": [
            asdict(CarrierMetricState("vin", "Input Voltage", None, "V", 10, "0x48", 0, False, error)),
            asdict(CarrierMetricState("5v", "+5 V Rail", None, "V", 10, "0x48", 1, False, error)),
            asdict(CarrierMetricState("3v3", "+3.3 V Rail", None, "V", 10, "0x48", 2, False, error)),
        ],
    }


def _utc_timestamp() -> str:
    return time.strftime("%H:%M:%S UTC", time.gmtime())
