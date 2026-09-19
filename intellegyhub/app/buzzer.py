from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Any


PIGPIO_HOST = "127.0.0.1"
PIGPIO_PORT = 8888


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
        }

    async def stop(self) -> None:
        async with self._lock:
            await self._stop_locked()

    async def test_pwm(self, frequency: int, duration_ms: int, duty: float) -> dict[str, Any]:
        self._validate(frequency, duration_ms, duty)
        async with self._lock:
            await self._stop_locked()
            await asyncio.to_thread(self._start_pigpio_pwm, frequency, duty)
            self._active_backend = "pigpio_hardware_pwm"
            self._active_task = asyncio.create_task(self._auto_stop_after(duration_ms))
            return {
                "status": "ok",
                "backend": "pigpio_hardware_pwm",
                "duration_ms": duration_ms,
                **self.status(),
            }

    async def play_pwm_once(self, frequency: int, duration_ms: int, duty: float) -> dict[str, Any]:
        self._validate(frequency, duration_ms, duty)
        async with self._lock:
            await self._stop_locked()
            self._active_backend = "pigpio_hardware_pwm"
            try:
                await asyncio.to_thread(self._run_pigpio_pwm_once, frequency, duration_ms, duty)
            finally:
                await asyncio.to_thread(self._disable_pwm)
                self._active_backend = None
        return {
            "status": "ok",
            "backend": "pigpio_hardware_pwm",
            "duration_ms": duration_ms,
            **self.status(),
        }

    async def test_gpio(self, frequency: int, duration_ms: int, duty: float) -> dict[str, Any]:
        self._validate(frequency, duration_ms, duty)
        async with self._lock:
            await self._stop_locked()
            await asyncio.to_thread(self._run_gpio_test, frequency, duration_ms, duty)
            return {"status": "ok", "backend": "software_gpio", **self.status()}

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

    def _start_pigpio_pwm(self, frequency: int, duty: float) -> None:
        pigpio = self._import_pigpio()
        pi = pigpio.pi(PIGPIO_HOST, PIGPIO_PORT)
        try:
            if not pi.connected:
                raise RuntimeError("pigpio daemon is not connected")
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
        pi = pigpio.pi(PIGPIO_HOST, PIGPIO_PORT)
        try:
            if not pi.connected:
                raise RuntimeError("pigpio daemon is not connected")
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
        self._export_pwm()
        pwm_path = self._pwm_path()
        period_ns = round(1_000_000_000 / frequency)
        duty_ns = round(period_ns * duty)
        self._write_text(pwm_path / "enable", "0")
        self._write_text(pwm_path / "period", str(period_ns))
        self._write_text(pwm_path / "duty_cycle", str(duty_ns))
        self._write_text(pwm_path / "enable", "1")
        time.sleep(duration_ms / 1000)
        self._write_text(pwm_path / "enable", "0")

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
            pi = pigpio.pi(PIGPIO_HOST, PIGPIO_PORT)
            try:
                if pi.connected:
                    pi.hardware_PWM(self.gpio_line, 0, 0)
            finally:
                pi.stop()
        except Exception:
            pass

    def _pwm_path(self) -> Path:
        return self.pwmchip / f"pwm{self.pwm_channel}"

    @staticmethod
    def _validate(frequency: int, duration_ms: int, duty: float) -> None:
        if frequency < 20 or frequency > 20_000:
            raise ValueError("frequency must be between 20 and 20000 Hz")
        if duration_ms < 10 or duration_ms > 5000:
            raise ValueError("duration_ms must be between 10 and 5000")
        if duty <= 0 or duty >= 1:
            raise ValueError("duty must be greater than 0 and less than 1")

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
        status: dict[str, Any] = {"host": PIGPIO_HOST, "port": PIGPIO_PORT, "connected": False}
        try:
            pigpio = self._import_pigpio()
            pi = pigpio.pi(PIGPIO_HOST, PIGPIO_PORT)
            try:
                status["connected"] = bool(pi.connected)
            finally:
                pi.stop()
        except Exception as exc:
            status["error"] = str(exc)
        return status
