from __future__ import annotations

import asyncio
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable


DEFAULT_STE_HEARTBEAT_ON_SECONDS = 0.2
DEFAULT_STE_HEARTBEAT_OFF_SECONDS = 1.8
DEFAULT_WEBSOCKET_CONNECTION_GRACE_SECONDS = 30.0


@dataclass
class DiagnosticIndicatorSettings:
    ste_heartbeat_enabled: bool = False
    ste_heartbeat_on_seconds: float = DEFAULT_STE_HEARTBEAT_ON_SECONDS
    ste_heartbeat_off_seconds: float = DEFAULT_STE_HEARTBEAT_OFF_SECONDS
    net_indicator_enabled: bool = False
    err_indicator_enabled: bool = False
    websocket_connection_grace_seconds: float = DEFAULT_WEBSOCKET_CONNECTION_GRACE_SECONDS

    def snapshot(self) -> dict:
        return {
            "ste": {
                "heartbeat_enabled": self.ste_heartbeat_enabled,
                "heartbeat_on_seconds": self.ste_heartbeat_on_seconds,
                "heartbeat_off_seconds": self.ste_heartbeat_off_seconds,
            },
            "net": {"indicator_enabled": self.net_indicator_enabled},
            "err": {"indicator_enabled": self.err_indicator_enabled},
        }


class DiagnosticIndicatorStore:
    def __init__(self, path: Path | None = None, defaults: DiagnosticIndicatorSettings | None = None) -> None:
        self.path = path or _default_db_path()
        self.defaults = defaults or DiagnosticIndicatorSettings()

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    async def load(self) -> DiagnosticIndicatorSettings:
        return await asyncio.to_thread(self._load_sync)

    async def save(self, settings: DiagnosticIndicatorSettings) -> None:
        await asyncio.to_thread(self._save_sync, settings)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(self.path)

    def _initialize_sync(self) -> None:
        with self._connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS diagnostic_indicator_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )

    def _load_sync(self) -> DiagnosticIndicatorSettings:
        with self._connect() as db:
            rows = dict(db.execute("SELECT key,value FROM diagnostic_indicator_settings").fetchall())
        settings = DiagnosticIndicatorSettings(
            ste_heartbeat_enabled=rows.get("ste_heartbeat_enabled", "0") == "1",
            ste_heartbeat_on_seconds=_float(rows.get("ste_heartbeat_on_seconds"), self.defaults.ste_heartbeat_on_seconds),
            ste_heartbeat_off_seconds=_float(rows.get("ste_heartbeat_off_seconds"), self.defaults.ste_heartbeat_off_seconds),
            net_indicator_enabled=rows.get("net_indicator_enabled", "0") == "1",
            err_indicator_enabled=rows.get("err_indicator_enabled", "0") == "1",
            websocket_connection_grace_seconds=self.defaults.websocket_connection_grace_seconds,
        )
        _validate_heartbeat(settings.ste_heartbeat_on_seconds, settings.ste_heartbeat_off_seconds)
        _validate_websocket_grace(settings.websocket_connection_grace_seconds)
        return settings

    def _save_sync(self, settings: DiagnosticIndicatorSettings) -> None:
        _validate_heartbeat(settings.ste_heartbeat_on_seconds, settings.ste_heartbeat_off_seconds)
        values = {
            "ste_heartbeat_enabled": "1" if settings.ste_heartbeat_enabled else "0",
            "ste_heartbeat_on_seconds": str(settings.ste_heartbeat_on_seconds),
            "ste_heartbeat_off_seconds": str(settings.ste_heartbeat_off_seconds),
            "net_indicator_enabled": "1" if settings.net_indicator_enabled else "0",
            "err_indicator_enabled": "1" if settings.err_indicator_enabled else "0",
        }
        with self._connect() as db:
            db.executemany(
                "INSERT INTO diagnostic_indicator_settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                values.items(),
            )


