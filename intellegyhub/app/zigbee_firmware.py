from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import shlex
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Awaitable, Callable, Protocol

from .carrier import CarrierManager
from .paths import default_data_path

LOGGER = logging.getLogger(__name__)

FIRMWARE_RELEASES_URL = "https://api.github.com/repos/Koenkk/Z-Stack-firmware/releases?per_page=100"
FIRMWARE_DOWNLOAD_PREFIX = "https://github.com/Koenkk/Z-Stack-firmware/releases/download/"
FIRMWARE_PROFILE = "custom-cc2652p7-xmod-uart"
FIRMWARE_CHIP = "CC2652P7"
FIRMWARE_TARGET = "CC1352P7"
FIRMWARE_BAUDRATE = 115200
MOCK_COORDINATOR_REVISION = "20250321-mock"
ROM_BSL_ACK = 0xCC
ROM_BSL_NACK = 0x33
ROM_BSL_SYNC = b"\x55\x55"
ROM_BSL_STATUS = b"\x03\x23\x23"
ROM_BSL_TIMEOUT_SECONDS = 8.0
ROM_BSL_STATUS_SUCCESS = 0x40
ROM_BSL_STATUS_NAMES = {
    0x40: "success",
    0x41: "unknown command",
    0x42: "invalid command",
    0x43: "invalid address",
    0x44: "flash operation failed",
}
ZNP_SOF = 0xFE
ZNP_SYS_VERSION_CMD = (0x21, 0x02)
ZNP_SYS_VERSION_RESPONSE_CMD = (0x61, 0x02)
ZNP_SYS_VERSION_REQUEST = bytes((ZNP_SOF, 0x00, *ZNP_SYS_VERSION_CMD, 0x23))
ZNP_READ_TIMEOUT_SECONDS = 1.0
FIRMWARE_TAG_PREFIX = "Z-Stack_3.x.0_coordinator"
FIRMWARE_ASSET_RE = re.compile(r"^CC1352P7_coordinator_(?P<revision>\d+)\.zip$", re.IGNORECASE)
FIRMWARE_HEX_RE = re.compile(r"^CC1352P7_coordinator_(?P<revision>\d+)\.hex$", re.IGNORECASE)
MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_HEX_BYTES = 64 * 1024 * 1024
MAX_LOG_LINES = 2000
FLASH_PROGRESS_START = 40
FLASH_PROGRESS_END = 96

UPDATE_STATES = {
    "IDLE",
    "DOWNLOADING",
    "VERIFYING_IMAGE",
    "WAITING_FOR_BOOTLOADER",
    "ERASING",
    "FLASHING",
    "VERIFYING_FLASH",
    "RESTARTING_COORDINATOR",
    "COMPLETED",
    "FAILED",
}
ACTIVE_UPDATE_STATES = UPDATE_STATES - {"IDLE", "COMPLETED", "FAILED"}

PORT_PROFILES: dict[str, dict[str, str]] = {
    "/dev/ttyUSB0": {
        "label": "X-Mod1",
        "flash_output": "xmod1_flash_enable",
        "reset_output": "xmod1_reset",
    },
    "/dev/ttyUSB1": {
        "label": "X-Mod2",
        "flash_output": "xmod2_flash_enable",
        "reset_output": "xmod2_reset",
    },
}


class ZigbeeFirmwareError(RuntimeError):
    """A user-actionable firmware update failure."""


class ZigbeeFirmwareBusyError(ZigbeeFirmwareError):
    pass


class CoordinatorFirmwareReaderProtocol(Protocol):
    async def read_firmware_version(self, port: str) -> dict[str, Any] | None: ...


class RomBootloaderProbeProtocol(Protocol):
    async def probe(self, port: str) -> dict[str, Any]: ...


def parse_znp_sys_version_frame(frame: bytes) -> dict[str, Any] | None:
    """Decode a ZNP SYS_VERSION response from a running coordinator."""
    if len(frame) < 6 or frame[0] != ZNP_SOF:
        return None
    length = frame[1]
    expected_size = length + 5
    if len(frame) != expected_size or frame[2:4] != bytes(ZNP_SYS_VERSION_RESPONSE_CMD):
        return None
    if _znp_checksum(frame[1:-1]) != frame[-1]:
        return None
    payload = frame[4:-1]
    if len(payload) < 9:
        return None
    revision = int.from_bytes(payload[5:9], byteorder="little", signed=False)
    if revision <= 0:
        return None
    return {
        "version": str(revision),
        "revision": str(revision),
        "type": "zStack3x0",
        "source": "znp_sys_version",
        "transportrev": payload[0],
        "product": payload[1],
        "majorrel": payload[2],
        "minorrel": payload[3],
        "maintrel": payload[4],
        "raw": frame.hex(" "),
    }


def _znp_checksum(data: bytes) -> int:
    checksum = 0
    for value in data:
        checksum ^= value
    return checksum


class ZnpCoordinatorReader:
    """Read coordinator metadata through the running ZNP application UART."""

    async def read_firmware_version(self, port: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._read_firmware_version_sync, port)

    def _read_firmware_version_sync(self, port: str) -> dict[str, Any] | None:
        try:
            import serial

            with serial.Serial(
                port=port,
                baudrate=FIRMWARE_BAUDRATE,
                timeout=0.1,
                write_timeout=1.0,
                exclusive=True,
            ) as connection:
                connection.reset_input_buffer()
                connection.write(ZNP_SYS_VERSION_REQUEST)
                connection.flush()
                frame = self._read_response_frame(connection)
        except Exception as exc:
            raise ZigbeeFirmwareError(f"Unable to read coordinator version from {port}: {_safe_error(exc)}") from exc
        return parse_znp_sys_version_frame(frame) if frame else None

    @staticmethod
    def _read_response_frame(connection: Any) -> bytes | None:
        deadline = time.monotonic() + ZNP_READ_TIMEOUT_SECONDS

        def read_exact(size: int) -> bytes | None:
            data = bytearray()
            while len(data) < size and time.monotonic() < deadline:
                chunk = connection.read(size - len(data))
                if chunk:
                    data.extend(chunk)
            return bytes(data) if len(data) == size else None

        while time.monotonic() < deadline:
            marker = read_exact(1)
            if marker is None:
                return None
            if marker[0] != ZNP_SOF:
                continue
            length = read_exact(1)
            if length is None:
                return None
            body = read_exact(length[0] + 3)
            if body is None:
                return None
            frame = bytes((ZNP_SOF, length[0])) + body
            if parse_znp_sys_version_frame(frame) is not None:
                return frame
        return None


class MockCoordinatorReader:
    """In-memory SYS_VERSION responder used by the local/mock backend."""

    def __init__(self) -> None:
        self._erased_ports: set[str] = set()

    def mark_erased(self, port: str) -> None:
        self._erased_ports.add(port)

    def mark_flashed(self, port: str) -> None:
        self._erased_ports.discard(port)

    async def read_firmware_version(self, port: str) -> dict[str, Any] | None:
        if port != "/dev/ttyUSB0" or port in self._erased_ports:
            return None
        return {
            "version": MOCK_COORDINATOR_REVISION,
            "revision": MOCK_COORDINATOR_REVISION,
            "type": "zStack3x0",
            "source": "mock_znp_sys_version",
            "raw": "mock SYS_VERSION response",
        }


