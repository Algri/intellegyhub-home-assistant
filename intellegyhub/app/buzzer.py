from __future__ import annotations

import asyncio
import contextlib
import io
import os
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PIGPIO_HOST = "127.0.0.1"
PIGPIO_PORT = 8888
MIN_BUZZER_FREQUENCY_HZ = 300
MAX_BUZZER_FREQUENCY_HZ = 2800
MIN_BUZZER_DURATION_MS = 10
MAX_BUZZER_DURATION_MS = 1000
MAX_PHYSICAL_DUTY = 0.22
DEFAULT_FREQUENCY = 2000
DEFAULT_DURATION_MS = 300
DEFAULT_VOLUME_PERCENT = 50


def volume_percent_to_duty(volume_percent: float) -> float:
    volume = max(0.0, min(100.0, float(volume_percent)))
    if volume <= 0:
        return 0.0
    return MAX_PHYSICAL_DUTY * (volume / 100.0)


@dataclass
class BuzzerSettings:
    frequency: int = DEFAULT_FREQUENCY
    duration_ms: int = DEFAULT_DURATION_MS
    volume_percent: int = DEFAULT_VOLUME_PERCENT
    last_volume_percent: int = DEFAULT_VOLUME_PERCENT


class BuzzerSettingsStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or _default_store_path()
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    async def load(self) -> BuzzerSettings:
        return await asyncio.to_thread(self._load_sync)

    async def save(self, settings: BuzzerSettings) -> None:
        async with self._lock:
            await asyncio.to_thread(self._save_sync, settings)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(self.path)

    def _initialize_sync(self) -> None:
        with self._connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS buzzer_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )

    def _load_sync(self) -> BuzzerSettings:
        with self._connect() as db:
            rows = dict(db.execute("SELECT key,value FROM buzzer_settings").fetchall())

        settings = BuzzerSettings(
            frequency=_int_from_store(rows.get("frequency"), DEFAULT_FREQUENCY),
            duration_ms=_int_from_store(rows.get("duration_ms"), DEFAULT_DURATION_MS),
            volume_percent=_int_from_store(rows.get("volume_percent"), DEFAULT_VOLUME_PERCENT),
            last_volume_percent=_int_from_store(rows.get("last_volume_percent"), DEFAULT_VOLUME_PERCENT),
        )
        try:
            BuzzerManager._validate(settings.frequency, settings.duration_ms, settings.volume_percent)
            BuzzerManager._validate_volume(settings.last_volume_percent)
        except ValueError:
            return BuzzerSettings()
        if settings.volume_percent > 0:
            settings.last_volume_percent = settings.volume_percent
        elif settings.last_volume_percent <= 0:
            settings.last_volume_percent = DEFAULT_VOLUME_PERCENT
        return settings

    def _save_sync(self, settings: BuzzerSettings) -> None:
        BuzzerManager._validate(settings.frequency, settings.duration_ms, settings.volume_percent)
        BuzzerManager._validate_volume(settings.last_volume_percent)
        rows = {
            "frequency": str(settings.frequency),
            "duration_ms": str(settings.duration_ms),
            "volume_percent": str(settings.volume_percent),
            "last_volume_percent": str(settings.last_volume_percent),
        }
        with self._connect() as db:
            db.executemany(
                "INSERT INTO buzzer_settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                rows.items(),
            )