class DiagnosticIndicatorManager:
    def __init__(
        self,
        set_output: Callable[[str, bool], Awaitable[bool]],
        store: DiagnosticIndicatorStore | None = None,
        default_settings: DiagnosticIndicatorSettings | None = None,
    ) -> None:
        self.store = store or DiagnosticIndicatorStore(defaults=default_settings)
        self.settings = default_settings or DiagnosticIndicatorSettings()
        self._set_output = set_output
        self._publisher: Callable[[dict], Awaitable[None]] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._net_blink_task: asyncio.Task[None] | None = None
        self._err_blink_task: asyncio.Task[None] | None = None
        self._connection_grace_task: asyncio.Task[None] | None = None
        self._connected_clients = 0
        self._disconnected_since: float | None = None

    def set_publisher(self, publisher: Callable[[dict], Awaitable[None]]) -> None:
        self._publisher = publisher

    async def start(self) -> None:
        await self.store.initialize()
        self.settings = await self.store.load()
        self._begin_connection_grace()
        await self._apply_ste()
        await self._apply_net()
        await self._apply_err()

    async def stop(self) -> None:
        await self._stop_connection_grace()
        await self._stop_heartbeat()
        await self._stop_net_blink()
        await self._stop_err_blink()
        await self._set_output("ste", False)
        await self._set_output("net", False)
        await self._set_output("err", False)

    def snapshot(self) -> dict:
        payload = self.settings.snapshot()
        payload["net"]["connected_clients"] = self._connected_clients
        payload["net"]["websocket_status"] = self._websocket_status()
        payload["net"]["connection_grace_seconds"] = self.settings.websocket_connection_grace_seconds
        return payload

    async def set_ste_heartbeat(
        self,
        enabled: bool | None = None,
        on_seconds: float | None = None,
        off_seconds: float | None = None,
    ) -> dict:
        settings = DiagnosticIndicatorSettings(
            ste_heartbeat_enabled=self.settings.ste_heartbeat_enabled if enabled is None else bool(enabled),
            ste_heartbeat_on_seconds=self.settings.ste_heartbeat_on_seconds if on_seconds is None else float(on_seconds),
            ste_heartbeat_off_seconds=self.settings.ste_heartbeat_off_seconds if off_seconds is None else float(off_seconds),
            net_indicator_enabled=self.settings.net_indicator_enabled,
            err_indicator_enabled=self.settings.err_indicator_enabled,
            websocket_connection_grace_seconds=self.settings.websocket_connection_grace_seconds,
        )
        _validate_heartbeat(settings.ste_heartbeat_on_seconds, settings.ste_heartbeat_off_seconds)
        self.settings = settings
        await self.store.save(self.settings)
        await self._apply_ste()
        await self._publish()
        return self.snapshot()

    async def set_net_indicator(self, enabled: bool) -> dict:
        self.settings.net_indicator_enabled = bool(enabled)
        await self.store.save(self.settings)
        await self._apply_net()
        await self._publish()
        return self.snapshot()

    async def set_err_indicator(self, enabled: bool) -> dict:
        self.settings.err_indicator_enabled = bool(enabled)
        await self.store.save(self.settings)
        await self._apply_err()
        await self._publish()
        return self.snapshot()

    async def set_connected_clients(self, count: int) -> None:
        previous = self._connected_clients
        self._connected_clients = max(0, int(count))
        if self._connected_clients > 0:
            self._disconnected_since = None
            await self._stop_connection_grace()
        elif previous > 0 or self._disconnected_since is None:
            self._begin_connection_grace()
        await self._apply_ste()
        await self._apply_net()
        await self._apply_err()

    async def _apply_ste(self) -> None:
        await self._stop_heartbeat()
        if not self.settings.ste_heartbeat_enabled or self._websocket_status() != "connected":
            await self._set_output("ste", False)
            return
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(), name="intellegyhub-ste-heartbeat")

    async def _apply_net(self) -> None:
        await self._stop_net_blink()
        if not self.settings.net_indicator_enabled:
            await self._set_output("net", False)
            return
        status = self._websocket_status()
        if status == "connected":
            await self._set_output("net", True)
            return
        if status == "connecting":
            self._net_blink_task = asyncio.create_task(self._net_blink_loop(), name="intellegyhub-net-connecting")
            return
        await self._set_output("net", False)

    async def _apply_err(self) -> None:
        await self._stop_err_blink()
        if not self.settings.err_indicator_enabled or self._websocket_status() != "error":
            await self._set_output("err", False)
            return
        self._err_blink_task = asyncio.create_task(self._err_blink_loop(), name="intellegyhub-err-indicator")

    async def _heartbeat_loop(self) -> None:
        try:
            while True:
                await self._set_output("ste", True)
                await asyncio.sleep(self.settings.ste_heartbeat_on_seconds)
                await self._set_output("ste", False)
                await asyncio.sleep(self.settings.ste_heartbeat_off_seconds)
        except asyncio.CancelledError:
            raise

    async def _net_blink_loop(self) -> None:
        try:
            while True:
                await self._set_output("net", True)
                await asyncio.sleep(0.35)
                await self._set_output("net", False)
                await asyncio.sleep(0.65)
        except asyncio.CancelledError:
            raise

    async def _err_blink_loop(self) -> None:
        try:
            while True:
                await self._set_output("err", True)
                await asyncio.sleep(0.2)
                await self._set_output("err", False)
                await asyncio.sleep(0.8)
        except asyncio.CancelledError:
            raise

    async def _stop_heartbeat(self) -> None:
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None

    async def _stop_net_blink(self) -> None:
        if self._net_blink_task:
            self._net_blink_task.cancel()
            try:
                await self._net_blink_task
            except asyncio.CancelledError:
                pass
            self._net_blink_task = None

    async def _stop_err_blink(self) -> None:
        if self._err_blink_task:
            self._err_blink_task.cancel()
            try:
                await self._err_blink_task
            except asyncio.CancelledError:
                pass
            self._err_blink_task = None

    def _begin_connection_grace(self) -> None:
        self._disconnected_since = time.monotonic()
        if self._connection_grace_task:
            self._connection_grace_task.cancel()
        self._connection_grace_task = asyncio.create_task(
            self._connection_grace_loop(), name="intellegyhub-websocket-grace"
        )

    async def _connection_grace_loop(self) -> None:
        try:
            await asyncio.sleep(self.settings.websocket_connection_grace_seconds)
            if self._connected_clients <= 0:
                await self._apply_ste()
                await self._apply_net()
                await self._apply_err()
        except asyncio.CancelledError:
            raise

    async def _stop_connection_grace(self) -> None:
        if self._connection_grace_task:
            self._connection_grace_task.cancel()
            try:
                await self._connection_grace_task
            except asyncio.CancelledError:
                pass
            self._connection_grace_task = None

    def _websocket_status(self) -> str:
        if self._connected_clients > 0:
            return "connected"
        if self._disconnected_since is None:
            return "connecting"
        elapsed = time.monotonic() - self._disconnected_since
        if elapsed < self.settings.websocket_connection_grace_seconds:
            return "connecting"
        return "error"

    async def _publish(self) -> None:
        if self._publisher:
            await self._publisher({"type": "diagnostic_indicators_changed", "diagnostic_indicators": self.snapshot()})


def _validate_heartbeat(on_seconds: float, off_seconds: float) -> None:
    if not 0.05 <= on_seconds <= 10:
        raise ValueError("ste heartbeat on time must be between 0.05 and 10 seconds")
    if not 0.05 <= off_seconds <= 60:
        raise ValueError("ste heartbeat off time must be between 0.05 and 60 seconds")


def _validate_websocket_grace(seconds: float) -> None:
    if not 0.1 <= seconds <= 3600:
        raise ValueError("websocket connection grace time must be between 0.1 and 3600 seconds")


def _float(value: str | None, default: float) -> float:
    try:
        return float(value) if value is not None else default
    except ValueError:
        return default


def _default_db_path() -> Path:
    if Path("/data").exists():
        return Path("/data/intellegyhub.sqlite3")
    return Path(".data/intellegyhub.sqlite3")