class RomBootloaderProbe:
    """Perform a non-destructive ROM BSL handshake before erase or write."""

    async def probe(self, port: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._probe_sync, port)

    @staticmethod
    def _probe_sync(port: str) -> dict[str, Any]:
        try:
            import serial

            with serial.Serial(
                port=port,
                baudrate=FIRMWARE_BAUDRATE,
                timeout=0.1,
                write_timeout=1.0,
                exclusive=True,
            ) as connection:
                connection.reset_input_buffer()
                connection.write(ROM_BSL_SYNC)
                if not RomBootloaderProbe._wait_for_ack(connection):
                    raise ZigbeeFirmwareError("ROM bootloader rejected the synchronization sequence")
                connection.write(ROM_BSL_STATUS)
                if not RomBootloaderProbe._wait_for_ack(connection):
                    raise ZigbeeFirmwareError("ROM bootloader did not acknowledge status request")
                status = RomBootloaderProbe._read_packet(connection)
                if not status or status[0] != ROM_BSL_STATUS_SUCCESS:
                    code = status[0] if status else "missing"
                    description = ROM_BSL_STATUS_NAMES.get(code, "unknown status") if isinstance(code, int) else "missing status"
                    if isinstance(code, int):
                        raise ZigbeeFirmwareError(
                            f"ROM bootloader returned status {code} (0x{code:02X}, {description})"
                        )
                    raise ZigbeeFirmwareError(f"ROM bootloader returned status {code}")
                connection.write(b"\x00\xCC")
                connection.flush()
        except ZigbeeFirmwareError:
            raise
        except Exception as exc:
            raise ZigbeeFirmwareError(f"Unable to probe ROM bootloader on {port}: {_safe_error(exc)}") from exc
        return {
            "responsive": True,
            "protocol": "cc2538-rom-bsl",
            "port": port,
            "status": ROM_BSL_STATUS_SUCCESS,
        }

    @staticmethod
    def _wait_for_ack(connection: Any) -> bool:
        deadline = time.monotonic() + ROM_BSL_TIMEOUT_SECONDS
        previous = None
        while time.monotonic() < deadline:
            value = connection.read(1)
            if not value:
                continue
            current = value[0]
            if previous == 0 and current in {ROM_BSL_ACK, ROM_BSL_NACK}:
                return current == ROM_BSL_ACK
            previous = current
        raise ZigbeeFirmwareError("Timeout waiting for ROM bootloader ACK")

    @staticmethod
    def _read_packet(connection: Any) -> bytes:
        deadline = time.monotonic() + ROM_BSL_TIMEOUT_SECONDS
        header = RomBootloaderProbe._read_exact(connection, 2, deadline)
        size, checksum = header
        if size < 2:
            raise ZigbeeFirmwareError("ROM bootloader returned an invalid packet size")
        payload = RomBootloaderProbe._read_exact(connection, size - 2, deadline)
        if sum(payload) & 0xFF != checksum:
            raise ZigbeeFirmwareError("ROM bootloader returned an invalid packet checksum")
        return payload

    @staticmethod
    def _read_exact(connection: Any, size: int, deadline: float) -> bytes:
        data = bytearray()
        while len(data) < size and time.monotonic() < deadline:
            chunk = connection.read(size - len(data))
            if chunk:
                data.extend(chunk)
        if len(data) != size:
            raise ZigbeeFirmwareError("Timeout reading ROM bootloader response")
        return bytes(data)


class MockRomBootloaderProbe:
    async def probe(self, port: str) -> dict[str, Any]:
        return {"responsive": True, "protocol": "mock-cc2538-rom-bsl", "port": port}


class CarrierProtocol(Protocol):
    async def set_output(self, output_id: str, on: bool) -> Any: ...


class FirmwareSourceProtocol(Protocol):
    async def fetch_catalog(self) -> list[dict[str, Any]]: ...

    async def prepare_image(
        self,
        release: dict[str, Any],
        destination: Path,
    ) -> dict[str, Any]: ...


class MockFirmwareSource:
    """Deterministic firmware source used only by the local/mock backend."""

    def _release(self) -> dict[str, Any]:
        revision = "20250321"
        return {
            "tag": f"{FIRMWARE_TAG_PREFIX}_{revision}",
            "revision": revision,
            "published_at": "2025-07-10T00:00:00Z",
            "firmware_target": FIRMWARE_TARGET,
            "profile": FIRMWARE_PROFILE,
            "asset_name": f"CC1352P7_coordinator_{revision}.zip",
            "asset_url": f"{FIRMWARE_DOWNLOAD_PREFIX}{FIRMWARE_TAG_PREFIX}_{revision}/CC1352P7_coordinator_{revision}.zip",
            "size": 0,
            "digest": None,
        }

    async def fetch_catalog(self) -> list[dict[str, Any]]:
        return [self._release()]

    async def prepare_image(self, release: dict[str, Any], destination: Path) -> dict[str, Any]:
        revision = str(release.get("revision") or "20250321")
        operation_dir = destination / revision
        operation_dir.mkdir(parents=True, exist_ok=True)
        image_path = operation_dir / f"CC1352P7_coordinator_{revision}.hex"
        image_path.write_text(":00000001FF\n", encoding="ascii")
        return {
            "profile": FIRMWARE_PROFILE,
            "chip": FIRMWARE_CHIP,
            "firmware_target": FIRMWARE_TARGET,
            "hex": str(image_path),
            "archive_crc_verified": True,
            "digest_verified": False,
            "mock": True,
        }


@dataclass(frozen=True)
class FirmwareTiming:
    flash_to_reset_seconds: float = 0.05
    flash_after_reset_seconds: float = 0.1
    reset_pulse_seconds: float = 0.05
    bootloader_startup_seconds: float = 0.1
    coordinator_startup_seconds: float = 3.0

    @classmethod
    def from_environment(cls) -> "FirmwareTiming":
        values: dict[str, float] = {}
        names = {
            "flash_to_reset_seconds": "ZIGBEE_FLASH_TO_RESET_SECONDS",
            "reset_pulse_seconds": "ZIGBEE_RESET_PULSE_SECONDS",
            "flash_after_reset_seconds": "ZIGBEE_FLASH_AFTER_RESET_SECONDS",
            "bootloader_startup_seconds": "ZIGBEE_BOOTLOADER_STARTUP_SECONDS",
            "coordinator_startup_seconds": "ZIGBEE_COORDINATOR_STARTUP_SECONDS",
        }
        for field, name in names.items():
            value = os.environ.get(name)
            if value is None or not value.strip():
                continue
            try:
                values[field] = float(value)
            except ValueError as exc:
                raise ZigbeeFirmwareError(f"{name} must be a number") from exc
        return cls(**values)

    def validate(self) -> None:
        values = {
            "flash_to_reset_seconds": self.flash_to_reset_seconds,
            "reset_pulse_seconds": self.reset_pulse_seconds,
            "flash_after_reset_seconds": self.flash_after_reset_seconds,
            "bootloader_startup_seconds": self.bootloader_startup_seconds,
            "coordinator_startup_seconds": self.coordinator_startup_seconds,
        }
        for name, value in values.items():
            if not 0.001 <= value <= 5:
                raise ZigbeeFirmwareError(f"{name} must be between 0.001 and 5 seconds")


def empty_update_state() -> dict[str, Any]:
    return {
        "status": "IDLE",
        "operation_id": None,
        "port": None,
        "port_label": None,
        "release": None,
        "stage": None,
        "progress": None,
        "detail": "No operation running.",
        "target": None,
        "logs": [],
        "error": None,
        "started_at": None,
        "finished_at": None,
        "installed_firmware": None,
        "detected_ports": {},
        "operation_type": None,
        "verification": None,
    }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_error(exc: BaseException) -> str:
    message = str(exc).strip()
    return message or exc.__class__.__name__


