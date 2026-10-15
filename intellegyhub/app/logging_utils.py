"""Small helpers for consistent, safe add-on logs.

The add-on runs inside Home Assistant's logging pipeline, so this module keeps
existing handler routing intact. It standardizes the message body and uses the
application formatter for existing stream handlers or the default root setup.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from typing import Any


_SAFE_VALUE = re.compile(r"^[A-Za-z0-9_./:@%+,-]+$")
_LOG_FORMAT = "[%(asctime)s] %(levelname)s %(name)s: %(message)s"
_LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def configure_logging() -> None:
    """Configure app logging while preserving the existing handler routing.

    Uvicorn or Home Assistant may install handlers before the app module is
    imported. In that case ``basicConfig`` is a no-op, so format the existing
    stream handlers and configure the app logger explicitly.
    """

    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATE_FORMAT)
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(level=logging.INFO, format=_LOG_FORMAT, datefmt=_LOG_DATE_FORMAT)
    else:
        for handler in root.handlers:
            if isinstance(handler, logging.StreamHandler):
                handler.setFormatter(formatter)

    logging.getLogger("intellegyhub").setLevel(logging.INFO)


def _value(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (Mapping, list, tuple, set)):
        return json.dumps(value, ensure_ascii=True, sort_keys=True, default=str, separators=(",", ":"))
    text = str(value)
    return text if _SAFE_VALUE.fullmatch(text) else json.dumps(text, ensure_ascii=True)


def event_message(event: str, **fields: Any) -> str:
    """Build one-line structured text suitable for logging or startup stdout."""

    message = [f"event={_value(event)}"]
    message.extend(f"{key}={_value(value)}" for key, value in fields.items() if value is not None)
    return " ".join(message)


def log_event(logger: logging.Logger, level: int, event: str, **fields: Any) -> None:
    """Emit one structured event without leaking arbitrary object reprs."""

    if not logger.isEnabledFor(level):
        return
    exc_info = bool(fields.pop("exc_info", False))
    logger.log(level, event_message(event, **fields), exc_info=exc_info)

