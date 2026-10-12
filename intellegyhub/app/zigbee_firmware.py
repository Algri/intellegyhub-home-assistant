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
FIRMWARE_TAG_PREFIX = "Z-Stack_3.x.0_coordinator"
FIRMWARE_ASSET_RE = re.compile(r"^CC1352P7_coordinator_(?P<revision>\d+)\.zip$", re.IGNORECASE)
FIRMWARE_HEX_RE = re.compile(r"^CC1352P7_coordinator_(?P<revision>\d+)\.hex$", re.IGNORECASE)
MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_HEX_BYTES = 64 * 1024 * 1024
MAX_LOG_LINES = 2000

UPDATE_STATES = {
    "IDLE",
    "DOWNLOADING",
    "VERIFYING_IMAGE",
    "STOPPING_ZIGBEE",
    "WAITING_FOR_BOOTLOADER",
    "ERASING",
    "FLASHING",
    "VERIFYING_FLASH",
    "RESTARTING_COORDINATOR",
    "STARTING_ZIGBEE",
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


class SupervisorClientProtocol(Protocol):
    async def find_zigbee2mqtt(self) -> dict[str, Any]: ...

    async def stop_addon(self, slug: str) -> None: ...

    async def start_addon(self, slug: str) -> None: ...

    async def wait_for_state(self, slug: str, expected: str, timeout: float) -> dict[str, Any]: ...

    async def wait_for_ready_logs(self, slug: str, timeout: float) -> bool: ...

    async def read_firmware_version(self, slug: str) -> dict[str, Any] | None: ...


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
    flash_to_reset_seconds: float = 1.0
    flash_after_reset_seconds: float = 1.5
    reset_pulse_seconds: float | None = None

    @classmethod
    def from_environment(cls) -> "FirmwareTiming":
        value = os.environ.get("ZIGBEE_RESET_PULSE_SECONDS")
        reset_pulse = None
        if value is not None and value.strip():
            try:
                reset_pulse = float(value)
            except ValueError as exc:
                raise ZigbeeFirmwareError("ZIGBEE_RESET_PULSE_SECONDS must be a number") from exc
        return cls(reset_pulse_seconds=reset_pulse)

    def validate(self) -> None:
        if self.reset_pulse_seconds is None:
            raise ZigbeeFirmwareError(
                "Reset pulse duration is not configured; set ZIGBEE_RESET_PULSE_SECONDS before flashing"
            )
        if not 0.001 <= self.reset_pulse_seconds <= 5:
            raise ZigbeeFirmwareError("Reset pulse duration must be between 0.001 and 5 seconds")


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
        "zigbee2mqtt": None,
        "installed_firmware": None,
        "verification": None,
    }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_error(exc: BaseException) -> str:
    message = str(exc).strip()
    return message or exc.__class__.__name__


def parse_coordinator_firmware_version(log_content: str) -> dict[str, Any] | None:
    """Parse the Z-Stack coordinator version emitted by Zigbee2MQTT."""
    match = re.search(r"coordinator\s+firmware\s+version\s*:\s*(.+)", log_content, re.IGNORECASE)
    if match is None:
        return None
    raw = match.group(1).strip().strip("'").strip('"')
    metadata: dict[str, Any] = {}
    firmware_type: Any = None
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        with contextlib.suppress(json.JSONDecodeError):
            payload = json.loads(raw[start : end + 1])
            if isinstance(payload, dict):
                candidate = payload.get("meta")
                if isinstance(candidate, dict):
                    metadata = candidate
                firmware_type = payload.get("type")
            else:
                firmware_type = None
    else:
        firmware_type = None
    revision = metadata.get("revision")
    if revision is None:
        revision_match = re.search(r"(?<!\d)(20\d{6,})(?!\d)", raw)
        revision = revision_match.group(1) if revision_match else None
    version = str(revision).strip() if revision is not None else raw
    if not version:
        return None
    return {
        "version": version,
        "revision": str(revision).strip() if revision is not None else None,
        "type": str(firmware_type) if firmware_type else None,
        "source": "zigbee2mqtt_logs",
        "raw": raw[:512],
    }


def _release_label(release: dict[str, Any]) -> str:
    return " · ".join(
        str(value)
        for value in (
            release.get("revision") or "unknown",
            release.get("firmware_target") or FIRMWARE_TARGET,
            release.get("published_at") or "date unavailable",
        )
    )


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
            from intelhex import IntelHex

            image = IntelHex(str(path))
            if not image.todict():
                raise ZigbeeFirmwareError("Firmware HEX contains no data")
        except ZigbeeFirmwareError:
            raise
        except Exception as exc:
            raise ZigbeeFirmwareError(f"Firmware HEX validation failed for {revision}: {_safe_error(exc)}") from exc