class BuzzerManager:
    def __init__(
        self,
        pwmchip: Path | None = None,
        pwm_channel: int = 0,
        gpiochip: str = "/dev/gpiochip0",
        gpio_line: int = 18,
    ) -> None:
        self.pwmchip = pwmchip or Path("/sys/class/pwm/pwmchip0")
        self.pwm_channel = pwm_channel
        self.gpiochip = gpiochip
        self.gpio_line = gpio_line
        self._lock = asyncio.Lock()
        self._active_task: asyncio.Task | None = None
        self._active_backend: str | None = None
        self.frequency = DEFAULT_FREQUENCY
        self.duration_ms = DEFAULT_DURATION_MS
        self.volume_percent = DEFAULT_VOLUME_PERCENT
        self.last_volume_percent = DEFAULT_VOLUME_PERCENT
        self._pigpio_connected: bool | None = None
        self._pigpio_error: str | None = None

    def status(self) -> dict[str, Any]:
        pwm_path = self._pwm_path()
        export_path = self.pwmchip / "export"
        pigpio_status = self._pigpio_status()
        mock = _is_mock_platform()
        return {
            "pigpio": pigpio_status,
            "pwm": {
                "chip": str(self.pwmchip),
                "channel": self.pwm_channel,
                "available": mock or self.pwmchip.exists(),
                "exported": mock or pwm_path.exists(),
                "npwm": self._read_text(self.pwmchip / "npwm"),
                "export_writable": mock or os.access(export_path, os.W_OK),
            },
            "gpio": {
                "chip": self.gpiochip,
                "line": self.gpio_line,
                "available": mock or Path(self.gpiochip).exists(),
            },
            "active": {
                "running": self._active_task is not None,
                "backend": self._active_backend,
            },
            "frequency": self.frequency,
            "duration_ms": self.duration_ms,
            "volume_percent": self.volume_percent,
            "last_volume_percent": self.last_volume_percent,
            "min_frequency_hz": MIN_BUZZER_FREQUENCY_HZ,
            "max_frequency_hz": MAX_BUZZER_FREQUENCY_HZ,
            "min_duration_ms": MIN_BUZZER_DURATION_MS,
            "max_duration_ms": MAX_BUZZER_DURATION_MS,
            "max_physical_duty": MAX_PHYSICAL_DUTY,
        }

    def apply_settings(self, settings: BuzzerSettings) -> None:
        self.frequency = int(settings.frequency)
        self.duration_ms = int(settings.duration_ms)
        self.volume_percent = int(settings.volume_percent)
        self.last_volume_percent = int(settings.last_volume_percent)

    def settings(self) -> BuzzerSettings:
        return BuzzerSettings(
            frequency=self.frequency,
            duration_ms=self.duration_ms,
            volume_percent=self.volume_percent,
            last_volume_percent=self.last_volume_percent,
        )

    async def stop(self) -> None:
        async with self._lock:
            await self._stop_locked()

    async def set_volume_percent(self, volume_percent: int) -> dict[str, Any]:
        return await self.set_settings(volume_percent=volume_percent)

    async def set_settings(
        self,
        frequency: int | None = None,
        duration_ms: int | None = None,
        volume_percent: int | None = None,
    ) -> dict[str, Any]:
        next_frequency = self.frequency if frequency is None else int(frequency)
        next_duration = self.duration_ms if duration_ms is None else int(duration_ms)
        next_volume = self.volume_percent if volume_percent is None else int(volume_percent)
        self._validate(next_frequency, next_duration, next_volume)
        self.frequency = next_frequency
        self.duration_ms = next_duration
        self.volume_percent = next_volume
        if next_volume > 0:
            self.last_volume_percent = next_volume
        return self.status()

    async def test_configured_pwm(self) -> dict[str, Any]:
        return await self.play_pwm(self.frequency, self.duration_ms, volume_percent=self.volume_percent)

    async def play_pwm(self, frequency: int, duration_ms: int, duty: float | None = None, volume_percent: int | None = None) -> dict[str, Any]:
        volume = self._resolve_volume(duty, volume_percent)
        self._validate(frequency, duration_ms, volume)
        physical_duty = volume_percent_to_duty(volume)
        async with self._lock:
            await self._stop_locked()
            backend = "disabled"
            if physical_duty > 0:
                backend = "mock_pwm" if _is_mock_platform() else await asyncio.to_thread(
                    self._start_hardware_pwm,
                    frequency,
                    physical_duty,
                )
                self._active_backend = backend
                self._active_task = asyncio.create_task(self._auto_stop_after(duration_ms))
            self.frequency = int(frequency)
            self.duration_ms = int(duration_ms)
            self.volume_percent = int(volume)
            if volume > 0:
                self.last_volume_percent = int(volume)
            return {
                "status": "ok",
                "backend": backend,
                "duration_ms": duration_ms,
                "volume_percent": self.volume_percent,
                "physical_duty": physical_duty,
                **self.status(),
            }

    async def play_pwm_once(self, frequency: int, duration_ms: int, duty: float | None = None, volume_percent: int | None = None) -> dict[str, Any]:
        volume = self._resolve_volume(duty, volume_percent)
        self._validate(frequency, duration_ms, volume)
        physical_duty = volume_percent_to_duty(volume)
        async with self._lock:
            await self._stop_locked()
            backend = "disabled"
            try:
                if physical_duty > 0:
                    if _is_mock_platform():
                        await asyncio.sleep(duration_ms / 1000)
                        backend = "mock_pwm"
                    else:
                        backend = await asyncio.to_thread(
                            self._run_hardware_pwm_once,
                            frequency,
                            duration_ms,
                            physical_duty,
                        )
                    self._active_backend = backend
            finally:
                await asyncio.to_thread(self._disable_pwm)
                self._active_backend = None
            self.frequency = int(frequency)
            self.duration_ms = int(duration_ms)
            self.volume_percent = int(volume)
            if volume > 0:
                self.last_volume_percent = int(volume)
        return {
            "status": "ok",
            "backend": backend,
            "duration_ms": duration_ms,
            "volume_percent": self.volume_percent,
            "physical_duty": physical_duty,
            **self.status(),
        }

    async def test_pwm(self, frequency: int, duration_ms: int, duty: float | None = None, volume_percent: int | None = None) -> dict[str, Any]:
        return await self.play_pwm(frequency, duration_ms, duty, volume_percent)

    async def test_gpio(self, frequency: int, duration_ms: int, duty: float | None = None, volume_percent: int | None = None) -> dict[str, Any]:
        volume = self._resolve_volume(duty, volume_percent)
        self._validate(frequency, duration_ms, volume)
        physical_duty = volume_percent_to_duty(volume)
        async with self._lock:
            await self._stop_locked()
            if physical_duty > 0:
                await asyncio.to_thread(self._run_gpio_test, frequency, duration_ms, physical_duty)
            self.frequency = int(frequency)
            self.duration_ms = int(duration_ms)
            self.volume_percent = int(volume)
            if volume > 0:
                self.last_volume_percent = int(volume)
            return {
                "status": "ok",
                "backend": "software_gpio",
                "volume_percent": self.volume_percent,
                "physical_duty": physical_duty,
                **self.status(),
            }

    async def _stop_locked(self) -> None:
        if self._active_task:
            self._active_task.cancel()
            try:
                await self._active_task
            except asyncio.CancelledError:
                pass
            self._active_task = None
        await asyncio.to_thread(self._disable_pwm)
        await asyncio.to_thread(self._disable_pigpio_pwm)
        self._active_backend = None

    async def _auto_stop_after(self, duration_ms: int) -> None:
        try:
            await asyncio.sleep(duration_ms / 1000)
            async with self._lock:
                current = asyncio.current_task()
                if self._active_task is current:
                    self._active_task = None
                    await asyncio.to_thread(self._disable_pigpio_pwm)
                    await asyncio.to_thread(self._disable_pwm)
                    self._active_backend = None
        except asyncio.CancelledError:
            raise

    def _start_hardware_pwm(self, frequency: int, duty: float) -> str:
        try:
            self._start_pigpio_pwm(frequency, duty)
            return "pigpio_hardware_pwm"
        except Exception as pigpio_exc:
            try:
                self._start_sysfs_pwm(frequency, duty)
                return "sysfs_pwm"
            except Exception as sysfs_exc:
                raise RuntimeError(f"pigpio failed: {pigpio_exc}; sysfs pwm failed: {sysfs_exc}") from sysfs_exc

    def _run_hardware_pwm_once(self, frequency: int, duration_ms: int, duty: float) -> str:
        try:
            self._run_pigpio_pwm_once(frequency, duration_ms, duty)
            return "pigpio_hardware_pwm"
        except Exception as pigpio_exc:
            try:
                self._run_pwm_test(frequency, duration_ms, duty)
                return "sysfs_pwm"
            except Exception as sysfs_exc:
                raise RuntimeError(f"pigpio failed: {pigpio_exc}; sysfs pwm failed: {sysfs_exc}") from sysfs_exc

    def _start_pigpio_pwm(self, frequency: int, duty: float) -> None:
        pigpio = self._import_pigpio()
        with contextlib.redirect_stderr(io.StringIO()):
            pi = pigpio.pi(PIGPIO_HOST, PIGPIO_PORT)
        try:
            if not pi.connected:
                self._pigpio_connected = False
                self._pigpio_error = "pigpio daemon is not connected"
                raise RuntimeError("pigpio daemon is not connected")
            self._pigpio_connected = True
            self._pigpio_error = None
            duty_micros = round(duty * 1_000_000)
            result = pi.hardware_PWM(self.gpio_line, frequency, duty_micros)
            if result < 0:
                raise RuntimeError(f"pigpio hardware_PWM failed with code {result}")
        finally:
            try:
                pi.stop()
            except Exception:
                pass

    def _run_pigpio_pwm_once(self, frequency: int, duration_ms: int, duty: float) -> None:
        pigpio = self._import_pigpio()
        with contextlib.redirect_stderr(io.StringIO()):
            pi = pigpio.pi(PIGPIO_HOST, PIGPIO_PORT)
        try:
            if not pi.connected:
                self._pigpio_connected = False
                self._pigpio_error = "pigpio daemon is not connected"
                raise RuntimeError("pigpio daemon is not connected")
            self._pigpio_connected = True
            self._pigpio_error = None
            duty_micros = round(duty * 1_000_000)
            result = pi.hardware_PWM(self.gpio_line, frequency, duty_micros)
            if result < 0:
                raise RuntimeError(f"pigpio hardware_PWM failed with code {result}")
            time.sleep(duration_ms / 1000)
        finally:
            try:
                if pi.connected:
                    pi.hardware_PWM(self.gpio_line, 0, 0)
            finally:
                try:
                    pi.stop()
                except Exception:
                    pass

    def _run_pwm_test(self, frequency: int, duration_ms: int, duty: float) -> None:
        if not self.pwmchip.exists():
            raise RuntimeError(f"{self.pwmchip} does not exist")
        self._start_sysfs_pwm(frequency, duty)
        time.sleep(duration_ms / 1000)
        self._write_text(self._pwm_path() / "enable", "0")

    def _start_sysfs_pwm(self, frequency: int, duty: float) -> None:
        if not self.pwmchip.exists():
            raise RuntimeError(f"{self.pwmchip} does not exist")
        self._export_pwm()
        pwm_path = self._pwm_path()
        period_ns = round(1_000_000_000 / frequency)
        duty_ns = round(period_ns * duty)
        self._write_text(pwm_path / "enable", "0")
        self._write_text(pwm_path / "period", str(period_ns))
        self._write_text(pwm_path / "duty_cycle", str(duty_ns))
        self._write_text(pwm_path / "enable", "1")

    def _run_gpio_test(self, frequency: int, duration_ms: int, duty: float) -> None:
        if sys.platform == "win32":
            time.sleep(duration_ms / 1000)
            return
        import gpiod

        period = 1 / frequency
        high_time = period * duty
        low_time = period - high_time
        end_at = time.monotonic() + duration_ms / 1000
        with gpiod.Chip(self.gpiochip) as chip:
            request = chip.request_lines(
                consumer="intellegyhub-buzzer-test",
                config={
                    self.gpio_line: gpiod.LineSettings(
                        direction=gpiod.line.Direction.OUTPUT,
                        output_value=gpiod.line.Value.INACTIVE,
                    )
                },
            )
            try:
                while time.monotonic() < end_at:
                    request.set_value(self.gpio_line, gpiod.line.Value.ACTIVE)
                    time.sleep(high_time)
                    request.set_value(self.gpio_line, gpiod.line.Value.INACTIVE)
                    time.sleep(low_time)
            finally:
                request.set_value(self.gpio_line, gpiod.line.Value.INACTIVE)
                request.release()

    def _export_pwm(self) -> None:
        pwm_path = self._pwm_path()
        if pwm_path.exists():
            return
        self._write_text(self.pwmchip / "export", str(self.pwm_channel))

    def _disable_pwm(self) -> None:
        pwm_path = self._pwm_path()
        if pwm_path.exists():
            try:
                self._write_text(pwm_path / "enable", "0")
            except OSError:
                pass

    def _disable_pigpio_pwm(self) -> None:
        try:
            pigpio = self._import_pigpio()
            with contextlib.redirect_stderr(io.StringIO()):
                pi = pigpio.pi(PIGPIO_HOST, PIGPIO_PORT)
            try:
                if pi.connected:
                    self._pigpio_connected = True
                    self._pigpio_error = None
                    pi.hardware_PWM(self.gpio_line, 0, 0)
                else:
                    self._pigpio_connected = False
                    self._pigpio_error = "pigpio daemon is not connected"
            finally:
                pi.stop()
        except Exception:
            pass

    def _pwm_path(self) -> Path:
        return self.pwmchip / f"pwm{self.pwm_channel}"

    @staticmethod
    def _resolve_volume(duty: float | None, volume_percent: int | None) -> int:
        if volume_percent is not None:
            return int(volume_percent)
        if duty is not None:
            return round(float(duty) * 100)
        return 50

    @staticmethod
    def _validate(frequency: int, duration_ms: int, volume_percent: int) -> None:
        if frequency < MIN_BUZZER_FREQUENCY_HZ or frequency > MAX_BUZZER_FREQUENCY_HZ:
            raise ValueError(
                f"frequency must be between {MIN_BUZZER_FREQUENCY_HZ} and {MAX_BUZZER_FREQUENCY_HZ} Hz"
            )
        if duration_ms < MIN_BUZZER_DURATION_MS or duration_ms > MAX_BUZZER_DURATION_MS:
            raise ValueError(
                f"duration_ms must be between {MIN_BUZZER_DURATION_MS} and {MAX_BUZZER_DURATION_MS}"
            )
        BuzzerManager._validate_volume(volume_percent)

    @staticmethod
    def _validate_volume(volume_percent: int) -> None:
        if volume_percent < 0 or volume_percent > 100:
            raise ValueError("volume_percent must be between 0 and 100")

    @staticmethod
    def _read_text(path: Path) -> str | None:
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            return None

    @staticmethod
    def _write_text(path: Path, value: str) -> None:
        path.write_text(value, encoding="utf-8")

    @staticmethod
    def _import_pigpio():
        try:
            import pigpio
        except ImportError as exc:
            raise RuntimeError("pigpio Python module is not installed") from exc
        return pigpio

    def _pigpio_status(self) -> dict[str, Any]:
        if _is_mock_platform():
            return {
                "host": PIGPIO_HOST,
                "port": PIGPIO_PORT,
                "connected": True,
                "checked": True,
                "mock": True,
            }
        status: dict[str, Any] = {
            "host": PIGPIO_HOST,
            "port": PIGPIO_PORT,
            "connected": bool(self._pigpio_connected),
            "checked": self._pigpio_connected is not None,
        }
        if self._pigpio_error:
            status["error"] = self._pigpio_error
        return status


def _int_from_store(value: str | None, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _default_store_path() -> Path:
    if sys.platform == "win32":
        return Path(".data/intellegyhub.sqlite3")
    return Path("/data/intellegyhub.sqlite3")


def _is_mock_platform() -> bool:
    return (
        os.environ.get("INTELLEGY_GPIO_MOCK", "").lower() in {"1", "true", "yes"}
        or os.environ.get("INTELLEGY_MOCK_GPIO", "").lower() in {"1", "true", "yes"}
    )
