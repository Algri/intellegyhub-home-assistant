from __future__ import annotations

import asyncio
import sqlite3
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

UiTheme = Literal["auto", "light", "dark"]
VALID_THEMES: set[str] = {"auto", "light", "dark"}


@dataclass
class UiSettings:
    theme: UiTheme = "auto"

    def snapshot(self) -> dict[str, str]:
        return asdict(self)


class UiSettingsStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or _default_store_path()
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    async def load(self) -> UiSettings:
        return await asyncio.to_thread(self._load_sync)

    async def save(self, settings: UiSettings) -> None:
        async with self._lock:
            await asyncio.to_thread(self._save_sync, settings)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(self.path)

    def _initialize_sync(self) -> None:
        with self._connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS ui_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )

    def _load_sync(self) -> UiSettings:
        with self._connect() as db:
            row = db.execute("SELECT value FROM ui_settings WHERE key='theme'").fetchone()
        theme = row[0] if row else "auto"
        if theme not in VALID_THEMES:
            theme = "auto"
        return UiSettings(theme=theme)  # type: ignore[arg-type]

    def _save_sync(self, settings: UiSettings) -> None:
        if settings.theme not in VALID_THEMES:
            raise ValueError("theme must be auto, light, or dark")
        with self._connect() as db:
            db.execute(
                "INSERT INTO ui_settings(key,value) VALUES('theme',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (settings.theme,),
            )


def _default_store_path() -> Path:
    if sys.platform == "win32":
        return Path(".data/intellegyhub.sqlite3")
    return Path("/data/intellegyhub.sqlite3")