def _read_intel_hex_data(path: Path) -> dict[int, int]:
    """Read Intel HEX records using only the Python standard library.

    The add-on normally uses ``intelhex`` for validation, but the local/mock
    backend must remain usable in a lightweight development environment where
    optional runtime dependencies may not be installed.
    """
    data: dict[int, int] = {}
    upper_address = 0
    segment_address = 0
    saw_eof = False

    try:
        lines = path.read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"unable to read Intel HEX: {_safe_error(exc)}") from exc

    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line:
            continue
        if saw_eof:
            raise ValueError(f"data found after EOF record on line {line_number}")
        if not line.startswith(":"):
            raise ValueError(f"line {line_number} does not start with ':'")
        try:
            record = bytes.fromhex(line[1:])
        except ValueError as exc:
            raise ValueError(f"line {line_number} contains non-hex characters") from exc
        if len(record) < 5:
            raise ValueError(f"line {line_number} is too short")
        byte_count = record[0]
        if len(record) != byte_count + 5:
            raise ValueError(f"line {line_number} has an invalid byte count")
        if sum(record) & 0xFF:
            raise ValueError(f"line {line_number} has an invalid checksum")

        address = int.from_bytes(record[1:3], byteorder="big")
        record_type = record[3]
        payload = record[4:-1]
        if len(payload) != byte_count:
            raise ValueError(f"line {line_number} has an invalid payload length")

        if record_type == 0x00:
            base_address = upper_address + segment_address
            for offset, value in enumerate(payload):
                data[base_address + address + offset] = value
        elif record_type == 0x01:
            if byte_count != 0:
                raise ValueError(f"line {line_number} has a non-empty EOF record")
            saw_eof = True
        elif record_type == 0x02:
            if byte_count != 2:
                raise ValueError(f"line {line_number} has an invalid segment address record")
            segment_address = int.from_bytes(payload, byteorder="big") << 4
            upper_address = 0
        elif record_type == 0x04:
            if byte_count != 2:
                raise ValueError(f"line {line_number} has an invalid linear address record")
            upper_address = int.from_bytes(payload, byteorder="big") << 16
            segment_address = 0
        elif record_type in {0x03, 0x05}:
            # Start-address records do not contribute flash data.
            continue
        else:
            raise ValueError(f"line {line_number} uses unsupported record type 0x{record_type:02X}")

    if not saw_eof:
        raise ValueError("Intel HEX EOF record is missing")
    return data


def _is_uart_busy_error(exc: BaseException) -> bool:
    message = _safe_error(exc).lower()
    return "could not exclusively lock" in message or "resource temporarily unavailable" in message or "errno 11" in message


def _release_label(release: dict[str, Any]) -> str:
    return " · ".join(
        str(value)
        for value in (
            release.get("revision") or "unknown",
            release.get("firmware_target") or FIRMWARE_TARGET,
            release.get("published_at") or "date unavailable",
        )
    )


def _public_release(release: dict[str, Any]) -> dict[str, Any]:
    """Remove manager-only fields before a release reaches the API/UI."""
    return {key: value for key, value in release.items() if not key.startswith("_")}


