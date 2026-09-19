from __future__ import annotations

import asyncio
import errno
import json
import os
import sqlite3
import sys
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

I2C_SLAVE = 0x0703
POWER_SETTLE_SECONDS = 0.15
XDI16_INTERRUPT_DRAIN_LIMIT = 64
XDI16_INTERRUPT_RETRY_SECONDS = 0.25


@dataclass
class ExtensionModule:
    id: str
    name: str
    model: str
    chip: str
    bus: int
    address: str
    kind: str
    channels: int
    available: bool
    relays: list[bool] | None = None
    inputs: list[bool] | None = None
    error: str | None = None


XDo8Module = ExtensionModule


class ExtensionStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or _default_store_path()
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    async def load_power(self) -> bool:
        return await asyncio.to_thread(self._load_power_sync)

    async def save_power(self, on: bool) -> None:
        async with self._lock:
            await asyncio.to_thread(self._save_power_sync, on)

    async def load_modules(self) -> list[ExtensionModule]:
        return await asyncio.to_thread(self._load_modules_sync)

    async def save_modules(self, modules: list[ExtensionModule]) -> None:
        async with self._lock:
            await asyncio.to_thread(self._save_modules_sync, modules)

    async def delete_module(self, module_id: str) -> None:
        async with self._lock:
            await asyncio.to_thread(self._delete_module_sync, module_id)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(self.path)

    def _initialize_sync(self) -> None:
        with self._connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS extension_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS extension_modules (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    model TEXT NOT NULL,
                    chip TEXT NOT NULL,
                    bus INTEGER NOT NULL,
                    address TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    channels INTEGER NOT NULL,
                    relays TEXT NOT NULL,
                    inputs TEXT NULL,
                    error TEXT NULL
                )
                """
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(extension_modules)").fetchall()}
            if "inputs" not in columns:
                db.execute("ALTER TABLE extension_modules ADD COLUMN inputs TEXT NULL")

    def _load_power_sync(self) -> bool:
        with self._connect() as db:
            row = db.execute("SELECT value FROM extension_settings WHERE key='bus_power'").fetchone()
        return bool(row and row[0] == "1")

    def _save_power_sync(self, on: bool) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO extension_settings(key,value) VALUES('bus_power',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("1" if on else "0",),
            )

    def _load_modules_sync(self) -> list[ExtensionModule]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT id,name,model,chip,bus,address,kind,channels,relays,inputs,error FROM extension_modules ORDER BY bus,address"
            ).fetchall()
        modules = []
        for row in rows:
            relays = json.loads(row[8]) if row[8] else None
            inputs = json.loads(row[9]) if row[9] else None
            modules.append(
                ExtensionModule(
                    id=row[0],
                    name=row[1],
                    model=row[2],
                    chip=row[3],
                    bus=row[4],
                    address=row[5],
                    kind=row[6],
                    channels=row[7],
                    available=False,
                    relays=[bool(value) for value in relays] if relays is not None else None,
                    inputs=[bool(value) for value in inputs] if inputs is not None else None,
                    error=row[10],
                )
            )
        return modules

    def _save_modules_sync(self, modules: list[ExtensionModule]) -> None:
        with self._connect() as db:
            module_ids = [module.id for module in modules]
            if module_ids:
                placeholders = ",".join("?" for _ in module_ids)
                db.execute(f"DELETE FROM extension_modules WHERE id NOT IN ({placeholders})", module_ids)
            else:
                db.execute("DELETE FROM extension_modules")
            for module in modules:
                db.execute(
                    """
                    INSERT INTO extension_modules(id,name,model,chip,bus,address,kind,channels,relays,inputs,error)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET
                      name=excluded.name,
                      model=excluded.model,
                      chip=excluded.chip,
                      bus=excluded.bus,
                      address=excluded.address,
                      kind=excluded.kind,
                      channels=excluded.channels,
                      relays=excluded.relays,
                      inputs=excluded.inputs,
                      error=excluded.error
                    """,
                    (
                        module.id,
                        module.name,
                        module.model,
                        module.chip,
                        module.bus,
                        module.address,
                        module.kind,
                        module.channels,
                        json.dumps(module.relays or []),
                        json.dumps(module.inputs) if module.inputs is not None else None,
                        module.error,
                    ),
                )

    def _delete_module_sync(self, module_id: str) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM extension_modules WHERE id=?", (module_id,))


class ExtensionHardware:
    power_bus = 10
    power_address = 0x20
    power_bit = 15
    gpiochip_path = "/dev/gpiochip0"
    xdi_interrupt_gpio = 6
    xdo_bus = 1
    xdo_addresses = tuple(range(0x20, 0x28))

    _mcp23017_iodira = 0x00
    _mcp23017_iodirb = 0x01
    _mcp23017_gpioa = 0x12
    _mcp23017_gpiob = 0x13
    _mcp23008_iodir = 0x00
    _mcp23008_gpio = 0x09
    _mcp_iocon = 0x0B

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._power_gpio_a = 0
        self._power_gpio_b = 0
        self._relay_shadows: dict[int, int] = {}
        self._input_shadows: dict[int, int] = {}
        self._xdi_chip: Any = None
        self._xdi_interrupt_request: Any = None
        self._xdi_interrupt_thread: threading.Thread | None = None
        self._xdi_interrupt_stop = threading.Event()
        self._xdi_interrupt_callback: Callable[[], None] | None = None

    async def initialize(self) -> bool:
        if _is_mock_platform():
            return False
        async with self._lock:
            if not self._probe(self.power_bus, self.power_address, self._mcp23017_iodirb, 1):
                return False
            await asyncio.to_thread(self._write_power_direction)
            return bool((await asyncio.to_thread(self._read_power_gpio_b)) & 0x80)

    async def start_xdi16_interrupt(self, callback: Callable[[], None]) -> bool:
        if _is_mock_platform():
            return False
        self._xdi_interrupt_callback = callback
        await asyncio.to_thread(self._open_xdi16_interrupt)
        self._xdi_interrupt_stop.clear()
        self._xdi_interrupt_thread = threading.Thread(
            target=self._watch_xdi16_interrupt,
            name="intellegyhub-xdi16-interrupt",
            daemon=True,
        )
        self._xdi_interrupt_thread.start()
        return True

    async def stop_xdi16_interrupt(self) -> None:
        self._xdi_interrupt_stop.set()
        if self._xdi_interrupt_thread:
            await asyncio.to_thread(self._xdi_interrupt_thread.join, 2)
            self._xdi_interrupt_thread = None
        if self._xdi_interrupt_request:
            self._xdi_interrupt_request.release()
            self._xdi_interrupt_request = None
        if self._xdi_chip:
            self._xdi_chip.close()
            self._xdi_chip = None

    async def xdi16_interrupt_active(self) -> bool:
        if _is_mock_platform() or self._xdi_interrupt_request is None:
            return False
        return await asyncio.to_thread(self._read_xdi16_interrupt_active)

    async def set_power(self, on: bool) -> bool:
        if _is_mock_platform():
            return on
        async with self._lock:
            await asyncio.to_thread(self._write_power_direction)
            if on:
                self._power_gpio_b |= 1 << 7
            else:
                self._power_gpio_b &= ~(1 << 7)
                self._relay_shadows.clear()
            await asyncio.to_thread(self._write_power_outputs)
            return on

    async def scan_xdo8(self, power_on: bool) -> list[ExtensionModule]:
        return await self.scan_modules(power_on)

    async def scan_modules(self, power_on: bool) -> list[ExtensionModule]:
        if _is_mock_platform():
            return []
        if not power_on:
            return []
        async with self._lock:
            modules = []
            for address in self.xdo_addresses:
                if not self._probe(self.xdo_bus, address, self._mcp_iocon, 1):
                    continue
                try:
                    detected = await asyncio.to_thread(self._detect_module_type, address)
                    if detected == "xdi16":
                        await asyncio.to_thread(self._initialize_xdi16, address)
                        modules.append(self._xdi16_module(address, True, None))
                    else:
                        await asyncio.to_thread(self._initialize_xdo8, address)
                        modules.append(self._xdo8_module(address, True, None))
                except Exception as exc:
                    modules.append(self._unknown_module(address, False, str(exc)))
            return modules

    async def set_xdo8_relay(self, address_text: str, channel: int, on: bool) -> XDo8Module:
        if channel < 1 or channel > 8:
            raise ValueError("xDO-8 relay channel must be in range 1-8")
        address = int(address_text, 16)
        if address not in self.xdo_addresses:
            raise ValueError("xDO-8 address is outside supported range 0x20-0x27")
        if _is_mock_platform():
            return self._xdo8_module(address, True, None, relays={channel: on})
        async with self._lock:
            shadow = self._relay_shadows.get(address, 0)
            bit = 1 << (channel - 1)
            shadow = (shadow | bit) if on else (shadow & ~bit)
            self._relay_shadows[address] = shadow
            await asyncio.to_thread(self._write_xdo8_outputs, address, shadow)
            return self._xdo8_module(address, True, None)

    async def set_xdo8_relays(self, address_text: str, relays: list[bool]) -> XDo8Module:
        address = int(address_text, 16)
        if address not in self.xdo_addresses:
            raise ValueError("xDO-8 address is outside supported range 0x20-0x27")
        shadow = 0
        for index, on in enumerate(relays[:8]):
            if on:
                shadow |= 1 << index
        if _is_mock_platform():
            self._relay_shadows[address] = shadow
            return self._xdo8_module(address, True, None)
        async with self._lock:
            self._relay_shadows[address] = shadow
            await asyncio.to_thread(self._write_xdo8_outputs, address, shadow)
            return self._xdo8_module(address, True, None)

    async def read_xdi16_inputs(self, address_text: str) -> ExtensionModule:
        address = int(address_text, 16)
        if address not in self.xdo_addresses:
            raise ValueError("xDI-16 address is outside supported range 0x20-0x27")
        if _is_mock_platform():
            return self._xdi16_module(address, True, None)
        async with self._lock:
            await asyncio.to_thread(self._initialize_xdi16, address)
            await asyncio.to_thread(self._read_xdi16_inputs, address)
            return self._xdi16_module(address, True, None)

    def _xdo8_module(self, address: int, available: bool, error: str | None, relays: dict[int, bool] | None = None) -> ExtensionModule:
        shadow = self._relay_shadows.get(address, 0)
        relay_values = [bool(shadow & (1 << index)) for index in range(8)]
        for channel, value in (relays or {}).items():
            relay_values[channel - 1] = value
        return ExtensionModule(
            id=f"xdo8_bus{self.xdo_bus}_addr{address:02x}",
            name="xDO-8",
            model="xDO-8",
            chip="MCP23008",
            bus=self.xdo_bus,
            address=f"0x{address:02x}",
            kind="relay_output",
            channels=8,
            available=available,
            relays=relay_values,
            error=error,
        )

    def _xdi16_module(self, address: int, available: bool, error: str | None) -> ExtensionModule:
        shadow = self._input_shadows.get(address, 0)
        return ExtensionModule(
            id=f"xdi16_bus{self.xdo_bus}_addr{address:02x}",
            name="xDI-16",
            model="xDI-16",
            chip="MCP23017",
            bus=self.xdo_bus,
            address=f"0x{address:02x}",
            kind="digital_input",
            channels=16,
            available=available,
            inputs=[bool(shadow & (1 << index)) for index in range(16)],
            error=error,
        )

    def _unknown_module(self, address: int, available: bool, error: str | None) -> ExtensionModule:
        return ExtensionModule(
            id=f"xbus_bus{self.xdo_bus}_addr{address:02x}",
            name="X-BUS module",
            model="Unknown",
            chip="Unknown",
            bus=self.xdo_bus,
            address=f"0x{address:02x}",
            kind="unknown",
            channels=0,
            available=available,
            relays=[],
            error=error,
        )

    def _probe(self, bus: int, address: int, register: int, length: int) -> bool:
        try:
            self._i2c_read_register(bus, address, register, length)
            return True
        except FileNotFoundError:
            return False
        except OSError as exc:
            if exc.errno in {errno.ENXIO, errno.EREMOTEIO, errno.EIO, errno.ETIMEDOUT}:
                return False
            raise

    def _write_power_direction(self) -> None:
        try:
            self._power_gpio_a = self._i2c_read_register(self.power_bus, self.power_address, self._mcp23017_gpioa, 1)[0]
            self._power_gpio_b = self._i2c_read_register(self.power_bus, self.power_address, self._mcp23017_gpiob, 1)[0]
        except OSError:
            self._power_gpio_a = 0
            self._power_gpio_b = 0
        self._i2c_write(self.power_bus, self.power_address, bytes([self._mcp23017_iodira, 0x00]))
        self._i2c_write(self.power_bus, self.power_address, bytes([self._mcp23017_iodirb, 0x00]))
        self._write_power_outputs()

    def _read_power_gpio_b(self) -> int:
        self._power_gpio_b = self._i2c_read_register(self.power_bus, self.power_address, self._mcp23017_gpiob, 1)[0]
        return self._power_gpio_b

    def _write_power_outputs(self) -> None:
        self._i2c_write(self.power_bus, self.power_address, bytes([self._mcp23017_gpioa, self._power_gpio_a & 0xFF]))
        self._i2c_write(self.power_bus, self.power_address, bytes([self._mcp23017_gpiob, self._power_gpio_b & 0xFF]))

    def _open_xdi16_interrupt(self) -> None:
        import gpiod

        self._xdi_chip = gpiod.Chip(self.gpiochip_path)
        self._xdi_interrupt_request = self._xdi_chip.request_lines(
            consumer="intellegyhub-xdi16-interrupt",
            config={
                self.xdi_interrupt_gpio: gpiod.LineSettings(
                    direction=gpiod.line.Direction.INPUT,
                    edge_detection=gpiod.line.Edge.BOTH,
                    bias=gpiod.line.Bias.DISABLED,
                )
            },
        )

    def _read_xdi16_interrupt_active(self) -> bool:
        import gpiod

        if self._xdi_interrupt_request is None:
            return False
        value = self._xdi_interrupt_request.get_value(self.xdi_interrupt_gpio)
        return value == gpiod.line.Value.INACTIVE

    def _watch_xdi16_interrupt(self) -> None:
        while not self._xdi_interrupt_stop.is_set():
            try:
                if not self._xdi_interrupt_request.wait_edge_events(timeout=0.2):
                    continue
                self._xdi_interrupt_request.read_edge_events()
                if self._read_xdi16_interrupt_active() and self._xdi_interrupt_callback:
                    self._xdi_interrupt_callback()
            except Exception:
                time.sleep(1)

    def _initialize_xdo8(self, address: int) -> None:
        self._relay_shadows[address] = 0
        self._i2c_write(self.xdo_bus, address, bytes([0x02, 0x00]))
        self._i2c_write(self.xdo_bus, address, bytes([0x04, 0x00]))
        try:
            self._i2c_read_register(self.xdo_bus, address, 0x08, 1)
        except OSError:
            pass
        self._i2c_write(self.xdo_bus, address, bytes([self._mcp23008_iodir, 0x00]))
        self._write_xdo8_outputs(address, 0)

    def _write_xdo8_outputs(self, address: int, value: int) -> None:
        self._i2c_write(self.xdo_bus, address, bytes([0x0A, value & 0xFF]))
        self._i2c_write(self.xdo_bus, address, bytes([self._mcp23008_gpio, value & 0xFF]))

    def _detect_module_type(self, address: int) -> str:
        original = self._i2c_read_register(self.xdo_bus, address, self._mcp_iocon, 1)[0]
        try:
            self._i2c_write(self.xdo_bus, address, bytes([self._mcp_iocon, 0x00]))
            zero = self._i2c_read_register(self.xdo_bus, address, self._mcp_iocon, 1)[0]
            self._i2c_write(self.xdo_bus, address, bytes([self._mcp_iocon, 0x20]))
            bank = self._i2c_read_register(self.xdo_bus, address, self._mcp_iocon, 1)[0]
            return "xdi16" if zero == 0x00 and bank == 0x20 else "xdo8"
        finally:
            self._i2c_write(self.xdo_bus, address, bytes([self._mcp_iocon, original]))

    def _initialize_xdi16(self, address: int) -> None:
        self._i2c_write(self.xdo_bus, address, bytes([0x04, 0x00]))
        self._i2c_write(self.xdo_bus, address, bytes([0x05, 0x00]))
        self._i2c_write(self.xdo_bus, address, bytes([0x0A, 0x44]))
        self._i2c_write(self.xdo_bus, address, bytes([0x0B, 0x44]))
        self._i2c_write(self.xdo_bus, address, bytes([0x00, 0xFF]))
        self._i2c_write(self.xdo_bus, address, bytes([0x01, 0xFF]))
        self._i2c_write(self.xdo_bus, address, bytes([0x02, 0x00]))
        self._i2c_write(self.xdo_bus, address, bytes([0x03, 0x00]))
        self._i2c_write(self.xdo_bus, address, bytes([0x0C, 0xFF]))
        self._i2c_write(self.xdo_bus, address, bytes([0x0D, 0xFF]))
        self._i2c_write(self.xdo_bus, address, bytes([0x08, 0x00]))
        self._i2c_write(self.xdo_bus, address, bytes([0x09, 0x00]))
        self._read_xdi16_inputs(address)
        self._i2c_write(self.xdo_bus, address, bytes([0x04, 0xFF]))
        self._i2c_write(self.xdo_bus, address, bytes([0x05, 0xFF]))

    def _read_xdi16_inputs(self, address: int) -> int:
        interrupt_a = self._i2c_read_register(self.xdo_bus, address, 0x0E, 1)[0]
        interrupt_b = self._i2c_read_register(self.xdo_bus, address, 0x0F, 1)[0]
        if interrupt_a:
            self._i2c_read_register(self.xdo_bus, address, 0x10, 1)
        if interrupt_b:
            self._i2c_read_register(self.xdo_bus, address, 0x11, 1)
        gpio_a = self._i2c_read_register(self.xdo_bus, address, 0x12, 1)[0]
        gpio_b = self._i2c_read_register(self.xdo_bus, address, 0x13, 1)[0]
        raw = gpio_a | (gpio_b << 8)
        active = (~raw) & 0xFFFF
        self._input_shadows[address] = active
        return active

    @staticmethod
    def _i2c_write(bus: int, address: int, payload: bytes) -> None:
        import fcntl

        with Path(f"/dev/i2c-{bus}").open("rb+", buffering=0) as device:
            fcntl.ioctl(device, I2C_SLAVE, address)
            os.write(device.fileno(), payload)

    @staticmethod
    def _i2c_read_register(bus: int, address: int, register: int, length: int) -> bytes:
        import fcntl

        with Path(f"/dev/i2c-{bus}").open("rb+", buffering=0) as device:
            fcntl.ioctl(device, I2C_SLAVE, address)
            os.write(device.fileno(), bytes([register]))
            return os.read(device.fileno(), length)


class ExtensionManager:
    def __init__(self, hardware: ExtensionHardware | None = None, store: ExtensionStore | None = None) -> None:
        self.hardware = hardware or ExtensionHardware()
        self.store = store or ExtensionStore()
        self.power_on = False
        self.power_available = False
        self.modules: dict[str, ExtensionModule] = {}
        self.error: str | None = "startup pending"
        self._lock = asyncio.Lock()
        self._publisher: Callable[[dict[str, Any]], Any] | None = None
        self._xdi16_task: asyncio.Task | None = None
        self._xdi16_wake = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stopped = asyncio.Event()

    def set_publisher(self, publisher: Callable[[dict[str, Any]], Any]) -> None:
        self._publisher = publisher

    async def start(self) -> None:
        try:
            self._loop = asyncio.get_running_loop()
            await self.store.initialize()
            self.modules = {module.id: module for module in await self.store.load_modules()}
            desired_power = await self.store.load_power()
            await self.hardware.initialize()
            self.power_on = await self.hardware.set_power(desired_power)
            self.power_available = True
            self.error = None
            if self.power_on and self.modules:
                await asyncio.sleep(POWER_SETTLE_SECONDS)
                await self._scan_locked()
            self._stopped.clear()
            await self.hardware.start_xdi16_interrupt(self._wake_xdi16_interrupt)
            self._xdi16_task = asyncio.create_task(self._run_xdi16_interrupt_worker(), name="intellegyhub_xdi16_interrupt")
            self._wake_xdi16_interrupt()
        except Exception as exc:
            self.power_on = False
            self.power_available = False
            self.error = str(exc)

    async def stop(self) -> None:
        self._stopped.set()
        await self.hardware.stop_xdi16_interrupt()
        if self._xdi16_task:
            self._xdi16_task.cancel()
            try:
                await self._xdi16_task
            except asyncio.CancelledError:
                pass
            self._xdi16_task = None
        await self.store.save_power(self.power_on)
        await self.store.save_modules(list(self.modules.values()))

    def snapshot(self) -> dict[str, Any]:
        return {
            "power": {
                "on": self.power_on,
                "available": self.power_available,
                "controller": {"bus": 10, "address": "0x20", "chip": "MCP23017", "pin": "GPB7"},
            },
            "modules": [asdict(module) for module in sorted(self.modules.values(), key=_module_sort_key)],
            "error": self.error,
        }

    async def set_power(self, on: bool) -> dict[str, Any]:
        async with self._lock:
            self.power_on = await self.hardware.set_power(on)
            await self.store.save_power(self.power_on)
            if self.power_on:
                await asyncio.sleep(POWER_SETTLE_SECONDS)
                await self._refresh_stored_modules_locked()
                self._wake_xdi16_interrupt()
            else:
                self._mark_modules_unavailable("BUS_POWER_OFF")
                await self.store.save_modules(list(self.modules.values()))
            self.error = None
            await self._publish({"type": "extension_power_changed", "on": self.power_on})
            await self._publish({"type": "extensions_changed", "extensions": self.snapshot()})
            return self.snapshot()

    async def scan(self) -> dict[str, Any]:
        async with self._lock:
            removed_ids = await self._scan_locked()
            self._wake_xdi16_interrupt()
            self.error = None
            for module_id in removed_ids:
                await self._publish({"type": "extension_module_removed", "module_id": module_id})
            await self._publish({"type": "extensions_changed", "extensions": self.snapshot()})
            return self.snapshot()

    async def set_relay(self, module_id: str, channel: int, on: bool) -> dict[str, Any]:
        async with self._lock:
            if not self.power_on:
                raise RuntimeError("Extension bus power is off")
            module = self.modules.get(module_id)
            if module is None:
                raise ValueError("Extension module not found")
            if module.kind != "relay_output":
                raise ValueError("Extension module is not a relay output module")
            updated = await self.hardware.set_xdo8_relay(module.address, channel, on)
            self.modules[module_id] = updated
            await self.store.save_modules(list(self.modules.values()))
            await self._publish({"type": "extension_module_changed", "module": asdict(updated)})
            await self._publish({"type": "extensions_changed", "extensions": self.snapshot()})
            return {"module": asdict(updated), "channel": channel, "on": on}

    async def delete_module(self, module_id: str) -> dict[str, Any]:
        async with self._lock:
            module = self.modules.pop(module_id, None)
            if module is None:
                raise ValueError("Extension module not found")
            await self.store.delete_module(module_id)
            await self._publish({"type": "extension_module_removed", "module_id": module_id})
            await self._publish({"type": "extensions_changed", "extensions": self.snapshot()})
            return {"removed": module_id, "extensions": self.snapshot()}

    def _mark_modules_unavailable(self, error: str | None) -> None:
        for module in self.modules.values():
            module.available = False
            module.error = error

    async def _scan_locked(self) -> list[str]:
        found = await self.hardware.scan_modules(self.power_on)
        previous = dict(self.modules)
        detected = {module.id: module for module in found}
        detected_addresses = {_module_address_key(module) for module in found}
        removed_ids: list[str] = []
        for module_id, module in previous.items():
            if module_id in detected and module.kind == "relay_output" and module.relays:
                detected[module_id] = await self.hardware.set_xdo8_relays(module.address, module.relays)
            elif module_id in detected and module.kind == "digital_input":
                detected[module_id] = await self.hardware.read_xdi16_inputs(module.address)
            elif _module_address_key(module) in detected_addresses:
                removed_ids.append(module_id)
            elif module_id not in detected:
                module.available = False
                module.error = None if self.power_on else "BUS_POWER_OFF"
                detected[module_id] = module
        self.modules = detected
        await self.store.save_modules(list(self.modules.values()))
        return removed_ids

    async def _refresh_stored_modules_locked(self) -> None:
        if not self.power_on:
            self._mark_modules_unavailable("BUS_POWER_OFF")
            await self.store.save_modules(list(self.modules.values()))
            return
        updated_modules: dict[str, ExtensionModule] = {}
        for module_id, module in self.modules.items():
            try:
                if module.kind == "relay_output":
                    updated_modules[module_id] = await self.hardware.set_xdo8_relays(module.address, module.relays or [False] * 8)
                elif module.kind == "digital_input":
                    updated_modules[module_id] = await self.hardware.read_xdi16_inputs(module.address)
                else:
                    updated_modules[module_id] = module
            except Exception as exc:
                module.available = False
                module.error = str(exc)
                updated_modules[module_id] = module
        self.modules = updated_modules
        await self.store.save_modules(list(self.modules.values()))

    async def _refresh_xdi16_locked(self) -> bool:
        if not self.power_on:
            return False
        changed = False
        for module_id, module in list(self.modules.items()):
            if module.kind != "digital_input" or not module.available:
                continue
            previous = list(module.inputs or [])
            try:
                updated = await self.hardware.read_xdi16_inputs(module.address)
            except Exception as exc:
                module.available = False
                module.error = str(exc)
                self.modules[module_id] = module
                changed = True
                continue
            self.modules[module_id] = updated
            if previous != list(updated.inputs or []):
                changed = True
        if changed:
            await self.store.save_modules(list(self.modules.values()))
        return changed

    def _wake_xdi16_interrupt(self) -> None:
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._xdi16_wake.set)
        else:
            self._xdi16_wake.set()

    async def _run_xdi16_interrupt_worker(self) -> None:
        while not self._stopped.is_set():
            try:
                await self._xdi16_wake.wait()
                self._xdi16_wake.clear()
                consecutive_active = 0
                while not self._stopped.is_set():
                    changed = False
                    for _ in range(XDI16_INTERRUPT_DRAIN_LIMIT):
                        async with self._lock:
                            changed = await self._refresh_xdi16_locked() or changed
                        if not await self.hardware.xdi16_interrupt_active():
                            break
                    else:
                        consecutive_active += 1
                        await asyncio.sleep(min(5.0, XDI16_INTERRUPT_RETRY_SECONDS * min(consecutive_active, 20)))
                        continue

                    if changed:
                        await self._publish({"type": "extensions_changed", "extensions": self.snapshot()})
                    break
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(XDI16_INTERRUPT_RETRY_SECONDS)

    async def _publish(self, message: dict[str, Any]) -> None:
        if self._publisher:
            result = self._publisher(message)
            if asyncio.iscoroutine(result):
                await result


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


def _module_address_key(module: ExtensionModule) -> tuple[int, str]:
    return module.bus, module.address.lower()


def _module_sort_key(module: ExtensionModule) -> tuple[int, int, str]:
    try:
        address = int(module.address, 16)
    except ValueError:
        address = 999
    return module.bus, address, module.id
