from __future__ import annotations

import asyncio
import heapq
import itertools
import copy
import json
import logging
import os
import sqlite3
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Awaitable, Callable
from typing import Any

import yaml

try:
    from pymodbus.client import ModbusSerialClient
except ImportError:  # pragma: no cover - exercised only when optional runtime dependency is missing.
    ModbusSerialClient = None  # type: ignore[assignment]

try:
    from serial.tools import list_ports
except ImportError:  # pragma: no cover - optional when pyserial is not installed in pure mock dev.
    list_ports = None  # type: ignore[assignment]


VALID_TABLES = {"coil", "discrete_input", "holding_register", "input_register"}
VALID_CAPABILITY_TYPES = {"switch", "binary_input", "sensor", "number", "select"}
VALID_PROTOCOLS = {"modbus_rtu"}
LOGGER = logging.getLogger("intellegyhub.rs485")
MOCK_SERIAL_PORTS = {"/dev/ttyAMA3", "/dev/ttyAMA5"}
RS485_DEFAULT_POLLING = {
    "inputs": {"mode": "polling", "interval_ms": 100},
    "outputs": {"mode": "polling", "interval_ms": 250},
    "settings": {"mode": "on_demand"},
}
# Availability is time-based, rather than attempt-based. This keeps behavior
# consistent when the configured poll interval changes: five seconds without a
# valid complete poll means OFFLINE, and the next valid poll restores ONLINE.
RS485_OFFLINE_AFTER_SECONDS = 5.0
RS485_ONLINE_AFTER_SUCCESSES = 1
# A USB-RS485 adapter/device may need more than one 50 ms polling tick to
# turn the bus around and return a complete RTU frame. Keep retries disabled,
# but allow the current transaction enough time to finish cleanly.
RS485_RESPONSE_TIMEOUT_SECONDS = 0.5
RS485_DIAGNOSTICS_MAX_ERRORS = 5
MODBUS_EXCEPTION_CODES = {
    1: "Illegal Function",
    2: "Illegal Data Address",
    3: "Illegal Data Value",
    4: "Server Device Failure",
    6: "Server Device Busy",
    10: "Gateway Path Unavailable",
    11: "Gateway Target Device Failed to Respond",
}


def decode_modbus_frame(frame_hex: str | None) -> dict[str, Any]:
    """Decode the common RTU PDU fields without hiding malformed input."""
    raw = " ".join(str(frame_hex or "").split()).upper()
    try:
        data = bytes.fromhex(raw)
    except ValueError:
        return {"valid": False, "error": "malformed frame", "raw": raw}
    if len(data) < 4:
        return {"valid": False, "error": "malformed frame", "raw": raw}
    slave, function = data[0], data[1]
    result: dict[str, Any] = {"valid": True, "raw": raw, "slave": slave, "function": function}
    if function & 0x80:
        code = data[2] if len(data) > 2 else None
        result.update({"exception": code, "exception_name": MODBUS_EXCEPTION_CODES.get(code, "Unknown exception"), "valid": len(data) >= 5})
        return result
    if function in (1, 2, 3, 4) and len(data) >= 5:
        if len(data) >= 8:
            result.update({"address": int.from_bytes(data[2:4], "big"), "quantity": int.from_bytes(data[4:6], "big")})
        else:
            result["byte_count"] = data[2]
            result["data"] = list(data[3:-2])
    elif function in (5, 6) and len(data) >= 8:
        result.update({"address": int.from_bytes(data[2:4], "big"), "value": int.from_bytes(data[4:6], "big")})
    elif function in (15, 16) and len(data) >= 8:
        result.update({"address": int.from_bytes(data[2:4], "big"), "quantity": int.from_bytes(data[4:6], "big")})
    else:
        result["valid"] = False
        result["error"] = "malformed frame"
    result["crc"] = data[-2:].hex(" ").upper()
    return result


def classify_modbus_error(message: str) -> str | None:
    """Return the stable diagnostics bucket for a transport/protocol error."""
    text = str(message or "").lower()
    if "timeout" in text or "no response" in text:
        return "timeout"
    if "crc" in text:
        return "crc"
    if "exception" in text:
        return "exception"
    if "malformed" in text or "invalid frame" in text:
        return "malformed"
    if "wrong slave" in text:
        return "wrong_slave"
    if "wrong function" in text or "unexpected function" in text:
        return "wrong_function"
    return "protocol"


def validate_modbus_frame(frame_hex: str | None, expected_slave: int | None = None, expected_function: int | None = None) -> dict[str, Any]:
    decoded = decode_modbus_frame(frame_hex)
    raw = bytes.fromhex(str(frame_hex or "").replace(" ", "")) if decoded.get("valid") else b""
    if len(raw) >= 4:
        crc = 0xFFFF
        for byte in raw[:-2]:
            crc ^= byte
            for _ in range(8):
                crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
        expected_crc = bytes((crc & 0xFF, (crc >> 8) & 0xFF))
        decoded["crc_valid"] = raw[-2:] == expected_crc
        if not decoded["crc_valid"]:
            decoded.update({"valid": False, "error": "crc error"})
    if expected_slave is not None and decoded.get("slave") != expected_slave:
        decoded.update({"valid": False, "error": "wrong slave"})
    if expected_function is not None and decoded.get("function") not in {expected_function, expected_function | 0x80}:
        decoded.update({"valid": False, "error": "unexpected function"})
    return decoded


class Rs485PollPreempted(Exception):
    pass


class _NoopAsyncLock:
    """Async context-manager used when the bus queue is the serializer.

    Holding an application lock across a queued Modbus operation would defeat
    priority: a mode request could keep a later FC05 request out of the heap.
    The per-port bus worker is the physical transaction serializer.
    """

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


@dataclass
class Rs485Template:
    template_id: str
    manufacturer: str
    model: str
    name: str
    description: str
    protocol: str
    identity: dict[str, Any]
    communication: dict[str, Any]
    discovery: dict[str, Any]
    polling: dict[str, Any]
    groups: dict[str, dict[str, Any]]
    points: dict[str, dict[str, Any]]
    capabilities: dict[str, dict[str, Any]]
    errors: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "template_id": self.template_id,
            "manufacturer": self.manufacturer,
            "model": self.model,
            "name": self.name,
            "description": self.description,
            "protocol": self.protocol,
            "identity": self.identity,
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            **self.summary(),
            "communication": self.communication,
            "discovery": self.discovery,
            "polling": self.polling,
            "groups": self.groups,
            "points": self.points,
            "capabilities": normalized_capabilities(self),
            "errors": self.errors,
        }