def parse_compatible_releases(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        raise ZigbeeFirmwareError("GitHub releases response is not a list")

    seen: set[str] = set()
    releases: list[dict[str, Any]] = []
    for release in payload:
        if not isinstance(release, dict):
            continue
        tag = str(release.get("tag_name") or "")
        if release.get("draft") or release.get("prerelease") or not tag.startswith(FIRMWARE_TAG_PREFIX):
            continue
        for asset in release.get("assets") or []:
            if not isinstance(asset, dict):
                continue
            name = str(asset.get("name") or "")
            match = FIRMWARE_ASSET_RE.fullmatch(name)
            if match is None or match.group("revision") in seen:
                continue
            url = str(asset.get("browser_download_url") or "")
            if not url.startswith(FIRMWARE_DOWNLOAD_PREFIX):
                continue
            revision = match.group("revision")
            seen.add(revision)
            digest = asset.get("digest")
            releases.append(
                {
                    "tag": tag,
                    "revision": revision,
                    "published_at": release.get("published_at"),
                    "firmware_target": FIRMWARE_TARGET,
                    "profile": FIRMWARE_PROFILE,
                    "asset_name": name,
                    "asset_url": url,
                    "size": asset.get("size"),
                    "digest": digest if isinstance(digest, str) else None,
                }
            )
    return sorted(releases, key=lambda item: str(item["revision"]), reverse=True)


class GithubFirmwareSource:
    def __init__(self, releases_url: str = FIRMWARE_RELEASES_URL) -> None:
        self.releases_url = releases_url

    async def fetch_catalog(self) -> list[dict[str, Any]]:
        payload = await asyncio.to_thread(self._get_json, self.releases_url)
        return parse_compatible_releases(payload)

    async def prepare_image(self, release: dict[str, Any], destination: Path) -> dict[str, Any]:
        return await asyncio.to_thread(self._prepare_image_sync, release, destination)

    @staticmethod
    def _get_json(url: str) -> Any:
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/vnd.github+json", "User-Agent": "IntellegyHUB"},
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.loads(response.read().decode("utf-8"))
        except (OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise ZigbeeFirmwareError(f"Unable to fetch firmware releases: {_safe_error(exc)}") from exc

    @staticmethod
    def _prepare_image_sync(release: dict[str, Any], destination: Path) -> dict[str, Any]:
        revision = str(release.get("revision") or "")
        asset_name = str(release.get("asset_name") or "")
        asset_url = str(release.get("asset_url") or "")
        if release.get("profile") != FIRMWARE_PROFILE or release.get("firmware_target") != FIRMWARE_TARGET:
            raise ZigbeeFirmwareError("Selected firmware does not match the approved CC2652P7 profile")
        if not revision.isdigit() or FIRMWARE_ASSET_RE.fullmatch(asset_name) is None:
            raise ZigbeeFirmwareError("Selected firmware asset is not approved")
        parsed_url = urllib.parse.urlparse(asset_url)
        if parsed_url.scheme != "https" or parsed_url.netloc != "github.com" or not asset_url.startswith(FIRMWARE_DOWNLOAD_PREFIX):
            raise ZigbeeFirmwareError("Firmware download URL is not an approved Koenkk release URL")

        destination.mkdir(parents=True, exist_ok=True)
        operation_dir = destination / revision
        operation_dir.mkdir(parents=True, exist_ok=True)
        archive_path = operation_dir / asset_name
        temp_path = archive_path.with_suffix(".download")
        try:
            GithubFirmwareSource._download(asset_url, temp_path)
            temp_path.replace(archive_path)
            archive_sha256 = hashlib.sha256(archive_path.read_bytes()).hexdigest()
            expected_digest = str(release.get("digest") or "")
            digest_verified = False
            if expected_digest:
                if not expected_digest.lower().startswith("sha256:"):
                    raise ZigbeeFirmwareError("Unsupported firmware digest format")
                expected_sha256 = expected_digest.split(":", 1)[1].lower()
                if expected_sha256 != archive_sha256:
                    raise ZigbeeFirmwareError("Firmware archive checksum mismatch")
                digest_verified = True

            expected_hex_name = f"CC1352P7_coordinator_{revision}.hex"
            hex_path = operation_dir / expected_hex_name
            with zipfile.ZipFile(archive_path) as archive:
                bad_member = archive.testzip()
                if bad_member is not None:
                    raise ZigbeeFirmwareError(f"Firmware archive CRC check failed: {bad_member}")
                matching: list[zipfile.ZipInfo] = []
                for info in archive.infolist():
                    name = info.filename.replace("\\", "/")
                    pure = PurePosixPath(name)
                    if pure.is_absolute() or ".." in pure.parts:
                        raise ZigbeeFirmwareError("Firmware archive contains an unsafe path")
                    if info.is_dir():
                        continue
                    if pure.name == expected_hex_name:
                        matching.append(info)
                if len(matching) != 1:
                    raise ZigbeeFirmwareError("Approved firmware HEX was not found uniquely in the archive")
                info = matching[0]
                if info.file_size <= 0 or info.file_size > MAX_HEX_BYTES:
                    raise ZigbeeFirmwareError("Firmware HEX size is outside the allowed range")
                with archive.open(info) as source, hex_path.open("wb") as target:
                    total = 0
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > MAX_HEX_BYTES:
                            raise ZigbeeFirmwareError("Firmware HEX is too large")
                        target.write(chunk)
            GithubFirmwareSource._validate_hex(hex_path, revision)
            manifest = {
                "profile": FIRMWARE_PROFILE,
                "chip": FIRMWARE_CHIP,
                "firmware_target": FIRMWARE_TARGET,
                "revision": revision,
                "asset_name": asset_name,
                "archive": str(archive_path),
                "hex": str(hex_path),
                "sha256": archive_sha256,
                "expected_digest": expected_digest or None,
                "digest_verified": digest_verified,
                "archive_crc_verified": True,
                "prepared_at": _utc_now(),
            }
            (operation_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
            return manifest
        except Exception:
            with contextlib.suppress(OSError):
                temp_path.unlink()
            raise

    @staticmethod
    def _download(url: str, destination: Path) -> None:
        request = urllib.request.Request(url, headers={"User-Agent": "IntellegyHUB"})
        with urllib.request.urlopen(request, timeout=60) as response:
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > MAX_ARCHIVE_BYTES:
                raise ZigbeeFirmwareError("Firmware archive is too large")
            total = 0
            with destination.open("wb") as handle:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_ARCHIVE_BYTES:
                        raise ZigbeeFirmwareError("Firmware archive is too large")
                    handle.write(chunk)

    @staticmethod
    def _validate_hex(path: Path, revision: str) -> None:
        try:
            try:
                from intelhex import IntelHex
            except ModuleNotFoundError:
                if not _read_intel_hex_data(path):
                    raise ZigbeeFirmwareError("Firmware HEX contains no data")
                return

            image = IntelHex(str(path))
            if not image.todict():
                raise ZigbeeFirmwareError("Firmware HEX contains no data")
        except ZigbeeFirmwareError:
            raise
        except Exception as exc:
            raise ZigbeeFirmwareError(f"Firmware HEX validation failed for {revision}: {_safe_error(exc)}") from exc


class SerialLease:
    def __init__(self, path: str, lock_path: Path) -> None:
        self.path = path
        self.lock_path = lock_path
        self._handle: Any = None

    def acquire(self) -> None:
        port = Path(self.path)
        if not port.exists():
            raise ZigbeeFirmwareError(f"UART device is not available: {self.path}")
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.lock_path.open("a+")
        if sys.platform == "win32":
            return
        try:
            import fcntl

            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as exc:
            self.release()
            raise ZigbeeFirmwareError(f"UART is already reserved: {self.path}") from exc

    def release(self) -> None:
        if self._handle is None:
            return
        if sys.platform != "win32":
            with contextlib.suppress(OSError):
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            self._handle.close()
        self._handle = None


class ZigbeeFirmwareManager:
    def __init__(
        self,
        carrier: CarrierProtocol | None = None,
        publisher: Callable[[dict[str, Any]], Any] | None = None,
        data_dir: Path | None = None,
        source: FirmwareSourceProtocol | None = None,
        coordinator_reader: CoordinatorFirmwareReaderProtocol | None = None,
        bootloader_probe: RomBootloaderProbeProtocol | None = None,
        timing: FirmwareTiming | None = None,
        mock: bool = False,
        flasher: Callable[[str, Path, Callable[[str, str], Awaitable[None]]], Awaitable[tuple[bool, bool]]] | None = None,
    ) -> None:
        self.carrier = carrier
        self.publisher = publisher
        self.data_dir = data_dir or default_data_path("zigbee-firmware")
        self.state_path = self.data_dir / "state.json"
        self.mock = mock
        self.source = source or (MockFirmwareSource() if mock else GithubFirmwareSource())
        self.coordinator_reader = coordinator_reader or (
            MockCoordinatorReader() if mock else ZnpCoordinatorReader()
        )
        self.bootloader_probe = bootloader_probe or (MockRomBootloaderProbe() if mock else RomBootloaderProbe())
        self.timing = timing or FirmwareTiming.from_environment()
        self._flasher = flasher or (self._run_mock_flasher if mock else self._run_flasher)
        self._state = empty_update_state()
        self._catalog: list[dict[str, Any]] = []
        self._state_lock = asyncio.Lock()
        self._operation_lock = asyncio.Lock()
        self._operation_task: asyncio.Task[None] | None = None
        self._flash_progress_total = 0
        self._flash_progress_written = 0
        self._started = False

    async def start(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._state = await asyncio.to_thread(self._load_state)
        if self._state.get("status") in ACTIVE_UPDATE_STATES:
            self._state["status"] = "FAILED"
            self._state["error"] = "Add-on restarted while firmware update was in progress"
            self._state["detail"] = self._state["error"]
            self._state["finished_at"] = _utc_now()
            self._append_log_local("error", self._state["error"])
            await self._persist_and_publish()
            await self._release_controls(self._state.get("port"))
        self._started = True

    async def stop(self) -> None:
        task = self._operation_task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            if self._state.get("status") in ACTIVE_UPDATE_STATES:
                self._state["status"] = "FAILED"
                self._state["error"] = "Add-on stopped during firmware update"
                self._state["detail"] = self._state["error"]
                self._state["finished_at"] = _utc_now()
                self._append_log_local("error", self._state["error"])
        await self._release_controls(self._state.get("port"))
        await self._persist_and_publish()
        self._started = False

    def set_carrier(self, carrier: CarrierProtocol) -> None:
        self.carrier = carrier

    def set_publisher(self, publisher: Callable[[dict[str, Any]], Any] | None) -> None:
        self.publisher = publisher

    def set_timing(self, timing: FirmwareTiming) -> None:
        self.timing = timing

    def snapshot(self, port: str | None = None) -> dict[str, Any]:
        state = json.loads(json.dumps(self._state))
        selected_port = port if port in PORT_PROFILES else state.get("port")
        if selected_port in PORT_PROFILES:
            state["port"] = selected_port
            state["port_label"] = PORT_PROFILES[selected_port]["label"]
            selected = self._state.get("detected_ports", {}).get(selected_port)
            state["installed_firmware"] = selected.get("firmware") if selected and selected.get("detected") else None
        return {
            **state,
            "profile": FIRMWARE_PROFILE,
            "chip": FIRMWARE_CHIP,
            "firmware_target": FIRMWARE_TARGET,
            "ports": [
                {
                    "path": path,
                    "label": profile["label"],
                    **(
                        self._state.get("detected_ports", {}).get(path)
                        or {"available": False, "detected": False, "status": "UNAVAILABLE", "firmware": None}
                    ),
                }
                for path, profile in PORT_PROFILES.items()
            ],
            "ports_detected": bool(self._state.get("detected_ports")),
            "reset_pulse_configured": True,
            "bootloader_timing": {
                "flash_to_reset_ms": round(self.timing.flash_to_reset_seconds * 1000),
                "reset_pulse_ms": round(self.timing.reset_pulse_seconds * 1000),
                "flash_after_reset_ms": round(self.timing.flash_after_reset_seconds * 1000),
                "bootloader_startup_ms": round(self.timing.bootloader_startup_seconds * 1000),
                "coordinator_startup_ms": round(self.timing.coordinator_startup_seconds * 1000),
            },
            "mock": self.mock,
            "started": self._started,
        }

    async def check_releases(self) -> dict[str, Any]:
        releases = await self.source.fetch_catalog()
        local_releases = [item for item in self._catalog if item.get("source") == "local"]
        self._catalog = [item for item in releases if self._is_approved_release(item)] + local_releases
        return {"releases": [_public_release(item) for item in self._catalog], "profile": FIRMWARE_PROFILE}

    async def upload_local_hex(self, filename: str, content: bytes) -> dict[str, Any]:
        """Validate and add one local Intel HEX image to the approved catalog."""
        safe_name = Path(filename or "").name
        if not safe_name or safe_name in {".", ".."} or safe_name.lower().endswith(".hex") is False:
            raise ZigbeeFirmwareError("Only Intel HEX files with a .hex extension are supported")
        if not content:
            raise ZigbeeFirmwareError("The firmware HEX file is empty")
        if len(content) > MAX_HEX_BYTES:
            raise ZigbeeFirmwareError("Firmware HEX file is too large")

        local_dir = self.data_dir / "local"
        await asyncio.to_thread(local_dir.mkdir, parents=True, exist_ok=True)
        path = local_dir / f"{uuid.uuid4().hex}.hex"
        await asyncio.to_thread(path.write_bytes, content)
        try:
            GithubFirmwareSource._validate_hex(path, safe_name)
        except Exception:
            with contextlib.suppress(OSError):
                await asyncio.to_thread(path.unlink)
            raise

        for previous in self._catalog:
            if previous.get("source") == "local" and previous.get("_local_path"):
                with contextlib.suppress(OSError):
                    await asyncio.to_thread(Path(str(previous["_local_path"])).unlink)
        release = {
            "source": "local",
            "revision": f"local-{path.stem[:12]}",
            "published_at": None,
            "firmware_target": FIRMWARE_TARGET,
            "profile": FIRMWARE_PROFILE,
            "chip": FIRMWARE_CHIP,
            "asset_name": safe_name,
            "size": len(content),
            "_local_path": str(path),
        }
        self._catalog = [item for item in self._catalog if item.get("source") != "local"] + [release]
        return _public_release(release)

    async def refresh_installed_firmware(self, port: str = "/dev/ttyUSB0") -> dict[str, Any]:
        """Detect coordinator modules and read the selected one over its ZNP UART."""
        if self._state.get("status") in ACTIVE_UPDATE_STATES:
            return self.snapshot()
        if port not in PORT_PROFILES:
            raise ZigbeeFirmwareError("Unsupported coordinator port")
        detected_ports = await self.detect_coordinator_ports()
        selected = detected_ports.get(port) or {"detected": False, "firmware": None}
        # A manual detection starts a new UI inspection; do not keep showing a
        # stale install/erase result from an earlier operation.
        self._state = empty_update_state()
        self._state["detected_ports"] = detected_ports
        self._state["port"] = port
        self._state["port_label"] = PORT_PROFILES[port]["label"]
        self._state["installed_firmware"] = selected.get("firmware") if selected.get("detected") else None
        await self._persist_and_publish()
        return self.snapshot()

    async def clear_operation(self, port: str = "/dev/ttyUSB0") -> dict[str, Any]:
        """Clear the last operation while preserving the selected coordinator detection."""
        async with self._operation_lock:
            if self._state.get("status") in ACTIVE_UPDATE_STATES:
                raise ZigbeeFirmwareBusyError("A firmware update is already running")
            if port not in PORT_PROFILES:
                raise ZigbeeFirmwareError("Unsupported coordinator port")
            detected_ports = self._state.get("detected_ports", {})
            selected = detected_ports.get(port) or {}
            self._state = empty_update_state()
            self._state["detected_ports"] = detected_ports
            self._state["port"] = port
            self._state["port_label"] = PORT_PROFILES[port]["label"]
            self._state["installed_firmware"] = selected.get("firmware") if selected.get("detected") else None
            await self._persist_and_publish()
            return self.snapshot(port)

    async def detect_coordinator_ports(self) -> dict[str, dict[str, Any]]:
        async def probe(path: str) -> tuple[str, dict[str, Any]]:
            try:
                firmware = await self.coordinator_reader.read_firmware_version(path)
            except ZigbeeFirmwareError as exc:
                LOGGER.info("Coordinator probe failed port=%s: %s", path, exc)
                if _is_uart_busy_error(exc):
                    return path, {
                        "available": False,
                        "detected": False,
                        "status": "BUSY",
                        "detail": "Coordinator UART is busy; stop Zigbee2MQTT before detection or firmware operation.",
                        "firmware": None,
                    }
                return path, {"available": False, "detected": False, "status": "UNAVAILABLE", "firmware": None}
            return path, {
                "available": True,
                "detected": firmware is not None,
                "status": "READY" if firmware is not None else "NO_RESPONSE",
                "firmware": firmware,
            }

        results = await asyncio.gather(*(probe(path) for path in PORT_PROFILES))
        detected = dict(results)
        self._state["detected_ports"] = detected
        return detected

    async def start_update(self, port: str, revision: str) -> dict[str, Any]:
        async with self._operation_lock:
            return await self._start_update_locked(port, revision)

    async def start_erase(self, port: str) -> dict[str, Any]:
        async with self._operation_lock:
            if port not in PORT_PROFILES:
                raise ZigbeeFirmwareError("Unsupported coordinator port")
            if self._operation_task is not None and not self._operation_task.done():
                raise ZigbeeFirmwareBusyError("A firmware update is already running")
            operation_id = uuid.uuid4().hex
            self._state = empty_update_state()
            self._state.update(
                {
                    "status": "WAITING_FOR_BOOTLOADER",
                    "operation_id": operation_id,
                    "operation_type": "erase",
                    "port": port,
                    "port_label": PORT_PROFILES[port]["label"],
                    "stage": "connect",
                    "detail": "Preparing ROM bootloader",
                    "target": f"{PORT_PROFILES[port]['label']} · {FIRMWARE_CHIP} · erase",
                    "started_at": _utc_now(),
                }
            )
            await self._persist_and_publish()
            self._operation_task = asyncio.create_task(
                self._run_update(operation_id, port, None, erase_only=True),
                name=f"intellegyhub-zigbee-erase-{operation_id}",
            )
            return self.snapshot()

    async def _start_update_locked(self, port: str, revision: str) -> dict[str, Any]:
        if port not in PORT_PROFILES:
            raise ZigbeeFirmwareError("Unsupported coordinator port")
        if self._operation_task is not None and not self._operation_task.done():
            raise ZigbeeFirmwareBusyError("A firmware update is already running")
        release = next((item for item in self._catalog if str(item.get("revision")) == str(revision)), None)
        if release is None:
            raise ZigbeeFirmwareError("Firmware release is not in the approved catalog; check for updates first")
        operation_id = uuid.uuid4().hex
        self._state = empty_update_state()
        self._state.update(
            {
                "status": "DOWNLOADING",
                "operation_id": operation_id,
                "operation_type": "install",
                "port": port,
                "port_label": PORT_PROFILES[port]["label"],
                "release": _public_release(release),
                "stage": "download",
                "detail": "Downloading approved firmware image",
                "started_at": _utc_now(),
            }
        )
        await self._persist_and_publish()
        self._operation_task = asyncio.create_task(
            self._run_update(operation_id, port, release),
            name=f"intellegyhub-zigbee-firmware-{operation_id}",
        )
        return self.snapshot()

    async def _run_update(
        self,
        operation_id: str,
        port: str,
        release: dict[str, Any] | None,
        *,
        erase_only: bool = False,
    ) -> None:
        lease: SerialLease | None = None
        primary_error: str | None = None
        controls_asserted = False
        try:
            if operation_id != self._state.get("operation_id"):
                return
            self._flash_progress_total = 0
            self._flash_progress_written = 0
            if not self.mock:
                self.timing.validate()
            if self.carrier is None and not self.mock:
                raise ZigbeeFirmwareError("Carrier MCP23017 is not available")
            manifest = None if erase_only else await self._prepare_image(release or {})
            if not erase_only:
                self._flash_progress_total = await asyncio.to_thread(
                    self._hex_data_byte_count,
                    Path(manifest["hex"]),
                )
                await self._append_log("system", "Firmware image loaded and validated")
            release_revision = "erase" if erase_only else str((release or {}).get("revision"))
            separator = chr(0xB7)
            await self._set_state(
                status="WAITING_FOR_BOOTLOADER",
                stage="connect",
                detail="Preparing ROM bootloader",
                target=f"{PORT_PROFILES[port]['label']} {separator} {FIRMWARE_TARGET} {separator} {release_revision}",
            )

            if not self.mock:
                lease = SerialLease(port, self.data_dir / "locks" / (Path(port).name + ".lock"))
                lease.acquire()
                await self._check_uart(port)
            await self._set_state(status="WAITING_FOR_BOOTLOADER", stage="connect", detail="Entering ROM bootloader")
            await self._enter_bootloader(port)
            controls_asserted = True
            await self._append_log("system", "Flash/Reset sequence completed")
            await self._set_state(detail="Checking ROM bootloader communication")
            bootloader = await self.bootloader_probe.probe(port)
            if not bootloader.get("responsive"):
                raise ZigbeeFirmwareError("ROM bootloader did not respond; erase/write was not started")
            bootloader_status = bootloader.get("status")
            if isinstance(bootloader_status, int):
                await self._append_log("system", f"ROM bootloader connected (status 0x{bootloader_status:02X})")
            else:
                await self._append_log("system", "ROM bootloader connected")
            await self._set_state(detail="ROM bootloader connected")
            if not self.mock:
                # cc2538-bsl opens the UART itself and toggles DTR/RTS. Re-enter
                # ROM after the non-destructive preflight so that those lines
                # cannot leave the target between the probe and the utility.
                await self._append_log("system", "Re-entering ROM bootloader before cc2538-bsl")
                await self._enter_bootloader(port)
            if erase_only:
                await self._append_log("system", "Starting cc2538-bsl erase")
                erased = await (self._run_mock_eraser if self.mock else self._run_eraser)(port, self._process_line)
                if not erased:
                    raise ZigbeeFirmwareError("cc2538-bsl did not confirm a successful erase")
                self._mark_mock_memory_erased(port)
                await self._append_log("system", "cc2538-bsl erase completed")
            else:
                await self._append_log("system", "Starting cc2538-bsl write and verify")
                verified, verification_seen = await self._flasher(port, Path(manifest["hex"]), self._process_line)
                if not verified or not verification_seen:
                    raise ZigbeeFirmwareError("cc2538-bsl did not confirm a successful flash verification")
                self._mark_mock_memory_flashed(port)
                await self._append_log("system", "cc2538-bsl write and verification completed")
            await self._set_state(
                status="RESTARTING_COORDINATOR",
                stage="finish",
                detail="Releasing bootloader controls and restarting coordinator",
            )
            await self._append_log("system", "Releasing bootloader controls")
            await self._release_controls(port)
            await self._append_log("system", "Resetting coordinator")
            await self._restart_coordinator(port)
            await self._append_log("system", "Waiting for coordinator application startup")
            installed = await self._read_firmware_after_restart(port)
            if installed:
                self._state["installed_firmware"] = installed
                await self._append_log("system", f"Coordinator firmware readback: {installed.get('version', 'unknown')}")
                self._state["detected_ports"] = {
                    **self._state.get("detected_ports", {}),
                    port: {"available": True, "detected": True, "status": "READY", "firmware": installed},
                }
            elif erase_only:
                await self._append_log("system", "Coordinator firmware readback unavailable after erase")
                self._state["installed_firmware"] = None
                self._state["detected_ports"] = {
                    **self._state.get("detected_ports", {}),
                    port: {"available": True, "detected": False, "status": "NO_RESPONSE", "firmware": None},
                }
            else:
                await self._append_log("system", "Coordinator firmware readback unavailable")
            await self._persist_and_publish()
        except asyncio.CancelledError:
            primary_error = "Firmware update cancelled"
        except Exception as exc:
            primary_error = _safe_error(exc)
            LOGGER.exception("Zigbee firmware update failed")
            with contextlib.suppress(Exception):
                await self._append_log("error", primary_error)
        finally:
            if controls_asserted:
                await self._release_controls(port)
            if controls_asserted and primary_error is not None:
                try:
                    await self._restart_coordinator(port)
                    await self._append_log("system", "Coordinator reset after failed ROM bootloader operation")
                except Exception as exc:
                    LOGGER.exception("Unable to recover coordinator after failed firmware operation")
                    await self._append_log("error", f"Coordinator recovery failed: {_safe_error(exc)}")
            if lease is not None:
                lease.release()
            if primary_error is not None:
                await self._set_state(
                    status="FAILED",
                    stage="finish",
                    progress=None,
                    detail=primary_error,
                    error=primary_error,
                    finished_at=_utc_now(),
                )
            else:
                await self._set_state(
                    status="COMPLETED",
                    stage="finish",
                    progress=100,
                    detail=(
                        "Coordinator flash erased and restarted"
                        if erase_only
                        else "Coordinator firmware written, verified, and restarted"
                    ),
                    verification={
                        "erase_verified" if erase_only else "flash_verified": True,
                        "coordinator_restarted": True,
                    },
                    error=None,
                    finished_at=_utc_now(),
                )

    async def _prepare_image(self, release: dict[str, Any]) -> dict[str, Any]:
        await self._set_state(status="DOWNLOADING", stage="download", detail="Downloading approved firmware image", progress=None)
        if release.get("source") == "local":
            local_path = Path(str(release.get("_local_path") or ""))
            if not local_path.is_file():
                raise ZigbeeFirmwareError("The local firmware HEX file is no longer available")
            await asyncio.to_thread(GithubFirmwareSource._validate_hex, local_path, str(release.get("asset_name") or "local HEX"))
            manifest = {
                "profile": FIRMWARE_PROFILE,
                "chip": FIRMWARE_CHIP,
                "firmware_target": FIRMWARE_TARGET,
                "hex": str(local_path),
                "archive_crc_verified": True,
                "digest_verified": False,
                "local": True,
            }
        else:
            manifest = await self.source.prepare_image(release, self.data_dir)
        await self._set_state(status="VERIFYING_IMAGE", stage="download", detail="Verifying archive and Intel HEX", progress=None)
        if manifest.get("profile") != FIRMWARE_PROFILE or manifest.get("chip") != FIRMWARE_CHIP:
            raise ZigbeeFirmwareError("Prepared firmware manifest does not match the coordinator profile")
        if not manifest.get("archive_crc_verified"):
            raise ZigbeeFirmwareError("Firmware archive CRC was not verified")
        return manifest

    async def _check_uart(self, port: str) -> None:
        if self.mock:
            return
        try:
            import serial

            connection = await asyncio.to_thread(
                serial.Serial,
                port=port,
                baudrate=FIRMWARE_BAUDRATE,
                timeout=0,
                exclusive=True,
            )
            await asyncio.to_thread(connection.close)
        except Exception as exc:
            raise ZigbeeFirmwareError(f"UART is not available: {_safe_error(exc)}") from exc

    async def _enter_bootloader(self, port: str) -> None:
        if self.carrier is None:
            if self.mock:
                return
            raise ZigbeeFirmwareError("Carrier MCP23017 is not available")
        profile = PORT_PROFILES[port]
        self.timing.validate()
        await self.carrier.set_output(profile["flash_output"], True)
        try:
            await asyncio.sleep(self.timing.flash_to_reset_seconds)
            await self.carrier.set_output(profile["reset_output"], True)
            try:
                await asyncio.sleep(self.timing.reset_pulse_seconds)
            finally:
                await self.carrier.set_output(profile["reset_output"], False)
            await asyncio.sleep(self.timing.flash_after_reset_seconds)
            await self.carrier.set_output(profile["flash_output"], False)
            await asyncio.sleep(self.timing.bootloader_startup_seconds)
        except Exception:
            await self._release_controls(port)
            raise

    async def _release_controls(self, port: str | None) -> None:
        if not port or port not in PORT_PROFILES or self.carrier is None:
            return
        profile = PORT_PROFILES[port]
        for output_id in (profile["reset_output"], profile["flash_output"]):
            try:
                await self.carrier.set_output(output_id, False)
            except Exception:
                LOGGER.exception("Unable to release Zigbee bootloader control %s", output_id)

    async def _restart_coordinator(self, port: str) -> None:
        if self.carrier is None:
            if self.mock:
                return
            raise ZigbeeFirmwareError("Carrier MCP23017 is not available")
        if not self.mock:
            self.timing.validate()
        reset_output = PORT_PROFILES[port]["reset_output"]
        await self.carrier.set_output(reset_output, True)
        try:
            await asyncio.sleep(self.timing.reset_pulse_seconds)
        finally:
            await self.carrier.set_output(reset_output, False)

    async def _read_firmware_after_restart(self, port: str) -> dict[str, Any] | None:
        """Wait for the application to leave ROM BSL before reading SYS_VERSION."""
        deadline = time.monotonic() + self.timing.coordinator_startup_seconds
        while True:
            try:
                installed = await self.coordinator_reader.read_firmware_version(port)
            except ZigbeeFirmwareError:
                installed = None
                LOGGER.info("Coordinator firmware readback is not ready after reset", exc_info=True)
            if installed:
                return installed
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                LOGGER.info("Coordinator firmware readback was not available after reset")
                return None
            await asyncio.sleep(min(0.25, remaining))

    async def _run_mock_flasher(
        self,
        port: str,
        hex_path: Path,
        on_line: Callable[[str, str], Awaitable[None]],
    ) -> tuple[bool, bool]:
        label = PORT_PROFILES[port]["label"]
        events = (
            ("Firmware image loaded", 0.35),
            (f"Connecting to {label}...", 0.35),
            ("Bootloader connected", 0.35),
            ("Erasing target memory 8%", 0.25),
            ("Erasing target memory 16%", 0.25),
            ("Erasing target memory 24%", 0.25),
            ("Erasing target memory 32%", 0.25),
            ("Writing firmware 40%", 0.25),
            ("Writing sector 48/100 (48%)", 0.25),
            ("Writing sector 56/100 (56%)", 0.25),
            ("Writing sector 64/100 (64%)", 0.25),
            ("Writing sector 72/100 (72%)", 0.25),
            ("Writing sector 80/100 (80%)", 0.25),
            ("Writing sector 88/100 (88%)", 0.25),
            ("Writing sector 96/100 (96%)", 0.25),
            ("Verification started", 0.3),
            ("Verification successful 100%", 0.25),
        )
        for line, delay in events:
            await on_line("mock", line)
            await asyncio.sleep(delay)
        return True, True

    async def _run_mock_eraser(
        self,
        port: str,
        on_line: Callable[[str, str], Awaitable[None]],
    ) -> bool:
        label = PORT_PROFILES[port]["label"]
        events = (
            (f"Connecting to {label}...", 0.35),
            ("Bootloader connected", 0.35),
            ("Erasing target memory 25%", 0.25),
            ("Erasing target memory 50%", 0.25),
            ("Erasing target memory 75%", 0.25),
            ("Erasing target memory 100%", 0.25),
            ("Erase successful", 0.25),
        )
        for line, delay in events:
            await on_line("mock", line)
            await asyncio.sleep(delay)
        return True

    def _mark_mock_memory_erased(self, port: str) -> None:
        marker = getattr(self.coordinator_reader, "mark_erased", None)
        if self.mock and callable(marker):
            marker(port)

    def _mark_mock_memory_flashed(self, port: str) -> None:
        marker = getattr(self.coordinator_reader, "mark_flashed", None)
        if self.mock and callable(marker):
            marker(port)

    async def _run_eraser(
        self,
        port: str,
        on_line: Callable[[str, str], Awaitable[None]],
    ) -> bool:
        command = shlex.split(os.environ.get("ZIGBEE_BSL_COMMAND", "cc2538-bsl"))
        if not command:
            raise ZigbeeFirmwareError("ZIGBEE_BSL_COMMAND is empty")
        command.extend(["-f", "-p", port, "-b", str(FIRMWARE_BAUDRATE), "-e", "/dev/null"])
        await self._append_log("system", f"Starting utility: {shlex.join(command)}")
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise ZigbeeFirmwareError(f"Unable to start cc2538-bsl: {_safe_error(exc)}") from exc

        erase_seen = False

        async def read_stream(stream: asyncio.StreamReader, name: str) -> None:
            nonlocal erase_seen
            pending = ""
            while True:
                raw = await stream.read(4096)
                if not raw:
                    break
                pending += raw.decode("utf-8", errors="replace")
                lines = re.split(r"[\r\n]+", pending)
                pending = lines.pop()
                for line in lines:
                    if re.search(r"\berase\s+(?:done|successful|completed)\b", line, re.IGNORECASE):
                        erase_seen = True
                    await on_line(name, line)
            if pending:
                if re.search(r"\berase\s+(?:done|successful|completed)\b", pending, re.IGNORECASE):
                    erase_seen = True
                await on_line(name, pending)

        readers = [
            asyncio.create_task(read_stream(process.stdout, "stdout")),
            asyncio.create_task(read_stream(process.stderr, "stderr")),
        ]
        try:
            await asyncio.wait_for(process.wait(), timeout=1200)
            await asyncio.gather(*readers)
        except asyncio.CancelledError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()
            raise
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.wait()
            raise ZigbeeFirmwareError("cc2538-bsl erase timed out") from exc
        finally:
            for reader in readers:
                if not reader.done():
                    reader.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
        return process.returncode == 0 and erase_seen

    async def _run_flasher(
        self,
        port: str,
        hex_path: Path,
        on_line: Callable[[str, str], Awaitable[None]],
    ) -> tuple[bool, bool]:
        command = shlex.split(os.environ.get("ZIGBEE_BSL_COMMAND", "cc2538-bsl"))
        if not command:
            raise ZigbeeFirmwareError("ZIGBEE_BSL_COMMAND is empty")
        command.extend(["-f", "-p", port, "-b", str(FIRMWARE_BAUDRATE), "-e", "-w", "-v", str(hex_path)])
        await self._append_log("system", f"Starting utility: {shlex.join(command)}")
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise ZigbeeFirmwareError(f"Unable to start cc2538-bsl: {_safe_error(exc)}") from exc

        verification_seen = False

        async def read_stream(stream: asyncio.StreamReader, name: str) -> None:
            nonlocal verification_seen
            pending = ""
            while True:
                raw = await stream.read(4096)
                if not raw:
                    break
                pending += raw.decode("utf-8", errors="replace")
                lines = re.split(r"[\r\n]+", pending)
                pending = lines.pop()
                for line in lines:
                    lower = line.lower()
                    if (
                        "verify" in lower
                        or "verified" in lower
                        or "verification" in lower
                        or ("crc" in lower and "match" in lower)
                    ):
                        verification_seen = True
                    await on_line(name, line)
            if pending:
                lower = pending.lower()
                if (
                    "verify" in lower
                    or "verified" in lower
                    or "verification" in lower
                    or ("crc" in lower and "match" in lower)
                ):
                    verification_seen = True
                await on_line(name, pending)

        readers = [
            asyncio.create_task(read_stream(process.stdout, "stdout")),
            asyncio.create_task(read_stream(process.stderr, "stderr")),
        ]
        try:
            await asyncio.wait_for(process.wait(), timeout=1200)
            await asyncio.gather(*readers)
        except asyncio.CancelledError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()
            raise
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.wait()
            raise ZigbeeFirmwareError("cc2538-bsl timed out") from exc
        finally:
            for reader in readers:
                if not reader.done():
                    reader.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
        return process.returncode == 0, verification_seen

    async def _process_line(self, stream: str, line: str) -> None:
        lower = line.lower()
        status = self._state.get("status")
        stage = self._state.get("stage")
        if "erase" in lower:
            status, stage, detail = "ERASING", "erase", "Erasing target memory"
        elif "write" in lower or "flash" in lower:
            status, stage, detail = "FLASHING", "flash", "Writing firmware to CC2652P7"
        elif "verify" in lower or "verification" in lower:
            status, stage, detail = "VERIFYING_FLASH", "flash", "Verifying written firmware"
        elif "connect" in lower or "bootloader" in lower:
            status, stage, detail = "WAITING_FOR_BOOTLOADER", "connect", "Waiting for ROM bootloader"
        else:
            detail = self._state.get("detail") or "Running cc2538-bsl"
        progress = self._extract_progress(line)
        if progress is None:
            written = self._extract_flash_write_bytes(line)
            if written is not None and self._flash_progress_total > 0:
                self._flash_progress_written = min(
                    self._flash_progress_total,
                    self._flash_progress_written + written,
                )
                progress = self._flash_progress_from_bytes(
                    self._flash_progress_written,
                    self._flash_progress_total,
                )
        if progress is None and status == "FLASHING" and self._flash_progress_total > 0:
            progress = FLASH_PROGRESS_START
        if progress is None and status == "VERIFYING_FLASH" and self._flash_progress_total > 0:
            progress = FLASH_PROGRESS_END
        if progress is None:
            progress = self._state.get("progress")
        await self._append_log(stream, line)
        await self._set_state(status=status, stage=stage, detail=detail, progress=progress)

    @staticmethod
    def _extract_progress(line: str) -> int | None:
        match = re.search(r"(?<!\d)(\d{1,3})\s*%", line)
        if match is None:
            return None
        value = int(match.group(1))
        return value if 0 <= value <= 100 else None

    @staticmethod
    def _extract_flash_write_bytes(line: str) -> int | None:
        match = re.search(r"\bwrite\s+(\d+)\s+bytes?\s+at\s+0x[0-9a-f]+", line, re.IGNORECASE)
        return int(match.group(1)) if match else None

    @staticmethod
    def _flash_progress_from_bytes(written: int, total: int) -> int | None:
        if total <= 0:
            return None
        ratio = min(1.0, max(0.0, written / total))
        span = FLASH_PROGRESS_END - FLASH_PROGRESS_START
        return FLASH_PROGRESS_START + round(span * ratio)

    @staticmethod
    def _hex_data_byte_count(hex_path: Path) -> int:
        try:
            from intelhex import IntelHex
        except ModuleNotFoundError:
            return len(_read_intel_hex_data(hex_path))

        return len(IntelHex(str(hex_path)).todict())

    def _is_approved_release(self, release: dict[str, Any]) -> bool:
        return (
            release.get("profile") == FIRMWARE_PROFILE
            and release.get("firmware_target") == FIRMWARE_TARGET
            and FIRMWARE_ASSET_RE.fullmatch(str(release.get("asset_name") or "")) is not None
            and str(release.get("asset_url") or "").startswith(FIRMWARE_DOWNLOAD_PREFIX)
        )

    def _load_state(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return empty_update_state()
        except (OSError, json.JSONDecodeError):
            LOGGER.exception("Unable to load Zigbee firmware state")
            return empty_update_state()
        if not isinstance(payload, dict) or payload.get("status") not in UPDATE_STATES:
            return empty_update_state()
        state = empty_update_state()
        state.update(payload)
        if not isinstance(state.get("logs"), list):
            state["logs"] = []
        return state

    async def _set_state(self, **updates: Any) -> None:
        async with self._state_lock:
            self._state.update(updates)
            await asyncio.to_thread(self._write_state_sync, self._state)
            snapshot = self.snapshot()
        await self._publish(snapshot)

    async def _append_log(self, stream: str, line: str) -> None:
        async with self._state_lock:
            self._append_log_local(stream, line)
            await asyncio.to_thread(self._write_state_sync, self._state)
            snapshot = self.snapshot()
        await self._publish(snapshot)

    def _append_log_local(self, stream: str, line: str) -> None:
        text = line.strip()
        if not text:
            return
        logs = self._state.setdefault("logs", [])
        logs.append({"timestamp": datetime.now().astimezone().strftime("%H:%M:%S"), "stream": stream, "line": text})
        del logs[:-MAX_LOG_LINES]

    async def _persist_and_publish(self) -> None:
        async with self._state_lock:
            await asyncio.to_thread(self._write_state_sync, self._state)
            snapshot = self.snapshot()
        await self._publish(snapshot)

    def _write_state_sync(self, state: dict[str, Any]) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        # A fixed state.tmp collides when the Windows mock server is restarted
        # or briefly overlaps with another worker. Use a private temp file and
        # retry the final replace while the previous state file is released.
        temp_path = self.state_path.with_name(f"{self.state_path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temp_path.write_text(json.dumps(state, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
            for attempt in range(6):
                try:
                    temp_path.replace(self.state_path)
                    return
                except PermissionError:
                    if attempt == 5:
                        raise
                    time.sleep(0.02 * (attempt + 1))
        finally:
            with contextlib.suppress(OSError):
                temp_path.unlink()

    async def _publish(self, snapshot: dict[str, Any]) -> None:
        if self.publisher is None:
            return
        result = self.publisher({"type": "zigbee_firmware_changed", "firmware": snapshot})
        if hasattr(result, "__await__"):
            await result

