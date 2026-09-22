from __future__ import annotations

import asyncio
import errno
import math
import os
import sys
import sqlite3
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable

I2C_SLAVE = 0x0703


class XPortMode(StrEnum):
    DISABLED = "Disabled"
    DI_EXTERNAL = "DigitalInputExternalVoltage"
    DI_INTERNAL = "DigitalInputInternalPullUp"
    DO = "DigitalOutput"
    PWM = "PwmOutput"
    AI = "AnalogInput"
    COUNTER_EXTERNAL = "PulseCounterExternalVoltage"
    COUNTER_INTERNAL = "PulseCounterInternalPullUp"


class XPortTopology(StrEnum):
    NOT_INSTALLED = "NotInstalled"
    STANDARD = "Standard"
    EXTENDED = "Extended"
    FAULTED = "Faulted"
    INVALID_CONFIGURATION = "InvalidConfiguration"


class Availability(StrEnum):
    UNKNOWN = "Unknown"
    AVAILABLE = "Available"
    OPTIONAL_MISSING = "OptionalMissing"
    FAULTED = "Faulted"
    INVALID_CONFIGURATION = "InvalidConfiguration"


WRITABLE_MODES = {XPortMode.DO, XPortMode.PWM}
INPUT_MODES = {XPortMode.DI_EXTERNAL, XPortMode.DI_INTERNAL}
COUNTER_MODES = {XPortMode.COUNTER_EXTERNAL, XPortMode.COUNTER_INTERNAL}


@dataclass(frozen=True)
class XPortChannelBinding:
    input_gpio: int
    power_up_bit: int
    power_down_bit: int
    analog_select_bit: int
    standard_output_bit: int
    analog_channel: int
    analog_scale: float
    pca_channel: int


XPORT_BINDINGS: dict[int, XPortChannelBinding] = {
    1: XPortChannelBinding(25, 0, 7, 7, 3, 0, 12.0, 0),
    2: XPortChannelBinding(24, 1, 6, 6, 2, 1, 12.0, 1),
    3: XPortChannelBinding(8, 2, 5, 5, 1, 2, 12.0, 2),
    4: XPortChannelBinding(7, 3, 4, 4, 0, 3, 12.0, 3),
}


@dataclass
class XPortChannel:
    channel: int
    desired_mode: str = XPortMode.DISABLED
    confirmed_mode: str = XPortMode.DISABLED
    value: float = 0
    counter: int = 0
    revision: int = 0
    availability: str = Availability.UNKNOWN
    error: str | None = None
    updated_at: str = ""


class XPortStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or _default_store_path()
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    async def load(self) -> list[XPortChannel]:
        return await asyncio.to_thread(self._load_sync)

    async def save(self, channel: XPortChannel) -> None:
        async with self._lock:
            await asyncio.to_thread(self._save_sync, channel)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(self.path)

    def _initialize_sync(self) -> None:
        with self._connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS xport_channels (
                    channel INTEGER PRIMARY KEY CHECK(channel BETWEEN 1 AND 4),
                    desired_mode TEXT NOT NULL,
                    confirmed_mode TEXT NOT NULL,
                    value REAL NOT NULL,
                    counter INTEGER NOT NULL,
                    revision INTEGER NOT NULL,
                    availability TEXT NOT NULL,
                    error TEXT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def _load_sync(self) -> list[XPortChannel]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT channel,desired_mode,confirmed_mode,value,counter,revision,availability,error,updated_at "
                "FROM xport_channels ORDER BY channel"
            ).fetchall()
        return [XPortChannel(*row) for row in rows]

    def _save_sync(self, channel: XPortChannel) -> None:
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO xport_channels(channel,desired_mode,confirmed_mode,value,counter,revision,availability,error,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(channel) DO UPDATE SET
                  desired_mode=excluded.desired_mode,
                  confirmed_mode=excluded.confirmed_mode,
                  value=excluded.value,
                  counter=excluded.counter,
                  revision=excluded.revision,
                  availability=excluded.availability,
                  error=excluded.error,
                  updated_at=excluded.updated_at
                """,
                (
                    channel.channel,
                    channel.desired_mode,
                    channel.confirmed_mode,
                    channel.value,
                    channel.counter,
                    channel.revision,
                    channel.availability,
                    channel.error,
                    channel.updated_at,
                ),
            )


class XPortHardware:
    bus = 10
    mcp23017 = 0x21
    ads1115 = 0x49
    pca9632 = 0x62
    _mcp_iocon = 0x0A
    _mcp_iodira = 0x00
    _mcp_iodirb = 0x01
    _mcp_gpioa = 0x12
    _mcp_gpiob = 0x13
    _ads_config = 0x01
    _pca_mode1 = 0x00
    _pca_mode2 = 0x01
    _pca_ledout = 0x08

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._gpio_a = 0
        self._gpio_b = 0
        self._mcp_available = False
        self._ads_available = False
        self._pca_available = False
        self._topology = XPortTopology.NOT_INSTALLED

    async def discover(self) -> tuple[XPortTopology, Availability, str | None]:
        if _is_mock_platform():
            return XPortTopology.EXTENDED, Availability.AVAILABLE, None
        async with self._lock:
            devices = await asyncio.to_thread(self._scan_presence)
            mcp = self.mcp23017 in devices
            ads = self.ads1115 in devices
            pca = self.pca9632 in devices
            self._mcp_available = mcp
            self._ads_available = ads
            self._pca_available = pca
            if not mcp and not ads and not pca:
                self._topology = XPortTopology.NOT_INSTALLED
                return XPortTopology.NOT_INSTALLED, Availability.OPTIONAL_MISSING, None
            if not mcp or not ads:
                self._topology = XPortTopology.INVALID_CONFIGURATION
                return XPortTopology.INVALID_CONFIGURATION, Availability.INVALID_CONFIGURATION, "XPORT_TOPOLOGY_INVALID"
            self._topology = XPortTopology.EXTENDED if pca else XPortTopology.STANDARD
            await asyncio.to_thread(self._initialize_mcp)
            if pca and not await asyncio.to_thread(self._initialize_pca):
                self._topology = XPortTopology.FAULTED
                return XPortTopology.FAULTED, Availability.FAULTED, "XPORT_PCA_INITIALIZATION_FAILED"
            return self._topology, Availability.AVAILABLE, None

    async def disable_channel(self, channel: int) -> None:
        self._validate_channel(channel)
        if _is_mock_platform():
            return
        async with self._lock:
            self._ensure_available()
            binding = XPORT_BINDINGS[channel]
            self._clear_mode_bits(binding)
            await asyncio.to_thread(self._write_mcp_shadow)
            if self._pca_available:
                await asyncio.to_thread(self._set_pca, binding.pca_channel, 0)

    async def configure_channel(self, channel: int, mode: XPortMode, topology: XPortTopology) -> None:
        self._validate_channel(channel)
        if topology not in {XPortTopology.STANDARD, XPortTopology.EXTENDED} and mode != XPortMode.DISABLED:
            raise RuntimeError("X-Port is not available")
        if mode == XPortMode.PWM and topology != XPortTopology.EXTENDED:
            raise RuntimeError("PWM requires X-Port Extended topology with PCA9632 0x62")
        if mode == XPortMode.AI and not self._ads_available and not _is_mock_platform():
            raise RuntimeError("Analog input requires ADS1115 0x49")
        if _is_mock_platform():
            return
        async with self._lock:
            self._ensure_available()
            binding = XPORT_BINDINGS[channel]
            self._clear_mode_bits(binding)
            if self._pca_available:
                await asyncio.to_thread(self._set_pca, binding.pca_channel, 0)
            if mode == XPortMode.AI:
                self._gpio_a |= 1 << binding.analog_select_bit
            elif mode in {XPortMode.DI_EXTERNAL, XPortMode.COUNTER_EXTERNAL}:
                self._gpio_b |= 1 << binding.power_down_bit
            elif mode in {XPortMode.DI_INTERNAL, XPortMode.COUNTER_INTERNAL}:
                self._gpio_b |= 1 << binding.power_up_bit
            await asyncio.to_thread(self._write_mcp_shadow)

    async def set_value(self, channel: int, mode: XPortMode, value: float) -> float:
        self._validate_channel(channel)
        if mode == XPortMode.DO:
            normalized = 1 if value > 0 else 0
            if not _is_mock_platform():
                async with self._lock:
                    self._ensure_available()
                    binding = XPORT_BINDINGS[channel]
                    if self._topology == XPortTopology.EXTENDED and self._pca_available:
                        await asyncio.to_thread(self._set_pca, binding.pca_channel, 255 if normalized else 0)
                    else:
                        bit = 1 << binding.standard_output_bit
                        self._gpio_a = (self._gpio_a | bit) if normalized else (self._gpio_a & ~bit)
                        await asyncio.to_thread(self._write_mcp_shadow)
            return normalized
        if mode == XPortMode.PWM:
            normalized = min(1, max(0, value))
            if not _is_mock_platform():
                async with self._lock:
                    self._ensure_available()
                    if self._topology != XPortTopology.EXTENDED or not self._pca_available:
                        raise RuntimeError("PWM requires PCA9632 0x62")
                    await asyncio.to_thread(
                        self._set_pca,
                        XPORT_BINDINGS[channel].pca_channel,
                        brightness_to_pwm(normalized * 100),
                    )
            return normalized
        raise RuntimeError(f"Mode {mode} is not writable")

    async def read_value(self, channel: int, mode: XPortMode) -> float:
        self._validate_channel(channel)
        if _is_mock_platform():
            return 0
        async with self._lock:
            if mode == XPortMode.AI:
                if not self._ads_available:
                    raise RuntimeError("Analog input requires ADS1115 0x49")
                return await asyncio.to_thread(self._read_ads, channel)
            if mode in INPUT_MODES or mode in COUNTER_MODES:
                return await asyncio.to_thread(self._read_input_gpio, channel)
        raise RuntimeError(f"Mode {mode} is not readable")

    def _scan_presence(self) -> set[int]:
        try:
            import fcntl
        except ModuleNotFoundError:
            return set()

        path = Path(f"/dev/i2c-{self.bus}")
        if not path.exists():
            return set()
        found = set()
        with path.open("rb+", buffering=0) as device:
            if self._probe_read_register(device, fcntl, self.mcp23017, self._mcp_iocon, 1):
                found.add(self.mcp23017)
            if self._probe_read_register(device, fcntl, self.ads1115, self._ads_config, 2):
                found.add(self.ads1115)
            if self._probe_read_register(device, fcntl, self.pca9632, self._pca_mode1, 1):
                found.add(self.pca9632)
        return found

    @staticmethod
    def _probe_read_register(device: Any, fcntl_module: Any, address: int, register: int, length: int) -> bool:
        try:
            fcntl_module.ioctl(device, I2C_SLAVE, address)
            os.write(device.fileno(), bytes([register]))
            os.read(device.fileno(), length)
            return True
        except OSError as exc:
            if exc.errno in {errno.ENXIO, errno.EREMOTEIO, errno.EIO, errno.ETIMEDOUT}:
                return False
            raise

    @staticmethod
    def _validate_channel(channel: int) -> None:
        if channel < 1 or channel > 4:
            raise ValueError("X-Port channel must be in range 1-4")

    def _i2c_write(self, address: int, payload: bytes) -> None:
        import fcntl

        with Path(f"/dev/i2c-{self.bus}").open("rb+", buffering=0) as device:
            fcntl.ioctl(device, I2C_SLAVE, address)
            os.write(device.fileno(), payload)

    def _i2c_read_register(self, address: int, register: int, length: int) -> bytes:
        import fcntl

        with Path(f"/dev/i2c-{self.bus}").open("rb+", buffering=0) as device:
            fcntl.ioctl(device, I2C_SLAVE, address)
            os.write(device.fileno(), bytes([register]))
            return os.read(device.fileno(), length)

    def _initialize_mcp(self) -> None:
        self._gpio_a = 0
        self._gpio_b = 0
        self._i2c_write(self.mcp23017, bytes([self._mcp_iodira, 0x00]))
        self._i2c_write(self.mcp23017, bytes([self._mcp_iodirb, 0x00]))
        self._write_mcp_shadow()

    def _write_mcp_shadow(self) -> None:
        self._i2c_write(self.mcp23017, bytes([self._mcp_gpioa, self._gpio_a & 0xFF]))
        self._i2c_write(self.mcp23017, bytes([self._mcp_gpiob, self._gpio_b & 0xFF]))

    @staticmethod
    def _clear_mode_bits_for(value: int, *bits: int) -> int:
        for bit in bits:
            value &= ~(1 << bit)
        return value

    def _clear_mode_bits(self, binding: XPortChannelBinding) -> None:
        self._gpio_a = self._clear_mode_bits_for(self._gpio_a, binding.standard_output_bit, binding.analog_select_bit)
        self._gpio_b = self._clear_mode_bits_for(self._gpio_b, binding.power_up_bit, binding.power_down_bit)

    def _initialize_pca(self) -> bool:
        self._i2c_write(self.pca9632, bytes([self._pca_mode1, 0x00]))
        self._i2c_write(self.pca9632, bytes([self._pca_mode2, 0x14]))
        time.sleep(0.001)
        self._i2c_write(self.pca9632, bytes([self._pca_ledout, 0x00]))
        mode2 = self._i2c_read_register(self.pca9632, self._pca_mode2, 1)[0]
        ledout = self._i2c_read_register(self.pca9632, self._pca_ledout, 1)[0]
        return mode2 == 0x14 and ledout == 0x00

    def _set_pca(self, channel: int, pwm: int) -> None:
        pwm = min(255, max(0, int(pwm)))
        self._i2c_write(self.pca9632, bytes([0x02 + channel, pwm]))
        ledout = self._i2c_read_register(self.pca9632, self._pca_ledout, 1)[0]
        shift = channel * 2
        mode = 0 if pwm == 0 else 1 if pwm == 255 else 2
        ledout = (ledout & ~(0x03 << shift)) | (mode << shift)
        self._i2c_write(self.pca9632, bytes([self._pca_ledout, ledout]))

    def _read_ads(self, channel: int) -> float:
        binding = XPORT_BINDINGS[channel]
        mux = 0x4000 + (binding.analog_channel << 12)
        config = 0x8000 | mux | 0x0200 | 0x0100 | 0x0080 | 0x0003
        self._i2c_write(self.ads1115, bytes([self._ads_config, (config >> 8) & 0xFF, config & 0xFF]))
        time.sleep(0.01)
        read = self._i2c_read_register(self.ads1115, 0x00, 2)
        raw = int.from_bytes(read, "big", signed=True)
        return raw * 4.096 / 32768.0 * binding.analog_scale

    def _read_input_gpio(self, channel: int) -> float:
        import gpiod

        line = XPORT_BINDINGS[channel].input_gpio
        with gpiod.Chip("/dev/gpiochip0") as chip:
            request = chip.request_lines(
                consumer=f"intellegyhub-xport-x{channel}-input",
                config={line: gpiod.LineSettings(direction=gpiod.line.Direction.INPUT, bias=gpiod.line.Bias.DISABLED)},
            )
            try:
                value = request.get_value(line)
            finally:
                request.release()
        gpio_high = value == gpiod.line.Value.ACTIVE
        return 0 if gpio_high else 1

    def _ensure_available(self) -> None:
        if not self._mcp_available or self._topology not in {XPortTopology.STANDARD, XPortTopology.EXTENDED}:
            raise RuntimeError("X-Port is not available")


def _is_mock_platform() -> bool:
    return (
        sys.platform == "win32"
        or os.environ.get("INTELLEGY_GPIO_MOCK", "").lower() in {"1", "true", "yes"}
        or os.environ.get("INTELLEGY_MOCK_GPIO", "").lower() in {"1", "true", "yes"}
    )


def _default_store_path() -> Path:
    if sys.platform == "win32":
        return Path(".data/intellegyhub.sqlite3")
    return Path("/data/intellegyhub.sqlite3")


def brightness_to_pwm(brightness: float) -> int:
    brightness = min(100.0, max(0.0, brightness))
    if brightness <= 0:
        return 0
    pwm = math.pow(255.0, brightness / 100.0)
    return min(255, max(1, round(pwm)))


class XPortManager:
    def __init__(self, store: XPortStore | None = None, hardware: XPortHardware | None = None) -> None:
        self.store = store or XPortStore()
        self.hardware = hardware or XPortHardware()
        self.topology = XPortTopology.NOT_INSTALLED
        self.availability = Availability.UNKNOWN
        self.error: str | None = "startup pending"
        self.channels: dict[int, XPortChannel] = {}
        self._lock = asyncio.Lock()
        self._publisher: Callable[[dict[str, Any]], Any] | None = None
        self._monitor_task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._counter_last_edges: dict[int, float] = {}
        self._input_last_values: dict[int, bool] = {}

    def set_publisher(self, publisher: Callable[[dict[str, Any]], Any]) -> None:
        self._publisher = publisher

    async def start(self) -> None:
        await self.store.initialize()
        topology, availability, error = await self.hardware.discover()
        self.topology = topology
        self.availability = availability
        self.error = error
        stored = {item.channel: item for item in await self.store.load()}
        now = self._now()
        for channel in range(1, 5):
            state = stored.get(channel) or XPortChannel(channel=channel)
            state.confirmed_mode = XPortMode.DISABLED
            state.availability = availability
            state.error = error
            state.updated_at = now
            self.channels[channel] = state
            await self.store.save(state)
            if availability == Availability.AVAILABLE and state.desired_mode != XPortMode.DISABLED:
                restored_value = state.value
                try:
                    await self.configure(channel, XPortMode(state.desired_mode), state.revision)
                    restored_state = self.channels[channel]
                    if XPortMode(restored_state.confirmed_mode) in WRITABLE_MODES:
                        await self.set_value(channel, restored_value)
                except Exception as exc:
                    state.confirmed_mode = XPortMode.DISABLED
                    state.availability = Availability.FAULTED
                    state.error = f"restore failed: {exc}"
                    state.updated_at = self._now()
                    await self.store.save(state)
        self._stopping.clear()
        self._monitor_task = asyncio.create_task(self._monitor_inputs(), name="intellegy-xport-monitor")

    async def stop(self) -> None:
        self._stopping.set()
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
            self._monitor_task = None
        for channel in range(1, 5):
            try:
                await self.hardware.disable_channel(channel)
            except Exception as exc:
                state = self.channels.get(channel)
                if state:
                    state.availability = Availability.FAULTED
                    state.error = f"shutdown disable failed: {exc}"
                    state.updated_at = self._now()
                    await self.store.save(state)

    def snapshot(self) -> dict[str, Any]:
        return {
            "topology": self.topology,
            "availability": self.availability,
            "error": self.error,
            "modes": [mode.value for mode in XPortMode],
            "channels": [asdict(self.channels[index]) for index in sorted(self.channels)],
        }

    async def configure(self, channel: int, mode: XPortMode, revision: int | None = None) -> dict[str, Any]:
        async with self._lock:
            state = self._required(channel)
            state.desired_mode = mode
            state.revision = max(state.revision + 1, revision or 0)
            state.updated_at = self._now()
            await self.store.save(state)
            if self.availability != Availability.AVAILABLE and mode != XPortMode.DISABLED:
                state.error = self.error or "XPORT_UNAVAILABLE"
                await self.store.save(state)
                return self._operation(state, "Rejected", state.error)
            try:
                await self.hardware.disable_channel(channel)
                state.confirmed_mode = XPortMode.DISABLED
                state.value = 0
                await self.hardware.configure_channel(channel, mode, self.topology)
                if mode in INPUT_MODES or mode == XPortMode.AI:
                    state.value = await self.hardware.read_value(channel, mode)
                if mode in COUNTER_MODES:
                    state.value = state.counter
                state.confirmed_mode = mode
                state.availability = self.availability
                state.error = None
                state.updated_at = self._now()
                await self.store.save(state)
                await self._publish_state(state)
                return self._operation(state, "Succeeded", None)
            except Exception as exc:
                state.confirmed_mode = XPortMode.DISABLED
                state.availability = Availability.FAULTED
                state.error = str(exc)
                state.updated_at = self._now()
                await self.store.save(state)
                await self._publish_state(state)
                return self._operation(state, "Failed", state.error)

    async def set_value(self, channel: int, value: float) -> dict[str, Any]:
        async with self._lock:
            state = self._required(channel)
            mode = XPortMode(state.confirmed_mode)
            if mode not in WRITABLE_MODES:
                return self._operation(state, "Rejected", f"Mode {mode} is not writable")
            try:
                state.value = await self.hardware.set_value(channel, mode, value)
                state.updated_at = self._now()
                await self.store.save(state)
                await self._publish_state(state)
                return self._operation(state, "Succeeded", None)
            except Exception as exc:
                state.error = str(exc)
                await self.store.save(state)
                return self._operation(state, "Failed", state.error)

    async def reset_counter(self, channel: int) -> dict[str, Any]:
        async with self._lock:
            state = self._required(channel)
            state.counter = 0
            state.value = 0
            state.updated_at = self._now()
            await self.store.save(state)
            await self._publish_state(state)
            return self._operation(state, "Succeeded", None)

    async def _monitor_inputs(self) -> None:
        try:
            while not self._stopping.is_set():
                await asyncio.sleep(0.1)
                for channel in range(1, 5):
                    async with self._lock:
                        state = self.channels.get(channel)
                        if not state:
                            continue
                        mode = XPortMode(state.confirmed_mode)
                        if mode not in INPUT_MODES and mode not in COUNTER_MODES and mode != XPortMode.AI:
                            continue
                        try:
                            value = await self.hardware.read_value(channel, mode)
                        except Exception as exc:
                            state.availability = Availability.FAULTED
                            state.error = str(exc)
                            state.updated_at = self._now()
                            await self.store.save(state)
                            await self._publish_state(state)
                            continue
                        if mode in COUNTER_MODES:
                            active = value > 0
                            previous = self._input_last_values.get(channel, active)
                            self._input_last_values[channel] = active
                            now = time.monotonic()
                            last = self._counter_last_edges.get(channel, 0)
                            if active and not previous and now - last >= 0.05:
                                self._counter_last_edges[channel] = now
                                state.counter += 1
                                state.value = state.counter
                            else:
                                continue
                        elif state.value == value and state.availability == Availability.AVAILABLE and state.error is None:
                            continue
                        else:
                            state.value = value
                        state.availability = Availability.AVAILABLE
                        state.error = None
                        state.updated_at = self._now()
                        await self.store.save(state)
                        await self._publish_state(state)
        except asyncio.CancelledError:
            raise

    def _required(self, channel: int) -> XPortChannel:
        if channel not in self.channels:
            raise ValueError("X-Port channel must be in range 1-4")
        return self.channels[channel]

    async def _publish_state(self, state: XPortChannel) -> None:
        if self._publisher:
            result = self._publisher({"type": "xport_channel_changed", "channel": asdict(state)})
            if asyncio.iscoroutine(result):
                await result

    @staticmethod
    def _operation(state: XPortChannel, status: str, error: str | None) -> dict[str, Any]:
        return {"channel": asdict(state), "status": status, "error": error}

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()
