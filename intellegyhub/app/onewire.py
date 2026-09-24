from __future__ import annotations

import asyncio
import errno
import json
import os
import sqlite3
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from .carrier import CarrierManager

I2C_SLAVE = 0x0703
POWER_SETTLE_SECONDS = 0.35
DS18B20_CONVERT_SECONDS = 0.75


@dataclass
class OneWireBridge:
    id: str
    name: str
    model: str
    chip: str
    bus: int
    address: str
    available: bool
    enabled: bool
    sensors: list[str]
    error: str | None = None


@dataclass
class OneWireSensor:
    id: str
    name: str
    model: str
    family: str
    rom: str
    bridge_id: str
    bus: int
    address: str
    available: bool
    added: bool = False
    temperature_c: float | None = None
    error: str | None = None


class OneWireStore:
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

    async def load_bridge_enabled(self, addresses: tuple[int, ...]) -> dict[str, bool]:
        return await asyncio.to_thread(self._load_bridge_enabled_sync, addresses)

    async def save_bridge_enabled(self, bridge_id: str, enabled: bool) -> None:
        async with self._lock:
            await asyncio.to_thread(self._save_setting_sync, f"bridge_enabled_{bridge_id}", "1" if enabled else "0")

    async def load_sensors(self) -> list[OneWireSensor]:
        return await asyncio.to_thread(self._load_sensors_sync)

    async def save_sensors(self, sensors: list[OneWireSensor]) -> None:
        async with self._lock:
            await asyncio.to_thread(self._save_sensors_sync, sensors)

    async def delete_sensor(self, sensor_id: str) -> None:
        async with self._lock:
            await asyncio.to_thread(self._delete_sensor_sync, sensor_id)

    async def set_sensor_added(self, sensor_id: str, added: bool) -> None:
        async with self._lock:
            await asyncio.to_thread(self._set_sensor_added_sync, sensor_id, added)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(self.path)

    def _initialize_sync(self) -> None:
        with self._connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS onewire_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS onewire_sensors (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    model TEXT NOT NULL,
                    family TEXT NOT NULL,
                    rom TEXT NOT NULL,
                    bridge_id TEXT NOT NULL,
                    bus INTEGER NOT NULL,
                    address TEXT NOT NULL,
                    added INTEGER NOT NULL DEFAULT 0,
                    temperature_c REAL NULL,
                    error TEXT NULL
                )
                """
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(onewire_sensors)").fetchall()}
            if "added" not in columns:
                db.execute("ALTER TABLE onewire_sensors ADD COLUMN added INTEGER NOT NULL DEFAULT 0")

    def _load_power_sync(self) -> bool:
        with self._connect() as db:
            row = db.execute("SELECT value FROM onewire_settings WHERE key='bus_power'").fetchone()
        return bool(row and row[0] == "1")

    def _save_power_sync(self, on: bool) -> None:
        self._save_setting_sync("bus_power", "1" if on else "0")

    def _save_setting_sync(self, key: str, value: str) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO onewire_settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def _load_bridge_enabled_sync(self, addresses: tuple[int, ...]) -> dict[str, bool]:
        keys = {f"onewire_bus10_addr{address:02x}": f"bridge_enabled_onewire_bus10_addr{address:02x}" for address in addresses}
        with self._connect() as db:
            rows = db.execute(
                "SELECT key,value FROM onewire_settings WHERE key LIKE 'bridge_enabled_onewire_bus10_addr%'"
            ).fetchall()
        values = {row[0]: row[1] for row in rows}
        return {bridge_id: values.get(key, "1") == "1" for bridge_id, key in keys.items()}

    def _load_sensors_sync(self) -> list[OneWireSensor]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT id,name,model,family,rom,bridge_id,bus,address,added,temperature_c,error "
                "FROM onewire_sensors ORDER BY id"
            ).fetchall()
        return [
            OneWireSensor(
                id=row[0],
                name=row[1],
                model=row[2],
                family=row[3],
                rom=row[4],
                bridge_id=row[5],
                bus=row[6],
                address=row[7],
                available=False,
                added=bool(row[8]),
                temperature_c=row[9],
                error=row[10],
            )
            for row in rows
        ]

    def _save_sensors_sync(self, sensors: list[OneWireSensor]) -> None:
        with self._connect() as db:
            sensor_ids = [sensor.id for sensor in sensors]
            if sensor_ids:
                placeholders = ",".join("?" for _ in sensor_ids)
                db.execute(f"DELETE FROM onewire_sensors WHERE id NOT IN ({placeholders})", sensor_ids)
            else:
                db.execute("DELETE FROM onewire_sensors")
            for sensor in sensors:
                db.execute(
                    """
                    INSERT INTO onewire_sensors(id,name,model,family,rom,bridge_id,bus,address,added,temperature_c,error)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET
                      name=excluded.name,
                      model=excluded.model,
                      family=excluded.family,
                      rom=excluded.rom,
                      bridge_id=excluded.bridge_id,
                      bus=excluded.bus,
                      address=excluded.address,
                      added=excluded.added,
                      temperature_c=excluded.temperature_c,
                      error=excluded.error
                    """,
                    (
                        sensor.id,
                        sensor.name,
                        sensor.model,
                        sensor.family,
                        sensor.rom,
                        sensor.bridge_id,
                        sensor.bus,
                        sensor.address,
                        1 if sensor.added else 0,
                        sensor.temperature_c,
                        sensor.error,
                    ),
                )

    def _delete_sensor_sync(self, sensor_id: str) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM onewire_sensors WHERE id=?", (sensor_id,))

    def _set_sensor_added_sync(self, sensor_id: str, added: bool) -> None:
        with self._connect() as db:
            db.execute("UPDATE onewire_sensors SET added=? WHERE id=?", (1 if added else 0, sensor_id))


class OneWireHardware:
    power_bus = 10
    power_address = 0x20
    power_bit = 14
    bridge_bus = 10
    bridge_addresses = (0x1A, 0x1B)

    _mcp23017_iodira = 0x00
    _mcp23017_iodirb = 0x01
    _mcp23017_gpioa = 0x12
    _mcp23017_gpiob = 0x13

    _ds2482_cmd_device_reset = 0xF0
    _ds2482_cmd_set_read_pointer = 0xE1
    _ds2482_cmd_write_config = 0xD2
    _ds2482_cmd_1wire_reset = 0xB4
    _ds2482_cmd_1wire_single_bit = 0x87
    _ds2482_cmd_1wire_write_byte = 0xA5
    _ds2482_cmd_1wire_read_byte = 0x96
    _ds2482_cmd_1wire_triplet = 0x78
    _ds2482_ptr_status = 0xF0
    _ds2482_ptr_data = 0xE1
    _status_1wb = 0x01
    _status_ppd = 0x02
    _status_sbr = 0x20
    _status_tsb = 0x40
    _status_dir = 0x80

    def __init__(self, carrier: CarrierManager | None = None) -> None:
        self._lock = asyncio.Lock()
        self.carrier = carrier or CarrierManager()
        self._power_gpio_a = 0
        self._power_gpio_b = 0

    async def initialize(self) -> bool:
        if _is_mock_platform():
            return True
        async with self._lock:
            if not self._probe_register(self.power_bus, self.power_address, self._mcp23017_iodirb, 1):
                return False
            return await self.carrier.onewire_power_on()

    async def set_power(self, on: bool) -> bool:
        if _is_mock_platform():
            return on
        async with self._lock:
            await self.carrier.set_onewire_power(on)
            return on

    async def scan(self, power_on: bool, enabled_bridges: dict[str, bool]) -> tuple[list[OneWireBridge], list[OneWireSensor]]:
        if _is_mock_platform():
            if not power_on:
                return [], []
            sensors = [
                self._sensor(0x1A, bytes.fromhex("28ff641d7216035c"), True, None, 23.625),
                self._sensor(0x1A, bytes.fromhex("28ff0c4a7216039b"), True, None, 24.125),
                self._sensor(0x1B, bytes.fromhex("28ff7b91721604d1"), True, None, 21.875),
            ]
            bridge_sensors = {
                self._bridge_id(0x1A): [sensor.id for sensor in sensors if sensor.address == "0x1a"],
                self._bridge_id(0x1B): [sensor.id for sensor in sensors if sensor.address == "0x1b"],
            }
            bridges = [
                self._bridge(0x1A, enabled_bridges.get(self._bridge_id(0x1A), True), enabled_bridges.get(self._bridge_id(0x1A), True), bridge_sensors[self._bridge_id(0x1A)], None),
                self._bridge(0x1B, enabled_bridges.get(self._bridge_id(0x1B), True), enabled_bridges.get(self._bridge_id(0x1B), True), bridge_sensors[self._bridge_id(0x1B)], None),
            ]
            return bridges, sensors
        if not power_on:
            return [], []
        async with self._lock:
            bridges: list[OneWireBridge] = []
            sensors: list[OneWireSensor] = []
            for address in self.bridge_addresses:
                bridge_id = self._bridge_id(address)
                bridge_sensors: list[str] = []
                if not enabled_bridges.get(bridge_id, True):
                    bridges.append(self._bridge(address, False, False, bridge_sensors, "POLLING_DISABLED"))
                    continue
                if not self._probe_ds2482(address):
                    continue
                try:
                    roms = await asyncio.to_thread(self._search_ds18b20, address)
                    for rom in roms:
                        sensor = self._sensor(address, rom, True, None)
                        bridge_sensors.append(sensor.id)
                        sensors.append(sensor)
                    bridges.append(self._bridge(address, True, True, bridge_sensors, None))
                except Exception as exc:
                    bridges.append(self._bridge(address, False, True, bridge_sensors, str(exc)))
                    continue
            return bridges, sensors

    async def read_temperatures(self, sensors: list[OneWireSensor], power_on: bool) -> list[OneWireSensor]:
        if _is_mock_platform():
            if not power_on:
                return []
            updated = []
            for index, sensor in enumerate(sensors):
                sensor.available = True
                sensor.error = None
                sensor.temperature_c = round(22.25 + index * 0.5, 3)
                updated.append(sensor)
            return updated
        if not power_on:
            return []
        async with self._lock:
            updated: list[OneWireSensor] = []
            for sensor in sensors:
                try:
                    address = int(sensor.address, 16)
                    temperature = await asyncio.to_thread(self._read_ds18b20_temperature, address, sensor.rom)
                    updated.append(self._sensor(address, bytes.fromhex(sensor.rom), True, None, temperature))
                except Exception as exc:
                    sensor.available = False
                    sensor.error = str(exc)
                    updated.append(sensor)
            return updated

    def _bridge(
        self,
        address: int,
        available: bool,
        enabled: bool,
        sensors: list[str],
        error: str | None,
    ) -> OneWireBridge:
        bridge_number = self.bridge_addresses.index(address) + 1 if address in self.bridge_addresses else address
        return OneWireBridge(
            id=self._bridge_id(address),
            name=f"1-Wire Bridge {bridge_number}",
            model="DS2482S-100",
            chip="DS2482S-100",
            bus=self.bridge_bus,
            address=f"0x{address:02x}",
            available=available,
            enabled=enabled,
            sensors=sorted(sensors),
            error=error,
        )

    def _sensor(
        self,
        address: int,
        rom: bytes,
        available: bool,
        error: str | None,
        temperature_c: float | None = None,
    ) -> OneWireSensor:
        rom_hex = rom.hex()
        return OneWireSensor(
            id=f"ds18b20_bus{self.bridge_bus}_addr{address:02x}_{rom_hex}",
            name=f"DS18B20 {rom_hex}",
            model="DS18B20",
            family=f"0x{rom[0]:02x}",
            rom=rom_hex,
            bridge_id=self._bridge_id(address),
            bus=self.bridge_bus,
            address=f"0x{address:02x}",
            available=available,
            temperature_c=temperature_c,
            error=error,
        )

    def _probe_register(self, bus: int, address: int, register: int, length: int) -> bool:
        try:
            self._i2c_read_register(bus, address, register, length)
            return True
        except FileNotFoundError:
            return False
        except OSError as exc:
            if exc.errno in {errno.ENXIO, errno.EREMOTEIO, errno.EIO, errno.ETIMEDOUT}:
                return False
            raise

    def _probe_ds2482(self, address: int) -> bool:
        try:
            self._ds2482_reset(address)
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

    def _search_ds18b20(self, address: int) -> list[bytes]:
        self._ds2482_reset(address)
        self._ds2482_write_config(address, 0x01)
        return [rom for rom in self._onewire_search(address) if rom[0] == 0x28 and crc8(rom) == 0]

    def _read_ds18b20_temperature(self, address: int, rom_hex: str) -> float:
        rom = bytes.fromhex(rom_hex)
        self._ds2482_reset(address)
        self._ds2482_write_config(address, 0x01)
        if not self._onewire_reset(address):
            raise RuntimeError("No 1-Wire presence pulse")
        self._onewire_write_byte(address, 0x55)
        for value in rom:
            self._onewire_write_byte(address, value)
        self._onewire_write_byte(address, 0x44)
        time.sleep(DS18B20_CONVERT_SECONDS)
        if not self._onewire_reset(address):
            raise RuntimeError("No 1-Wire presence pulse after conversion")
        self._onewire_write_byte(address, 0x55)
        for value in rom:
            self._onewire_write_byte(address, value)
        self._onewire_write_byte(address, 0xBE)
        scratchpad = bytes(self._onewire_read_byte(address) for _ in range(9))
        if crc8(scratchpad) != 0:
            raise RuntimeError("DS18B20 scratchpad CRC failed")
        raw = int.from_bytes(scratchpad[:2], "little", signed=True)
        return round(raw / 16.0, 3)

    def _onewire_search(self, address: int) -> list[bytes]:
        last_discrepancy = 0
        last_device = False
        roms: list[bytes] = []
        while not last_device:
            if not self._onewire_reset(address):
                break
            self._onewire_write_byte(address, 0xF0)
            rom = [0] * 8
            discrepancy_marker = 0
            for bit_number in range(1, 65):
                byte_index = (bit_number - 1) // 8
                bit_mask = 1 << ((bit_number - 1) % 8)
                if bit_number < last_discrepancy:
                    direction = 1 if (roms[-1][byte_index] & bit_mask) else 0
                elif bit_number == last_discrepancy:
                    direction = 1
                else:
                    direction = 0
                status = self._onewire_triplet(address, direction)
                sbr = bool(status & self._status_sbr)
                tsb = bool(status & self._status_tsb)
                if sbr and tsb:
                    return roms
                chosen = 1 if (status & self._status_dir) else 0
                if not sbr and not tsb and chosen == 0:
                    discrepancy_marker = bit_number
                if chosen:
                    rom[byte_index] |= bit_mask
            candidate = bytes(rom)
            if crc8(candidate) == 0:
                roms.append(candidate)
            last_discrepancy = discrepancy_marker
            last_device = last_discrepancy == 0
        return roms

    def _ds2482_reset(self, address: int) -> None:
        self._i2c_write(self.bridge_bus, address, bytes([self._ds2482_cmd_device_reset]))
        self._wait_ready(address)

    def _ds2482_write_config(self, address: int, config: int) -> None:
        value = ((~config & 0x0F) << 4) | (config & 0x0F)
        self._i2c_write(self.bridge_bus, address, bytes([self._ds2482_cmd_write_config, value]))
        self._wait_ready(address)

    def _onewire_reset(self, address: int) -> bool:
        self._i2c_write(self.bridge_bus, address, bytes([self._ds2482_cmd_1wire_reset]))
        status = self._wait_ready(address)
        return bool(status & self._status_ppd)

    def _onewire_write_byte(self, address: int, value: int) -> None:
        self._i2c_write(self.bridge_bus, address, bytes([self._ds2482_cmd_1wire_write_byte, value & 0xFF]))
        self._wait_ready(address)

    def _onewire_read_byte(self, address: int) -> int:
        self._i2c_write(self.bridge_bus, address, bytes([self._ds2482_cmd_1wire_read_byte]))
        self._wait_ready(address)
        self._i2c_write(self.bridge_bus, address, bytes([self._ds2482_cmd_set_read_pointer, self._ds2482_ptr_data]))
        return self._i2c_read(self.bridge_bus, address, 1)[0]

    def _onewire_triplet(self, address: int, direction: int) -> int:
        self._i2c_write(self.bridge_bus, address, bytes([self._ds2482_cmd_1wire_triplet, 0x80 if direction else 0x00]))
        return self._wait_ready(address)

    def _wait_ready(self, address: int, timeout: float = 0.2) -> int:
        deadline = time.monotonic() + timeout
        status = 0
        while time.monotonic() < deadline:
            self._i2c_write(self.bridge_bus, address, bytes([self._ds2482_cmd_set_read_pointer, self._ds2482_ptr_status]))
            status = self._i2c_read(self.bridge_bus, address, 1)[0]
            if not status & self._status_1wb:
                return status
            time.sleep(0.001)
        raise TimeoutError("DS2482 1-Wire busy timeout")

    def _bridge_id(self, address: int) -> str:
        return f"onewire_bus{self.bridge_bus}_addr{address:02x}"

    @staticmethod
    def _i2c_write(bus: int, address: int, payload: bytes) -> None:
        import fcntl

        with Path(f"/dev/i2c-{bus}").open("rb+", buffering=0) as device:
            fcntl.ioctl(device, I2C_SLAVE, address)
            os.write(device.fileno(), payload)

    @staticmethod
    def _i2c_read(bus: int, address: int, length: int) -> bytes:
        import fcntl

        with Path(f"/dev/i2c-{bus}").open("rb+", buffering=0) as device:
            fcntl.ioctl(device, I2C_SLAVE, address)
            return os.read(device.fileno(), length)

    @staticmethod
    def _i2c_read_register(bus: int, address: int, register: int, length: int) -> bytes:
        import fcntl

        with Path(f"/dev/i2c-{bus}").open("rb+", buffering=0) as device:
            fcntl.ioctl(device, I2C_SLAVE, address)
            os.write(device.fileno(), bytes([register]))
            return os.read(device.fileno(), length)


class OneWireManager:
    def __init__(
        self,
        hardware: OneWireHardware | None = None,
        store: OneWireStore | None = None,
        poll_intervals: dict[str, int] | None = None,
    ) -> None:
        self.hardware = hardware or OneWireHardware()
        self.store = store or OneWireStore()
        self.poll_intervals = poll_intervals or {}
        self.power_on = False
        self.power_available = False
        self.bridge_enabled: dict[str, bool] = {}
        self.bridges: dict[str, OneWireBridge] = {}
        self.sensors: dict[str, OneWireSensor] = {}
        self.error: str | None = "startup pending"
        self._lock = asyncio.Lock()
        self._publisher: Callable[[dict[str, Any]], Any] | None = None
        self._poll_tasks: dict[str, asyncio.Task] = {}
        self._settle_scan_task: asyncio.Task[None] | None = None

    def set_publisher(self, publisher: Callable[[dict[str, Any]], Any]) -> None:
        self._publisher = publisher

    async def start(self) -> None:
        try:
            await self.store.initialize()
            self.bridge_enabled = await self.store.load_bridge_enabled(self.hardware.bridge_addresses)
            self.bridges = self._configured_bridges()
            self.sensors = {sensor.id: sensor for sensor in await self.store.load_sensors()}
            desired_power = await self.store.load_power()
            if _is_mock_platform() and type(self.hardware) is OneWireHardware:
                desired_power = True
            await self.hardware.initialize()
            self.power_on = await self.hardware.set_power(desired_power)
            self.power_available = True
            self.error = None
            if self.power_on:
                await asyncio.sleep(POWER_SETTLE_SECONDS)
                await self._scan_locked()
                self._sync_poll_tasks()
        except Exception as exc:
            self.power_on = False
            self.power_available = False
            self.error = str(exc)

    async def stop(self) -> None:
        if self._settle_scan_task:
            self._settle_scan_task.cancel()
            try:
                await self._settle_scan_task
            except asyncio.CancelledError:
                pass
            self._settle_scan_task = None
        await self._cancel_poll_tasks()
        await self.store.save_power(self.power_on)
        await self.store.save_sensors(list(self.sensors.values()))

    def snapshot(self) -> dict[str, Any]:
        return {
            "power": {
                "on": self.power_on,
                "available": self.power_available,
                "controller": {"bus": 10, "address": "0x20", "chip": "MCP23017", "pin": "GPB6"},
            },
            "bridges": [asdict(self.bridges[key]) for key in sorted(self.bridges)],
            "sensors": [asdict(self.sensors[key]) for key in sorted(self.sensors)],
            "error": self.error,
        }

    async def set_power(self, on: bool) -> dict[str, Any]:
        async with self._lock:
            self.power_on = await self.hardware.set_power(on)
            await self.store.save_power(self.power_on)
            if self.power_on:
                self.bridges = self._configured_bridges()
            else:
                if self._settle_scan_task:
                    self._settle_scan_task.cancel()
                    self._settle_scan_task = None
                self.bridges = self._configured_bridges()
                self._mark_sensors_unavailable("BUS_POWER_OFF")
                await self.store.save_sensors(list(self.sensors.values()))
            self._sync_poll_tasks()
            self.error = None
            await self._publish({"type": "onewire_power_changed", "on": self.power_on})
            await self._publish({"type": "onewire_changed", "onewire": self.snapshot()})
            return self.snapshot()

    async def scan(self) -> dict[str, Any]:
        async with self._lock:
            await self._scan_locked()
            self.error = None
            await self._publish({"type": "onewire_changed", "onewire": self.snapshot()})
            return self.snapshot()

    async def refresh(self) -> dict[str, Any]:
        async with self._lock:
            if not self.power_on:
                raise RuntimeError("1-Wire bus power is off")
            updated = await self.hardware.read_temperatures(self._enabled_sensors(), self.power_on)
            self._merge_sensors(updated)
            await self.store.save_sensors(list(self.sensors.values()))
            await self._publish({"type": "onewire_changed", "onewire": self.snapshot()})
            return self.snapshot()

    async def add_sensor(self, sensor_id: str) -> dict[str, Any]:
        async with self._lock:
            sensor = self.sensors.get(sensor_id)
            if sensor is None:
                raise ValueError("1-Wire sensor not found")
            sensor.added = True
            await self.store.set_sensor_added(sensor_id, True)
            await self._publish({"type": "onewire_changed", "onewire": self.snapshot()})
            return {"added": sensor_id, "onewire": self.snapshot()}

    async def set_bridge_enabled(self, bridge_id: str, enabled: bool) -> dict[str, Any]:
        async with self._lock:
            if bridge_id not in self.bridge_enabled:
                raise ValueError("1-Wire bridge not found")
            self.bridge_enabled[bridge_id] = enabled
            await self.store.save_bridge_enabled(bridge_id, enabled)
            if not enabled:
                self._mark_bridge_sensors_unavailable(bridge_id, "POLLING_DISABLED")
                address = self._bridge_address_from_id(bridge_id)
                if address is not None:
                    self.bridges[bridge_id] = self.hardware._bridge(address, False, False, [], "POLLING_DISABLED")
                await self.store.save_sensors(list(self.sensors.values()))
            elif self.power_on:
                await self._scan_locked()
            else:
                self.bridges = self._configured_bridges()
            self._sync_poll_tasks()
            await self._publish({"type": "onewire_changed", "onewire": self.snapshot()})
            return self.snapshot()

    async def delete_sensor(self, sensor_id: str) -> dict[str, Any]:
        async with self._lock:
            sensor = self.sensors.pop(sensor_id, None)
            if sensor is None:
                raise ValueError("1-Wire sensor not found")
            await self.store.delete_sensor(sensor_id)
            await self._publish({"type": "onewire_sensor_removed", "sensor_id": sensor_id})
            await self._publish({"type": "onewire_changed", "onewire": self.snapshot()})
            return {"removed": sensor_id, "onewire": self.snapshot()}

    def _mark_sensors_unavailable(self, error: str | None) -> None:
        for sensor in self.sensors.values():
            sensor.available = False
            sensor.error = error

    def _mark_bridge_sensors_unavailable(self, bridge_id: str, error: str | None) -> None:
        for sensor in self.sensors.values():
            if sensor.bridge_id == bridge_id:
                sensor.available = False
                sensor.error = error

    async def _scan_locked(self) -> None:
        if not self.power_on:
            self.bridges = self._configured_bridges()
            self._mark_sensors_unavailable("BUS_POWER_OFF")
            await self.store.save_sensors(list(self.sensors.values()))
            return
        bridges, found = await self.hardware.scan(self.power_on, self.bridge_enabled)
        previous = dict(self.sensors)
        detected = {sensor.id: sensor for sensor in found}
        if detected:
            read_back = await self.hardware.read_temperatures(list(detected.values()), self.power_on)
            detected.update({sensor.id: sensor for sensor in read_back})
        for sensor_id, sensor in detected.items():
            previous_sensor = previous.get(sensor_id)
            if previous_sensor is not None:
                sensor.added = previous_sensor.added
        for sensor_id, sensor in previous.items():
            if sensor_id not in detected:
                sensor.available = False
                sensor.error = None
                detected[sensor_id] = sensor
        self.bridges = {bridge.id: bridge for bridge in bridges}
        for bridge_id, bridge in self._configured_bridges().items():
            self.bridges.setdefault(bridge_id, bridge)
        self.sensors = detected
        await self.store.save_sensors(list(self.sensors.values()))

    def _merge_sensors(self, sensors: list[OneWireSensor]) -> None:
        for sensor in sensors:
            previous = self.sensors.get(sensor.id)
            if previous is not None:
                sensor.added = previous.added
            self.sensors[sensor.id] = sensor

    def _enabled_sensors(self) -> list[OneWireSensor]:
        return [
            sensor
            for sensor in self.sensors.values()
            if self.bridge_enabled.get(sensor.bridge_id, True)
        ]

    def _enabled_bridge_sensors(self, bridge_id: str) -> list[OneWireSensor]:
        return [
            sensor
            for sensor in self.sensors.values()
            if sensor.bridge_id == bridge_id and self.bridge_enabled.get(sensor.bridge_id, True)
        ]

    def _configured_bridges(self) -> dict[str, OneWireBridge]:
        return {
            self.hardware._bridge_id(address): self.hardware._bridge(
                address,
                False,
                self.bridge_enabled.get(self.hardware._bridge_id(address), True),
                [],
                None,
            )
            for address in self.hardware.bridge_addresses
        }

    def _bridge_address_from_id(self, bridge_id: str) -> int | None:
        for address in self.hardware.bridge_addresses:
            if self.hardware._bridge_id(address) == bridge_id:
                return address
        return None

    def _bridge_poll_interval(self, bridge_id: str) -> int:
        if bridge_id in self.poll_intervals:
            return self.poll_intervals[bridge_id]
        address = self._bridge_address_from_id(bridge_id)
        if address is not None:
            bridge_number = self.hardware.bridge_addresses.index(address) + 1
            key = f"bridge{bridge_number}"
            if key in self.poll_intervals:
                return self.poll_intervals[key]
        return 30

    def _sync_poll_tasks(self) -> None:
        wanted = {
            bridge_id
            for bridge_id, enabled in self.bridge_enabled.items()
            if self.power_on and enabled
        }
        for bridge_id, task in list(self._poll_tasks.items()):
            if bridge_id not in wanted:
                task.cancel()
                self._poll_tasks.pop(bridge_id, None)
        for bridge_id in wanted:
            task = self._poll_tasks.get(bridge_id)
            if task is None or task.done():
                self._poll_tasks[bridge_id] = asyncio.create_task(
                    self._poll_bridge_loop(bridge_id),
                    name=f"intellegyhub_onewire_poll_{bridge_id}",
                )

    def _schedule_settle_scan(self) -> None:
        if self._settle_scan_task:
            self._settle_scan_task.cancel()
        self._settle_scan_task = asyncio.create_task(
            self._settle_scan_after_power_on(),
            name="intellegyhub_onewire_settle_scan",
        )

    async def _settle_scan_after_power_on(self) -> None:
        try:
            await asyncio.sleep(POWER_SETTLE_SECONDS)
            async with self._lock:
                if not self.power_on:
                    return
                await self._scan_locked()
                self.error = None
                self._sync_poll_tasks()
                await self._publish({"type": "onewire_changed", "onewire": self.snapshot()})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            async with self._lock:
                self.error = str(exc)
                await self._publish({"type": "onewire_changed", "onewire": self.snapshot()})

    async def _cancel_poll_tasks(self) -> None:
        tasks = list(self._poll_tasks.values())
        self._poll_tasks.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _poll_bridge_loop(self, bridge_id: str) -> None:
        while True:
            interval = self._bridge_poll_interval(bridge_id)
            try:
                await asyncio.sleep(interval)
                await self._refresh_bridge(bridge_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                async with self._lock:
                    self._mark_bridge_sensors_unavailable(bridge_id, str(exc))
                    await self.store.save_sensors(list(self.sensors.values()))
                    await self._publish({"type": "onewire_changed", "onewire": self.snapshot()})

    async def _refresh_bridge(self, bridge_id: str) -> None:
        async with self._lock:
            if not self.power_on or not self.bridge_enabled.get(bridge_id, True):
                return
            sensors = self._enabled_bridge_sensors(bridge_id)
            if not sensors:
                await self._scan_locked()
                await self._publish({"type": "onewire_changed", "onewire": self.snapshot()})
                return
            updated = await self.hardware.read_temperatures(sensors, self.power_on)
            self._merge_sensors(updated)
            await self.store.save_sensors(list(self.sensors.values()))
            await self._publish({"type": "onewire_changed", "onewire": self.snapshot()})

    async def _publish(self, message: dict[str, Any]) -> None:
        if self._publisher:
            result = self._publisher(message)
            if asyncio.iscoroutine(result):
                await result


def crc8(data: bytes) -> int:
    crc = 0
    for value in data:
        byte = value
        for _ in range(8):
            mix = (crc ^ byte) & 0x01
            crc >>= 1
            if mix:
                crc ^= 0x8C
            byte >>= 1
    return crc


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
