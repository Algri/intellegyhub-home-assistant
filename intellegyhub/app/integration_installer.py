from __future__ import annotations

import json
import os
import shutil
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path


DOMAIN = "intellegyhub"
DEFAULT_BUNDLED_ROOT = Path("/opt/intellegyhub/bundled_custom_components")
DEFAULT_HA_CONFIG_ROOT = Path("/ha_config")
DEFAULT_STATUS_PATH = Path("/data/integration_install_status.json")
BACKEND_HINT_FILENAME = "backend.json"


@dataclass(frozen=True)
class IntegrationInstallStatus:
    status: str
    bundled_version: str | None
    installed_version: str | None
    target: str
    restart_required: bool
    error: str | None = None


def _read_manifest_version(path: Path) -> str | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError):
        return None
    version = payload.get("version")
    return version if isinstance(version, str) and version else None


def _version_key(version: str | None) -> tuple[int, ...]:
    if not version:
        return ()
    parts: list[int] = []
    for piece in version.split("."):
        try:
            parts.append(int(piece))
        except ValueError:
            return ()
    return tuple(parts)


def _copy_component(source: Path, target: Path) -> None:
    temp = target.with_name(f".{target.name}.tmp")
    if temp.exists():
        shutil.rmtree(temp)
    shutil.copytree(
        source,
        temp,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    if target.exists():
        shutil.rmtree(target)
    temp.replace(target)


def _addon_self_slug() -> str | None:
    token = os.environ.get("SUPERVISOR_TOKEN")
    request = urllib.request.Request("http://supervisor/addons/self/info")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError):
        return None
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    slug = data.get("slug") if isinstance(data, dict) else None
    return slug if isinstance(slug, str) and slug else None


def _write_backend_hint(target: Path) -> str | None:
    slug = _addon_self_slug()
    if not slug:
        return None
    backend_url = f"http://{slug.replace('_', '-')}:8098"
    payload = {
        "backend_url": backend_url,
        "addon_slug": slug,
    }
    (target / BACKEND_HINT_FILENAME).write_text(
        json.dumps(payload, ensure_ascii=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return backend_url


def _write_status(path: Path, status: IntegrationInstallStatus) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(status), ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def install_bundled_integration(
    bundled_root: Path = DEFAULT_BUNDLED_ROOT,
    ha_config_root: Path = DEFAULT_HA_CONFIG_ROOT,
    status_path: Path = DEFAULT_STATUS_PATH,
) -> IntegrationInstallStatus:
    source = bundled_root / DOMAIN
    target = ha_config_root / "custom_components" / DOMAIN
    bundled_version = _read_manifest_version(source / "manifest.json")
    installed_version = _read_manifest_version(target / "manifest.json")

    if not source.exists() or bundled_version is None:
        status = IntegrationInstallStatus(
            status="missing_bundled_integration",
            bundled_version=bundled_version,
            installed_version=installed_version,
            target=str(target),
            restart_required=False,
            error=f"{source} is missing or has no manifest version",
        )
        _write_status(status_path, status)
        return status

    if not ha_config_root.exists():
        status = IntegrationInstallStatus(
            status="ha_config_not_mounted",
            bundled_version=bundled_version,
            installed_version=installed_version,
            target=str(target),
            restart_required=False,
            error=f"{ha_config_root} does not exist",
        )
        _write_status(status_path, status)
        return status

    if _version_key(installed_version) >= _version_key(bundled_version):
        _write_backend_hint(target)
        status = IntegrationInstallStatus(
            status="up_to_date",
            bundled_version=bundled_version,
            installed_version=installed_version,
            target=str(target),
            restart_required=False,
        )
        _write_status(status_path, status)
        return status

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        _copy_component(source, target)
        _write_backend_hint(target)
    except OSError as exc:
        status = IntegrationInstallStatus(
            status="install_failed",
            bundled_version=bundled_version,
            installed_version=installed_version,
            target=str(target),
            restart_required=False,
            error=str(exc),
        )
        _write_status(status_path, status)
        return status

    status = IntegrationInstallStatus(
        status="installed" if installed_version is None else "updated",
        bundled_version=bundled_version,
        installed_version=installed_version,
        target=str(target),
        restart_required=True,
    )
    _write_status(status_path, status)
    return status


def main() -> int:
    status = install_bundled_integration(
        bundled_root=Path(os.environ.get("INTELLEGY_BUNDLED_COMPONENTS", str(DEFAULT_BUNDLED_ROOT))),
        ha_config_root=Path(os.environ.get("INTELLEGY_HA_CONFIG", str(DEFAULT_HA_CONFIG_ROOT))),
        status_path=Path(os.environ.get("INTELLEGY_INTEGRATION_STATUS", str(DEFAULT_STATUS_PATH))),
    )
    print(
        "[intellegyhub] integration installer "
        f"status={status.status} bundled={status.bundled_version} "
        f"installed={status.installed_version} restart_required={str(status.restart_required).lower()} "
        f"target={status.target}"
    )
    if status.error:
        print(f"[intellegyhub] integration installer error={status.error}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
