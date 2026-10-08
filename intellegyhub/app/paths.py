from __future__ import annotations

import sys
from pathlib import Path


def default_data_path(name: str, platform: str | None = None) -> Path:
    current_platform = sys.platform if platform is None else platform
    root = Path(".data") if current_platform in {"win32", "darwin"} else Path("/data")
    return root / name
