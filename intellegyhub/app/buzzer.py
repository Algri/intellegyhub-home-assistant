from __future__ import annotations

import asyncio
import contextlib
import io
import os
import sys
import time
from pathlib import Path
from typing import Any


PIGPIO_HOST = "127.0.0.1"
PIGPIO_PORT = 8888
MAX_PHYSICAL_DUTY = 0.375


def volume_percent_to_duty(volume_percent: float) -> float:
    volume = max(0.0, min(100.0, float(volume_percent)))
    if volume <= 0:
        return 0.0
    return MAX_PHYSICAL_DUTY * (volume / 100.0)


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
        self.frequency = 2000
        self.duration_ms = 300
        self.volume_percent = 50
        self._pigpio_connected: bool | None = None
        self._pigpio_error: str | None = None

    def status(self) -> dict[str, Any]:
        pwm_path = self._pwm_path()
        export_path = self.pwmchip / "export"
        pigpio_status = self._pigpio_status()
        return {
            "pigpio": pigpio_status,
            "pwm": {
                "chip": str(self.pwmchip),
                "channel": self.pwm_channel,
                "available": self.pwmchip.exists(),
                "exported": pwm_path.exists(),
                "npwm": self._read_text(self.pwmchip / "npwm"),
                "export_writable": os.access(export_path, os.W_OK),
            },
            "gpio": {
                "chip": self.gpiochip,
                "line": self.gpio_line,
                "available": Path(self.gpiochip).exists(),
            },
            "active": {
                "running": self._active_task is not None,
                "backend": self._active_backend,
            },
            "frequency": self.frequency,
            "duration_ms": self.duration_ms,
            "volume_percent": self.volume_percent,
            "max_physical_duty": MAX_PHYSICAL_DUTY,
        }

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
                backend = await asyncio.to_thread(self._start_hardware_pwm, frequency, physical_duty)
                self._active_backend = backend
                self._active_task = asyncio.create_task(self._auto_stop_after(duration_ms))
            self.frequency = int(frequency)
            self.duration_ms = int(duration_ms)
            self.volume_percent = int(volume)
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
                    backend = await asyncio.to_thread(self._run_hardware_pwm_once, frequency, duration_ms, physical_duty)
                    self._active_backend = backend
            finally:
                await asyncio.to_thread(self._disable_pwm)
                self._active_backend = None
            self.frequency = int(frequency)
            self.duration_ms = int(duration_ms)
            self.volume_percent = int(volume)
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
        if frequency < 20 or frequency > 20_000:
            raise ValueError("frequency must be between 20 and 20000 Hz")
        if duration_ms < 10 or duration_ms > 5000:
            raise ValueError("duration_ms must be between 10 and 5000")
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
        status: dict[str, Any] = {
            "host": PIGPIO_HOST,
            "port": PIGPIO_PORT,
            "connected": bool(self._pigpio_connected),
            "checked": self._pigpio_connected is not None,
        }
        if self._pigpio_error:
            status["error"] = self._pigpio_error
        return status
