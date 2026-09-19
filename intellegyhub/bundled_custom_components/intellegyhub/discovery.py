from __future__ import annotations

import json
from pathlib import Path

from .const import NAME


def normalize_url(value: str) -> str:
    from urllib.parse import urlparse

    parsed = urlparse(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("invalid_url")
    return value.strip().rstrip("/")


def addon_hostname_from_slug(slug: str) -> str:
    return slug.replace("_", "-")


def addon_matches_intellegyhub(addon: dict) -> bool:
    slug = str(addon.get("slug") or "")
    name = str(addon.get("name") or "")
    return slug.endswith("_intellegyhub") or slug == "intellegyhub" or name.casefold() == NAME.casefold()


def addon_is_installed(addon: dict) -> bool:
    installed = addon.get("installed")
    return installed is True or (isinstance(installed, str) and installed not in {"", "false", "none", "null"})


def backend_urls_from_addons_payload(payload: dict) -> list[str]:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    addons = data.get("addons") if isinstance(data, dict) else None
    if not isinstance(addons, list):
        return []

    urls: list[str] = []
    for addon in addons:
        if not isinstance(addon, dict):
            continue
        slug = addon.get("slug")
        if not isinstance(slug, str) or not addon_matches_intellegyhub(addon) or not addon_is_installed(addon):
            continue
        url = f"http://{addon_hostname_from_slug(slug)}:8098"
        if url not in urls:
            urls.append(url)
    return urls


def backend_url_from_hint(path: Path) -> str | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    url = payload.get("backend_url")
    if not isinstance(url, str) or not url:
        return None
    try:
        return normalize_url(url)
    except ValueError:
        return None