class SupervisorClient:
    base_url = "http://supervisor"

    def __init__(self, token: str | None = None, request_timeout: float = 10.0) -> None:
        self.token = token or os.environ.get("SUPERVISOR_TOKEN")
        self.request_timeout = request_timeout

    async def find_zigbee2mqtt(self) -> dict[str, Any]:
        configured_slug = os.environ.get("ZIGBEE2MQTT_ADDON_SLUG")
        if configured_slug:
            info = await self.info(configured_slug)
            info.setdefault("slug", configured_slug)
            return info
        payload = await self._request_json("/addons")
        data = payload.get("data") if isinstance(payload, dict) else None
        addons = data.get("addons") if isinstance(data, dict) else None
        if not isinstance(addons, list):
            raise ZigbeeFirmwareError("Supervisor did not return the add-on list")
        candidates = []
        for item in addons:
            if not isinstance(item, dict):
                continue
            slug = str(item.get("slug") or "")
            name = str(item.get("name") or "")
            if "zigbee2mqtt" in slug.lower() or "zigbee2mqtt" in name.lower():
                candidates.append(slug)
        if len(candidates) != 1:
            raise ZigbeeFirmwareError("Unable to identify exactly one Zigbee2MQTT add-on")
        return await self.info(candidates[0])

    async def info(self, slug: str) -> dict[str, Any]:
        payload = await self._request_json(f"/addons/{urllib.parse.quote(slug, safe='')}/info")
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise ZigbeeFirmwareError("Supervisor returned invalid Zigbee2MQTT information")
        return data

    async def stop_addon(self, slug: str) -> None:
        await self._request_json(f"/addons/{urllib.parse.quote(slug, safe='')}/stop", method="POST")

    async def start_addon(self, slug: str) -> None:
        await self._request_json(f"/addons/{urllib.parse.quote(slug, safe='')}/start", method="POST")

    async def wait_for_state(self, slug: str, expected: str, timeout: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            info = await self.info(slug)
            if str(info.get("state") or "").lower() == expected.lower():
                return info
            await asyncio.sleep(1)
        raise ZigbeeFirmwareError(f"Zigbee2MQTT did not reach state {expected!r} within {timeout:g}s")

    async def wait_for_ready_logs(self, slug: str, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        ready_patterns = ("zigbee2mqtt started", "zigbee-herdsman started", "coordinator started")
        while time.monotonic() < deadline:
            try:
                payload = await self._request_json(f"/addons/{urllib.parse.quote(slug, safe='')}/logs")
                data = payload.get("data") if isinstance(payload, dict) else None
                content = data.get("content") if isinstance(data, dict) else ""
                normalized = str(content).lower()
                if any(pattern in normalized for pattern in ready_patterns):
                    return True
            except ZigbeeFirmwareError:
                return False
            await asyncio.sleep(1)
        return False

    async def read_firmware_version(self, slug: str) -> dict[str, Any] | None:
        content = await self._read_logs(slug)
        return parse_coordinator_firmware_version(content)

    async def _read_logs(self, slug: str) -> str:
        payload = await self._request_json(f"/addons/{urllib.parse.quote(slug, safe='')}/logs")
        data = payload.get("data") if isinstance(payload, dict) else None
        content = data.get("content") if isinstance(data, dict) else ""
        return str(content)

    async def _request_json(self, path: str, method: str = "GET") -> Any:
        if not self.token:
            raise ZigbeeFirmwareError("SUPERVISOR_TOKEN is not available")
        return await asyncio.to_thread(self._request_json_sync, path, method)

    def _request_json_sync(self, path: str, method: str) -> Any:
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            method=method,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.request_timeout) as response:
                body = response.read()
                if not body:
                    return {}
                return json.loads(body.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise ZigbeeFirmwareError(f"Supervisor request failed: HTTP {exc.code}") from exc
        except (OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise ZigbeeFirmwareError(f"Supervisor request failed: {_safe_error(exc)}") from exc


class MockSupervisor:
    """In-memory Supervisor facade used only by the local/mock backend."""

    def __init__(self) -> None:
        self.state = "started"

    async def find_zigbee2mqtt(self) -> dict[str, Any]:
        return {"slug": "zigbee2mqtt", "state": self.state}

    async def stop_addon(self, slug: str) -> None:
        self.state = "stopped"

    async def start_addon(self, slug: str) -> None:
        self.state = "started"

    async def wait_for_state(self, slug: str, expected: str, timeout: float) -> dict[str, Any]:
        if self.state != expected:
            raise ZigbeeFirmwareError(f"Mock Zigbee2MQTT did not reach state {expected!r}")
        return {"slug": slug, "state": self.state}

    async def wait_for_ready_logs(self, slug: str, timeout: float) -> bool:
        return self.state == "started"

    async def read_firmware_version(self, slug: str) -> dict[str, Any] | None:
        return None


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
        supervisor: SupervisorClientProtocol | None = None,
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
        self.supervisor = supervisor or (MockSupervisor() if mock else SupervisorClient())
        self.timing = timing or FirmwareTiming.from_environment()
        self._flasher = flasher or (self._run_mock_flasher if mock else self._run_flasher)
        self._state = empty_update_state()
        self._catalog: list[dict[str, Any]] = []
        self._state_lock = asyncio.Lock()
        self._operation_lock = asyncio.Lock()
        self._operation_task: asyncio.Task[None] | None = None
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

    def snapshot(self) -> dict[str, Any]:
        state = json.loads(json.dumps(self._state))
        return {
            **state,
            "profile": FIRMWARE_PROFILE,
            "chip": FIRMWARE_CHIP,
            "firmware_target": FIRMWARE_TARGET,
            "ports": [
                {"path": path, "label": profile["label"]}
                for path, profile in PORT_PROFILES.items()
            ],
            "reset_pulse_configured": self.timing.reset_pulse_seconds is not None,
            "mock": self.mock,
            "started": self._started,
        }

    async def check_releases(self) -> dict[str, Any]:
        releases = await self.source.fetch_catalog()
        self._catalog = [item for item in releases if self._is_approved_release(item)]
        return {"releases": json.loads(json.dumps(self._catalog)), "profile": FIRMWARE_PROFILE}

    async def refresh_installed_firmware(self) -> dict[str, Any]:
        """Read the running coordinator version without opening its UART here."""
        if self.mock or self._state.get("status") in ACTIVE_UPDATE_STATES:
            return self.snapshot()
        reader = getattr(self.supervisor, "read_firmware_version", None)
        if not callable(reader):
            return self.snapshot()
        try:
            session = await self.supervisor.find_zigbee2mqtt()
            slug = str(session.get("slug") or "")
            if str(session.get("state") or "").lower() != "started" or not slug:
                return self.snapshot()
            installed = await reader(slug)
        except ZigbeeFirmwareError:
            LOGGER.info("Coordinator firmware version is not available from Zigbee2MQTT logs", exc_info=True)
            return self.snapshot()
        if installed:
            self._state["installed_firmware"] = installed
            await self._persist_and_publish()
        return self.snapshot()

    async def start_update(self, port: str, revision: str) -> dict[str, Any]:
        async with self._operation_lock:
            return await self._start_update_locked(port, revision)

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
                "port": port,
                "port_label": PORT_PROFILES[port]["label"],
                "release": release,
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

    async def _run_update(self, operation_id: str, port: str, release: dict[str, Any]) -> None:
        lease: SerialLease | None = None
        supervisor_session: dict[str, Any] | None = None
        primary_error: str | None = None
        recovery_error: str | None = None
        controls_asserted = False
        try:
            if operation_id != self._state.get("operation_id"):
                return
            if not self.mock:
                self.timing.validate()
            if self.carrier is None and not self.mock:
                raise ZigbeeFirmwareError("Carrier MCP23017 is not available")
            manifest = await self._prepare_image(release)
            await self._set_state(
                status="STOPPING_ZIGBEE",
                stage="connect",
                detail="Detecting and stopping Zigbee2MQTT",
                target=f"{PORT_PROFILES[port]['label']} · {FIRMWARE_TARGET} · {release['revision']}",
            )
            supervisor_session = await self.supervisor.find_zigbee2mqtt()
            slug = str(supervisor_session.get("slug") or "")
            if not slug:
                raise ZigbeeFirmwareError("Supervisor returned a Zigbee2MQTT add-on without a slug")
            supervisor_state = str(supervisor_session.get("state") or "").lower()
            if supervisor_state not in {"started", "stopped"}:
                raise ZigbeeFirmwareError("Supervisor did not return a known Zigbee2MQTT state")
            was_running = supervisor_state == "started"
            supervisor_session = {"slug": slug, "was_running": was_running, "stopped": False}
            self._state["zigbee2mqtt"] = supervisor_session
            await self._persist_and_publish()
            if was_running:
                await self.supervisor.stop_addon(slug)
                await self.supervisor.wait_for_state(slug, "stopped", timeout=60)
                supervisor_session["stopped"] = True
                await self._persist_and_publish()

            if not self.mock:
                lease = SerialLease(port, self.data_dir / "locks" / (Path(port).name + ".lock"))
                lease.acquire()
                await self._check_uart(port)
            await self._set_state(status="WAITING_FOR_BOOTLOADER", stage="connect", detail="Entering ROM bootloader")
            await self._enter_bootloader(port)
            controls_asserted = True
            verified, verification_seen = await self._flasher(port, Path(manifest["hex"]), self._process_line)
            if not verified or not verification_seen:
                raise ZigbeeFirmwareError("cc2538-bsl did not confirm a successful flash verification")
            await self._set_state(
                status="RESTARTING_COORDINATOR",
                stage="finish",
                detail="Releasing bootloader controls and restarting coordinator",
            )
            await self._release_controls(port)
            await self._restart_coordinator(port)
        except asyncio.CancelledError:
            primary_error = "Firmware update cancelled"
        except Exception as exc:
            primary_error = _safe_error(exc)
            LOGGER.exception("Zigbee firmware update failed")
        finally:
            if controls_asserted or primary_error is not None:
                await self._release_controls(port)
            if lease is not None:
                lease.release()
            if primary_error is None and supervisor_session and supervisor_session.get("was_running"):
                try:
                    slug = str(supervisor_session["slug"])
                    await self._set_state(status="STARTING_ZIGBEE", stage="finish", detail="Starting Zigbee2MQTT")
                    await self.supervisor.start_addon(slug)
                    await self.supervisor.wait_for_state(slug, "started", timeout=90)
                    if not await self.supervisor.wait_for_ready_logs(slug, timeout=90):
                        raise ZigbeeFirmwareError("Zigbee2MQTT started but coordinator reconnect was not verified")
                    reader = getattr(self.supervisor, "read_firmware_version", None)
                    if not self.mock and callable(reader):
                        try:
                            installed = await reader(slug)
                        except ZigbeeFirmwareError:
                            installed = None
                            LOGGER.info("Coordinator firmware version was not present in startup logs", exc_info=True)
                        if installed:
                            self._state["installed_firmware"] = installed
                    self._state["verification"] = {"zigbee2mqtt_ready": True, "flash_verified": True}
                except Exception as exc:
                    recovery_error = _safe_error(exc)
            if primary_error is not None or recovery_error is not None:
                details = primary_error or "Firmware was written, but Zigbee2MQTT recovery failed"
                if recovery_error:
                    details = f"{details}; Zigbee2MQTT recovery: {recovery_error}"
                await self._set_state(
                    status="FAILED",
                    stage="finish",
                    progress=None,
                    detail=details,
                    error=details,
                    finished_at=_utc_now(),
                )
            elif supervisor_session and supervisor_session.get("was_running"):
                await self._set_state(
                    status="COMPLETED",
                    stage="finish",
                    progress=100,
                    detail="Firmware written, verified, and Zigbee2MQTT restored",
                    error=None,
                    finished_at=_utc_now(),
                )
            elif primary_error is None:
                await self._set_state(
                    status="COMPLETED",
                    stage="finish",
                    progress=100,
                    detail="Firmware written and verified; Zigbee2MQTT was not running before the update",
                    verification={"flash_verified": True, "zigbee2mqtt_ready": False},
                    error=None,
                    finished_at=_utc_now(),
                )

    async def _prepare_image(self, release: dict[str, Any]) -> dict[str, Any]:
        await self._set_state(status="DOWNLOADING", stage="download", detail="Downloading approved firmware image", progress=None)
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
        await self.carrier.set_output(profile["flash_output"], True)
        try:
            await asyncio.sleep(0.01 if self.mock else self.timing.flash_to_reset_seconds)
            await self.carrier.set_output(profile["reset_output"], True)
            try:
                await asyncio.sleep(0.01 if self.mock else self.timing.reset_pulse_seconds or 0)
            finally:
                await self.carrier.set_output(profile["reset_output"], False)
            await asyncio.sleep(0.01 if self.mock else self.timing.flash_after_reset_seconds)
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
            await asyncio.sleep(0.01 if self.mock else self.timing.reset_pulse_seconds or 0)
        finally:
            await self.carrier.set_output(reset_output, False)

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

    async def _run_flasher(
        self,
        port: str,
        hex_path: Path,
        on_line: Callable[[str, str], Awaitable[None]],
    ) -> tuple[bool, bool]:
        command = shlex.split(os.environ.get("ZIGBEE_BSL_COMMAND", "cc2538-bsl"))
        if not command:
            raise ZigbeeFirmwareError("ZIGBEE_BSL_COMMAND is empty")
        command.extend(["-p", port, "-b", str(FIRMWARE_BAUDRATE), "-e", "-w", "-v", str(hex_path)])
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
            while True:
                raw = await stream.readline()
                if not raw:
                    return
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                lower = line.lower()
                if "verify" in lower or "verification" in lower or "crc" in lower and "match" in lower:
                    verification_seen = True
                await on_line(name, line)

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
        temp_path = self.state_path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(state, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
        temp_path.replace(self.state_path)

    async def _publish(self, snapshot: dict[str, Any]) -> None:
        if self.publisher is None:
            return
        result = self.publisher({"type": "zigbee_firmware_changed", "firmware": snapshot})
        if hasattr(result, "__await__"):
            await result