@dataclass
class Rs485Device:
    id: str
    name: str
    template_id: str
    serial_port: str
    baudrate: int
    parity: str
    stop_bits: int
    slave_address: int
    enabled: bool = True
    read_enabled: bool = True
    write_enabled: bool = True
    collapsed: bool = False
    values: dict[str, Any] = field(default_factory=dict)
    runtime: dict[str, Any] = field(default_factory=dict)
    polling: dict[str, Any] = field(default_factory=dict)

    def snapshot(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Rs485BusJob:
    priority: int
    sequence: int
    operation: Any
    future: asyncio.Future


class Rs485TemplateRegistry:
    def __init__(self, template_dirs: list[Path] | None = None) -> None:
        self.built_in_dir = Path(__file__).resolve().parent / "rs485_templates"
        self.upload_dir = _default_template_upload_dir()
        self.template_dirs = template_dirs or [
            self.built_in_dir,
            self.upload_dir,
        ]
        self.templates: dict[str, Rs485Template] = {}
        self.template_sources: dict[str, str] = {}
        self.errors: list[dict[str, str]] = []
        self.disabled_built_ins: set[str] = set()

    def reload(self) -> dict[str, Any]:
        templates: dict[str, Rs485Template] = {}
        sources: dict[str, str] = {}
        errors: list[dict[str, str]] = []
        self.disabled_built_ins = self._load_disabled_built_ins()
        for directory in self.template_dirs:
            if not directory.exists():
                continue
            for path in sorted([*directory.glob("*.yaml"), *directory.glob("*.yml")]):
                try:
                    template = self._load_one(path)
                    source = "uploaded" if directory == self.upload_dir else "built-in"
                    if source == "built-in" and template.template_id in self.disabled_built_ins:
                        continue
                    if template.template_id in templates and source != "uploaded":
                        raise ValueError(f'duplicate template_id "{template.template_id}"')
                    templates[template.template_id] = template
                    sources[template.template_id] = source
                except Exception as exc:
                    errors.append({"template": path.name, "error": str(exc)})
        self.templates = templates
        self.template_sources = sources
        self.errors = errors
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        return {
            "templates": [
                {**template.summary(), "source": self.template_sources.get(template.template_id, "built-in")}
                for template in sorted(self.templates.values(), key=lambda item: item.model)
            ],
            "errors": list(self.errors),
        }

    def get(self, template_id: str) -> Rs485Template:
        template = self.templates.get(template_id)
        if template is None:
            raise KeyError(template_id)
        return template

    def validate_template_bytes(self, data: bytes, filename: str = "uploaded.yaml") -> Rs485Template:
        try:
            raw = yaml.safe_load(data.decode("utf-8")) or {}
        except UnicodeDecodeError as exc:
            raise ValueError("template must be UTF-8 YAML") from exc
        return self._template_from_raw(raw, filename)

    def delete_uploaded_template(self, template_id: str) -> None:
        if self.template_sources.get(template_id) != "uploaded":
            raise ValueError("only uploaded templates can be deleted")
        target = self.upload_dir / f"{template_id}.yaml"
        if not target.exists():
            raise ValueError("uploaded template file not found")
        target.unlink()
        self.reload()

    def delete_template(self, template_id: str) -> None:
        source = self.template_sources.get(template_id)
        if source == "uploaded":
            self.delete_uploaded_template(template_id)
            return
        if source == "built-in":
            disabled = self._load_disabled_built_ins()
            disabled.add(template_id)
            self._save_disabled_built_ins(disabled)
            self.reload()
            return
        raise ValueError("template not found")

    def _disabled_file(self) -> Path:
        return self.upload_dir / ".disabled-built-ins.json"

    def _load_disabled_built_ins(self) -> set[str]:
        path = self._disabled_file()
        if not path.exists():
            return set()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return set()
        if not isinstance(raw, list):
            return set()
        return {item for item in raw if isinstance(item, str)}

    def _save_disabled_built_ins(self, template_ids: set[str]) -> None:
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self._disabled_file().write_text(json.dumps(sorted(template_ids), indent=2), encoding="utf-8")

    def has_built_in_template(self, template_id: str) -> bool:
        if not self.built_in_dir.exists():
            return False
        for path in sorted([*self.built_in_dir.glob("*.yaml"), *self.built_in_dir.glob("*.yml")]):
            try:
                if self._load_one(path).template_id == template_id:
                    return True
            except Exception:
                continue
        return False

    def _load_one(self, path: Path) -> Rs485Template:
        with path.open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
        return self._template_from_raw(raw, path.name)

    def _template_from_raw(self, raw: Any, source_name: str) -> Rs485Template:
        if not isinstance(raw, dict):
            raise ValueError(f"{source_name}: template root must be an object")
        if raw.get("schema_version") != 1:
            raise ValueError(f"{source_name}: unsupported or missing schema_version")

        device = _dict(raw.get("device"), "device")
        template_id = _text(device.get("template_id"), "device.template_id")
        model = _text(device.get("model"), "device.model")
        protocol = _text(device.get("protocol"), "device.protocol")
        if protocol not in VALID_PROTOCOLS:
            raise ValueError(f'unsupported protocol "{protocol}"')

        communication = _dict(raw.get("communication"), "communication")
        identity = _dict(raw.get("identity") or {}, "identity")
        discovery = _dict(raw.get("discovery") or {}, "discovery")
        polling = _dict(raw.get("polling") or {}, "polling")
        groups = _dict(raw.get("groups"), "groups")
        points = _dict(raw.get("points"), "points")
        capabilities = _dict(raw.get("capabilities"), "capabilities")

        for point_id, point in points.items():
            if not isinstance(point, dict):
                raise ValueError(f'point "{point_id}" must be an object')
            table = point.get("table")
            if table not in VALID_TABLES:
                raise ValueError(f'point "{point_id}" has invalid table "{table}"')

        for capability_id, capability in capabilities.items():
            if not isinstance(capability, dict):
                raise ValueError(f'capability "{capability_id}" must be an object')
            cap_type = capability.get("type")
            if cap_type not in VALID_CAPABILITY_TYPES:
                raise ValueError(f'capability "{capability_id}" has invalid type "{cap_type}"')
            source = capability.get("source")
            if isinstance(source, str) and source not in points:
                raise ValueError(f'capability "{capability_id}" references missing point "{source}"')
            group = capability.get("group")
            if isinstance(group, str) and group not in groups:
                raise ValueError(f'capability "{capability_id}" references missing group "{group}"')

        return Rs485Template(
            template_id=template_id,
            manufacturer=str(device.get("manufacturer") or "IntellegyHUB"),
            model=model,
            name=str(device.get("name") or model),
            description=str(device.get("description") or ""),
            protocol=protocol,
            identity=identity,
            communication=communication,
            discovery=discovery,
            polling=polling,
            groups=groups,
            points=points,
            capabilities=capabilities,
        )


class Rs485Store:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or _default_store_path()
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    async def load_diagnostics(self) -> dict[str, dict[str, Any]]:
        return await asyncio.to_thread(self._load_diagnostics_sync)

    async def save_diagnostics(self, device_id: str, diagnostics: dict[str, Any]) -> None:
        async with self._lock:
            await asyncio.to_thread(self._save_diagnostics_sync, device_id, diagnostics)

    async def load_bus(self) -> dict[str, Any]:
        return await asyncio.to_thread(self._load_bus_sync)

    async def load_buses(self) -> dict[str, dict[str, Any]]:
        return await asyncio.to_thread(self._load_buses_sync)

    async def save_bus(self, settings: dict[str, Any]) -> None:
        async with self._lock:
            await asyncio.to_thread(self._save_bus_sync, settings)

    async def save_bus_for_port(self, serial_port: str, settings: dict[str, Any]) -> None:
        async with self._lock:
            await asyncio.to_thread(self._save_bus_for_port_sync, serial_port, settings)

    async def load_devices(self) -> list[Rs485Device]:
        return await asyncio.to_thread(self._load_devices_sync)

    async def save_device(self, device: Rs485Device) -> None:
        async with self._lock:
            await asyncio.to_thread(self._save_device_sync, device)

    async def delete_device(self, device_id: str) -> None:
        async with self._lock:
            await asyncio.to_thread(self._delete_device_sync, device_id)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(self.path)

    def _initialize_sync(self) -> None:
        with self._connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS rs485_bus_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS rs485_devices (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    template_id TEXT NOT NULL,
                    serial_port TEXT NOT NULL,
                    baudrate INTEGER NOT NULL,
                    parity TEXT NOT NULL,
                    stop_bits INTEGER NOT NULL,
                    slave_address INTEGER NOT NULL,
                    enabled INTEGER NOT NULL,
                    read_enabled INTEGER NOT NULL DEFAULT 1,
                    write_enabled INTEGER NOT NULL DEFAULT 1,
                    collapsed INTEGER NOT NULL,
                    values_json TEXT NOT NULL,
                    polling_json TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(rs485_devices)").fetchall()}
            if "read_enabled" not in columns:
                db.execute("ALTER TABLE rs485_devices ADD COLUMN read_enabled INTEGER NOT NULL DEFAULT 1")
            if "write_enabled" not in columns:
                db.execute("ALTER TABLE rs485_devices ADD COLUMN write_enabled INTEGER NOT NULL DEFAULT 1")
            if "polling_json" not in columns:
                db.execute("ALTER TABLE rs485_devices ADD COLUMN polling_json TEXT NOT NULL DEFAULT '{}'")
            db.execute("CREATE TABLE IF NOT EXISTS rs485_diagnostics (device_id TEXT PRIMARY KEY, data_json TEXT NOT NULL)")

    def _load_diagnostics_sync(self) -> dict[str, dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT device_id,data_json FROM rs485_diagnostics").fetchall()
        result: dict[str, dict[str, Any]] = {}
        for device_id, data in rows:
            try:
                value = json.loads(data)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                result[str(device_id)] = value
        return result

    def _save_diagnostics_sync(self, device_id: str, diagnostics: dict[str, Any]) -> None:
        with self._connect() as db:
            db.execute("INSERT INTO rs485_diagnostics(device_id,data_json) VALUES(?,?) ON CONFLICT(device_id) DO UPDATE SET data_json=excluded.data_json", (device_id, json.dumps(diagnostics, separators=(",", ":"))))

    def _load_bus_sync(self) -> dict[str, Any]:
        settings = default_bus_settings()
        with self._connect() as db:
            rows = db.execute("SELECT key,value FROM rs485_bus_settings").fetchall()
        port_rows: dict[str, Any] = {}
        for key, value in rows:
            if key.startswith("port::"):
                _, port, field = key.split("::", 2)
                if port == str(settings["serial_port"]):
                    port_rows[field] = value
            elif key in {"baudrate", "stop_bits", "slave_address"}:
                settings[key] = int(value)
            else:
                settings[key] = value
        for key, value in port_rows.items():
            if key in {"baudrate", "stop_bits", "slave_address"}:
                settings[key] = int(value)
            else:
                settings[key] = value
        return normalize_bus_settings(settings)

    def _load_buses_sync(self) -> dict[str, dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT key,value FROM rs485_bus_settings").fetchall()
        grouped: dict[str, dict[str, Any]] = {}
        legacy: dict[str, Any] = {}
        for key, value in rows:
            if key.startswith("port::"):
                _, port, field = key.split("::", 2)
                target = grouped.setdefault(port, default_bus_settings())
                target[field] = value
            else:
                legacy[key] = int(value) if key in {"baudrate", "stop_bits", "slave_address"} else value
        if legacy:
            old = normalize_bus_settings({**default_bus_settings(), **legacy})
            grouped.setdefault(str(old["serial_port"]), old)
        return {port: normalize_bus_settings({**bus, "serial_port": port}) for port, bus in grouped.items()}

    def _save_bus_sync(self, settings: dict[str, Any]) -> None:
        normalized = normalize_bus_settings(settings)
        with self._connect() as db:
            for key, value in normalized.items():
                db.execute(
                    "INSERT INTO rs485_bus_settings(key,value) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, str(value)),
                )

    def _save_bus_for_port_sync(self, serial_port: str, settings: dict[str, Any]) -> None:
        normalized = normalize_bus_settings({**settings, "serial_port": serial_port})
        with self._connect() as db:
            for key, value in normalized.items():
                db.execute(
                    "INSERT INTO rs485_bus_settings(key,value) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (f"port::{serial_port}::{key}", str(value)),
                )

    def _load_devices_sync(self) -> list[Rs485Device]:
        with self._connect() as db:
            rows = db.execute(
                """
                    SELECT id,name,template_id,serial_port,baudrate,parity,stop_bits,slave_address,enabled,read_enabled,write_enabled,collapsed,values_json,polling_json
                FROM rs485_devices ORDER BY serial_port,slave_address
                """
            ).fetchall()
        return [
            _device_from_store_row(row)
            for row in rows
        ]

    def _save_device_sync(self, device: Rs485Device) -> None:
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO rs485_devices(
                    id,name,template_id,serial_port,baudrate,parity,stop_bits,slave_address,enabled,read_enabled,write_enabled,collapsed,values_json,polling_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name,
                    template_id=excluded.template_id,
                    serial_port=excluded.serial_port,
                    baudrate=excluded.baudrate,
                    parity=excluded.parity,
                    stop_bits=excluded.stop_bits,
                    slave_address=excluded.slave_address,
                    enabled=excluded.enabled,
                    read_enabled=excluded.read_enabled,
                    write_enabled=excluded.write_enabled,
                    collapsed=excluded.collapsed,
                    values_json=excluded.values_json,
                    polling_json=excluded.polling_json
                """,
                (
                    device.id,
                    device.name,
                    device.template_id,
                    device.serial_port,
                    device.baudrate,
                    device.parity,
                    device.stop_bits,
                    device.slave_address,
                    1 if device.enabled else 0,
                    1 if device.read_enabled else 0,
                    1 if device.write_enabled else 0,
                    1 if device.collapsed else 0,
                    json.dumps(device.values, separators=(",", ":")),
                    json.dumps(device.polling, separators=(",", ":")),
                ),
            )

    def _delete_device_sync(self, device_id: str) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM rs485_devices WHERE id=?", (device_id,))


def _device_from_store_row(row: tuple[Any, ...]) -> Rs485Device:
    values = json.loads(row[12] or "{}")
    legacy_polling = values.pop("__polling", {})
    polling = json.loads(row[13] or "{}") if len(row) > 13 else legacy_polling
    return Rs485Device(
                id=row[0],
                name=row[1],
                template_id=row[2],
                serial_port=row[3],
                baudrate=int(row[4]),
                parity=row[5],
                stop_bits=int(row[6]),
                slave_address=int(row[7]),
                enabled=bool(row[8]),
                read_enabled=bool(row[9]),
                write_enabled=bool(row[10]),
                collapsed=bool(row[11]),
                values=values,
                polling=polling if isinstance(polling, dict) else {},
    )


class Rs485ModbusTransport:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._clients: dict[tuple[str, int, str, int], Any] = {}
        self.last_error: str | None = None
        self.last_tx_hex: str | None = None
        self.last_rx_hex: str | None = None
        self.last_rx_detail: str | None = None

    async def probe(self, bus: dict[str, Any], template: Rs485Template, slave_address: int) -> bool:
        self.last_error = None
        self.last_tx_hex = None
        self.last_rx_hex = None
        self.last_rx_detail = None
        probe = template.discovery.get("probe") if isinstance(template.discovery, dict) else None
        if isinstance(probe, dict):
            function = int(probe.get("function", 0x03))
            address = int(probe.get("address"))
            count = int(probe.get("count") or 1)
            self.last_tx_hex = modbus_rtu_read_request_hex(slave_address, function, address, count)
            point = {
                "table": _table_for_read_function(function),
                "address": address,
                "data_type": "uint16",
                "access": "read",
            }
            try:
                value = await self.read_point(bus, {**point, "count": count}, slave_address)
                register = int(round(float(value) / float(point.get("scale", 1)))) if "scale" in point else int(value)
                self.last_rx_hex = modbus_rtu_read_register_response_hex(slave_address, function, [register])
                self.last_rx_detail = f"register=0x{register:04X}"
                return True
            except Exception as exc:
                self.last_error = str(exc)
                return False
        fallback_points = []
        software_version = template.points.get("software_version")
        if isinstance(software_version, dict):
            fallback_points.append(software_version)
        fallback_points.extend(
            point
            for point in template.points.values()
            if point is not software_version and point.get("table") in {"holding_register", "input_register"}
        )
        fallback_points.extend(point for point in template.points.values() if point not in fallback_points)
        for point in fallback_points:
            if point.get("access") in {"read", "read_write"}:
                function = int(point.get("read_function") or (0x04 if point.get("table") == "input_register" else 0x03))
                count = int(point.get("count") or 1)
                if point.get("table") in {"holding_register", "input_register"}:
                    self.last_tx_hex = modbus_rtu_read_request_hex(slave_address, function, int(point.get("address")), count)
                try:
                    value = await self.read_point(bus, point, slave_address)
                    if point.get("table") in {"holding_register", "input_register"}:
                        register = encode_register_value(point, value)
                        self.last_rx_hex = modbus_rtu_read_register_response_hex(slave_address, function, [register])
                        self.last_rx_detail = f"register=0x{register:04X}"
                    return True
                except Exception as exc:
                    self.last_error = str(exc)
                    return False
        return False

    async def read_point(self, bus: dict[str, Any], point: dict[str, Any], slave_address: int) -> Any:
        async with self._lock:
            return await asyncio.to_thread(self._read_point_sync, bus, point, slave_address)

    async def read_points(
        self,
        bus: dict[str, Any],
        items: list[tuple[str, dict[str, Any]]],
        slave_address: int,
    ) -> dict[str, Any]:
        async with self._lock:
            return await asyncio.to_thread(self._read_points_sync, bus, items, slave_address)

    async def write_point(
        self,
        bus: dict[str, Any],
        point: dict[str, Any],
        slave_address: int,
        value: Any,
    ) -> None:
        async with self._lock:
            await asyncio.to_thread(self._write_point_sync, bus, point, slave_address, value)

    async def manual_command(self, bus: dict[str, Any], slave_address: int, function: int, address: int, count: int = 1, value: int | None = None) -> dict[str, Any]:
        async with self._lock:
            return await asyncio.to_thread(self._manual_command_sync, bus, slave_address, function, address, count, value)

    def _manual_command_sync(self, bus: dict[str, Any], slave_address: int, function: int, address: int, count: int, value: int | None) -> dict[str, Any]:
        client = self._connected_client(bus)
        if function == 1:
            self.last_tx_hex = modbus_rtu_read_request_hex(slave_address, function, address, count)
            result = self._call_modbus(client.read_coils, slave_address, address=address, count=count)
            data = [int(bool(item)) for item in _result_bits(result)[:count]]
        elif function == 2:
            self.last_tx_hex = modbus_rtu_read_request_hex(slave_address, function, address, count)
            result = self._call_modbus(client.read_discrete_inputs, slave_address, address=address, count=count)
            data = [int(bool(item)) for item in _result_bits(result)[:count]]
        elif function in {3, 4}:
            self.last_tx_hex = modbus_rtu_read_request_hex(slave_address, function, address, count)
            method = client.read_holding_registers if function == 3 else client.read_input_registers
            result = self._call_modbus(method, slave_address, address=address, count=count)
            data = [int(item) for item in _result_registers(result)[:count]]
        elif function == 5:
            if value not in {0, 1}:
                raise ValueError("FC05 value must be 0 or 1")
            wire_value = 0xFF00 if value else 0x0000
            self.last_tx_hex = modbus_rtu_write_single_request_hex(slave_address, function, address, wire_value)
            result = self._call_modbus(client.write_coil, slave_address, address=address, value=bool(value))
            _raise_on_error(result)
            data = [int(value)]
        elif function == 6:
            if value is None or not 0 <= value <= 0xFFFF:
                raise ValueError("FC06 value must be an unsigned 16-bit integer")
            self.last_tx_hex = modbus_rtu_write_single_request_hex(slave_address, function, address, value)
            result = self._call_modbus(client.write_register, slave_address, address=address, value=value)
            _raise_on_error(result)
            data = [value]
        else:
            raise ValueError("Supported function codes: 01, 02, 03, 04, 05, 06")
        self.last_rx_hex = None
        self.last_rx_detail = f"manual FC{function:02d} data={data}"
        return {"function": function, "address": address, "count": count, "value": value, "data": data, "tx_hex": self.last_tx_hex, "rx_hex": self.last_rx_hex}

    def _client(self, bus: dict[str, Any]):
        if ModbusSerialClient is None:
            raise RuntimeError("pymodbus is not installed")
        return ModbusSerialClient(
            port=str(bus["serial_port"]),
            baudrate=int(bus["baudrate"]),
            parity=_modbus_parity(str(bus["parity"])),
            stopbits=int(bus["stop_bits"]),
            bytesize=8,
            timeout=RS485_RESPONSE_TIMEOUT_SECONDS,
            retries=0,
        )

    def _client_key(self, bus: dict[str, Any]) -> tuple[str, int, str, int]:
        return (
            str(bus["serial_port"]),
            int(bus["baudrate"]),
            _modbus_parity(str(bus["parity"])),
            int(bus["stop_bits"]),
        )

    def _connected_client(self, bus: dict[str, Any]):
        key = self._client_key(bus)
        client = self._clients.get(key)
        if client is None:
            client = self._client(bus)
            self._clients[key] = client
        if not getattr(client, "connected", False) and not client.connect():
            self._clients.pop(key, None)
            try:
                client.close()
            except Exception:
                pass
            raise RuntimeError(f"unable to open {bus['serial_port']}")
        return client

    async def close_all(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._close_all_sync)

    def _close_all_sync(self) -> None:
        clients = list(self._clients.values())
        self._clients.clear()
        for client in clients:
            try:
                client.close()
            except Exception:
                pass

    async def close_bus(self, bus: dict[str, Any]) -> None:
        async with self._lock:
            await asyncio.to_thread(self._close_bus_sync, bus)

    def _close_bus_sync(self, bus: dict[str, Any]) -> None:
        client = self._clients.pop(self._client_key(bus), None)
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

    async def close_port(self, serial_port: str) -> None:
        async with self._lock:
            await asyncio.to_thread(self._close_port_sync, serial_port)

    def _close_port_sync(self, serial_port: str) -> None:
        target = str(serial_port)
        clients = [
            (key, client)
            for key, client in self._clients.items()
            if key[0] == target
        ]
        for key, client in clients:
            self._clients.pop(key, None)
            try:
                client.close()
            except Exception:
                pass

    def _read_point_sync(self, bus: dict[str, Any], point: dict[str, Any], slave_address: int) -> Any:
        client = self._connected_client(bus)
        return self._read_point_with_client(client, point, slave_address)

    def _read_points_sync(
        self,
        bus: dict[str, Any],
        items: list[tuple[str, dict[str, Any]]],
        slave_address: int,
    ) -> dict[str, Any]:
        client = self._connected_client(bus)
        values: dict[str, Any] = {}
        for group in _contiguous_read_groups(items):
            if len(group) == 1 or any(int(point.get("count") or 1) != 1 for _, point in group):
                for capability_id, point in group:
                    values[capability_id] = self._read_point_with_client(client, point, slave_address)
                continue
            table = group[0][1]["table"]
            start = int(group[0][1]["address"])
            count = int(group[-1][1]["address"]) - start + 1
            if table == "coil":
                self.last_tx_hex = modbus_rtu_read_request_hex(slave_address, 0x01, start, count)
                result = self._call_modbus(client.read_coils, slave_address, address=start, count=count)
                bits = _result_bits(result)
                for capability_id, point in group:
                    values[capability_id] = bool(bits[int(point["address"]) - start])
                self.last_rx_hex = None
                self.last_rx_detail = f"bits={count}"
                continue
            if table == "discrete_input":
                self.last_tx_hex = modbus_rtu_read_request_hex(slave_address, 0x02, start, count)
                result = self._call_modbus(client.read_discrete_inputs, slave_address, address=start, count=count)
                bits = _result_bits(result)
                for capability_id, point in group:
                    values[capability_id] = bool(bits[int(point["address"]) - start])
                self.last_rx_hex = None
                self.last_rx_detail = f"bits={count}"
                continue
            if table == "holding_register":
                function = 0x03
                method = client.read_holding_registers
            elif table == "input_register":
                function = 0x04
                method = client.read_input_registers
            else:
                raise RuntimeError(f"unsupported Modbus table {table}")
            self.last_tx_hex = modbus_rtu_read_request_hex(slave_address, function, start, count)
            result = self._call_modbus(method, slave_address, address=start, count=count)
            registers = _result_registers(result)
            for capability_id, point in group:
                register = int(registers[int(point["address"]) - start])
                values[capability_id] = decode_register_value(point, register)
            self.last_rx_hex = modbus_rtu_read_register_response_hex(slave_address, function, registers[:count])
            self.last_rx_detail = f"registers={count}"
        return values

    def _read_point_with_client(self, client: Any, point: dict[str, Any], slave_address: int) -> Any:
        table = point["table"]
        address = int(point["address"])
        count = int(point.get("count") or 1)
        if table == "coil":
            self.last_tx_hex = modbus_rtu_read_request_hex(slave_address, 0x01, address, count)
            result = self._call_modbus(client.read_coils, slave_address, address=address, count=count)
            bits = _result_bits(result)
            value = bool(bits[0])
            self.last_rx_hex = None
            self.last_rx_detail = f"bit={1 if value else 0}"
            return value
        if table == "discrete_input":
            self.last_tx_hex = modbus_rtu_read_request_hex(slave_address, 0x02, address, count)
            result = self._call_modbus(client.read_discrete_inputs, slave_address, address=address, count=count)
            bits = _result_bits(result)
            value = bool(bits[0])
            self.last_rx_hex = None
            self.last_rx_detail = f"bit={1 if value else 0}"
            return value
        if table == "holding_register":
            self.last_tx_hex = modbus_rtu_read_request_hex(slave_address, 0x03, address, count)
            result = self._call_modbus(client.read_holding_registers, slave_address, address=address, count=count)
        elif table == "input_register":
            self.last_tx_hex = modbus_rtu_read_request_hex(slave_address, 0x04, address, count)
            result = self._call_modbus(client.read_input_registers, slave_address, address=address, count=count)
        else:
            raise RuntimeError(f"unsupported Modbus table {table}")
        register = int(_result_registers(result)[0])
        value = decode_register_value(point, register)
        self.last_rx_hex = modbus_rtu_read_register_response_hex(
            slave_address,
            0x04 if table == "input_register" else 0x03,
            [register],
        )
        self.last_rx_detail = f"register=0x{register:04X}"
        return value

    def _write_point_sync(self, bus: dict[str, Any], point: dict[str, Any], slave_address: int, value: Any) -> None:
        client = self._connected_client(bus)
        table = point["table"]
        address = int(point["address"])
        if table == "coil":
            self.last_tx_hex = modbus_rtu_write_single_request_hex(slave_address, 0x05, address, 0xFF00 if value else 0x0000)
            result = self._call_modbus(client.write_coil, slave_address, address=address, value=bool(value))
            _raise_on_error(result)
            self.last_rx_hex = self.last_tx_hex
            self.last_rx_detail = f"coil={1 if value else 0}"
            return
        if table != "holding_register":
            raise RuntimeError(f"Modbus table {table} is not writable")
        encoded = encode_register_value(point, value)
        if point.get("read_modify_write"):
            self.last_tx_hex = modbus_rtu_read_request_hex(slave_address, 0x03, address, 1)
            current_result = self._call_modbus(client.read_holding_registers, slave_address, address=address, count=1)
            current = int(_result_registers(current_result)[0])
            mask = int(point.get("mask", 0xFFFF))
            shift = int(point.get("shift", 0))
            encoded = (current & ~mask) | ((encoded << shift) & mask)
        self.last_tx_hex = modbus_rtu_write_single_request_hex(slave_address, 0x06, address, encoded)
        result = self._call_modbus(client.write_register, slave_address, address=address, value=encoded)
        _raise_on_error(result)
        self.last_rx_hex = self.last_tx_hex
        self.last_rx_detail = f"register=0x{encoded:04X}"

    def _call_modbus(self, method, slave_address: int, **kwargs):
        try:
            return method(**kwargs, device_id=slave_address)
        except TypeError as exc:
            if "device_id" not in str(exc):
                raise
            return method(**kwargs, slave=slave_address)


class Rs485Manager:
    def __init__(
        self,
        store: Rs485Store | None = None,
        registry: Rs485TemplateRegistry | None = None,
        transport: Rs485ModbusTransport | None = None,
        mock: bool = True,
    ) -> None:
        self.store = store or Rs485Store()
        self.registry = registry or Rs485TemplateRegistry()
        self.transport = transport or Rs485ModbusTransport()
        self.mock = mock
        self.bus = default_bus_settings()
        self.buses: dict[str, dict[str, Any]] = {}
        self.devices: dict[str, Rs485Device] = {}
        self.scanned: list[dict[str, Any]] = []
        self.scan_errors: list[dict[str, Any]] = []
        self.scan_log: list[dict[str, Any]] = []
        self.diagnostics: dict[str, dict[str, Any]] = {}
        self.diagnostics_paused: set[str] = set()
        self.scan_state: dict[str, Any] = {"running": False, "stop_requested": False}
        self.status = "RS-485: template-driven mock"
        self._scan_task: asyncio.Task | None = None
        self._scan_stop_event: asyncio.Event | None = None
        self._poll_tasks: dict[str, asyncio.Task] = {}
        self._bus_worker_tasks: dict[str, asyncio.Task] = {}
        # One heap per physical port keeps command writes ahead of polling
        # while preserving FIFO order within each priority class.
        self._bus_queues: dict[str, list[tuple[int, int, Rs485BusJob]]] = {}
        self._bus_sequence = itertools.count()
        self._bus_queue_events: dict[str, asyncio.Event] = {}
        self._poll_tick_seconds = 0.05
        self._poll_due: dict[tuple[str, str], float] = {}
        self._command_lock = asyncio.Lock()
        self._command_locks: dict[str, asyncio.Lock] = {}
        self._command_pending_by_port: dict[str, int] = {}
        self._command_pending = 0
        self._publisher: Callable[[dict[str, Any]], Awaitable[None]] | None = None
        self._stopping = False

    def set_publisher(self, publisher: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        self._publisher = publisher

    def _command_lock_for_port(self, serial_port: str) -> asyncio.Lock:
        return self._command_locks.setdefault(serial_port, asyncio.Lock())

    async def _publish(self, event: dict[str, Any]) -> None:
        if self._publisher is not None:
            await self._publisher(event)

    async def _publish_device(self, device: Rs485Device) -> None:
        template = self.registry.get(device.template_id)
        diagnostics = self._diagnostics_for(device.id)
        for entry in diagnostics.get("entries", [])[-4:]:
            entry["ws_published"] = True
            entry["online"] = device.runtime.get("online")
            entry["backoff_ms"] = device.runtime.get("backoff_ms", 0)
        snapshot = device.snapshot()
        snapshot["identity"] = device_identity_snapshot(template, device)
        snapshot["mock"] = self._device_is_mock(device)
        snapshot["diagnostics"] = copy.deepcopy(diagnostics)
        await self._publish({"type": "rs485_device_changed", "device": snapshot})

    async def start(self) -> None:
        self._stopping = False
        self.registry.reload()
        await self.store.initialize()
        self.bus = await self.store.load_bus()
        self.buses = await self.store.load_buses()
        self.buses.setdefault(str(self.bus["serial_port"]), dict(self.bus))
        for port in {str(device.serial_port) for device in await self.store.load_devices()}:
            self.buses.setdefault(port, {**default_bus_settings(), "serial_port": port})
        self.devices = {device.id: device for device in await self.store.load_devices()}
        # Older versions embedded the Modbus address in the display name (for
        # example, ``MIO-8 #1``). Keep user-defined names intact, but migrate
        # those generated names so the address has one explicit location in
        # the identity block and device list.
        for device in self.devices.values():
            template = self.registry.get(device.template_id)
            legacy_name = f"{template.model} #{device.slave_address}"
            if device.name == legacy_name:
                device.name = template.model
                await self.store.save_device(device)
                await self._publish_device(device)
        self.diagnostics = await self.store.load_diagnostics()
        # Traffic capture is opt-in. Restore an explicit persisted choice, but
        # old diagnostic records without the flag start with capture disabled.
        for device_id, diagnostics in self.diagnostics.items():
            diagnostics.setdefault("paused", True)
            if diagnostics["paused"]:
                self.diagnostics_paused.add(device_id)
            else:
                self.diagnostics_paused.discard(device_id)
        for serial_port in {str(device.serial_port) for device in self.devices.values()}:
            self._ensure_bus_worker(serial_port)
        await self.refresh()
        for device in self.devices.values():
            if device.enabled:
                self._schedule_device_polling(device)
        for serial_port in {str(device.serial_port) for device in self.devices.values() if device.enabled}:
            self._ensure_poll_task(serial_port)

    async def stop(self) -> None:
        self._stopping = True
        await self.stop_scan()
        scan_task = self._scan_task
        self._scan_task = None
        if scan_task and not scan_task.done():
            scan_task.cancel()
            try:
                await scan_task
            except asyncio.CancelledError:
                pass
        await self.flush_diagnostics()
        poll_tasks = list(self._poll_tasks.values())
        self._poll_tasks.clear()
        for task in poll_tasks:
            task.cancel()
        for task in poll_tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        workers = list(self._bus_worker_tasks.values())
        self._bus_worker_tasks.clear()
        for task in workers:
            task.cancel()
        for task in workers:
            try:
                await task
            except asyncio.CancelledError:
                pass
        if hasattr(self.transport, "close_all"):
            await self.transport.close_all()

    async def stop_scan(self) -> dict[str, Any]:
        self.scan_state["stop_requested"] = True
        if self._scan_stop_event is not None:
            self._scan_stop_event.set()
        return self.snapshot(status="RS-485: scan stop requested")

    async def clear_scan_results(self) -> dict[str, Any]:
        current_port = str(self.bus.get("serial_port") or "")
        self.scanned = [item for item in self.scanned if item.get("serial_port") != current_port]
        self.scan_errors = [item for item in self.scan_errors if item.get("serial_port") != current_port]
        self.scan_log = [item for item in self.scan_log if item.get("serial_port") != current_port]
        self.scan_state = {
            "running": False,
            "stop_requested": False,
            "serial_port": current_port,
            "template_id": self.scan_state.get("template_id"),
            "current_address": None,
            "scanned": 0,
            "total": int(self.scan_state.get("total") or 255),
            "found": 0,
            "last_error": None,
            "started_at": None,
        }
        return self.snapshot(status="RS-485: scan results cleared")

    def _new_diagnostics(self) -> dict[str, Any]:
        return {"entries": [], "errors": [], "sequence": 0, "total_requests": 0, "tx_count": 0, "rx_count": 0, "successful_responses": 0, "timeout_count": 0, "crc_errors": 0, "exception_count": 0, "malformed_count": 0, "wrong_slave_count": 0, "wrong_function_count": 0, "protocol_errors": 0, "consecutive_failures": 0, "latency_total_ms": 0, "latency_min_ms": None, "latency_max_ms": None, "last_success": None, "last_error": None, "last_valid_response_at": None, "possible_bus_conflict": False, "paused": True}

    def _diagnostics_for(self, device_id: str) -> dict[str, Any]:
        diagnostics = self.diagnostics.setdefault(device_id, self._new_diagnostics())
        defaults = self._new_diagnostics()
        for key, value in defaults.items():
            diagnostics.setdefault(key, value)
        if diagnostics.get("paused", True):
            self.diagnostics_paused.add(device_id)
        return diagnostics

    def clear_device_diagnostics(self, device_id: str) -> dict[str, Any]:
        self._device(device_id)
        diagnostics = self._diagnostics_for(device_id)
        diagnostics["entries"] = []
        diagnostics["errors"] = []
        asyncio.create_task(self.store.save_diagnostics(device_id, copy.deepcopy(diagnostics)))
        return self.snapshot(selected_id=device_id, status="RS-485: diagnostics log cleared")

    def reset_device_diagnostic_counters(self, device_id: str) -> dict[str, Any]:
        self._device(device_id)
        diagnostics = self._diagnostics_for(device_id)
        keep = {"entries": diagnostics.get("entries", []), "errors": diagnostics.get("errors", []), "sequence": diagnostics.get("sequence", 0), "paused": diagnostics.get("paused", True)}
        fresh = self._new_diagnostics()
        fresh.update(keep)
        self.diagnostics[device_id] = fresh
        asyncio.create_task(self.store.save_diagnostics(device_id, fresh.copy()))
        return self.snapshot(selected_id=device_id, status="RS-485: diagnostic counters reset")

    def set_device_diagnostics_paused(self, device_id: str, paused: bool) -> dict[str, Any]:
        self._device(device_id)
        diagnostics = self.diagnostics.setdefault(device_id, self._new_diagnostics())
        diagnostics["paused"] = paused
        if paused:
            self.diagnostics_paused.add(device_id)
            try:
                asyncio.create_task(self.store.save_diagnostics(device_id, copy.deepcopy(diagnostics)))
            except RuntimeError:
                pass
        else:
            self.diagnostics_paused.discard(device_id)
        return self.snapshot(selected_id=device_id, status=f"RS-485: diagnostics {'paused' if paused else 'resumed'}")

    async def flush_diagnostics(self) -> None:
        """Persist the in-memory capture at an explicit lifecycle boundary."""
        for device_id, diagnostics in self.diagnostics.items():
            await self.store.save_diagnostics(device_id, copy.deepcopy(diagnostics))

    def _record_diagnostic(
        self,
        device: Rs485Device,
        result: str,
        message: str,
        started: float,
        transaction_id: str | None = None,
        group_id: str | None = None,
        operation: str | None = None,
    ) -> None:
        diagnostics = self._diagnostics_for(device.id)
        # Diagnostics capture is independent from RS-485 polling. When the
        # user presses Stop, freeze the complete diagnostic measurement rather
        # than only hiding journal rows while counters keep increasing.
        if device.id in self.diagnostics_paused or diagnostics.get("paused", False):
            return
        diagnostics["sequence"] += 1
        transaction_id = transaction_id or f"{device.id}:{diagnostics['sequence']}"
        if result == "scan":
            diagnostics["total_requests"] += 1
            diagnostics["tx_count"] += 1
        response_ms = max(0, round((time.monotonic() - started) * 1000))
        if result in {"response", "error"}:
            diagnostics["latency_total_ms"] += response_ms
            diagnostics["latency_min_ms"] = response_ms if diagnostics["latency_min_ms"] is None else min(diagnostics["latency_min_ms"], response_ms)
            diagnostics["latency_max_ms"] = response_ms if diagnostics["latency_max_ms"] is None else max(diagnostics["latency_max_ms"], response_ms)
        failed = result == "error"
        lower_message = message.lower()
        if failed:
            category = classify_modbus_error(message)
            diagnostics["timeout_count"] += int(category == "timeout")
            diagnostics["protocol_errors"] += int(category == "protocol")
            diagnostics["crc_errors"] += int(category == "crc")
            diagnostics["exception_count"] += int(category == "exception")
            diagnostics["malformed_count"] += int(category == "malformed")
            diagnostics["wrong_slave_count"] += int(category == "wrong_slave")
            diagnostics["wrong_function_count"] += int(category == "wrong_function")
            diagnostics["possible_bus_conflict"] = diagnostics["possible_bus_conflict"] or any(token in lower_message for token in ("echo", "bus busy", "collision", "another master"))
            diagnostics["consecutive_failures"] += 1
            diagnostics["last_error"] = {"ts": datetime.now(timezone.utc).isoformat(), "message": message}
        else:
            if result == "response":
                diagnostics["successful_responses"] += 1
                diagnostics["rx_count"] += 1
                diagnostics["last_valid_response_at"] = datetime.now(timezone.utc).isoformat()
                diagnostics["last_success"] = {"ts": datetime.now(timezone.utc).isoformat(), "message": message}
            diagnostics["consecutive_failures"] = 0
        tx_hex = getattr(self.transport, "last_tx_hex", None)
        rx_hex = getattr(self.transport, "last_rx_hex", None)
        decoded = validate_modbus_frame(rx_hex or tx_hex, device.slave_address)
        entry_ts = datetime.now(timezone.utc).isoformat()
        diagnostics["entries"].append({
            "seq": diagnostics["sequence"], "ts": datetime.now(timezone.utc).isoformat(), "result": result,
            "slave_address": device.slave_address, "message": message,
            "serial_port": device.serial_port, "group": group_id or "unknown",
            "operation": operation or ("WRITE" if message.lower().startswith("write") else "READ"),
            "online": device.runtime.get("online"),
            "backoff_ms": device.runtime.get("backoff_ms", 0),
            "ws_published": False,
            "direction": "MASTER" if result == "scan" else "SLAVE", "function": message.split(" ", 1)[0],
            "payload": rx_hex or tx_hex or "—", "bytes": len(bytes.fromhex((rx_hex or tx_hex or "").replace(" ", ""))) if (rx_hex or tx_hex) else 0,
            "crc": "OK" if result != "error" else "—", "tx_hex": tx_hex, "rx_hex": rx_hex,
            "response_ms": response_ms, "transaction_id": transaction_id,
            "tx_ts": entry_ts if result == "scan" else None,
            "rx_ts": entry_ts if result == "response" else None,
            "decoded": decoded,
        })
        if result in {"response", "error"}:
            for prior in reversed(diagnostics["entries"][:-1]):
                if prior.get("transaction_id") == transaction_id and prior.get("result") == "scan":
                    prior["rx_hex"] = rx_hex
                    prior["rx_ts"] = entry_ts
                    prior["response_ms"] = response_ms
                    prior["pair_result"] = result
                    prior["pair_message"] = message
                    prior["rx_decoded"] = decoded
                    break
        if failed:
            diagnostics["errors"].append({"ts": entry_ts, "message": message, "transaction_id": transaction_id})
            diagnostics["errors"] = diagnostics["errors"][-RS485_DIAGNOSTICS_MAX_ERRORS:]
        diagnostics["entries"] = diagnostics["entries"][-1000:]
    async def _bus_worker(self, serial_port: str) -> None:
        while True:
            job = self._next_bus_job(serial_port)
            if job is None:
                event = self._bus_queue_events[serial_port]
                event.clear()
                # Close the check/clear race: a producer may enqueue a job
                # between the first queue check and event.clear(). Re-check
                # before sleeping so a polling worker cannot stall forever.
                if not self._port_queues_empty(serial_port):
                    continue
                await event.wait()
                continue
            await self._execute_bus_job(job)

    async def _execute_bus_job(self, job: Rs485BusJob) -> None:
        if job.future.cancelled():
            return
        try:
            result = await job.operation()
        except Exception as exc:
            if not job.future.cancelled():
                job.future.set_exception(exc)
        else:
            if not job.future.cancelled():
                job.future.set_result(result)

    def _next_bus_job(self, serial_port: str) -> Rs485BusJob | None:
        queue = self._bus_queues[serial_port]
        while queue:
            _priority, _sequence, job = heapq.heappop(queue)
            if not job.future.cancelled():
                return job
        return None

    def _port_queues_empty(self, serial_port: str) -> bool:
        return not self._bus_queues[serial_port]

    async def _run_bus_job(self, priority: str, operation, serial_port: str) -> Any:
        self._ensure_bus_worker(serial_port)
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        priority_value = {"command": 0, "mode": 1, "poll": 2, "scan": 3}.get(priority, 2)
        job = Rs485BusJob(
            priority=priority_value,
            sequence=next(self._bus_sequence),
            operation=operation,
            future=future,
        )
        if priority in {"command", "mode"}:
            self._drop_pending_poll_jobs(serial_port)
        heapq.heappush(self._bus_queues[serial_port], (job.priority, job.sequence, job))
        self._bus_queue_events[serial_port].set()
        return await future

    def _drop_pending_poll_jobs(self, serial_port: str | None = None) -> None:
        ports = [serial_port] if serial_port and serial_port in self._bus_queues else list(self._bus_queues)
        for port in ports:
            queue = self._bus_queues[port]
            retained = []
            for item in queue:
                if item[2].priority >= 2:
                    if not item[2].future.done():
                        item[2].future.cancel()
                else:
                    retained.append(item)
            queue[:] = retained
            heapq.heapify(queue)

    def _reset_poll_schedule_for_serial_port(self, serial_port: str) -> None:
        """Resume every device on this bus from the command completion window."""
        now = time.monotonic()
        port_device_ids = {
            device.id
            for device in self.devices.values()
            if str(device.serial_port) == serial_port
        }
        self._poll_due = {
            key: now if key[0] in port_device_ids else due
            for key, due in self._poll_due.items()
        }

    async def _read_point(self, bus: dict[str, Any], point: dict[str, Any], slave_address: int, priority: str) -> Any:
        return await self._run_bus_job(priority, lambda: self.transport.read_point(bus, point, slave_address), str(bus["serial_port"]))

    async def _read_points(
        self,
        bus: dict[str, Any],
        items: list[tuple[str, dict[str, Any]]],
        slave_address: int,
        priority: str,
    ) -> dict[str, Any]:
        if hasattr(self.transport, "read_points"):
            return await self._run_bus_job(priority, lambda: self.transport.read_points(bus, items, slave_address), str(bus["serial_port"]))

        async def read_each() -> dict[str, Any]:
            values = {}
            for capability_id, point in items:
                values[capability_id] = await self.transport.read_point(bus, point, slave_address)
            return values

        return await self._run_bus_job(priority, read_each, str(bus["serial_port"]))

    async def _write_point(self, bus: dict[str, Any], point: dict[str, Any], slave_address: int, value: Any, priority: str) -> None:
        await self._run_bus_job(priority, lambda: self.transport.write_point(bus, point, slave_address, value), str(bus["serial_port"]))

    def templates_snapshot(self) -> dict[str, Any]:
        return self.registry.snapshot()

    def template_snapshot(self, template_id: str) -> dict[str, Any]:
        return self.registry.get(template_id).snapshot()

    async def upload_template(self, filename: str, data: bytes) -> dict[str, Any]:
        if not filename.lower().endswith((".yaml", ".yml")):
            raise ValueError("template file must be .yaml or .yml")
        if len(data) > 256 * 1024:
            raise ValueError("template file is too large")
        template = self.registry.validate_template_bytes(data, filename)
        directory = _default_template_upload_dir()
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{template.template_id}.yaml"
        target.write_bytes(data)
        self.registry.reload()
        return {
            **self.snapshot(status=f"RS-485: uploaded template {template.template_id}"),
            "uploaded_template_id": template.template_id,
        }

    async def delete_template(self, template_id: str) -> dict[str, Any]:
        is_used = any(device.template_id == template_id for device in self.devices.values())
        if is_used:
            raise ValueError("template is used by configured devices")
        self.registry.delete_template(template_id)
        return self.snapshot(status=f"RS-485: deleted template {template_id}")

    async def save_bus(self, settings: dict[str, Any]) -> dict[str, Any]:
        self._command_pending += 1
        try:
            async with self._command_lock:
                requested_port = str(settings.get("serial_port") or self.bus.get("serial_port") or "")
                previous_bus = dict(self.buses.get(requested_port) or {**default_bus_settings(), "serial_port": requested_port})
                next_bus = normalize_bus_settings({**previous_bus, **settings, "serial_port": requested_port})
                self.buses[requested_port] = next_bus
                self.bus = next_bus
                previous_transport = (
                    previous_bus.get("serial_port"),
                    previous_bus.get("baudrate"),
                    previous_bus.get("parity"),
                    previous_bus.get("stop_bits"),
                    previous_bus.get("mode"),
                )
                next_transport = (
                    self.bus.get("serial_port"),
                    self.bus.get("baudrate"),
                    self.bus.get("parity"),
                    self.bus.get("stop_bits"),
                    self.bus.get("mode"),
                )
                affected_ports = {
                    str(previous_bus.get("serial_port") or ""),
                    str(self.bus.get("serial_port") or ""),
                }
                affected_device_ids = {
                    device.id
                    for device in self.devices.values()
                    if str(device.serial_port) in affected_ports
                }
                transport_changed = previous_transport != next_transport
                master_changed = bool(previous_bus.get("enabled", True)) != bool(self.bus.get("enabled", True))
                master_disabled = previous_bus.get("enabled", True) and not self.bus.get("enabled", True)
                if transport_changed or master_changed:
                    for port in affected_ports:
                        if port:
                            self._drop_pending_poll_jobs(port)
                if transport_changed or master_disabled:
                    if hasattr(self.transport, "close_port"):
                        await self.transport.close_port(str(previous_bus.get("serial_port") or ""))
                        if transport_changed and previous_bus.get("serial_port") != self.bus.get("serial_port"):
                            await self.transport.close_port(str(self.bus.get("serial_port") or ""))
                    elif hasattr(self.transport, "close_bus"):
                        await self.transport.close_bus(previous_bus)
                await self.store.save_bus_for_port(requested_port, self.bus)
                for device in self.devices.values():
                    if device.enabled and str(device.serial_port) in affected_ports:
                        if transport_changed or master_changed:
                            self._schedule_device_polling(device)
                mode = "USB/Real" if self.bus.get("mode") == "usb_real" else ("mock" if self._bus_is_mock(self.bus) else "Modbus")
                self.status = f"RS-485: {mode} on {self.bus['serial_port']} {self.bus['baudrate']} {self.bus['parity']} {self.bus['stop_bits']} stop"
                return self.snapshot(status=self.status)
        finally:
            self._command_pending = max(0, self._command_pending - 1)

    async def patch_bus(self, changes: dict[str, Any]) -> dict[str, Any]:
        """Apply a partial bus update without allowing stale UI state to overwrite siblings."""
        selected_port = str(changes.get("serial_port") or self.bus.get("serial_port") or "")
        merged = dict(self.buses.get(selected_port) or self.bus)
        for key in ("serial_port", "baudrate", "parity", "stop_bits", "mode", "enabled"):
            if key in changes:
                merged[key] = changes[key]
        return await self.save_bus(merged)

    def _bus_for_port(self, serial_port: str) -> dict[str, Any]:
        port = str(serial_port or "")
        if port in self.buses:
            return self.buses[port]
        # Keep direct manager.bus assignment compatible with older callers and
        # tests while normal runtime state is always persisted per port.
        if port and str(self.bus.get("serial_port") or "") == port:
            return self.bus
        return {**default_bus_settings(), "serial_port": port} if port else self.bus

    async def scan(self, settings: dict[str, Any]) -> dict[str, Any]:
        if self._scan_task and not self._scan_task.done():
            return self.snapshot(status="RS-485: scan already running")
        self._scan_stop_event = asyncio.Event()
        # Mock discovery is deterministic and short; return its completed
        # snapshot so API callers can immediately add a discovered device.
        if self._bus_is_mock(normalize_bus_settings(settings)):
            return await self._scan_impl(settings)
        self._scan_task = asyncio.create_task(self._scan_impl(settings), name="rs485_scan")
        return self.snapshot(status="RS-485: scan started")

    async def _scan_impl(self, settings: dict[str, Any]) -> dict[str, Any]:
        self._command_pending += 1
        stop_event = self._scan_stop_event
        try:
            self.bus = normalize_bus_settings(settings)
            current_port = str(self.bus["serial_port"])
            self._drop_pending_poll_jobs(current_port)
            await self.store.save_bus(self.bus)
            requested_template = str(settings.get("template_id") or "")
            default_template = requested_template if requested_template in self.registry.templates else next(iter(self.registry.templates), "mio-8")
            template = self.registry.get(default_template)
            mock_bus = self._bus_is_mock(self.bus)
            addresses = [1, 2] if mock_bus else template_slave_addresses(template)
            scanned_count = len(addresses)
            self.scanned = [item for item in self.scanned if item.get("serial_port") != current_port]
            self.scan_errors = [item for item in self.scan_errors if item.get("serial_port") != current_port]
            self.scan_log = [item for item in self.scan_log if item.get("serial_port") != current_port]
            self.scan_state = {
                "running": True,
                "stop_requested": False,
                "serial_port": current_port,
                "template_id": default_template,
                "current_address": None,
                "scanned": 0,
                "total": scanned_count,
                "found": 0,
                "last_error": None,
                "started_at": datetime.now(timezone.utc).isoformat(),
            }
            self._append_scan_log(
                current_port,
                0,
                "started",
                f"Starting scan on {current_port} ({self.bus['baudrate']}, {self.bus['parity'].capitalize()}, {self.bus['stop_bits']}) - addresses 1 - {scanned_count}",
            )
            found_count = 0
            stopped = False
            try:
                for found in addresses:
                    if self.scan_state.get("stop_requested") or (stop_event is not None and stop_event.is_set()):
                        stopped = True
                        self._append_scan_log(current_port, found, "stopped", "Scan stopped by user")
                        break
                    self.scan_state["current_address"] = found
                    scan_id = _device_id(current_port, found)
                    already = scan_id in self.devices
                    status = "Configured" if already else "Found"
                    confidence = "Mock match"
                    if not mock_bus:
                        matched = await self._run_bus_job(
                            "scan",
                            lambda: self.transport.probe(self.bus, template, found),
                            current_port,
                        )
                        tx_hex = getattr(self.transport, "last_tx_hex", None)
                        rx_hex = getattr(self.transport, "last_rx_hex", None)
                        rx_detail = getattr(self.transport, "last_rx_detail", None)
                        if not matched:
                            status = "No response"
                            error = getattr(self.transport, "last_error", None) or "No supported response"
                            confidence = error
                            self.scan_state["last_error"] = error
                            self._append_scan_log(
                                current_port,
                                found,
                                "timeout",
                                error,
                                tx_hex=tx_hex,
                                rx_hex=rx_hex,
                                rx_detail=rx_detail,
                            )
                            if len(self.scan_errors) < 8:
                                self.scan_errors.append(
                                    {
                                        "serial_port": current_port,
                                        "slave_address": found,
                                        "template_id": default_template,
                                        "error": error,
                                    }
                                )
                            if is_fatal_scan_error(error):
                                break
                        else:
                            confidence = "Template probe"
                            self._append_scan_log(
                                current_port,
                                found,
                                "matched",
                                rx_detail or "Template probe",
                                tx_hex=tx_hex,
                                rx_hex=rx_hex,
                                rx_detail=rx_detail,
                            )
                    else:
                        self._append_scan_log(
                            current_port,
                            found,
                            "matched",
                            f"Found device at address {found}",
                            rx_detail=f"{template.model} ({template.template_id})",
                        )
                    if not mock_bus and status == "No response":
                        self.scan_state["scanned"] = int(self.scan_state.get("scanned") or 0) + 1
                        continue
                    self.scanned.append(
                        {
                            "id": scan_id,
                            "serial_port": current_port,
                            "slave_address": found,
                            "template_id": default_template,
                            "baudrate": int(self.bus.get("baudrate") or 9600),
                            "parity": str(self.bus.get("parity") or "none"),
                            "stop_bits": int(self.bus.get("stop_bits") or 1),
                            "status": status,
                            "confidence": confidence,
                            "configured": already,
                        }
                    )
                    found_count += 1
                    self.scan_state["found"] = found_count
                    self.scan_state["scanned"] = int(self.scan_state.get("scanned") or 0) + 1
            finally:
                self.scan_state["running"] = False
                self.scan_state["stop_requested"] = False
                self.scan_state["current_address"] = None
                if stop_event is self._scan_stop_event:
                    self._scan_stop_event = None
        finally:
            self._command_pending = max(0, self._command_pending - 1)
        mode = "mock scan" if mock_bus else "scan"
        if stopped:
            self.status = f"RS-485: {mode} stopped after {self.scan_state.get('scanned', 0)} address(es), found {found_count} result(s) on {current_port}"
            return self.snapshot(status=self.status)
        if found_count:
            self.scan_errors = [item for item in self.scan_errors if item.get("serial_port") != current_port]
            self.status = f"RS-485: {mode} scanned {scanned_count} address(es), found {found_count} result(s) on {current_port}"
            self._append_scan_log(current_port, int(self.scan_state.get("scanned") or scanned_count), "progress", f"Scanning... {self.scan_state.get('scanned', scanned_count)} / {scanned_count} addresses ({found_count} devices found)")
            return self.snapshot(status=self.status)
        suffix = f": {self.scan_errors[0]['error']}" if self.scan_errors else ""
        self.status = f"RS-485: {mode} scanned {scanned_count} address(es), found 0 result(s) on {current_port}{suffix}"
        return self.snapshot(status=self.status)

    def _append_scan_log(
        self,
        serial_port: str,
        slave_address: int,
        result: str,
        message: str,
        *,
        tx_hex: str | None = None,
        rx_hex: str | None = None,
        rx_detail: str | None = None,
    ) -> None:
        self.scan_log.append(
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "serial_port": serial_port,
                "slave_address": slave_address,
                "result": result,
                "tx_hex": tx_hex,
                "rx_hex": rx_hex,
                "rx_detail": rx_detail,
                "message": message,
            }
        )
        if len(self.scan_log) > 512:
            self.scan_log = self.scan_log[-512:]

    async def add_device(self, scan_id: str) -> dict[str, Any]:
        LOGGER.info("RS-485 add requested: scan_id=%s running=%s scanned=%s devices=%s", scan_id, bool(self._scan_task and not self._scan_task.done()), len(self.scanned), len(self.devices))
        self._command_pending += 1
        try:
            scan = next((item for item in self.scanned if item["id"] == scan_id), None)
            if scan is None:
                LOGGER.warning("RS-485 add rejected: scan result not found: %s", scan_id)
                raise ValueError("scan result not found")
            if self._scan_task and not self._scan_task.done():
                self.scan_state["stop_requested"] = True
            if scan.get("status") == "No response":
                raise ValueError("device did not respond")
            self._drop_pending_poll_jobs(str(scan["serial_port"]))
            async with self._command_lock:
                if scan_id in self.devices:
                    raise ValueError("device already configured")
                template = self.registry.get(scan["template_id"])
                values = default_values_for_template(template)
                if "device_address" in values:
                    values["device_address"] = int(scan["slave_address"])
                if "device_baudrate" in values:
                    values["device_baudrate"] = int(self.bus["baudrate"])
                if "device_parity" in values:
                    values["device_parity"] = str(self.bus["parity"])
                device = Rs485Device(
                    id=scan_id,
                    name=template.model,
                    template_id=template.template_id,
                    serial_port=scan["serial_port"],
                    baudrate=int(self.bus["baudrate"]),
                    parity=str(self.bus["parity"]),
                    stop_bits=int(self.bus["stop_bits"]),
                    slave_address=int(scan["slave_address"]),
                    values=values,
                    runtime=runtime_state_for_template(template, online=True),
                )
                self.devices[device.id] = device
                await self.store.save_device(device)
                self._schedule_device_polling(device)
                self._ensure_bus_worker(device.serial_port)
                self._ensure_poll_task(device.serial_port)
                self.scanned = [item for item in self.scanned if item["id"] != scan_id]
                LOGGER.info("RS-485 add committed: device_id=%s remaining_scanned=%s devices=%s", device.id, len(self.scanned), len(self.devices))
                if not self._device_is_mock(device):
                    asyncio.create_task(
                        self._hydrate_added_device(device, template),
                        name=f"rs485_hydrate_{device.id}",
                    )
                await self._publish_device(device)
                return self.snapshot(selected_id=device.id, status=f"RS-485: configured {device.name}")
        finally:
            self._command_pending = max(0, self._command_pending - 1)

    async def _hydrate_added_device(self, device: Rs485Device, template: Rs485Template) -> None:
        try:
            await self._read_device_values(device, template, log_transport=True, log_result="add", priority="command")
            mark_device_runtime(device, template, None, success=True, group_id="settings")
        except Exception as exc:
            mark_device_runtime(device, template, str(exc), success=False, group_id="settings")
        try:
            await self.store.save_device(device)
        except Exception:
            pass

    async def remove_device(self, device_id: str) -> dict[str, Any]:
        if device_id not in self.devices:
            raise ValueError("device not found")
        del self.devices[device_id]
        await self.store.delete_device(device_id)
        await self._publish({"type": "rs485_device_removed", "device_id": device_id})
        return self.snapshot(status="RS-485: configured device removed")

    async def rename_device(self, device_id: str, name: str) -> dict[str, Any]:
        device = self._device(device_id)
        normalized = " ".join(str(name or "").split())
        if not normalized:
            raise ValueError("device name must not be empty")
        if len(normalized) > 80:
            raise ValueError("device name is too long")
        device.name = normalized
        await self.store.save_device(device)
        return self.snapshot(selected_id=device.id, status=f"RS-485: renamed {device.name}")

    async def set_capability(self, device_id: str, capability_id: str, value: Any) -> dict[str, Any]:
        device = self._device(device_id)
        if not bool(self.bus.get("enabled", True)) and str(device.serial_port) == str(self.bus.get("serial_port") or ""):
            raise ValueError("RS-485 port is disabled")
        if not device.write_enabled:
            raise ValueError("device writes are disabled")
        serial_port = str(device.serial_port)
        self._command_pending_by_port[serial_port] = self._command_pending_by_port.get(serial_port, 0) + 1
        try:
            # The per-port worker serializes transactions and applies
            # FC05 > mode > polling > scan priority before each transaction.
            command_lock = _NoopAsyncLock()
            async with command_lock:
                communication_setting = capability_id in {"device_baudrate", "device_parity", "device_address"}
                template = self.registry.get(device.template_id)
                capability = normalized_capabilities(template).get(capability_id)
                if capability is None:
                    raise ValueError("capability not found")
                if capability.get("type") == "binary_input" or capability.get("type") == "sensor":
                    raise ValueError("capability is read-only")
                serial_port = str(device.serial_port)
                self._drop_pending_poll_jobs(serial_port)
                self._reset_poll_schedule_for_serial_port(serial_port)
                normalized = normalize_capability_value(capability, value)
                write_priority = "command" if capability.get("type") == "switch" else "mode"
                if not self._device_is_mock(device):
                    point = template.points.get(str(capability.get("source")))
                    if point is None:
                        raise ValueError("capability point not found")
                    bus = self._effective_device_bus(device)
                    write_started = time.monotonic()
                    write_function = int(point.get("write_function") or (0x05 if point.get("table") == "coil" else 0x06))
                    write_transaction_id = f"{device.id}:{int(write_started * 1000)}"
                    self._record_diagnostic(device, "scan", f"Write FC{write_function:02d} {capability_id}", write_started, write_transaction_id, "settings" if write_priority == "mode" else "outputs", "WRITE")
                    try:
                        await self._write_point(bus, point, device.slave_address, normalized, write_priority)
                        self._record_diagnostic(device, "response", f"Write FC{write_function:02d} {capability_id}", write_started, write_transaction_id, "settings" if write_priority == "mode" else "outputs", "WRITE")
                        self._append_transport_log(device, "write", f"{capability_id}={normalized}")
                        if capability.get("type") != "switch" and not communication_setting:
                            normalized = await self._read_point(bus, point, device.slave_address, write_priority)
                            self._append_transport_log(device, "readback", f"{capability_id}={normalized}")
                    except Exception as exc:
                        error = str(exc)
                        self._record_diagnostic(device, "error", f"Write FC{write_function:02d} {capability_id}: {error}", write_started, write_transaction_id, "settings" if write_priority == "mode" else "outputs", "WRITE")
                        self._append_transport_log(device, "error", f"{capability_id}: {error}")
                        if communication_setting:
                            self._append_transport_log(device, "write-pending", f"{capability_id}={normalized}; device may have switched communication settings")
                            if hasattr(self.transport, "close_port"):
                                await self.transport.close_port(device.serial_port)
                            elif hasattr(self.transport, "close_bus"):
                                await self.transport.close_bus(bus)
                        else:
                            device.values[f"{capability_id}__error"] = error
                            mark_device_runtime(device, template, capability, success=False, error=error)
                            device.runtime["online"] = False
                            device.runtime["communication_status"] = "OFFLINE"
                            if hasattr(self.transport, "close_port"):
                                await self.transport.close_port(device.serial_port)
                            elif hasattr(self.transport, "close_bus"):
                                await self.transport.close_bus(bus)
                            await self.store.save_device(device)
                            return self.snapshot(selected_id=device.id, status=f"RS-485: {capability.get('name', capability_id)} failed: {error}")
                device.values[capability_id] = normalized
                device.values.pop(f"{capability_id}__error", None)
                mark_device_runtime(device, template, capability, success=True)
                old_device_id = device.id
                old_bus = device_bus(device)
                if capability_id == "device_address":
                    was_generated_name = device.name == f"{template.model} #{device.slave_address}"
                    next_address = int(normalized)
                    device.slave_address = next_address
                    device.id = _device_id(device.serial_port, next_address)
                    if was_generated_name:
                        device.name = template.model
                elif capability_id == "device_baudrate":
                    device.baudrate = int(normalized)
                    normalized = device.baudrate
                    device.values[capability_id] = normalized
                    if hasattr(self.transport, "close_port"):
                        await self.transport.close_port(device.serial_port)
                    elif hasattr(self.transport, "close_bus"):
                        await self.transport.close_bus(old_bus)
                    await asyncio.sleep(0.2)
                elif capability_id == "device_parity":
                    device.parity = str(normalized)
                    if hasattr(self.transport, "close_port"):
                        await self.transport.close_port(device.serial_port)
                    elif hasattr(self.transport, "close_bus"):
                        await self.transport.close_bus(old_bus)
                    await asyncio.sleep(0.2)
                self.devices.pop(device_id, None)
                if old_device_id != device_id:
                    self.devices.pop(old_device_id, None)
                self.devices[device.id] = device
                await self.store.save_device(device)
                if device.id != device_id:
                    await self.store.delete_device(device_id)
                name = capability.get("name", capability_id)
                if capability.get("type") == "switch":
                    state = "ON" if bool(normalized) else "OFF"
                    return self.snapshot(selected_id=device.id, status=f"RS-485: {name} {state}")
                return self.snapshot(selected_id=device.id, status=f"RS-485: updated {name}")
        finally:
            remaining = self._command_pending_by_port.get(serial_port, 1) - 1
            if remaining > 0:
                self._command_pending_by_port[serial_port] = remaining
            else:
                self._command_pending_by_port.pop(serial_port, None)

    async def read_device(self, device_id: str, poll_group: str | None = None) -> dict[str, Any]:
        self._command_pending += 1
        async with self._command_lock:
            self._command_pending -= 1
            try:
                device = self._device(device_id)
                if not bool(self.bus.get("enabled", True)) and str(device.serial_port) == str(self.bus.get("serial_port") or ""):
                    raise ValueError("RS-485 port is disabled")
                if not device.read_enabled:
                    raise ValueError("device reads are disabled")
                template = self.registry.get(device.template_id)
                if self._device_is_mock(device):
                    mark_device_runtime(device, template, None, success=True, group_id=poll_group)
                    await self.store.save_device(device)
                    return self.snapshot(selected_id=device.id, status=f"RS-485: read {device.name}")
                failed_error: str | None = None
                try:
                    await self._read_device_values(device, template, poll_group=poll_group, log_transport=True, log_result="read", priority="command")
                    mark_device_runtime(device, template, None, success=True, group_id=poll_group)
                except Exception as exc:
                    failed_error = str(exc)
                    mark_device_runtime(device, template, None, success=False, error=failed_error, group_id=poll_group)
                await self.store.save_device(device)
                if failed_error:
                    return self.snapshot(selected_id=device.id, status=f"RS-485: read failed for {device.name}: {failed_error}")
                return self.snapshot(selected_id=device.id, status=f"RS-485: read {device.name}")
            finally:
                self._command_pending = max(0, self._command_pending)

    async def manual_command(self, device_id: str, function: int, address: int, count: int = 1, value: int | None = None, slave_address: int | None = None) -> dict[str, Any]:
        self._command_pending += 1
        async with self._command_lock:
            self._command_pending -= 1
            device = self._device(device_id)
            if not bool(self.bus.get("enabled", True)) and str(device.serial_port) == str(self.bus.get("serial_port") or ""):
                raise ValueError("RS-485 port is disabled")
            if function in {1, 2, 3, 4} and not device.read_enabled:
                raise ValueError("device reads are disabled")
            if function in {5, 6, 15, 16} and not device.write_enabled:
                raise ValueError("device writes are disabled")
            started = time.monotonic()
            transaction_id = f"{device.id}:manual:{int(started * 1000)}"
            try:
                target_slave = int(slave_address if slave_address is not None else device.slave_address)
                if not 1 <= target_slave <= 247:
                    raise ValueError("slave ID must be between 1 and 247")
                bus = self._effective_device_bus(device)
                result = await self._run_bus_job(
                    "command",
                    lambda: self.transport.manual_command(bus, target_slave, function, address, count, value),
                    str(device.serial_port),
                )
                elapsed = max(0, round((time.monotonic() - started) * 1000))
                self._record_diagnostic(device, "scan", f"Manual FC{function:02d}", started, transaction_id)
                self._record_diagnostic(device, "response", f"Manual FC{function:02d}", started, transaction_id)
                return self.snapshot(selected_id=device.id, status=f"RS-485: manual FC{function:02d} sent ({elapsed} ms)")
            except Exception as exc:
                self._record_diagnostic(device, "scan", f"Manual FC{function:02d}", started, transaction_id)
                self._record_diagnostic(device, "error", f"Manual FC{function:02d}: {exc}", started, transaction_id)
                return self.snapshot(selected_id=device.id, status=f"RS-485: manual command failed: {exc}")
            finally:
                self._command_pending = max(0, self._command_pending)

    async def refresh(self) -> dict[str, Any]:
        for device in list(self.devices.values()):
            if not device.enabled or not device.read_enabled:
                continue
            template = self.registry.get(device.template_id)
            if self._device_is_mock(device):
                mark_device_runtime(device, template, None, success=True)
                await self.store.save_device(device)
                continue
            try:
                await self._read_device_values(device, template, log_transport=True, log_result="refresh", priority="command")
                mark_device_runtime(device, template, None, success=True)
            except Exception as exc:
                mark_device_runtime(device, template, None, success=False, error=str(exc))
            await self.store.save_device(device)
        return self.snapshot(status="RS-485: refreshed")

    def _ensure_bus_worker(self, serial_port: str) -> None:
        if self._stopping or not serial_port or serial_port in self._bus_worker_tasks:
            return
        self._bus_queues[serial_port] = []
        self._bus_queue_events[serial_port] = asyncio.Event()
        self._bus_worker_tasks[serial_port] = asyncio.create_task(
            self._bus_worker(serial_port),
            name=f"intellegyhub_rs485_bus_{serial_port.rsplit('/', 1)[-1]}",
        )

    def _ensure_poll_task(self, serial_port: str) -> None:
        if not self._stopping and serial_port and serial_port not in self._poll_tasks:
            self._poll_tasks[serial_port] = asyncio.create_task(
                self._poll_loop(serial_port),
                name=f"intellegyhub_rs485_poll_{serial_port.rsplit('/', 1)[-1]}",
            )

    async def _poll_loop(self, serial_port: str) -> None:
        while not self._stopping:
            await asyncio.sleep(self._poll_tick_seconds)
            try:
                await self._poll_due_groups(serial_port)
            except Exception as exc:
                self.status = f"RS-485: polling loop error: {exc}"
                self._append_scan_log(
                    str(self.bus.get("serial_port") or ""),
                    0,
                    "poll-loop-error",
                    str(exc),
                )
                continue

    async def _poll_due_groups(self, serial_port: str | None = None) -> None:
        now = time.monotonic()
        target_serial_port = serial_port or str(self.bus.get("serial_port") or "")
        bus = self._bus_for_port(target_serial_port)
        if not bool(bus.get("enabled", True)):
            return
        if self._command_pending_by_port.get(target_serial_port, 0):
            return
        for device in list(self.devices.values()):
            if not device.enabled or not device.read_enabled:
                continue
            if str(device.serial_port) != target_serial_port:
                continue
            template = self.registry.get(device.template_id)
            for group_id, interval_seconds in poll_intervals_seconds(template, device.polling).items():
                key = (device.id, group_id)
                retry_at = float(device.runtime.get("backoff_until", 0) or 0)
                if now < retry_at and group_id != "outputs":
                    continue
                if now < self._poll_due.get(key, 0):
                    continue
                self._poll_due[key] = now + interval_seconds
                previous_values = dict(device.values)
                previous_health = (
                    device.runtime.get("online"),
                    device.runtime.get("communication_status"),
                    device.runtime.get("polling"),
                    device.runtime.get("port_status"),
                    (device.runtime.get("groups") or {}).get(group_id, {}).get("status"),
                )
                try:
                    if self._command_pending_by_port.get(target_serial_port, 0):
                        return
                    if not self._device_is_mock(device):
                        # Live state polling updates device values/runtime only. It must
                        # not flood the user-facing scan log with successful heartbeats.
                        await self._read_device_values(device, template, poll_group=group_id, log_transport=False, log_result="poll", priority="poll")
                    else:
                        # Mock mode must exercise the same observable logger
                        # contract as real Modbus mode, even without raw RTU
                        # frames or serial I/O.
                        started = time.monotonic()
                        transaction_id = f"{device.id}:mock:{int(started * 1000)}"
                        self._record_diagnostic(device, "scan", f"Read {group_id}", started, transaction_id, group_id, "READ")
                        self._record_diagnostic(device, "response", f"Read {group_id}", started, transaction_id, group_id, "READ")
                    if not device.enabled:
                        continue
                    mark_device_runtime(device, template, None, success=True, group_id=group_id)
                except asyncio.CancelledError:
                    # A queued poll can be deliberately evicted when a command
                    # arrives. Keep the per-port polling task alive.
                    return
                except Rs485PollPreempted:
                    return
                except Exception as exc:
                    if not device.enabled:
                        continue
                    self._append_transport_log(device, "poll-error", f"{group_id}: {exc}")
                    mark_device_runtime(device, template, None, success=False, error=str(exc), group_id=group_id)
                current_health = (
                    device.runtime.get("online"),
                    device.runtime.get("communication_status"),
                    device.runtime.get("polling"),
                    device.runtime.get("port_status"),
                    (device.runtime.get("groups") or {}).get(group_id, {}).get("status"),
                )
                # A successful poll is also a state transition for the UI. The
                # values can remain unchanged while a device moves from the
                # persisted OFFLINE state to ONLINE. Publish that health change
                # even when no channel value changed; otherwise the UI keeps
                # showing stale availability until a later unrelated update.
                if device.values != previous_values or current_health != previous_health:
                    await self._publish_device(device)

    async def toggle_collapsed(self, device_id: str) -> dict[str, Any]:
        device = self._device(device_id)
        device.collapsed = not device.collapsed
        await self.store.save_device(device)
        return self.snapshot(selected_id=device.id)

    async def set_device_enabled(self, device_id: str, enabled: bool) -> dict[str, Any]:
        device = self._device(device_id)
        template = self.registry.get(device.template_id)
        device.enabled = enabled
        if enabled:
            # Enabling polling does not prove reachability, but it must not
            # erase the last known health/value state. Queue an immediate
            # probe; the response will decide ONLINE/OFFLINE.
            runtime = device.runtime or runtime_state_for_template(template, online=None, polling=device.polling)
            runtime["polling"] = "live" if self._device_is_mock(device) else "starting"
            runtime["last_error"] = None
            device.runtime = runtime
            self._poll_due = {key: value for key, value in self._poll_due.items() if key[0] != device.id}
            self._poll_due.update({(device.id, group_id): 0.0 for group_id in poll_intervals_seconds(template, device.polling)})
        else:
            runtime = device.runtime or runtime_state_for_template(template, online=None, polling=device.polling)
            runtime["polling"] = "paused"
            runtime["last_error"] = None
            for group in runtime.get("groups", {}).values():
                if group.get("mode") != "on_demand":
                    group["status"] = "paused"
            device.runtime = runtime
            self._poll_due = {key: value for key, value in self._poll_due.items() if key[0] != device.id}
        await self.store.save_device(device)
        return self.snapshot(selected_id=device.id, status=f"RS-485: polling {'enabled' if enabled else 'disabled'} for {device.name}")

    async def set_device_access(self, device_id: str, *, read_enabled: bool | None = None, write_enabled: bool | None = None) -> dict[str, Any]:
        device = self._device(device_id)
        if read_enabled is not None:
            device.read_enabled = bool(read_enabled)
            if not device.read_enabled:
                self._poll_due = {key: value for key, value in self._poll_due.items() if key[0] != device.id}
            elif device.enabled:
                self._schedule_device_polling(device)
        if write_enabled is not None:
            device.write_enabled = bool(write_enabled)
        await self.store.save_device(device)
        return self.snapshot(selected_id=device.id, status=f"RS-485: access updated for {device.name}")

    async def set_device_polling(self, device_id: str, settings: dict[str, Any]) -> dict[str, Any]:
        device = self._device(device_id)
        template = self.registry.get(device.template_id)
        device.polling = normalize_polling_settings(settings)
        old_runtime = device.runtime or runtime_state_for_template(template, online=None, polling=device.polling)
        fresh_groups = runtime_state_for_template(template, online=None, polling=device.polling)["groups"]
        old_runtime["groups"] = fresh_groups
        if device.enabled:
            old_runtime["polling"] = old_runtime.get("polling") if old_runtime.get("polling") not in {"paused", "offline"} else "starting"
        else:
            old_runtime["polling"] = "paused"
        device.runtime = old_runtime
        self._poll_due = {key: value for key, value in self._poll_due.items() if key[0] != device.id}
        if device.enabled:
            self._poll_due.update({(device.id, group_id): 0.0 for group_id in poll_intervals_seconds(template, device.polling)})
        await self.store.save_device(device)
        return self.snapshot(selected_id=device.id, status=f"RS-485: polling settings updated for {device.name}")

    def snapshot(self, selected_id: str | None = None, status: str | None = None) -> dict[str, Any]:
        # Successful background polling is not scan activity and does not belong
        # in the user-facing scan log. Filter legacy entries from older sessions too.
        self.scan_log = [entry for entry in self.scan_log if entry.get("result") not in {"poll", "poll-start"}]
        templates = self.templates_snapshot()
        devices = []
        for device in sorted(self.devices.values(), key=lambda item: (item.serial_port, item.slave_address)):
            template = self.registry.templates.get(device.template_id)
            if template is not None and not device.runtime:
                device.runtime = runtime_state_for_template(template, online=self._device_is_mock(device), polling=device.polling)
            diagnostics = self._diagnostics_for(device.id)
            device_snapshot = device.snapshot()
            device_snapshot["identity"] = device_identity_snapshot(template, device)
            devices.append({**device_snapshot, "mock": self._device_is_mock(device), "diagnostics": {**diagnostics, "paused": device.id in self.diagnostics_paused}})
        current_port_devices = [device for device in devices if device["serial_port"] == self.bus["serial_port"]]
        effective_mode = "mock" if self._bus_is_mock(self.bus) else "usb_real" if self.bus.get("mode") == "usb_real" else "modbus"
        default_status = (
            "RS-485: template-driven mock"
            if effective_mode == "mock"
            else f"RS-485: {'USB/Real' if effective_mode == 'usb_real' else 'Modbus'} on {self.bus['serial_port']} {self.bus['baudrate']} {self.bus['parity']} {self.bus['stop_bits']} stop"
        )
        stale_mock_status = self.status == "RS-485: template-driven mock" and effective_mode != "mock"
        return {
            "status": status or (default_status if stale_mock_status else self.status or default_status),
            "bus": dict(self.bus),
            "templates": templates["templates"],
            "template_details": [
                {**template.snapshot(), "source": self.registry.template_sources.get(template.template_id, "built-in")}
                for template in sorted(self.registry.templates.values(), key=lambda item: item.model)
            ],
            "template_errors": templates["errors"],
            "scanned": list(self.scanned),
            "scan_errors": list(self.scan_errors),
            "scan_log": list(self.scan_log),
            "scan_state": dict(self.scan_state),
            "devices": devices,
            "selected_id": selected_id or (current_port_devices[0]["id"] if current_port_devices else (devices[0]["id"] if devices else None)),
            "mock": self.mock,
            "effective_mode": effective_mode,
            "serial_ports": serial_port_options(),
        }

    def _bus_is_mock(self, bus: dict[str, Any]) -> bool:
        if str(bus.get("mode") or "mock") == "usb_real":
            return False
        return self.mock and str(bus.get("serial_port") or "") in MOCK_SERIAL_PORTS

    def _device_is_mock(self, device: Rs485Device) -> bool:
        if str(self.bus.get("mode") or "mock") == "usb_real" and device.serial_port not in MOCK_SERIAL_PORTS:
            return False
        return self.mock and device.serial_port in MOCK_SERIAL_PORTS

    def _device(self, device_id: str) -> Rs485Device:
        device = self.devices.get(device_id)
        if device is None:
            raise ValueError("device not found")
        return device

    def _schedule_device_polling(self, device: Rs485Device, group_ids: set[str] | None = None) -> None:
        """Make selected enabled device groups immediately eligible for a poll."""
        template = self.registry.get(device.template_id)
        intervals = poll_intervals_seconds(template, device.polling)
        selected = set(intervals) if group_ids is None else set(group_ids)
        for group_id in selected:
            key = (device.id, group_id)
            if not device.enabled or group_id not in intervals:
                self._poll_due.pop(key, None)
            else:
                self._poll_due[key] = 0.0

    def _effective_device_bus(self, device: Rs485Device) -> dict[str, Any]:
        bus = device_bus(device)
        port_bus = self._bus_for_port(str(device.serial_port))
        bus["baudrate"] = int(port_bus.get("baudrate") or bus["baudrate"])
        bus["parity"] = str(port_bus.get("parity") or bus["parity"])
        bus["stop_bits"] = int(port_bus.get("stop_bits") or bus["stop_bits"])
        bus["mode"] = str(port_bus.get("mode") or bus.get("mode") or "mock")
        return bus

    async def _read_device_values(
        self,
        device: Rs485Device,
        template: Rs485Template,
        poll_group: str | None = None,
        log_transport: bool = False,
        log_result: str = "read",
        priority: str = "command",
    ) -> None:
        bus = self._effective_device_bus(device)
        items: list[tuple[str, dict[str, Any]]] = []
        for capability_id, capability in normalized_capabilities(template).items():
            source = capability.get("source")
            if not isinstance(source, str):
                continue
            point = template.points.get(source)
            if point is None or point.get("access") not in {"read", "read_write"}:
                continue
            resolved_poll_group = point_poll_group(point, capability)
            # Keep the old manual/API `state` selector working while the
            # template uses separate fast input and output groups.
            if poll_group is not None and not (poll_group == "state" and resolved_poll_group in {"inputs", "outputs"}) and resolved_poll_group != poll_group:
                continue
            if log_result == "poll" and self._command_pending_by_port.get(str(device.serial_port), 0):
                self._append_scan_log(device.serial_port, device.slave_address, "poll-preempt", "command pending")
                raise Rs485PollPreempted()
            items.append((capability_id, {**point, "poll_group": resolved_poll_group}))
        if not items:
            return
        # Keep independent Modbus tables independent. A failed relay read (FC01)
        # must not prevent the digital-input read (FC02) from reaching the device.
        batches: list[list[tuple[str, dict[str, Any]]]] = []
        for table in ("discrete_input", "coil", "holding_register", "input_register"):
            batch = [item for item in items if item[1].get("table") == table]
            if batch:
                batches.append(batch)
        values: dict[str, Any] = {}
        errors: list[tuple[list[tuple[str, dict[str, Any]]], RuntimeError]] = []
        for batch in batches:
            started = time.monotonic()
            transaction_id = f"{device.id}:{int(started * 1000)}"
            function = str(batch[0][1].get("function") or batch[0][1].get("table") or "read")
            self._record_diagnostic(device, "scan", f"Read {function} ({len(batch)} point(s))", started, transaction_id, poll_group or "state", "READ")
            try:
                batch_values = await self._read_points(bus, batch, device.slave_address, priority)
                self._record_diagnostic(device, "response", f"Read {function} ({len(batch_values)} point(s))", started, transaction_id, poll_group or "state", "READ")
                values.update(batch_values)
                for capability_id, value in batch_values.items():
                    device.values[capability_id] = value
                    device.values.pop(f"{capability_id}__error", None)
            except RuntimeError as exc:
                errors.append((batch, exc))
                error = str(exc)
                self._record_diagnostic(device, "error", error, started, transaction_id, poll_group or "state", "READ")
                for capability_id, _point in batch:
                    device.values[f"{capability_id}__error"] = error
                if log_transport:
                    self._append_transport_log(device, "error", f"{poll_group or 'all'}: {error}")
        sync_device_settings_from_values(device)
        if log_transport and values:
            self._append_transport_log(device, log_result, f"{len(values)}/{len(items)} point(s)")
        if errors:
            # Partial values may remain visible, but a failed required table
            # means this poll did not confirm that the device is reachable.
            raise errors[0][1]

    def _append_transport_log(self, device: Rs485Device, result: str, message: str) -> None:
        self._append_scan_log(
            device.serial_port,
            device.slave_address,
            result,
            message,
            tx_hex=getattr(self.transport, "last_tx_hex", None),
            rx_hex=getattr(self.transport, "last_rx_hex", None),
            rx_detail=getattr(self.transport, "last_rx_detail", None),
        )


def runtime_state_for_template(
    template: Rs485Template | None = None,
    online: bool | None = None,
    polling: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = _now_iso()
    groups: dict[str, dict[str, Any]] = {}
    for group_id, group in effective_polling_config(polling).items():
        groups[group_id] = _runtime_group(group, now)
    communication_status = "ONLINE" if online is True else "OFFLINE" if online is False else "UNKNOWN"
    return {
        "online": online,
        "communication_status": communication_status,
        "polling": "live" if online else "offline",
        "last_update": now if online else None,
        "last_seen": now if online else None,
        "last_error": None,
        "availability_failures": 0,
        "availability_successes": 0,
        "availability_failure_started_at": None,
        "availability_failure_seconds": 0.0,
        "backoff_level": 0,
        "backoff_ms": 0,
        "backoff_until": 0.0,
        "last_error_type": None,
        "port_status": "ONLINE" if online is True else "UNKNOWN",
        "groups": groups,
    }


def mark_device_runtime(
    device: Rs485Device,
    template: Rs485Template,
    capability: dict[str, Any] | None,
    *,
    success: bool,
    error: str | None = None,
    group_id: str | None = None,
) -> None:
    runtime = device.runtime or runtime_state_for_template(template, online=success, polling=device.polling)
    now = _now_iso()
    failures = int(runtime.get("availability_failures") or 0)
    successes = int(runtime.get("availability_successes") or 0)
    if success:
        failures = 0
        successes += 1
        runtime["availability_failure_started_at"] = None
        runtime["availability_failure_seconds"] = 0.0
        if successes >= RS485_ONLINE_AFTER_SUCCESSES:
            runtime["online"] = True
            runtime["communication_status"] = "ONLINE"
    else:
        successes = 0
        failures += 1
        failure_started_at = runtime.get("availability_failure_started_at")
        if not isinstance(failure_started_at, (int, float)):
            failure_started_at = time.time()
            runtime["availability_failure_started_at"] = failure_started_at
        # Use elapsed wall time for the externally observable availability
        # contract. Poll frequency only affects how quickly this is noticed.
        failure_elapsed = max(0.0, time.time() - failure_started_at)
        runtime["availability_failure_seconds"] = round(failure_elapsed, 3)
        if failure_elapsed >= RS485_OFFLINE_AFTER_SECONDS:
            runtime["online"] = False
            runtime["communication_status"] = "OFFLINE"
        level = min(5, int(runtime.get("backoff_level") or 0) + 1)
        backoff_ms = min(10000, 500 * (2 ** (level - 1)))
        runtime["backoff_level"] = level
        runtime["backoff_ms"] = backoff_ms
        runtime["backoff_until"] = time.monotonic() + (backoff_ms / 1000)
        runtime["last_error_type"] = classify_modbus_error(error or "polling error")
    runtime["availability_failures"] = failures
    runtime["availability_successes"] = successes
    runtime["polling"] = "live" if success else "error"
    runtime["last_update"] = now
    if success:
        runtime["last_seen"] = now
        runtime["last_error"] = None
        runtime["backoff_level"] = 0
        runtime["backoff_ms"] = 0
        runtime["backoff_until"] = 0.0
        runtime["last_error_type"] = None
        runtime["port_status"] = "ONLINE"
    else:
        runtime["last_error"] = error or "polling error"
    group_id = group_id or (str(capability.get("group")) if capability else "state")
    if group_id == "control_modes":
        group_id = "settings"
    if group_id == "device_settings":
        group_id = "settings"
    defaults = runtime_state_for_template(template, online=success, polling=device.polling)["groups"]
    groups = runtime.setdefault("groups", defaults)
    for default_group_id, default_group in defaults.items():
        groups.setdefault(default_group_id, default_group)
    group = groups.setdefault(group_id, {"mode": "polling", "last_update": None, "status": "idle"})
    group["last_update"] = now
    group["status"] = "live" if success and group.get("mode") != "on_demand" else ("updated" if success else "error")
    if not success:
        group["last_error"] = runtime["last_error"]
    else:
        group.pop("last_error", None)
    device.runtime = runtime


def _runtime_group(group: dict[str, Any], now: str) -> dict[str, Any]:
    if str(group.get("mode")) == "on_demand":
        return {"mode": "on_demand", "last_update": None, "status": "idle"}
    return {
        "mode": "polling",
        "interval_ms": int(group.get("interval_ms") or 0),
        "last_update": now,
        "status": "live",
    }


def poll_intervals_seconds(
    _template: Rs485Template | None = None,
    polling: dict[str, Any] | None = None,
) -> dict[str, float]:
    intervals: dict[str, float] = {}
    for group_id, group in effective_polling_config(polling).items():
        if str(group.get("mode")) == "on_demand":
            continue
        if str(group.get("mode")) == "disabled":
            continue
        interval_ms = int(group.get("interval_ms") or 0)
        if interval_ms > 0:
            intervals[str(group_id)] = max(0.05, interval_ms / 1000)
    return intervals


def effective_polling_config(polling: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    result = {group_id: dict(group) for group_id, group in RS485_DEFAULT_POLLING.items()}
    incoming_polling = dict(polling or {})
    # Migrate the pre-separated `state` setting without changing the public
    # API shape used by existing devices. Explicit new groups always win.
    legacy_state = incoming_polling.get("state")
    if isinstance(legacy_state, dict):
        for group_id in ("inputs", "outputs"):
            incoming_polling.setdefault(group_id, dict(legacy_state))
    for group_id, group in incoming_polling.items():
        if group_id not in result or not isinstance(group, dict):
            continue
        merged = dict(result[group_id])
        mode = str(group.get("mode") or merged.get("mode") or "polling")
        if mode not in {"polling", "on_demand", "disabled"}:
            mode = str(merged.get("mode") or "polling")
        merged["mode"] = mode
        if mode == "polling":
            interval_ms = int(group.get("interval_ms") or merged.get("interval_ms") or 0)
            merged["interval_ms"] = max(50, min(600000, interval_ms))
        else:
            merged.pop("interval_ms", None)
        result[group_id] = merged
    return result


def normalize_polling_settings(settings: dict[str, Any]) -> dict[str, dict[str, Any]]:
    incoming = settings.get("groups") if "groups" in settings else settings
    if not isinstance(incoming, dict):
        incoming = {}
    return effective_polling_config(incoming)


def point_poll_group(point: dict[str, Any], capability: dict[str, Any]) -> str:
    explicit = point.get("poll_group")
    if explicit:
        explicit_group = str(explicit)
        return "settings" if explicit_group == "modes" else explicit_group
    group = str(capability.get("group") or "")
    if group in {"outputs", "inputs"}:
        return group
    if group == "control_modes":
        return "settings"
    if group in {"device_settings", "diagnostics"}:
        return "settings"
    table = str(point.get("table") or "")
    if table in {"coil", "discrete_input"}:
        return "outputs" if table == "coil" else "inputs"
    if table in {"holding_register", "input_register"}:
        return "settings"
    return "state"


def template_slave_addresses(template: Rs485Template) -> list[int]:
    settings = _dict(template.communication.get("slave_address") or {}, "communication.slave_address")
    minimum = int(settings.get("min") or 1)
    maximum = int(settings.get("max") or 247)
    maximum = min(255, max(minimum, maximum))
    return list(range(minimum, maximum + 1))


def is_fatal_scan_error(error: str) -> bool:
    text = error.lower()
    return any(
        marker in text
        for marker in (
            "pymodbus is not installed",
            "unable to open",
            "no such file",
            "permission",
            "access denied",
            "unexpected keyword",
            "got an unexpected",
        )
    )


def _table_for_read_function(function_code: int) -> str:
    return {
        0x01: "coil",
        0x02: "discrete_input",
        0x03: "holding_register",
        0x04: "input_register",
    }.get(function_code, "holding_register")


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def default_bus_settings() -> dict[str, Any]:
    return {
        "serial_port": "/dev/ttyAMA3",
        "baudrate": 9600,
        "parity": "none",
        "stop_bits": 1,
        "mode": "mock",
        "enabled": True,
    }


def serial_port_options() -> list[dict[str, str]]:
    options: list[dict[str, str]] = []
    seen = {option["value"] for option in options}
    if list_ports is not None:
        try:
            ports = sorted(list_ports.comports(), key=lambda item: str(item.device))
        except Exception:
            ports = []
        for port in ports:
            device = str(port.device)
            if device in seen or not is_allowed_serial_port(device):
                continue
            label = f"USB RS-485 ({device})"
            description = str(getattr(port, "description", "") or "")
            if description and description != "n/a":
                label = description if device in description else f"{description} ({device})"
            options.append({"value": device, "label": label})
            seen.add(device)
    for device in windows_serial_ports_from_registry():
        if device in seen or not is_allowed_serial_port(device):
            continue
        options.append({"value": device, "label": f"USB RS-485 ({device})"})
        seen.add(device)
    return options


def windows_serial_ports_from_registry() -> list[str]:
    if sys.platform != "win32":
        return []
    try:
        import winreg
    except ImportError:
        return []
    ports: list[str] = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DEVICEMAP\SERIALCOMM") as key:
            index = 0
            while True:
                try:
                    _, value, _ = winreg.EnumValue(key, index)
                except OSError:
                    break
                port = str(value)
                if port.upper().startswith("COM"):
                    ports.append(port.upper())
                index += 1
    except OSError:
        return []
    return sorted(set(ports), key=lambda item: int(item[3:]) if item[3:].isdigit() else 9999)


def device_bus(device: Rs485Device) -> dict[str, Any]:
    return {
        "serial_port": device.serial_port,
        "baudrate": device.baudrate,
        "parity": device.parity,
        "stop_bits": device.stop_bits,
        "slave_address": device.slave_address,
    }


def device_identity_snapshot(template: Rs485Template, device: Rs485Device) -> dict[str, Any]:
    """Resolve template identity without inventing unavailable device data."""
    configured = template.identity or {}
    identity: dict[str, Any] = {}
    for field_name in ("manufacturer", "model", "hardware_version", "serial_number"):
        value = configured.get(field_name)
        if isinstance(value, str) and value.strip():
            identity[field_name] = value.strip()
    firmware = configured.get("firmware_version")
    if isinstance(firmware, dict):
        ref = firmware.get("register_ref")
        value = device.values.get(ref) if isinstance(ref, str) else None
        if value is not None and value != "":
            identity["firmware_version"] = value
    elif isinstance(firmware, str) and firmware.strip():
        identity["firmware_version"] = firmware.strip()
    return identity


def normalize_bus_settings(settings: dict[str, Any]) -> dict[str, Any]:
    serial_port = str(settings.get("serial_port") or settings.get("serial") or "/dev/ttyAMA3")
    if not is_allowed_serial_port(serial_port):
        serial_port = "/dev/ttyAMA3"
    baudrate = int(settings.get("baudrate") or 9600)
    parity = str(settings.get("parity") or "none").lower()
    if parity not in {"none", "even", "odd"}:
        parity = "none"
    stop_bits = int(settings.get("stop_bits") or settings.get("stopbits") or 1)
    if stop_bits not in {1, 2}:
        stop_bits = 1
    mode = str(settings.get("mode") or "mock").lower()
    if mode not in {"mock", "usb_real"}:
        mode = "mock"
    if serial_port.upper().startswith("COM"):
        mode = "usb_real"
    return {
        "serial_port": serial_port,
        "baudrate": baudrate,
        "parity": parity,
        "stop_bits": stop_bits,
        "mode": mode,
        "enabled": _coerce_bool(settings.get("enabled", True), True),
    }


def _coerce_bool(value: Any, default: bool = False) -> bool:
    """Decode booleans persisted by SQLite and received from JSON consistently."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return value != 0
    normalized = str(value).strip().lower()
    if normalized in {"true", "1", "yes", "on"}:
        return True
    if normalized in {"false", "0", "no", "off", ""}:
        return False
    return default


def is_allowed_serial_port(serial_port: str) -> bool:
    if serial_port in MOCK_SERIAL_PORTS:
        return True
    if serial_port.startswith("/dev/ttyUSB") or serial_port.startswith("/dev/ttyACM"):
        return serial_port[len("/dev/ttyUSB") :].isdigit() or serial_port[len("/dev/ttyACM") :].isdigit()
    if serial_port.upper().startswith("COM"):
        suffix = serial_port[3:]
        return suffix.isdigit() and 1 <= int(suffix) <= 256
    return False


def default_values_for_template(template: Rs485Template) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for capability_id, capability in normalized_capabilities(template).items():
        cap_type = capability.get("type")
        if cap_type == "switch":
            values[capability_id] = False
        elif cap_type == "binary_input":
            values[capability_id] = int(capability.get("order", 0)) % 30 == 10
        elif cap_type == "select":
            options = capability.get("options") or []
            values[capability_id] = options[0]["id"] if options else None
        elif cap_type == "number":
            values[capability_id] = capability.get("min", 1)
        elif cap_type == "sensor":
            values[capability_id] = "Mock"
    if "device_baudrate" in values:
        values["device_baudrate"] = 9600
    if "device_parity" in values:
        values["device_parity"] = "none"
    if "device_address" in values:
        values["device_address"] = 1
    return values


def sync_device_settings_from_values(device: Rs485Device) -> None:
    if "device_baudrate" in device.values:
        try:
            device.baudrate = int(device.values["device_baudrate"])
        except (TypeError, ValueError):
            pass
    if "device_parity" in device.values:
        parity = str(device.values["device_parity"]).lower()
        if parity in {"none", "even", "odd"}:
            device.parity = parity
    if "device_address" in device.values:
        try:
            address = int(device.values["device_address"])
        except (TypeError, ValueError):
            return
        if 1 <= address <= 247:
            device.slave_address = address


def normalize_capability_value(capability: dict[str, Any], value: Any) -> Any:
    cap_type = capability.get("type")
    if cap_type == "switch":
        return bool(value)
    if cap_type == "number":
        number = int(value)
        minimum = int(capability.get("min", number))
        maximum = int(capability.get("max", number))
        return max(minimum, min(maximum, number))
    if cap_type == "select":
        options = capability.get("options") or []
        allowed = {str(option.get("id")) for option in options}
        text = str(value)
        if text not in allowed:
            raise ValueError("invalid select option")
        return text
    return value


def normalized_capabilities(template: Rs485Template) -> dict[str, dict[str, Any]]:
    normalized: dict[str, dict[str, Any]] = {}
    for capability_id, capability in template.capabilities.items():
        item = dict(capability)
        if item.get("type") == "select" and not item.get("options"):
            item["options"] = select_options_for_capability(template, capability_id, item)
        normalized[capability_id] = item
    return normalized


def select_options_for_capability(
    template: Rs485Template,
    capability_id: str,
    capability: dict[str, Any],
) -> list[dict[str, str]]:
    source = capability.get("source")
    point = template.points.get(str(source)) if isinstance(source, str) else None
    if point and isinstance(point.get("map"), dict):
        return [
            {"id": str(value), "name": display_select_name(str(value))}
            for _, value in sorted(point["map"].items(), key=lambda pair: int(pair[0]))
        ]
    if capability_id == "device_baudrate":
        allowed = template.communication.get("baudrate", {}).get("allowed", [])
        return [{"id": str(value), "name": str(value)} for value in allowed]
    if capability_id == "device_parity":
        allowed = template.communication.get("parity", {}).get("allowed", [])
        return [{"id": str(value), "name": display_select_name(str(value))} for value in allowed]
    return []


def display_select_name(value: str) -> str:
    labels = {
        "none": "None",
        "even": "Even",
        "odd": "Odd",
        "normal": "Normal",
        "linkage": "Linkage",
        "toggle": "Toggle",
        "edge_trigger": "Edge",
    }
    return labels.get(value, value)


def decode_register_value(point: dict[str, Any], register: int) -> Any:
    value = register
    if "mask" in point:
        value = (value & int(point["mask"])) >> int(point.get("shift", 0))
    value_map = point.get("map")
    if isinstance(value_map, dict):
        mapped = value_map.get(value, value_map.get(str(value)))
        if mapped is not None:
            return mapped
    data_type = str(point.get("data_type") or "uint16")
    if data_type == "int16" and value >= 0x8000:
        value -= 0x10000
    if "scale" in point:
        return value * float(point["scale"])
    return value


def encode_register_value(point: dict[str, Any], value: Any) -> int:
    value_map = point.get("map")
    if isinstance(value_map, dict):
        for raw, mapped in value_map.items():
            if str(mapped) == str(value):
                return int(raw)
    if "scale" in point:
        return int(round(float(value) / float(point["scale"])))
    return int(value)


def _contiguous_read_groups(items: list[tuple[str, dict[str, Any]]]) -> list[list[tuple[str, dict[str, Any]]]]:
    keyed = sorted(
        items,
        key=lambda item: (
            str(item[1].get("table")),
            int(item[1].get("read_function") or _default_read_function(str(item[1].get("table")))),
            int(item[1].get("address")),
        ),
    )
    groups: list[list[tuple[str, dict[str, Any]]]] = []
    current: list[tuple[str, dict[str, Any]]] = []
    last_key: tuple[str, int] | None = None
    last_end: int | None = None
    for item in keyed:
        _capability_id, point = item
        table = str(point.get("table"))
        function = int(point.get("read_function") or _default_read_function(table))
        address = int(point.get("address"))
        count = int(point.get("count") or 1)
        key = (table, function)
        if current and (key != last_key or address != last_end):
            groups.append(current)
            current = []
        current.append(item)
        last_key = key
        last_end = address + count
    if current:
        groups.append(current)
    return groups


def _default_read_function(table: str) -> int:
    return {
        "coil": 0x01,
        "discrete_input": 0x02,
        "holding_register": 0x03,
        "input_register": 0x04,
    }.get(table, 0x03)


def _modbus_parity(parity: str) -> str:
    return {"none": "N", "even": "E", "odd": "O"}.get(parity.lower(), "N")


def modbus_rtu_read_request_hex(slave_address: int, function: int, address: int, count: int) -> str:
    frame = bytes(
        [
            slave_address & 0xFF,
            function & 0xFF,
            (address >> 8) & 0xFF,
            address & 0xFF,
            (count >> 8) & 0xFF,
            count & 0xFF,
        ]
    )
    return _hex_with_crc(frame)


def modbus_rtu_write_single_request_hex(slave_address: int, function: int, address: int, value: int) -> str:
    frame = bytes(
        [
            slave_address & 0xFF,
            function & 0xFF,
            (address >> 8) & 0xFF,
            address & 0xFF,
            (value >> 8) & 0xFF,
            value & 0xFF,
        ]
    )
    return _hex_with_crc(frame)


def modbus_rtu_read_register_response_hex(slave_address: int, function: int, registers: list[int]) -> str:
    payload: list[int] = [slave_address & 0xFF, function & 0xFF, len(registers) * 2]
    for register in registers:
        payload.extend([(int(register) >> 8) & 0xFF, int(register) & 0xFF])
    return _hex_with_crc(bytes(payload))


def _hex_with_crc(frame: bytes) -> str:
    crc = _modbus_crc16(frame)
    return " ".join(f"{byte:02X}" for byte in frame + bytes([crc & 0xFF, (crc >> 8) & 0xFF]))


def _modbus_crc16(frame: bytes) -> int:
    crc = 0xFFFF
    for byte in frame:
        crc ^= byte
        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc & 0xFFFF


def _raise_on_error(result: Any) -> None:
    if result is None:
        raise RuntimeError("empty Modbus response")
    if hasattr(result, "isError") and result.isError():
        raise RuntimeError(str(result))


def _result_bits(result: Any) -> list[bool]:
    _raise_on_error(result)
    bits = getattr(result, "bits", None)
    if not bits:
        raise RuntimeError("Modbus response has no bits")
    return [bool(bit) for bit in bits]


def _result_registers(result: Any) -> list[int]:
    _raise_on_error(result)
    registers = getattr(result, "registers", None)
    if not registers:
        raise RuntimeError("Modbus response has no registers")
    return [int(register) for register in registers]


def _dict(value: Any, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be an object")
    return value


def _text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _device_id(serial_port: str, slave_address: int) -> str:
    port = serial_port.strip("/").replace("/", "_")
    return f"rs485_{port}_slave{slave_address}"


def _default_store_path() -> Path:
    if sys.platform == "win32":
        return Path(".data/intellegyhub.sqlite3")
    return Path("/data/intellegyhub.sqlite3")


def _default_template_upload_dir() -> Path:
    override = os.environ.get("INTELLEGY_RS485_TEMPLATE_DIR")
    if override:
        return Path(override)
    if sys.platform == "win32":
        return Path(".data/rs485_templates")
    return Path("/data/rs485_templates")
