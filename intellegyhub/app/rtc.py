from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class RtcStatus:
    available: bool
    rtc_time: str | None
    system_time: str
    difference_seconds: float | None
    status: str
    error: str | None = None


class HostRtc:
    """Access the kernel-owned RTC through host tools, never raw I2C."""

    def __init__(self, command: str = "hwclock") -> None:
        self.command = command

    async def status(self) -> RtcStatus:
        system_time = datetime.now(timezone.utc)
        if shutil.which(self.command) is None:
            return RtcStatus(
                available=False,
                rtc_time=None,
                system_time=system_time.isoformat(),
                difference_seconds=None,
                status="UNAVAILABLE",
                error=f"{self.command} not found",
            )
        try:
            completed = await self._run("-r", "-u")
            rtc_time = _parse_hwclock_time(completed.strip())
            return RtcStatus(
                available=True,
                rtc_time=rtc_time.isoformat(),
                system_time=system_time.isoformat(),
                difference_seconds=round((rtc_time - system_time).total_seconds(), 3),
                status="OK",
            )
        except (OSError, RuntimeError, ValueError) as exc:
            return RtcStatus(
                available=False,
                rtc_time=None,
                system_time=system_time.isoformat(),
                difference_seconds=None,
                status="ERROR",
                error=str(exc),
            )

    async def sync_from_system(self) -> RtcStatus:
        if shutil.which(self.command) is None:
            return await self.status()
        await self._run("-w", "-u")
        return await self.status()

    async def _run(self, *args: str) -> str:
        process = await asyncio.create_subprocess_exec(
            self.command,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            detail = stderr.decode(errors="replace").strip() or stdout.decode(errors="replace").strip()
            raise RuntimeError(detail or f"{self.command} exited with {process.returncode}")
        return stdout.decode(errors="replace")


def _parse_hwclock_time(value: str) -> datetime:
    # BusyBox/util-linux usually returns: 2026-09-30 21:10:00.123456+03:00
    first_line = value.splitlines()[0].strip()
    if not first_line:
        raise ValueError("hwclock returned empty output")
    normalized = first_line.replace("  ", " ")
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    if " " in normalized and "T" not in normalized:
        normalized = normalized.replace(" ", "T", 1)
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
