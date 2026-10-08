from __future__ import annotations

import asyncio
import errno
import glob
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from .backends import MockGpioBackend, RealGpiodBackend
from .carrier import APP_VERSION
from .config import load_config
from .rs485 import Rs485Manager, Rs485Store
from .rtc import HostRtc, MockRtc
from .runtime import AppRuntime
from .xport import XPortGroupMode, XPortMode

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
LOGGER = logging.getLogger("intellegyhub")
I2C_SLAVE = 0x0703
I2C_QUICK_WRITE = b""


class LedPayload(BaseModel):
    on: bool


class OutputPayload(BaseModel):
    on: bool


class XPortModePayload(BaseModel):
    mode: XPortMode
    revision: int | None = None


class XPortGroupModePayload(BaseModel):
    mode: XPortGroupMode


class XPortValuePayload(BaseModel):
    value: float


class ExtensionPowerPayload(BaseModel):
    on: bool


class ExtensionRelayPayload(BaseModel):
    on: bool


class OneWirePowerPayload(BaseModel):
    on: bool


class OneWireBridgeEnabledPayload(BaseModel):
    enabled: bool


class Rs485DeviceEnabledPayload(BaseModel):
    enabled: bool


class Rs485DeviceAccessPayload(BaseModel):
    read_enabled: bool | None = None
    write_enabled: bool | None = None


class Rs485PollingPayload(BaseModel):
    groups: dict[str, dict[str, int | str]] | None = None


class BuzzerTestPayload(BaseModel):
    frequency: int = 2000
    duration_ms: int = 300
    duty: float | None = None
    volume_percent: int | None = None


class BuzzerVolumePayload(BaseModel):
    volume_percent: int


class BuzzerSettingsPayload(BaseModel):
    frequency: int | None = None
    duration_ms: int | None = None
    volume_percent: int | None = None


class SteHeartbeatPayload(BaseModel):
    enabled: bool
    on_seconds: float | None = None
    off_seconds: float | None = None


class DiagnosticIndicatorPayload(BaseModel):
    enabled: bool


class UiThemePayload(BaseModel):
    theme: Literal["auto", "light", "dark"]


class Rs485BusPayload(BaseModel):
    serial_port: str = "/dev/ttyAMA3"
    baudrate: int = 9600
    parity: str = "none"
    stop_bits: int = 1
    mode: Literal["mock", "usb_real"] = "mock"
    template_id: str | None = None
    enabled: bool = True


class Rs485BusPatchPayload(BaseModel):
    serial_port: str | None = None
    baudrate: int | None = None
    parity: str | None = None
    stop_bits: int | None = None
    mode: Literal["mock", "usb_real"] | None = None
    enabled: bool | None = None


class Rs485CapabilityPayload(BaseModel):
    value: bool | int | float | str


class Rs485DeviceNamePayload(BaseModel):
    name: str


class Rs485ManualCommandPayload(BaseModel):
    slave_id: int = 1
    function: Literal[1, 2, 3, 4, 5, 6]
    address: int = 0
    count: int = 1
    value: int | None = None


def collect_device_diagnostics() -> dict[str, list[str]]:
    if is_mock_enabled():
        return {
            "gpio": ["/dev/gpiochip0", "/dev/gpiomem"],
            "i2c": ["/dev/i2c-1", "/dev/i2c-10"],
            "rtc": ["/dev/rtc0"],
        }
    return {
        "gpio": sorted(glob.glob("/dev/gpio*")),
        "i2c": sorted(glob.glob("/dev/i2c*")),
        "rtc": sorted(glob.glob("/dev/rtc*") + glob.glob("/dev/misc/rtc")),
    }


def scan_i2c_bus(bus: int) -> dict:
    if bus < 0 or bus > 255:
        raise ValueError("bus must be between 0 and 255")
    if is_mock_enabled():
        mock_devices = {
            1: ["0x20", "0x21", "0x22"],
            10: ["0x18", "0x1a", "0x1b", "0x20", "0x21", "0x48", "0x49", "0x50", "0x51", "0x62"],
        }
        return {
            "bus": bus,
            "device": f"/dev/i2c-{bus}",
            "mock": True,
            "addresses": mock_devices.get(bus, []),
            "errors": {},
        }

    path = Path(f"/dev/i2c-{bus}")
    if not path.exists():
        raise FileNotFoundError(f"{path} does not exist")

    import fcntl

    found: list[str] = []
    errors: dict[str, str] = {}
    with path.open("rb+", buffering=0) as device:
        for address in range(0x08, 0x78):
            try:
                fcntl.ioctl(device, I2C_SLAVE, address)
                os.write(device.fileno(), I2C_QUICK_WRITE)
                found.append(f"0x{address:02x}")
            except OSError as exc:
                if exc.errno in {errno.ENXIO, errno.EREMOTEIO, errno.EIO}:
                    continue
                errors[f"0x{address:02x}"] = exc.strerror or str(exc)

    return {"bus": bus, "device": str(path), "addresses": found, "errors": errors}


def is_mock_enabled() -> bool:
    return (
        os.environ.get("INTELLEGY_GPIO_MOCK", "").lower() in {"1", "true", "yes"}
        or os.environ.get("INTELLEGY_MOCK_GPIO", "").lower() in {"1", "true", "yes"}
    )


def is_rs485_enabled(options_path: Path | None = None) -> bool:
    return load_config(options_path).rs485_enabled


def _health_payload(current: AppRuntime) -> dict:
    snapshot = current.snapshot()
    carrier = snapshot.get("carrier", {})
    xport = snapshot.get("xport", {})
    extensions = snapshot.get("extensions", {})
    onewire = snapshot.get("onewire", {})
    buzzer = snapshot.get("buzzer", {})
    return {
        "status": "ok" if current.ready else "error",
        "ready": current.ready,
        "error": current.error,
        "version": APP_VERSION,
        "mock": is_mock_enabled(),
        "uptime_seconds": snapshot.get("app", {}).get("uptime_seconds", 0),
        "carrier": {
            "available": carrier.get("available", False),
            "identity_status": carrier.get("identity_status"),
            "model": carrier.get("identity", {}).get("model") or carrier.get("identity", {}).get("product"),
            "serial_number": carrier.get("identity", {}).get("serial_number"),
        },
        "xport": {
            "topology": xport.get("topology"),
            "availability": xport.get("availability"),
            "channels": len(xport.get("channels", [])),
        },
        "xbus": {
            "power": extensions.get("power", {}).get("on", False),
            "modules": len(extensions.get("modules", [])),
        },
        "onewire": {
            "power": onewire.get("power", {}).get("on", False),
            "bridges": len(onewire.get("bridges", [])),
            "sensors": len(onewire.get("sensors", [])),
        },
        "buzzer": {
            "frequency": buzzer.get("frequency"),
            "duration_ms": buzzer.get("duration_ms"),
            "volume_percent": buzzer.get("volume_percent"),
            "backend": buzzer.get("active", {}).get("backend"),
        },
    }


def create_app(options_path: Path | None = None, runtime: AppRuntime | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if app.state.runtime is None:
            config = load_config(app.state.options_path)
            LOGGER.info(
                "Starting v0.5.209 chip=%s led=%s active_low=%s fn1_gpio=27 fn2_gpio=%s active_low=%s bias=%s debounce_ms=%s startup_buzzer=%s shutdown_buzzer=%s buzzer_frequency=%s buzzer_duration_ms=%s shutdown_buzzer_volume_percent=80 carrier_monitoring_poll_interval_seconds=%s ste_heartbeat_on_seconds=%s ste_heartbeat_off_seconds=%s websocket_connection_grace_seconds=%s onewire_bus1_poll_interval_seconds=%s onewire_bus2_poll_interval_seconds=%s power_button_shutdown_enabled=%s power_button_shutdown_hold_seconds=%s mock=%s port=8098",
                config.chip_path,
                config.led_gpio,
                config.led_active_low,
                config.button_gpio,
                config.button_active_low,
                config.button_bias,
                config.button_debounce_ms,
                config.startup_buzzer_enabled,
                config.shutdown_buzzer_enabled,
                config.startup_buzzer_frequency,
                config.startup_buzzer_duration_ms,
                config.carrier_monitoring_poll_interval_seconds,
                config.ste_heartbeat_on_seconds,
                config.ste_heartbeat_off_seconds,
                config.websocket_connection_grace_seconds,
                config.onewire_bridge1_poll_interval_seconds,
                config.onewire_bridge2_poll_interval_seconds,
                config.power_button_shutdown_enabled,
                config.power_button_shutdown_hold_seconds,
                config.mock,
            )
            LOGGER.info("Visible devices: %s", collect_device_diagnostics())
            backend = MockGpioBackend() if config.mock else RealGpiodBackend(config)
            app.state.runtime = AppRuntime(
                backend,
                startup_buzzer_enabled=config.startup_buzzer_enabled,
                startup_buzzer_frequency=config.startup_buzzer_frequency,
                startup_buzzer_duration_ms=config.startup_buzzer_duration_ms,
                shutdown_buzzer_enabled=config.shutdown_buzzer_enabled,
                carrier_monitoring_poll_interval_seconds=config.carrier_monitoring_poll_interval_seconds,
                ste_heartbeat_on_seconds=config.ste_heartbeat_on_seconds,
                ste_heartbeat_off_seconds=config.ste_heartbeat_off_seconds,
                websocket_connection_grace_seconds=config.websocket_connection_grace_seconds,
                onewire_poll_intervals={
                    "onewire_bus10_addr1a": config.onewire_bridge1_poll_interval_seconds,
                    "onewire_bus10_addr1b": config.onewire_bridge2_poll_interval_seconds,
                },
                power_button_shutdown_enabled=config.power_button_shutdown_enabled,
                power_button_shutdown_hold_seconds=config.power_button_shutdown_hold_seconds,
            )
        # The production app creates AppRuntime lazily inside lifespan. Bind
        # RS-485 to the final runtime here, before polling starts, so live
        # device changes are published to the integration WebSocket.
        app.state.rs485.set_publisher(app.state.runtime.broadcast_nowait)
        if app.state.rs485_enabled:
            await app.state.rs485.start()
        await app.state.runtime.start()
        try:
            yield
        finally:
            if app.state.rs485_enabled:
                await app.state.rs485.stop()
            await app.state.runtime.stop()

    app = FastAPI(title="IntellegyHUB", version="0.5.209", lifespan=lifespan)
    app.state.runtime = runtime
    app.state.options_path = options_path
    app.state.rtc = MockRtc() if is_mock_enabled() else HostRtc()
    app.state.rs485_enabled = is_rs485_enabled(options_path)
    rs485_store_path = getattr(getattr(runtime, "ui_store", None), "path", None) if runtime is not None else None
    rs485_mock = is_mock_enabled() or (runtime is not None and isinstance(runtime.backend, MockGpioBackend))
    app.state.rs485 = Rs485Manager(store=Rs485Store(rs485_store_path), mock=rs485_mock)
    if runtime is not None:
        app.state.rs485.set_publisher(runtime.broadcast_nowait)

    def runtime_or_503() -> AppRuntime:
        current: AppRuntime = app.state.runtime
        if not current.ready:
            raise HTTPException(status_code=503, detail={"status": "error", "error": current.error or "not ready"})
        return current

    def rs485_or_404() -> Rs485Manager:
        if not app.state.rs485_enabled:
            raise HTTPException(status_code=404, detail="RS-485 is disabled")
        return app.state.rs485

    @app.get("/health")
    async def health() -> dict:
        current: AppRuntime = app.state.runtime
        if not current.ready:
            raise HTTPException(status_code=503, detail={"status": "error", "error": current.error or "not ready"})
        return _health_payload(current)

    @app.get("/api/v1/integration/status")
    async def integration_status() -> dict:
        status_path = Path("/data/integration_install_status.json")
        try:
            return json.loads(status_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {
                "status": "unknown",
                "bundled_version": None,
                "installed_version": None,
                "target": "/ha_config/custom_components/intellegyhub",
                "restart_required": False,
                "error": "integration installer has not written status yet",
            }
        except (OSError, json.JSONDecodeError) as exc:
            return {
                "status": "error",
                "bundled_version": None,
                "installed_version": None,
                "target": "/ha_config/custom_components/intellegyhub",
                "restart_required": False,
                "error": str(exc),
            }

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        html = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>IntellegyHUB</title>
  <link rel="icon" href="favicon.ico">
  <style>
    :root {
      color-scheme: dark;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      --ha-primary: #03a9f4;
      --ha-success: #00e676;
      --ha-success-text: #8ff0a4;
      --ha-success-border: #36543e;
      --ha-success-bg: rgba(0, 200, 83, .08);
      --ha-danger-text: #ffb4ab;
      --ha-danger-border: #5f3434;
      --ha-danger-bg: rgba(244, 67, 54, .10);
      --ha-page: #111111;
      --ha-card: #1c1c1c;
      --ha-card-border: #343434;
      --ha-text: #e8eaed;
      --ha-secondary: #b8c5d0;
      --ha-field: #202020;
      --ha-surface: #202020;
      --ha-row: #1b1b1b;
      --ha-row-border: #333333;
      --ha-menu-hover: #2a2a2a;
      --ha-strong: #ffffff;
      --ha-pre: #0b0b0b;
      --ha-action-hover: rgba(3, 169, 244, .14);
      --wordmark-main: #ffffff;
      --wordmark-accent: #4ea1ff;
      background: var(--ha-page);
      color: var(--ha-text);
    }
    html[data-theme="light"] {
      color-scheme: light;
      --ha-page: #f3f5f7;
      --ha-success: #087f23;
      --ha-success-text: #0b6f28;
      --ha-success-border: #58a868;
      --ha-success-bg: rgba(11, 111, 40, .10);
      --ha-danger-text: #b3261e;
      --ha-danger-border: #d48a84;
      --ha-danger-bg: rgba(179, 38, 30, .08);
      --ha-card: #ffffff;
      --ha-card-border: #d6dde5;
      --ha-text: #111827;
      --ha-secondary: #536170;
      --ha-field: #f8fafc;
      --ha-surface: #f8fafc;
      --ha-row: #ffffff;
      --ha-row-border: #d6dde5;
      --ha-menu-hover: #e8f4fb;
      --ha-strong: #0f172a;
      --ha-pre: #111827;
      --ha-action-hover: rgba(3, 169, 244, .12);
      --wordmark-main: #111827;
      --wordmark-accent: #1976d2;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      padding: 24px;
      background: var(--ha-page);
      color: var(--ha-text);
    }
    main { width: min(100%, 1520px); margin: 0 auto; padding-bottom: 35vh; }
    .app-toolbar { display: flex; justify-content: flex-end; margin-bottom: 10px; }
    .global-nav { position: sticky; top: 5px; z-index: 80; display: flex; gap: 2px; overflow-x: auto; margin-top: -19px; margin-bottom: 0; padding: 3px; border: 1px solid var(--ha-row-border); border-radius: 9px; background: color-mix(in srgb, var(--ha-surface) 94%, transparent); box-shadow: 0 6px 18px rgba(0, 0, 0, .18); scrollbar-width: thin; }
    .global-nav button { flex: 0 0 auto; min-height: 38px; padding: 7px 14px; border: 0 !important; border-radius: 6px; background: transparent !important; color: var(--ha-secondary) !important; font-size: 12px; font-weight: 800; white-space: nowrap; box-shadow: none !important; }
    .global-nav > button[data-ui-nav-target] { min-width: 86px; text-align: center; }
    .global-nav button:hover, .global-nav button:focus-visible { background: var(--ha-field) !important; color: var(--ha-text) !important; outline: none; box-shadow: none !important; }
    .global-nav button.active { border: 1px solid var(--ha-primary) !important; border-radius: 10px !important; background: var(--ha-field) !important; color: var(--ha-primary) !important; }
    .global-nav .theme-switcher { flex: 0 0 auto; margin-left: auto; }
    .global-nav .theme-switcher { border: 0; background: transparent; box-shadow: none; padding: 0; }
    .global-nav .theme-switcher .theme-choice { min-height: 38px !important; }
    .global-nav .theme-switcher .theme-choice.active { border-color: var(--ha-primary) !important; color: var(--ha-primary) !important; background: var(--ha-field) !important; }
    .global-nav .theme-switcher { flex: 0 0 auto; margin-left: auto; }
    .ui-nav-section { scroll-margin-top: 74px; }
    .theme-switcher { display: inline-flex; align-items: center; gap: 6px; border: 1px solid var(--ha-card-border); border-radius: 999px; background: var(--ha-card); padding: 4px; }
    .theme-switcher span { color: var(--ha-secondary); font-size: 12px; font-weight: 800; padding: 0 8px; text-transform: uppercase; letter-spacing: .08em; }
    .theme-choice { min-height: 30px !important; border-radius: 999px !important; padding: 4px 12px !important; border-color: transparent !important; color: var(--ha-secondary) !important; }
    .theme-choice.active { border-color: var(--ha-primary) !important; color: var(--ha-primary) !important; background: rgba(3, 169, 244, .12) !important; }
    .overview-panel { display: grid; grid-template-columns: minmax(420px, .9fr) minmax(520px, 1.5fr); gap: 0; margin-top: 0; margin-bottom: 18px; padding: 0; overflow: hidden; }
    .overview-identity { display: flex; flex-direction: column; min-height: 300px; padding: 34px 42px 32px; border-right: 1px solid var(--ha-card-border); }
    .overview-badge-row { display: flex; align-items: flex-start; justify-content: space-between; gap: 24px; margin-bottom: 16px; }
    .overview-wordmark { display: inline-flex; align-items: baseline; min-width: 0; color: var(--wordmark-main); font-size: 32px; line-height: 1; font-weight: 640; letter-spacing: .18em; white-space: nowrap; }
    .overview-wordmark span { color: var(--wordmark-accent); }
    .overview-logo-mark { width: 76px; height: 76px; object-fit: contain; flex: 0 0 auto; }
    .overview-title-row { display: flex; align-items: center; justify-content: space-between; gap: 24px; margin-bottom: 20px; }
    .overview-title { min-width: 0; }
    .overview-title h1 { font-family: ui-monospace, "SFMono-Regular", Consolas, monospace; font-size: 48px; line-height: 1; margin-bottom: 8px; font-weight: 800; }
    .overview-title .subtitle { color: var(--ha-secondary); font-size: 21px; line-height: 1.25; margin-bottom: 0; font-weight: 700; }
    .overview-title-divider { width: 100%; height: 1px; background: var(--ha-card-border); margin: 0 0 22px; }
    .overview-facts { display: grid; grid-template-columns: 142px minmax(0, 1fr); gap: 12px 24px; margin-top: 0; font-size: 18px; line-height: 1.2; }
    .overview-facts dt { color: var(--ha-secondary); font-size: 18px; font-weight: 800; letter-spacing: 0; }
    .overview-facts dd { margin: 0; color: var(--ha-text); font-family: ui-monospace, "SFMono-Regular", Consolas, monospace; font-size: 18px; font-weight: 800; min-width: 0; overflow-wrap: anywhere; }
    .overview-divider { width: 100%; height: 1px; background: var(--ha-card-border); margin: 34px 0 26px; }
    .overview-health { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 0; font-size: 22px; line-height: 1.25; }
    .overview-health-item { min-width: 0; }
    .overview-health-item + .overview-health-item { border-left: 1px solid var(--ha-card-border); padding-left: 46px; }
    .overview-health dt { color: var(--ha-secondary); font-size: 17px; font-weight: 800; letter-spacing: .08em; text-transform: uppercase; margin-bottom: 14px; }
    .overview-health dd { margin: 0; color: var(--ha-text); font-family: ui-monospace, "SFMono-Regular", Consolas, monospace; font-size: 22px; font-weight: 800; min-width: 0; overflow-wrap: anywhere; }
    .overview-health-value { display: inline-flex; align-items: center; gap: 14px; font-family: inherit; }
    .overview-sync-dot { width: 18px; height: 18px; border-radius: 50%; background: #448aff; }
    .overview-sync-dot.online { background: var(--ha-success); }
    .overview-sync-dot.offline { background: #ff6b6b; }
    .overview-metrics { position: relative; display: grid; grid-template-columns: repeat(2, minmax(220px, 1fr)); padding-bottom: 24px; }
    .overview-metric { min-height: 135px; padding: 30px 24px; border-left: 0; border-top: 0; border-right: 1px solid var(--ha-card-border); border-bottom: 1px solid var(--ha-card-border); border-radius: 0; background: transparent; }
    .overview-metric:nth-child(2n) { border-right: 0; }
    .overview-metric:nth-last-child(-n+3) { border-bottom: 0; }
    .overview-metric-label { color: var(--ha-secondary); font-size: 12px; font-weight: 800; letter-spacing: .18em; text-transform: uppercase; margin-bottom: 18px; }
    .overview-metric-value { color: var(--ha-text); font-size: 27px; line-height: 1; font-weight: 800; margin-bottom: 12px; }
    .overview-metric-meta { color: var(--ha-secondary); font-size: 12px; overflow-wrap: anywhere; }
    .overview-updated { position: absolute; right: 24px; bottom: 10px; color: var(--ha-secondary); font-size: 12px; }
    .xport-panel {
      border: 1px solid var(--ha-card-border);
      border-radius: 12px;
      background: var(--ha-card);
      box-shadow: 0 2px 4px rgba(0, 0, 0, .22);
      padding: 24px;
    }
    .module-row { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; margin-bottom: 22px; }
    .eyebrow { color: var(--ha-primary); font-size: 12px; font-weight: 700; letter-spacing: 2px; text-transform: uppercase; margin-bottom: 8px; }
    h1 { font-size: 28px; line-height: 1; margin: 0 0 7px; letter-spacing: 0; font-weight: 500; }
    .subtitle { color: var(--ha-secondary); font-size: 14px; margin-bottom: 8px; }
    .status { color: var(--ha-success); font-size: 14px; font-weight: 500; }
    .transport { border: 1px solid var(--ha-primary); color: var(--ha-primary); background: transparent; border-radius: 999px; padding: 6px 14px; font-size: 12px; font-weight: 500; margin-top: 38px; }
    .notice { color: var(--ha-secondary); font-size: 14px; line-height: 1.45; margin: 0 0 20px; overflow-wrap: anywhere; }
    .ports { display: grid; grid-template-columns: repeat(4, minmax(220px, 1fr)); gap: 16px; margin-top: 16px; }
    .notice.hidden { display: none; }
    .ports.hidden { display: none !important; }
    .xport-unavailable {
      margin-top: 16px;
      border: 1px solid var(--ha-card-border);
      border-radius: 10px;
      background: var(--ha-surface);
      padding: 16px;
      color: var(--ha-muted);
      font-size: 14px;
    }
    .xport-unavailable.hidden { display: none; }
    .xport-unavailable strong {
      display: block;
      color: var(--ha-text);
      margin-bottom: 6px;
    }
    .port-card { border: 1px solid var(--ha-card-border); border-radius: 12px; background: var(--ha-surface); padding: 20px; min-height: 228px; }
    .port-card.locked { opacity: .92; }
    .port-card.locked .active-row { grid-template-columns: minmax(0, 1fr); }
    .port-card.locked .xport-action-slot { display: none; }
    .port-card.locked .active-mode strong { overflow: visible; text-overflow: clip; }
    .port-title { font-size: 17px; font-weight: 800; margin-bottom: 20px; }
    label { display: block; font-size: 13px; font-weight: 700; margin-bottom: 8px; }
    .mode-select { position: relative; }
    .mode-trigger {
      width: 100%;
      height: 48px;
      border-radius: 8px;
      border: 1px solid var(--ha-row-border);
      background: var(--ha-field);
      color: var(--ha-text);
      padding: 0 42px 0 16px;
      font-weight: 600;
      text-align: left;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      box-shadow: none;
    }
    .mode-trigger::after {
      content: "";
      position: absolute;
      right: 16px;
      top: 19px;
      width: 8px;
      height: 8px;
      border-right: 2px solid var(--ha-text);
      border-bottom: 2px solid var(--ha-text);
      transform: rotate(45deg);
    }
    .mode-select.open .mode-trigger,
    .mode-trigger:focus,
    .mode-trigger:focus-visible { border-color: var(--ha-primary); box-shadow: none; outline: none; }
    .mode-select.locked .mode-trigger {
      cursor: default;
      color: var(--ha-secondary);
      border-color: var(--ha-row-border);
      background: var(--ha-row);
    }
    .mode-select.locked .mode-trigger::after { display: none; }
    .mode-menu {
      display: none;
      position: absolute;
      z-index: 1000;
      top: calc(100% + 4px);
      left: 0;
      right: 0;
      overflow: visible;
      border: 1px solid var(--ha-row-border);
      border-radius: 8px;
      background: var(--ha-surface);
      box-shadow: 0 8px 20px rgba(0, 0, 0, .42);
      padding: 4px 0;
    }
    .mode-select.open .mode-menu { display: block; }
    .mode-option {
      width: 100%;
      min-height: 32px;
      border: 0;
      border-radius: 0;
      background: transparent;
      color: var(--ha-text);
      padding: 6px 14px;
      text-align: left;
      font-weight: 500;
    }
    .mode-option:hover,
    .mode-option.active {
      background: var(--ha-menu-hover);
      color: var(--ha-strong);
    }
    .mode-option.disabled {
      cursor: not-allowed;
      color: var(--ha-secondary);
      opacity: .72;
    }
    .mode-option.disabled:hover {
      background: transparent;
      color: var(--ha-secondary);
    }
    .active-mode {
      display: flex;
      align-items: center;
      gap: 4px;
      margin-top: 10px;
      height: 24px;
      min-width: 0;
      font-size: 15px;
      line-height: 24px;
      white-space: nowrap;
    }
    .active-mode strong {
      display: inline-block;
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      vertical-align: bottom;
    }
    .active-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: center; gap: 12px; margin-top: 14px; }
    .active-row .active-mode { margin-top: 0; min-width: 0; }
    .xport-action-slot { min-width: 112px; min-height: 38px; display: flex; justify-content: flex-end; align-items: center; gap: 10px; }
    .xport-action-slot:empty { visibility: hidden; }
    .mode-body { color: var(--ha-secondary); font-size: 15px; line-height: 1.6; margin-top: 26px; min-height: 54px; }
    .metric strong, .active-mode strong { color: var(--ha-strong); font-weight: 700; }
    .mode-row { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
    .switch-row { justify-content: space-between; flex-wrap: nowrap; width: 100%; }
    .control-cluster { display: inline-flex; align-items: center; gap: 10px; margin-left: auto; }
    .xport-control-row { display: grid; grid-template-columns: minmax(0, 1fr) auto auto; align-items: center; gap: 10px; min-height: 42px; border: 1px solid var(--ha-row-border); border-radius: 8px; padding: 8px 10px; background: var(--ha-row); color: var(--ha-text); }
    .xport-control-row.readout { grid-template-columns: minmax(0, 1fr) auto; }
    .xport-control-row.pwm { grid-template-columns: auto minmax(120px, 1fr) 5ch; padding-right: 12px; }
    .xport-control-row.pwm strong { text-align: right; padding-right: 2px; }
    .xport-control-row span { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .toggle {
      position: relative;
      width: 58px;
      height: 26px;
      min-height: 26px;
      border: 0;
      border-radius: 999px;
      background: #5f6368;
      padding: 0;
      vertical-align: middle;
    }
    .toggle::before {
      content: "";
      position: absolute;
      width: 22px;
      height: 22px;
      left: 2px;
      top: 2px;
      border-radius: 50%;
      background: #ffffff;
      box-shadow: 0 1px 3px rgba(0, 0, 0, .45);
      transition: transform .16s ease;
    }
    .toggle.on { background: var(--ha-primary); }
    .toggle.on::before { transform: translateX(32px); }
    .toggle span { position: relative; z-index: 1; display: block; padding-left: 7px; color: #fff; font-size: 9px; font-weight: 900; line-height: 26px; text-align: left; pointer-events: none; }
    .toggle:not(.on) span { padding-left: 28px; color: #202124; }
    .toggle.readonly {
      cursor: default;
      pointer-events: none;
    }
    .toggle.readonly,
    .toggle.readonly:hover,
    .toggle.readonly:focus-visible,
    .toggle:disabled,
    .toggle:disabled:hover,
    .toggle:disabled:focus-visible {
      background: #5f6368;
      outline: none;
      cursor: default;
      opacity: .55;
    }
    .toggle.readonly.on,
    .toggle.readonly.on:hover,
    .toggle.readonly.on:focus-visible,
    .toggle.on:disabled,
    .toggle.on:disabled:hover,
    .toggle.on:disabled:focus-visible {
      background: var(--ha-primary);
    }
    .slider-row { display: flex; align-items: center; gap: 10px; margin-top: 14px; }
    input[type="range"] { width: 100%; accent-color: var(--ha-primary); }
    .reset-button {
      min-width: 98px;
      min-height: 42px;
      margin-left: auto;
      border-color: var(--ha-primary);
      background: transparent;
      color: var(--ha-primary);
      transition: background .15s ease, color .15s ease, border-color .15s ease;
    }
    .reset-button:hover,
    .reset-button:focus-visible {
      border-color: var(--ha-primary);
      background: var(--ha-action-hover);
      color: var(--ha-primary);
      outline: none;
    }
    .diagnostics-output { margin-top: 16px; }
    .extension-panel { margin-top: 18px; border: 1px solid var(--ha-card-border); border-radius: 12px; background: var(--ha-card); box-shadow: 0 2px 4px rgba(0, 0, 0, .22); padding: 24px; }
    .extension-actions { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin-top: 18px; }
    .bus-toolbar { align-items: stretch; border: 1px solid var(--ha-card-border); border-radius: 10px; background: var(--ha-surface); padding: 12px; }
    .bus-power-control { display: flex; align-items: center; gap: 10px; min-height: 42px; padding-right: 6px; }
    .bus-power-control .metric { white-space: nowrap; }
    .bus-power-control label { margin-bottom: 0; white-space: nowrap; }
    .bus-power-control .mode-select { width: min(270px, 62vw); }
    .xport-group-toolbar {
      --xport-card-gap: 16px;
      display: grid;
      grid-template-columns: repeat(4, minmax(220px, 1fr));
      gap: var(--xport-card-gap);
      align-items: center;
    }
    .xport-group-toolbar .bus-power-control {
      display: grid;
      grid-template-columns: auto minmax(0, 1fr);
      align-items: center;
      gap: 10px;
      padding-right: 0;
    }
    .xport-group-toolbar .bus-power-control .mode-select { width: 100%; min-width: 0; }
    .xport-group-toolbar .bus-power-control.hidden { display: none; }
    .xport-group-toolbar .bus-note { grid-column: 3 / -1; }
    .xport-group-toolbar.config-hidden {
      grid-template-columns: repeat(4, minmax(220px, 1fr));
    }
    .xport-group-toolbar.config-hidden .bus-note { grid-column: 2 / -1; }
    .xport-group-toolbar.hidden { display: none; }
    .bus-note { color: var(--ha-secondary); font-size: 13px; line-height: 1.4; flex: 1 1 360px; align-self: center; }
    .bus-action { min-width: 142px; }
    .rs485-toolbar {
      --rs485-label-height: 16px;
      --rs485-control-height: 42px;
      display: grid;
      grid-template-columns: minmax(190px, 280px) 250px 140px 120px 100px 220px 220px;
      column-gap: 14px;
      row-gap: 12px;
      align-items: end;
    }
    .rs485-toolbar > * { min-width: 0; }
    .rs485-toolbar .rs485-connection-intro { min-width: 0; }
    .rs485-toolbar + .rs485-layout { margin-top: 18px; }
    .rs485-port-controls { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-top: 10px; padding: 10px 12px; border: 1px solid var(--ha-card-border); border-radius: 8px; background: var(--ha-surface); }
    .rs485-port-controls-title { margin-right: auto; color: var(--ha-secondary); font-size: 12px; font-weight: 800; text-transform: uppercase; letter-spacing: .08em; }
    .rs485-port-controls .toggle { min-height: 32px; padding: 5px 10px; border: 1px solid var(--ha-row-border); border-radius: 7px; background: transparent; color: var(--ha-secondary); }
    .rs485-port-controls .toggle.on { border-color: var(--ha-primary); color: var(--ha-primary); background: rgba(3,169,244,.12); }
    .rs485-settings-polling-controls { display: grid; grid-template-columns: auto repeat(3, minmax(140px, 1fr)); align-items: center; gap: 8px; margin: 0 0 16px; padding: 10px 12px; border: 1px solid var(--ha-row-border); border-radius: 8px; background: var(--ha-row); }
    .rs485-device-access-controls { grid-template-columns: repeat(3, minmax(170px, 1fr)); grid-template-rows: auto auto; padding: 9px 14px 10px; column-gap: 22px; row-gap: 7px; }
    .rs485-device-access-controls > .polling-control-label { grid-column: 1 / -1; }
    .rs485-device-access-controls .rs485-polling-group-toggle { flex-direction: column; align-items: flex-start; gap: 6px; border: 0; border-radius: 0; padding: 3px 0; background: transparent; }
    .rs485-device-access-controls .rs485-polling-group-toggle .toggle { align-self: flex-start; }
    .rs485-device-access-controls .rs485-polling-group-toggle > span { display: flex; flex-direction: column; gap: 2px; color: var(--ha-text); font-size: 12px; line-height: 1.2; }
    .rs485-device-access-controls .rs485-polling-group-toggle > span small { color: var(--ha-secondary); font-size: 10px; font-weight: 600; line-height: 1.2; }
    .rs485-polling-strip + .rs485-settings-polling-controls { margin-top: -1px; border-top: 0; border-radius: 0 0 8px 8px; }
    .rs485-polling-strip:has(+ .rs485-settings-polling-controls) { border-radius: 8px 8px 0 0; }
    .rs485-settings-polling-controls .polling-control-label { color: var(--ha-secondary); font-size: 11px; font-weight: 800; letter-spacing: .08em; text-transform: uppercase; }
    .rs485-polling-group-toggle { display: flex; align-items: center; justify-content: space-between; gap: 8px; min-width: 0; padding: 4px 6px 4px 10px; border: 1px solid var(--ha-row-border); border-radius: 7px; }
    .rs485-polling-group-toggle > span { min-width: 0; overflow: hidden; color: var(--ha-secondary); font-size: 11px; font-weight: 800; text-overflow: ellipsis; white-space: nowrap; }
    @media (max-width: 900px) { .rs485-settings-polling-controls { grid-template-columns: 1fr 1fr; } .rs485-settings-polling-controls .polling-control-label { grid-column: 1 / -1; } }
    .rs485-field {
      min-width: 0;
      display: grid;
      grid-template-rows: var(--rs485-label-height, 16px) var(--rs485-control-height, 42px);
      gap: 8px;
      align-items: end;
    }
    .rs485-field label {
      height: var(--rs485-label-height, 16px);
      margin: 0;
      color: var(--ha-text);
      line-height: var(--rs485-label-height, 16px);
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .rs485-field select,
    .rs485-field input {
      width: 100%;
      height: var(--rs485-control-height, 42px);
      min-height: var(--rs485-control-height, 42px);
      box-sizing: border-box;
      border: 1px solid var(--ha-row-border);
      border-radius: 8px;
      background: var(--ha-field);
      color: var(--ha-text);
      padding: 0 10px;
      font-weight: 700;
      line-height: var(--rs485-control-height, 42px);
      min-width: 0;
    }
    .rs485-field input[type="number"] { appearance: textfield; }
    .rs485-field input[type="number"]::-webkit-outer-spin-button,
    .rs485-field input[type="number"]::-webkit-inner-spin-button { appearance: none; margin: 0; }
    .rs485-field .mode-select { width: 100%; }
    .rs485-field .mode-trigger {
      width: 100%;
      height: var(--rs485-control-height, 42px);
      min-height: var(--rs485-control-height, 42px);
      box-sizing: border-box;
      display: flex;
      align-items: center;
      line-height: var(--rs485-control-height, 42px);
      padding-top: 0;
      padding-bottom: 0;
    }
    .rs485-actions { display: flex; flex-direction: column; gap: 6px; align-items: stretch; justify-content: flex-end; flex-wrap: nowrap; height: auto; align-self: end; min-width: 0; }
    .rs485-actions .rs485-connection-polling { width: 100%; min-width: 0; margin-left: 0; justify-content: flex-end; box-sizing: border-box; }
    .rs485-actions > #rs485-scan-button { width: 100%; min-width: 0; height: var(--rs485-control-height, 42px); min-height: var(--rs485-control-height, 42px); box-sizing: border-box; display: inline-flex; align-items: center; justify-content: center; }
    #rs485-section button:not(.toggle):not(.mode-trigger):not(.mode-option) {
      transform: none !important;
      transition: none !important;
      box-shadow: none;
      will-change: auto;
    }
    #rs485-section button:not(.toggle):not(.mode-trigger):not(.mode-option):hover,
    #rs485-section button:not(.toggle):not(.mode-trigger):not(.mode-option):focus-visible {
      border-color: var(--ha-primary);
      background: transparent;
      color: var(--ha-primary);
      box-shadow: none;
      outline: none;
    }
    #rs485-section button:not(.toggle):not(.mode-trigger):not(.mode-option):disabled,
    #rs485-section button:not(.toggle):not(.mode-trigger):not(.mode-option):disabled:hover {
      border-color: #4a4a4a;
      background: transparent;
      color: #888;
      box-shadow: inset 0 0 0 1px transparent;
    }
    .rs485-local-mode { display: none; align-items: center; justify-content: flex-end; gap: 8px; margin-top: 10px; }
    .rs485-local-mode.visible { display: flex; }
    .rs485-local-mode button { height: 30px; min-height: 30px; min-width: 82px; padding: 0 12px; display: inline-flex; align-items: center; justify-content: center; }
    .rs485-layout { display: grid; grid-template-columns: minmax(320px, .72fr) minmax(0, 1.28fr); gap: 16px; align-items: start; }
    .rs485-layout > div { display: flex; flex-direction: column; gap: 16px; }
    #rs485-device-detail { margin-top: 16px; }
    #rs485-device-list-panel { order: 1; }
    #rs485-left-scan-service { display: none !important; }
    .rs485-scan-service { order: 2; }
    .rs485-service-panel:last-of-type { order: 3; }
    .rs485-panel { border: 1px solid var(--ha-card-border); border-radius: 12px; background: var(--ha-surface); padding: 16px; min-width: 0; }
    .rs485-panel + .rs485-panel { margin-top: 16px; }
    #rs485-scan-results-panel { display: none; }
    .rs485-service-panel { padding: 0; overflow: hidden; }
    .rs485-service-panel summary { cursor: pointer; list-style: none; padding: 12px 16px; color: var(--ha-text); font-weight: 800; }
    .rs485-service-panel summary::-webkit-details-marker { display: none; }
    .rs485-service-panel summary::before { content: '+'; display: inline-block; width: 18px; color: var(--ha-primary); }
    .rs485-service-panel[open] summary::before { content: '-'; }
    .rs485-service-panel summary span { float: right; color: var(--ha-secondary); font-size: 12px; font-weight: 600; }
    .rs485-service-panel[open] > :not(summary) { margin-left: 16px; margin-right: 16px; }
    .rs485-service-panel[open] > .rs485-panel-head { margin-top: 12px; }
    .rs485-service-panel[open] > .rs485-console-log { margin-bottom: 16px; }
    .rs485-panel-title { color: var(--ha-secondary); font-size: 12px; font-weight: 800; letter-spacing: .12em; text-transform: uppercase; margin-bottom: 14px; }
    .rs485-panel-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 14px; }
    .rs485-panel-head .rs485-panel-title { margin-bottom: 0; }
    .rs485-clear-button { min-height: 30px; min-width: 64px; padding: 4px 10px; }
    .rs485-table { display: grid; gap: 10px; }
    .rs485-table-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: center; gap: 12px; min-height: 42px; border: 1px solid var(--ha-row-border); border-radius: 8px; padding: 8px 10px; background: var(--ha-row); }
    .rs485-table-row.scan { grid-template-columns: auto minmax(80px, 1fr) auto auto; }
    .rs485-table-row.scan > span:nth-child(3) { overflow: visible; white-space: nowrap; }
    .rs485-table-row.configured { grid-template-columns: minmax(0, 1fr) auto; min-height: 48px; align-items: center; }
    .rs485-table-row.configured { cursor: pointer; }
    .rs485-table-row.configured:focus-visible { outline: 2px solid var(--ha-primary); outline-offset: 2px; }
    .rs485-table-row.selected { border-color: var(--ha-primary); background: var(--ha-row); }
    .rs485-table-row button { min-width: 76px; }
    .rs485-table-row span { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .rs485-row-actions { display: flex; justify-content: flex-end; gap: 8px; flex-wrap: wrap; min-width: 0; align-items: center; }
    .rs485-row-actions button { min-width: 78px; height: 42px; min-height: 42px; box-sizing: border-box; display: inline-flex; align-items: center; justify-content: center; }
    .rs485-table-row.configured { grid-template-columns: 28px minmax(0, 1fr) auto; }
    .rs485-device-list-head { display: grid; grid-template-columns: 20px minmax(120px, 1.4fr) 60px 92px 50px; align-items: center; gap: 8px; }
    .rs485-table-row.configured { display: grid; grid-template-columns: 20px minmax(120px, 1.4fr) 60px 92px 50px; align-items: center; gap: 8px; }
    .rs485-table-row.configured > * { min-width: 0; }
    .rs485-table-row.configured.discovered { grid-template-columns: 24px minmax(0, 1.25fr) 42px minmax(84px, 1.1fr) 78px auto; }
    .rs485-table-row.configured.discovered { grid-template-columns: 20px minmax(120px, 1.4fr) 60px 92px 50px; gap: 8px; padding: 6px 8px; min-height: 40px; font-size: 11px; }
    .rs485-table-row.configured.discovered .rs485-device-title,
    .rs485-table-row.configured.discovered .rs485-list-address,
    .rs485-table-row.configured.discovered .rs485-list-template,
    .rs485-table-row.configured.discovered .rs485-device-status { font-size: 11px; }
    .rs485-table-row.configured.discovered .rs485-device-status { gap: 4px; overflow: hidden; text-overflow: ellipsis; }
    .rs485-table-row.configured.discovered .rs485-device-status::before { width: 8px; height: 8px; flex-basis: 8px; }
    .rs485-table-row.configured .rs485-device-title, .rs485-table-row.configured .rs485-list-template, .rs485-table-row.configured .rs485-device-status { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .rs485-device-list-head { min-height: 34px; padding: 0 10px; color: var(--ha-secondary); font-size: 12px; font-weight: 800; }
    .rs485-device-list-head span { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .rs485-list-address, .rs485-list-template { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--ha-secondary); font-size: 13px; }
    .rs485-device-status { display: inline-flex; align-items: center; gap: 9px; min-width: 0; color: var(--ha-success-text); font-size: 13px; font-weight: 800; letter-spacing: .02em; text-transform: uppercase; white-space: nowrap; }
    .rs485-device-status::before { content: ''; width: 12px; height: 12px; flex: 0 0 12px; border-radius: 50%; background: #00e889; box-shadow: 0 0 0 1px rgba(0, 232, 137, .18); }
    .rs485-device-status.offline { color: #b5c9e5; }
    .rs485-device-status.offline::before { background: #a8bdd9; box-shadow: none; }
    .rs485-device-status.unknown { color: #ffc107; }
    .rs485-device-status.unknown::before { background: #ffc107; box-shadow: none; }
    .rs485-device-status.discovered { color: #159bff; }
    .rs485-device-status.discovered::before { background: #159bff; box-shadow: none; }
    .rs485-row-menu { min-width: 28px !important; width: 28px; height: 32px !important; padding: 0 !important; border: 0 !important; font-size: 22px; }
    .rs485-row-add { min-width: 52px !important; height: 30px !important; min-height: 30px !important; padding: 4px 7px !important; font-size: 12px; }
    .rs485-device-summary { display: flex; align-items: baseline; gap: 14px; min-width: 0; }
    .rs485-device-index { color: var(--ha-secondary); font-size: 12px; font-weight: 800; text-align: center; }
    #rs485-device-list-panel .rs485-table { max-height: 620px; overflow-y: auto; overflow-x: hidden; padding-right: 2px; }
    .rs485-device-title { display: flex; align-items: center; gap: 8px; color: var(--ha-text); font-size: 15px; font-weight: 800; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .rs485-list-icon { width: 28px; height: 28px; object-fit: contain; flex: 0 0 28px; }
    .rs485-discovered-icon { display: inline-grid; place-items: center; width: 28px; height: 28px; flex: 0 0 28px; border: 1px dashed var(--ha-secondary); border-radius: 5px; color: var(--ha-secondary); font-size: 0; }
    .rs485-pagination { display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 8px 2px 0; color: var(--ha-secondary); font-size: 12px; }
    .rs485-pagination button { min-height: 30px; padding: 5px 10px; }
    .rs485-device-meta { display: flex; gap: 12px; flex-wrap: nowrap; color: var(--ha-secondary); font-size: 13px; line-height: 1.35; min-width: 0; }
    .rs485-device-list-toolbar { display: grid; gap: 10px; margin-bottom: 12px; }
    .rs485-device-list-toolbar input { width: 100%; height: 38px; box-sizing: border-box; border: 1px solid var(--ha-border); border-radius: 8px; background: var(--ha-card); color: var(--ha-text); padding: 0 10px; }
    .rs485-device-filters { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 8px; }
    .rs485-device-filters button { display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 1px; min-width: 0; min-height: 38px; padding: 6px 8px; border: 1px solid var(--ha-row-border); border-radius: 7px; background: var(--ha-row); color: var(--ha-secondary); font-size: 12px; font-weight: 800; line-height: 1.1; }
    .rs485-device-filters button span { display: block; color: var(--ha-text); font-size: 14px; line-height: 1; }
    .rs485-device-filters button.active { border-color: var(--ha-primary); color: var(--ha-text); background: var(--ha-field); box-shadow: inset 0 0 0 1px var(--ha-primary); }
    .rs485-status-filter { position: relative; }
    .rs485-status-filter summary { display: grid; place-items: center; width: 38px; height: 38px; border: 1px solid var(--ha-row-border); border-radius: 7px; color: var(--ha-secondary); cursor: pointer; list-style: none; }
    .rs485-status-filter summary::-webkit-details-marker { display: none; }
    .rs485-status-filter[open] summary { border-color: var(--ha-primary); color: var(--ha-primary); }
    .rs485-status-filter-menu { position: absolute; right: 0; top: 44px; z-index: 20; display: grid; gap: 4px; min-width: 130px; padding: 6px; border: 1px solid var(--ha-border); border-radius: 8px; background: var(--ha-card); box-shadow: 0 8px 20px rgba(0,0,0,.28); }
    .rs485-status-filter-menu button { min-height: 30px; text-align: left; }
    .rs485-device-heading { display: flex; align-items: center; gap: 10px; min-width: 0; color: var(--ha-text); font-size: 20px; font-weight: 800; }
    .rs485-device-heading .rs485-device-count-badge { display: inline-flex; align-items: center; justify-content: center; min-width: 40px; height: 32px; padding: 0 10px; border-radius: 999px; background: #163c61; color: #b9ddff; font-size: 16px; line-height: 1; }
    .rs485-device-list-actions { display: flex; align-items: center; gap: 10px; }
    .rs485-device-list-actions .rs485-add-device { min-height: 42px; }
    .rs485-device-list-actions .rs485-list-menu { min-width: 42px; width: 42px; height: 42px; padding: 0; font-size: 24px; }
    .rs485-device-facts { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 10px; }
    .rs485-device-facts span { display: grid; gap: 3px; min-width: 0; padding: 8px 10px; border: 1px solid var(--ha-row-border); border-radius: 8px; background: var(--ha-row); }
    .rs485-device-facts strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .rs485-device-facts small { color: var(--ha-secondary); font-size: 11px; }
    .rs485-label { color: var(--ha-secondary); font-size: 12px; font-weight: 800; letter-spacing: .04em; text-transform: uppercase; }
    .rs485-primary { font-weight: 800; color: var(--ha-text); }
    .rs485-empty { color: var(--ha-secondary); font-size: 13px; line-height: 1.45; border: 1px solid var(--ha-row-border); border-radius: 8px; padding: 12px; background: var(--ha-row); overflow-wrap: anywhere; }
    .rs485-console-log {
      display: block;
      margin: 10px 0 0;
      min-height: calc(1.45em * 5 + 24px);
      max-height: 280px;
      overflow: auto;
      border: 1px solid var(--ha-row-border);
      border-radius: 8px;
      background: var(--ha-pre);
      color: var(--ha-text);
      padding: 12px;
      font-family: ui-monospace, "SFMono-Regular", Consolas, monospace;
      font-size: 12px;
      line-height: 1.45;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }
    .rs485-detail-head { position: relative; display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 12px 18px; margin-bottom: 16px; }
    .rs485-device-identity { display: grid; grid-template-columns: 168px minmax(0, 1fr); align-items: start; gap: 18px; min-width: 0; transform: translate(-12px, -8px); }
    .rs485-device-icon { width: 168px; height: 136px; object-fit: contain; flex: 0 0 168px; border-radius: 8px; }
    .rs485-device-title-row { display: flex; align-items: center; gap: 12px; min-width: 0; margin-top: 0; }
    .rs485-device-title-row .module-title { white-space: nowrap; font-size: 32px; line-height: 1.1; margin: 0; }
    .rs485-device-identity-copy { min-width: 0; }
    .rs485-device-subtitle { margin-top: 4px; color: var(--ha-secondary); font-size: 14px; }
    .rs485-device-identity-facts { display: grid; gap: 2px; margin-top: 12px; color: var(--ha-secondary); font-size: 13px; line-height: 1.35; }
    .rs485-device-identity-facts > span { display: grid; grid-template-columns: 80px minmax(0, 1fr); gap: 8px; align-items: baseline; }
    .rs485-device-identity-facts > span > span { display: block; color: var(--ha-secondary); white-space: nowrap; }
    .rs485-device-identity-facts strong { color: var(--ha-text); font-weight: 400; }
    .rs485-device-connection { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin-top: 12px; color: var(--ha-secondary); font-size: 13px; }
    .rs485-device-connection span + span::before { content: '·'; margin-right: 12px; color: var(--ha-border); }
    .rs485-edit-device { width: 42px; height: 42px; min-width: 42px; padding: 0; font-size: 22px; }
    .rs485-detail-actions { display: flex; align-items: flex-start; justify-content: flex-end; gap: 18px; flex-wrap: nowrap; }
    .rs485-detail-status { display: inline-flex; align-items: center; gap: 8px; min-height: 34px; padding: 0 4px 0 14px; border-left: 1px solid var(--ha-border); color: var(--ha-success-text); font-size: 12px; font-weight: 800; letter-spacing: .05em; text-transform: uppercase; white-space: nowrap; }
    .rs485-detail-status::before { content: ''; width: 8px; height: 8px; border-radius: 50%; background: currentColor; box-shadow: 0 0 0 4px color-mix(in srgb, currentColor 16%, transparent); }
    .rs485-detail-status.offline { color: var(--ha-danger-text); }
    .rs485-detail-status.unknown { color: #ffc107; }
    .rs485-device-menu { width: 42px; height: 42px; min-width: 42px; padding: 0; font-size: 24px; }
    .rs485-detail-action-menu { position: relative; }
    .rs485-detail-action-menu > summary,
    .rs485-template-menu > summary { display: grid; place-items: center; width: 42px; height: 42px; margin: 0; padding: 0; border: 0; outline: none; background: transparent; color: var(--ha-primary); cursor: pointer; list-style: none; appearance: none; }
    .rs485-menu-dots { display: inline-flex; flex-direction: column; align-items: center; justify-content: center; gap: 4px; width: 6px; height: 22px; }
    .rs485-menu-dots i { display: block; width: 4px; height: 4px; flex: 0 0 4px; border-radius: 50%; background: currentColor; }
    .rs485-detail-action-menu > summary:focus,
    .rs485-detail-action-menu > summary:focus-visible { border: 0; outline: none; box-shadow: none; background: transparent; }
    .rs485-detail-action-menu > summary::-webkit-details-marker { display: none; }
    .rs485-detail-action-menu-panel { position: absolute; right: 0; top: 46px; z-index: 30; display: grid; gap: 2px; min-width: 170px; padding: 6px; border: 1px solid var(--ha-border); border-radius: 8px; background: var(--ha-card); box-shadow: none; }
    .rs485-detail-action-menu-panel button,
    .rs485-detail-action-menu-panel button.danger-button { width: 100%; min-height: 32px; padding: 7px 10px; border: 0 !important; border-radius: 6px; background: transparent !important; color: var(--ha-text); box-shadow: none !important; text-align: left; }
    .rs485-detail-action-menu-panel button:hover { background: var(--ha-field) !important; color: var(--ha-primary); }
    .rs485-detail-action-menu-panel button.danger-button { color: var(--ha-danger-text); }
    .rs485-detail-action-menu-panel button:focus,
    .rs485-detail-action-menu-panel button:focus-visible { outline: none; box-shadow: none; }
    .rs485-edit-device, .rs485-device-menu,
    .rs485-edit-device:hover, .rs485-edit-device:focus-visible,
    .rs485-device-menu:hover, .rs485-device-menu:focus-visible { border: 0 !important; background: transparent !important; box-shadow: none !important; color: var(--ha-primary); outline: none; }
    .rs485-detail-actions .danger-button { min-height: 42px; padding: 8px 16px; color: #ff3b30; border-color: #ff3b30; background: transparent; }
    .rs485-detail-last-seen { position: absolute; right: 0; top: 34px; color: var(--ha-secondary); font-size: 13px; }
    .rs485-detail-stack { display: flex; flex-direction: column; gap: 16px; }
    .rs485-detail-stack > .rs485-template-strip { order: 1; }
    .rs485-detail-stack > .rs485-device-facts { order: 2; }
    .rs485-detail-stack > .rs485-detail-tabs { order: 3; }
    .rs485-detail-stack > .rs485-tab-panel { order: 4; }
    .rs485-detail-stack > .rs485-device-log { order: 5; }
    .rs485-detail-stack > .rs485-polling-strip { order: 1; }
    .rs485-template-strip { display: grid; grid-template-columns: minmax(0, 1fr); gap: 12px; align-items: center; border: 1px solid var(--ha-row-border); border-radius: 10px; padding: 10px 12px; background: var(--ha-row); }
    .rs485-template-strip strong { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .rs485-template-strip span { color: var(--ha-secondary); font-size: 12px; font-weight: 800; letter-spacing: .04em; text-transform: uppercase; white-space: nowrap; }
    .rs485-detail-tabs { grid-column: 1 / -1; width: min(100%, 780px); min-width: 0; justify-self: start; margin-top: 0; display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 2px; overflow: hidden; border: 1px solid var(--ha-row-border); border-radius: 8px; background: var(--ha-row); }
    .rs485-detail-tabs button { min-width: 0; min-height: 44px; padding: 7px 9px; overflow: hidden; border: 0 !important; border-radius: 0 !important; background: transparent !important; color: var(--ha-secondary) !important; font-size: 12px; font-weight: 800; white-space: nowrap; text-overflow: ellipsis; box-shadow: none !important; }
    .rs485-detail-tabs button::before { display: inline-block; margin-right: 10px; color: currentColor; font-size: 18px; vertical-align: -1px; }
    .rs485-detail-tabs button:nth-child(1)::before { content: '▦'; }
    .rs485-detail-tabs button:nth-child(2)::before { content: '⚙'; }
    .rs485-detail-tabs button:nth-child(3)::before { content: '▤'; }
    .rs485-detail-tabs button:nth-child(4)::before { content: '☷'; }
    .rs485-detail-tabs button.active { background: var(--ha-primary) !important; color: #fff !important; }
    .rs485-detail-tabs button:hover, .rs485-detail-tabs button:focus-visible { background: var(--ha-field) !important; color: var(--ha-text) !important; outline: none; }
    .rs485-detail-tabs button.active:hover, .rs485-detail-tabs button.active:focus-visible { background: var(--ha-primary) !important; color: #fff !important; }
    .rs485-tab-panel.hidden { display: none; }
    .rs485-logs-panel .rs485-diagnostics-cards { display: none; }
    .rs485-logs-panel .rs485-traffic-panel { order: 1; }
    .rs485-logs-component { border: 1px solid var(--ha-row-border); border-radius: 10px; background: var(--ha-row); overflow: hidden; }
    .rs485-logs-toolbar { display: grid; grid-template-columns: repeat(5, minmax(110px, 1fr)); gap: 8px; padding: 12px; border-bottom: 1px solid var(--ha-row-border); align-items: center; }
    .rs485-logs-toolbar select, .rs485-logs-toolbar button { box-sizing: border-box; min-height: 42px; border: 1px solid var(--ha-border); border-radius: 8px; background: var(--ha-field); color: var(--ha-text); font: inherit; font-size: 12px; font-weight: 800; }
    .rs485-logs-toolbar select { grid-row: 2; width: 100%; min-width: 0; padding: 0 30px 0 10px; appearance: auto; }
    .rs485-logs-toolbar button { padding: 8px 14px; color: var(--ha-primary); border-color: var(--ha-primary); background: transparent; white-space: nowrap; }
    .rs485-logs-toolbar button:hover, .rs485-logs-toolbar button:focus-visible { background: var(--ha-primary); color: #fff; outline: none; }
    .rs485-logs-toolbar label { display: inline-flex; align-items: center; gap: 7px; min-height: 42px; color: var(--ha-text); font-size: 12px; font-weight: 800; white-space: nowrap; }
    .rs485-logs-actions { grid-column: 1 / -1; grid-row: 1; display: flex; align-items: center; justify-content: flex-end; gap: 8px; min-height: 42px; }
    .rs485-logs-actions button { width: auto; min-width: 0; flex: 0 0 auto; padding: 8px 14px; }
    .rs485-logs-toolbar input[type="checkbox"] { width: 16px; height: 16px; accent-color: var(--ha-primary); }
    .rs485-logs-table { max-height: 430px; overflow-y: auto; overflow-x: hidden; }
    .rs485-logs-head, .rs485-log-row { display: grid; grid-template-columns: 38px 72px minmax(62px, .8fr) 42px minmax(58px, .8fr) minmax(70px, 1fr) minmax(68px, .9fr) 58px 68px 68px minmax(80px, 1.2fr); gap: 5px; align-items: center; min-width: 0; padding: 6px 8px; }
    .rs485-logs-head { position: sticky; top: 0; z-index: 1; color: var(--ha-secondary); font-size: 10px; font-weight: 800; border-bottom: 1px solid var(--ha-row-border); background: var(--ha-row); }
    .rs485-log-row { border-bottom: 1px solid var(--ha-row-border); font-size: 11px; }
    .rs485-log-row:last-child { border-bottom: 0; }
    .rs485-log-row .error { color: var(--ha-error); font-weight: 800; }
    .rs485-log-row details { min-width: 0; }
    .rs485-log-row details div { white-space: normal; color: var(--ha-secondary); }
    .rs485-log-empty { padding: 48px 20px; text-align: center; color: var(--ha-secondary); }
    .rs485-diagnostics-shell:not(.rs485-logs-panel) .rs485-traffic-panel { display: flex; flex-direction: column; }
    .rs485-diagnostics-shell:not(.rs485-logs-panel) .rs485-traffic-head { order: 1; }
    .rs485-diagnostics-shell:not(.rs485-logs-panel) .rs485-traffic-filters { order: 2; }
    .rs485-diagnostics-shell:not(.rs485-logs-panel) .rs485-manual-command { order: 3; }
    .rs485-diagnostics-shell:not(.rs485-logs-panel) .rs485-traffic-table-head { order: 4; }
    .rs485-diagnostics-shell:not(.rs485-logs-panel) .rs485-traffic-empty { order: 5; }
    .rs485-diagnostics-shell:not(.rs485-logs-panel) .rs485-diagnostics-footer { order: 6; }
    .rs485-diagnostics-shell:not(.rs485-logs-panel) .rs485-traffic-filters { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; align-items: center; }
    .rs485-diagnostics-shell:not(.rs485-logs-panel) .rs485-traffic-filters > .mode-select:nth-child(2),
    .rs485-diagnostics-shell:not(.rs485-logs-panel) .rs485-traffic-filters > .mode-select:nth-child(5) { display: none; }
    .rs485-diagnostics-shell:not(.rs485-logs-panel) .rs485-traffic-filters > .mode-select { grid-row: 1; width: 100% !important; min-width: 0; flex: none; box-sizing: border-box; }
    .rs485-diagnostics-shell:not(.rs485-logs-panel) .rs485-traffic-filters > button { grid-row: 2; justify-self: start; margin-left: 0; }
    .rs485-diagnostics-shell:not(.rs485-logs-panel) .rs485-traffic-filters > button:nth-last-child(3) { grid-column: 2; }
    .rs485-diagnostics-shell:not(.rs485-logs-panel) .rs485-traffic-filters > button:nth-last-child(2) { grid-column: 3; }
    .rs485-diagnostics-shell:not(.rs485-logs-panel) .rs485-traffic-filters > button:nth-last-child(1) { grid-column: 4; }
    .rs485-diagnostics-shell:not(.rs485-logs-panel) .rs485-traffic-head { align-items: start; }
    .rs485-diagnostics-shell:not(.rs485-logs-panel) .rs485-traffic-actions { display: flex; flex-direction: column; align-items: flex-end; gap: 8px; align-self: start; }
    .rs485-diagnostics-shell:not(.rs485-logs-panel) .rs485-export-actions-row,
    .rs485-diagnostics-shell:not(.rs485-logs-panel) .rs485-primary-actions-row { display: flex; justify-content: flex-end; gap: 8px; }
    .rs485-diagnostics-shell:not(.rs485-logs-panel) .rs485-traffic-actions button { width: auto; min-width: 0; }
    @media (max-width: 800px) { .rs485-logs-toolbar { grid-template-columns: repeat(2, minmax(120px, 1fr)); } }
    .rs485-polling-strip.hidden { display: none; }
    .rs485-scan-banner { display: none; min-height: 42px; grid-template-columns: auto minmax(180px, 1fr) auto repeat(3, auto); align-items: center; gap: 18px; padding: 8px 18px; border: 1px solid var(--ha-row-border); border-radius: 8px; background: var(--ha-row); color: var(--ha-secondary); font-size: 13px; }
    .rs485-scan-banner.active { display: grid; }
    .rs485-progress-label { color: var(--ha-text); font-weight: 800; white-space: nowrap; }
    .rs485-progress-track { height: 14px; overflow: hidden; border-radius: 999px; background: var(--ha-border); }
    .rs485-progress-fill { display: block; width: 0; height: 100%; border-radius: inherit; background: var(--ha-primary); transition: width .2s ease; }
    .rs485-progress-count, .rs485-progress-stat strong { color: var(--ha-text); white-space: nowrap; }
    .rs485-progress-stat { display: inline-flex; gap: 4px; min-width: 0; white-space: nowrap; }
    .rs485-scan-primary.stop { width: 180px; min-width: 180px; border-color: var(--ha-error); color: var(--ha-error); background: transparent; }
    .rs485-scan-primary .rs485-stop-square { display: inline-block; width: 14px; height: 14px; margin-right: 9px; background: currentColor; vertical-align: -2px; }
    @media (max-width: 1400px) { .rs485-scan-banner { grid-template-columns: auto minmax(160px, 1fr) auto repeat(3, minmax(118px, auto)); gap: 12px; } }
    @media (max-width: 1100px) { .rs485-scan-banner { grid-template-columns: 1fr 1fr; } .rs485-progress-track { grid-column: 1 / -1; grid-row: 2; } .rs485-io-head, .rs485-io-row { grid-template-columns: 52px minmax(150px, 1fr) minmax(150px, 1fr) minmax(130px, .8fr); } }
    @media (max-width: 620px) { .rs485-scan-banner { grid-template-columns: 1fr; gap: 8px; } .rs485-progress-track { grid-column: auto; grid-row: auto; } }
    .rs485-device-log { position: relative; display: grid; grid-template-columns: minmax(0, 1fr); gap: 0; border: 1px solid var(--ha-row-border); border-radius: 10px; background: var(--ha-row); padding: 0 12px; }
    .rs485-global-scan-log { margin-top: 10px; border: 1px solid var(--ha-row-border); border-radius: 10px; background: var(--ha-row); overflow: visible; }
    .rs485-global-scan-log > .rs485-device-log > summary { margin: 0 -12px; background: var(--ha-field); border-radius: 10px 10px 0 0; padding-left: 24px; padding-right: 140px; }
    .rs485-global-scan-log > .rs485-device-log > summary span { color: var(--ha-text); }
    .rs485-global-scan-log > .rs485-log-footer { background: var(--ha-field); border-top-color: var(--ha-card-border); border-radius: 0 0 10px 10px; }
    .rs485-global-scan-log .rs485-empty { border: 0; border-radius: 0; padding: 10px 12px 14px; background: transparent; }
    .rs485-global-scan-log > .rs485-device-log { border: 0; border-radius: 10px 10px 0 0; background: transparent; }
    .rs485-global-scan-log > .rs485-device-log[open] .rs485-console-log { height: 220px; max-height: 220px; }
    .rs485-device-log summary { grid-column: 1; grid-row: 1; display: flex; align-items: center; justify-content: flex-start; gap: 12px; min-height: 58px; cursor: pointer; list-style: none; padding: 12px 0; padding-right: 116px; color: var(--ha-text); font-weight: 800; }
    .rs485-device-log summary::-webkit-details-marker { display: none; }
    .rs485-device-log summary:focus, .rs485-device-log summary:focus-visible,
    .rs485-device-log:focus, .rs485-device-log:focus-visible { outline: none; box-shadow: none; }
    .rs485-device-log summary::before { content: '+'; display: inline-block; width: 18px; color: var(--ha-primary); }
    .rs485-device-log[open] summary::before { content: '-'; }
    .rs485-device-log summary span { color: var(--ha-secondary); font-size: 12px; font-weight: 600; white-space: nowrap; }
    .rs485-device-log > .rs485-clear-button { position: absolute; right: 12px; top: 9px; z-index: 1; }
    .rs485-device-log .rs485-console-log { grid-column: 1 / -1; grid-row: 2; margin: 0 -12px; max-height: 246px; overflow: auto; padding: 0 12px 8px; border: 0; border-radius: 0; background: transparent; white-space: normal; scrollbar-gutter: stable; }
    .rs485-log-entry { display: grid; grid-template-columns: 76px 22px minmax(0, 1fr); gap: 8px; align-items: center; min-height: 27px; padding: 2px 0; border-top: 1px solid color-mix(in srgb, var(--ha-row-border) 55%, transparent); color: var(--ha-secondary); font-size: 13px; }
    .rs485-log-entry:first-child { border-top: 0; }
    .rs485-log-time { color: var(--ha-secondary); font-variant-numeric: tabular-nums; }
    .rs485-log-icon { display: inline-grid; place-items: center; width: 16px; height: 16px; border-radius: 50%; color: var(--ha-primary); font-weight: 900; }
    .rs485-log-entry.matched .rs485-log-icon { color: var(--ha-success, #00e676); }
    .rs485-log-entry.timeout .rs485-log-icon { color: var(--ha-warning, #ffc107); }
    .rs485-log-entry.stopped .rs485-log-icon { color: var(--ha-error); }
    .rs485-log-entry.progress .rs485-log-icon { color: var(--ha-primary); }
    .rs485-log-message { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .rs485-log-footer { display: block; overflow-x: auto; padding: 10px 12px; border-top: 1px solid var(--ha-row-border); color: var(--ha-secondary); }
    .rs485-log-status { display: grid; grid-template-columns: auto auto minmax(150px, 1fr) auto auto auto auto auto; gap: 10px; align-items: center; min-width: max-content; white-space: nowrap; }
    .rs485-log-status > span, .rs485-log-status > strong { font-size: 12px; }
    .rs485-log-status > span:last-child { min-width: 112px; text-align: right; }
    .rs485-log-status .rs485-progress-track { width: 100%; min-width: 150px; }
    .rs485-log-footer > span { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12px; }
    .rs485-log-footer strong { color: var(--ha-text); }
    .rs485-log-footer .active { color: var(--ha-success, #00e676); font-weight: 800; }
    @media (max-width: 980px) { .rs485-log-status { grid-template-columns: auto auto minmax(150px, 1fr) auto auto auto auto auto; } }
    .rs485-diagnostics-panel { border: 1px solid var(--ha-row-border); border-radius: 10px; padding: 14px; background: var(--ha-row); }
    .rs485-diagnostics-shell { display: flex; flex-direction: column; gap: 16px; }
    .rs485-diagnostics-cards { order: 2; display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; align-items: stretch; }
    .rs485-diagnostics-card { display: flex; min-width: 0; flex-direction: column; border: 1px solid var(--ha-row-border); border-radius: 10px; padding: 16px; background: var(--ha-row); }
    .rs485-diagnostics-card-head { display: flex; align-items: center; gap: 10px; min-height: 24px; margin-bottom: 14px; color: var(--ha-text); font-size: 17px; font-weight: 800; }
    .rs485-diagnostics-card-head .diag-icon { color: var(--ha-primary); font-size: 22px; }
    .rs485-diagnostics-online { display: inline-flex; align-items: center; min-height: 28px; color: var(--ha-success, #00e676); font-size: 15px; font-weight: 900; }
    .rs485-diagnostics-online.offline { color: var(--ha-secondary); }
    .rs485-diagnostics-subtitle { margin: 2px 0 14px 28px; color: var(--ha-secondary); }
    .rs485-diagnostics-list .rs485-diagnostics-status-row { display: block; padding: 10px 12px 9px; }
    .rs485-diagnostics-status-row .rs485-diagnostics-online { min-height: 24px; }
    .rs485-diagnostics-status-row .rs485-diagnostics-subtitle { display: inline; margin-left: 8px; }
    .rs485-diagnostics-list { display: grid; flex: 1; align-content: start; gap: 1px; overflow: hidden; border: 1px solid var(--ha-row-border); border-radius: 8px; font-size: 13px; line-height: 1.35; }
    .rs485-diagnostics-list > div { display: grid; grid-template-columns: minmax(0, 1fr) minmax(100px, 1fr); gap: 12px; padding: 6px 10px; background: color-mix(in srgb, var(--ha-field) 55%, transparent); }
    .rs485-diagnostics-list > div:nth-child(even) { background: color-mix(in srgb, var(--ha-row) 72%, transparent); }
    .rs485-diagnostics-list span { color: var(--ha-secondary); }
    .rs485-diagnostics-list strong { min-width: 0; overflow-wrap: anywhere; white-space: normal; font-size: 13px; }
    .rs485-diagnostics-list strong[title] { white-space: normal; overflow-wrap: anywhere; text-overflow: clip; }
    .rs485-diagnostics-card:first-child .rs485-diagnostics-list > div:nth-child(4) { display: none; }
    .rs485-diagnostics-card:nth-child(2) .rs485-diagnostics-list > div:nth-child(3),
    .rs485-diagnostics-card:nth-child(2) .rs485-diagnostics-list > div:nth-child(5) { display: none; }
    .rs485-traffic-panel { position: relative; order: 1; min-width: 0; overflow: hidden; border: 1px solid var(--ha-row-border); border-radius: 10px; background: var(--ha-row); }
    .rs485-traffic-filters { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; padding: 9px 14px; border-bottom: 1px solid var(--ha-row-border); background: var(--ha-row); }
    .rs485-traffic-filters .mode-select { width: 150px; flex: 0 0 150px; }
    .rs485-traffic-filters .mode-select:nth-child(2) { width: 140px; flex-basis: 140px; }
    .rs485-traffic-filters .mode-select:nth-child(3) { width: 150px; flex-basis: 150px; }
    .rs485-traffic-filters .mode-trigger { width: 100%; min-height: 42px; height: 42px; box-sizing: border-box; padding: 0 36px 0 10px; font-size: 13px; }
    .rs485-traffic-filters button { min-height: 32px; padding: 5px 12px; font-size: 12px; }
    .rs485-traffic-filters button:nth-last-child(2) { margin-left: auto; }
    .rs485-traffic-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 12px 14px; border-bottom: 1px solid var(--ha-row-border); }
    .rs485-traffic-title { color: var(--ha-text); font-size: 17px; font-weight: 800; }
    .rs485-traffic-title span { color: var(--ha-secondary); font-size: 12px; font-weight: 600; }
    .rs485-traffic-actions { display: flex; gap: 8px; }
    .rs485-traffic-actions button { min-height: 34px; padding: 6px 12px; }
    .rs485-traffic-actions button { min-width: 76px; cursor: pointer; }
    .rs485-manual-command { display: none; margin: 16px 14px 14px; border: 1px solid var(--ha-row-border); border-radius: 8px; background: var(--ha-card); overflow: visible; }
    .rs485-manual-command.open { display: block; }
    .rs485-manual-command .hidden { display: none !important; }
    .rs485-manual-command-head { display: flex; align-items: center; justify-content: space-between; padding: 11px 14px; border-bottom: 1px solid var(--ha-row-border); color: var(--ha-text); font-weight: 800; }
    .rs485-manual-command-head button { min-width: 32px; min-height: 30px; padding: 4px 8px; border: 0; background: transparent; color: var(--ha-secondary); box-shadow: none; }
    .rs485-manual-command-body { display: grid; gap: 14px; padding: 14px; }
    .rs485-manual-command-tabs { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 2px; padding: 2px; border: 1px solid var(--ha-row-border); border-radius: 7px; background: var(--ha-row); }
    .rs485-manual-command-tabs button { min-height: 34px; border: 0; background: transparent; color: var(--ha-secondary); box-shadow: none; }
    .rs485-manual-command-tabs button.active { background: var(--ha-primary); color: #fff; }
    .rs485-manual-command-fields { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
    .rs485-manual-command-fields label { margin: 0; color: var(--ha-secondary); font-size: 10px; font-weight: 600; letter-spacing: .04em; text-transform: uppercase; }
    .rs485-manual-command-fields input, .rs485-manual-command-fields select { width: 100%; box-sizing: border-box; height: 42px; min-height: 42px; margin-top: 4px; border: 1px solid var(--ha-row-border); border-radius: 7px; background: var(--ha-field); color: var(--ha-text); padding: 0 10px; }
    .rs485-manual-command-fields input:focus, .rs485-manual-command-fields input:focus-visible,
    .rs485-manual-command-fields select:focus, .rs485-manual-command-fields select:focus-visible { outline: none; box-shadow: none; border-color: var(--ha-primary); }
    .rs485-manual-function-select .mode-trigger { height: 42px; min-height: 42px; margin-top: 4px; padding: 0 36px 0 10px; font-size: 13px; }
    .rs485-manual-function-select .mode-trigger::after { top: 16px; right: 14px; }
    .rs485-manual-function-select .mode-menu { max-height: 260px; overflow-y: auto; }
    .rs485-manual-preview { padding: 9px 10px; border: 1px solid var(--ha-row-border); border-radius: 7px; background: var(--ha-pre); color: var(--ha-secondary); font: 12px ui-monospace, Consolas, monospace; }
    .rs485-manual-result { min-height: 16px; color: var(--ha-success); font-size: 11px; }
    .rs485-manual-command-submit { width: 100%; }
    .rs485-traffic-empty { height: 220px; max-height: 220px; overflow: auto; display: block; color: var(--ha-secondary); font-size: 13px; scrollbar-gutter: stable; }
    .rs485-traffic-empty.no-entries { display: grid; place-items: center; }
    .rs485-traffic-table-head, .rs485-traffic-entry { width: 100%; box-sizing: border-box; display: grid; grid-template-columns: 34px 76px 46px 68px 40px 68px 34px minmax(180px, 1fr) 34px 32px; gap: 5px; align-items: center; justify-content: start; padding: 6px 8px; text-align: left; color: var(--ha-secondary); font-size: 11px; }
    .rs485-traffic-table-head { min-width: 0; padding-right: 24px; border-bottom: 1px solid var(--ha-row-border); color: var(--ha-secondary); font-weight: 800; font-size: 11px; }
    .rs485-traffic-entry { min-width: 0; border-top: 1px solid var(--ha-row-border); }
    .rs485-traffic-table-head span, .rs485-traffic-entry > span { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .rs485-traffic-entry:first-child { border-top: 0; }
    .rs485-traffic-entry strong { color: var(--ha-success, #00e676); }
    .rs485-traffic-entry.error strong { color: var(--ha-error); }
    .rs485-traffic-entry .traffic-direction { display: inline-flex; align-items: center; justify-content: center; width: 68px; box-sizing: border-box; padding: 2px 6px; border: 1px solid color-mix(in srgb, var(--ha-primary) 45%, transparent); border-radius: 4px; color: var(--ha-primary); font-size: 11px; font-weight: 900; }
    .rs485-traffic-entry .traffic-direction.slave { border-color: color-mix(in srgb, var(--ha-success) 45%, transparent); color: var(--ha-success); }
    .rs485-traffic-detail { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 8px 14px; padding: 8px 12px 10px 42px; border-top: 1px solid var(--ha-row-border); background: color-mix(in srgb, var(--ha-row) 82%, var(--ha-card)); color: var(--ha-secondary); font-size: 11px; }
    .rs485-traffic-detail > div { display: grid; gap: 3px; min-width: 0; }
    .rs485-traffic-detail strong { color: var(--ha-text); }
    .rs485-traffic-detail button { align-self: start; white-space: nowrap; }
    .rs485-traffic-detail.hidden { display: none; }
    .rs485-traffic-entry { cursor: pointer; }
    .rs485-traffic-payload { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--ha-secondary); font-family: ui-monospace, SFMono-Regular, Consolas, monospace; }
    .rs485-traffic-payload.decoded { font-family: inherit; color: var(--ha-text); }
    .rs485-traffic-payload .hex-token { display: inline-block; margin-right: 4px; padding: 1px 3px; border-radius: 3px; color: #fff; }
    .rs485-traffic-payload .hex-slave { background: #087fbd; }
    .rs485-traffic-payload .hex-function { background: #7250a8; }
    .rs485-traffic-payload .hex-address { background: #159fc4; }
    .rs485-traffic-payload .hex-quantity, .rs485-traffic-payload .hex-byte-count { background: #7b8794; color: #f5f7fa; }
    .rs485-traffic-payload .hex-data { color: #fff; }
    .rs485-traffic-payload .hex-crc { background: #b56a20; }
    .rs485-traffic-payload .hex-exception { background: #b3261e; }
    .rs485-traffic-crc { color: var(--ha-success); }
    .rs485-diagnostics-footer { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 1px; border-top: 1px solid var(--ha-row-border); background: var(--ha-row-border); }
    .rs485-diagnostics-footer > div { display: flex; align-items: center; justify-content: center; gap: 6px; min-height: 44px; padding: 6px; background: var(--ha-row); color: var(--ha-secondary); font-size: 11px; }
    .rs485-diagnostics-footer strong { color: var(--ha-text); font-size: 11px; }
    .rs485-diagnostics-footer .rs485-diagnostics-online { display: inline-block; flex: 0 0 10px; width: 10px; height: 10px; margin: 0; font-size: 0; }
    .rs485-diagnostics-footer .rs485-diagnostics-online::before { display: block; width: 10px; height: 10px; box-shadow: 0 0 0 3px color-mix(in srgb, currentColor 18%, transparent); }
    .toggle { cursor: pointer; }
    .toggle > span { pointer-events: none; }
    @media (max-width: 800px) { .rs485-diagnostics-cards { grid-template-columns: 1fr; } .rs485-diagnostics-footer { grid-template-columns: repeat(2, minmax(0, 1fr)); } .rs485-manual-command-fields { grid-template-columns: 1fr; } }
    .rs485-diagnostics-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
    .rs485-diagnostics-grid > div { display: grid; gap: 4px; min-width: 0; }
    .rs485-diagnostics-grid span { color: var(--ha-secondary); font-size: 12px; font-weight: 800; text-transform: uppercase; }
    .rs485-diagnostics-grid strong { overflow-wrap: anywhere; }
    .rs485-io-table { position: relative; z-index: 1; border: 1px solid var(--ha-row-border); border-radius: 10px; background: var(--ha-row); }
    .rs485-io-table::after { content: ''; position: absolute; inset: 0; z-index: 60; border: 1px solid var(--ha-row-border); border-radius: 10px; pointer-events: none; }
    .rs485-io-head, .rs485-io-row { display: grid; grid-template-columns: 52px minmax(180px, 1fr) minmax(180px, 1fr) minmax(220px, 1fr); align-items: center; }
    .rs485-io-head { position: sticky; top: 0; z-index: 4; min-height: 44px; border-radius: 9px 9px 0 0; clip-path: inset(0 round 9px 9px 0 0); background: var(--ha-card); color: var(--ha-secondary); font-size: 12px; font-weight: 800; text-transform: uppercase; }
    .rs485-io-head span, .rs485-io-row > div { min-width: 0; padding: 8px 12px; }
    .rs485-io-row { position: relative; min-height: 58px; border-top: 1px solid var(--ha-row-border); background: var(--ha-row); }
    .rs485-io-row:has(.mode-select.open) { z-index: 50; }
    .rs485-io-row:has(.mode-select.open) > div { overflow: visible; }
    .rs485-io-row .mode-select.open { z-index: 100; }
    .rs485-io-head span + span { border-left: 1px solid var(--ha-row-border); }
    .rs485-io-row > div + div { border-left: 1px solid var(--ha-row-border); }
    .rs485-io-head span { height: 100%; display: flex; align-items: center; }
    .rs485-io-head span:first-child { border-top-left-radius: 9px; }
    .rs485-io-head span:last-child { border-top-right-radius: 9px; }
    .rs485-io-row:last-child > div:first-child { border-bottom-left-radius: 9px; }
    .rs485-io-row:last-child > div:last-child { border-bottom-right-radius: 9px; }
    .rs485-io-row:last-child { border-bottom-left-radius: 9px; border-bottom-right-radius: 9px; }
    .rs485-io-head span:first-child { justify-content: center; }
    .rs485-io-row > div:last-child { display: flex; align-items: center; justify-content: stretch; }
    .rs485-io-row > div:last-child .rs485-mode-row { width: 100%; }
    #rs485-section .rs485-io-gear { display: none; }
    .rs485-di-indicator { display: inline-flex; align-items: center; gap: 6px; color: var(--ha-secondary); font-size: 12px; font-weight: 800; white-space: nowrap; }
    .rs485-di-indicator::before { content: '○'; color: var(--ha-secondary); font-size: 18px; line-height: 1; }
    .rs485-di-indicator.active { color: var(--ha-success, #00e676); }
    .rs485-di-indicator.active::before { content: '●'; color: var(--ha-success, #00e676); }
    .rs485-io-channel { color: var(--ha-secondary); font-weight: 800; text-align: center; }
    .rs485-empty-cell { color: var(--ha-secondary); }
    .rs485-header-actions { margin-top: 34px; }
    .rs485-connection-intro { display: flex; align-items: center; gap: 12px; min-width: 220px; }
    .rs485-connection-polling { display: flex; align-items: center; justify-content: flex-end; gap: 8px; min-width: 150px; margin-left: auto; white-space: nowrap; }
    .rs485-connection-polling .rs485-polling-title { margin: 0; font-size: 11px; }
    .rs485-connection-intro strong, .rs485-connection-intro span { display: block; }
    .rs485-connection-intro strong { font-size: 16px; }
    .rs485-connection-intro span { margin-top: 3px; color: var(--ha-secondary); font-size: 12px; line-height: 1.35; }
    .rs485-bus-icon { display: grid; place-items: center; width: 58px; height: 38px; flex: 0 0 58px; box-sizing: border-box; border: 2px solid var(--ha-primary); border-radius: 8px; color: var(--ha-primary); font-size: 11px; font-weight: 900; letter-spacing: .02em; white-space: nowrap; }
    .rs485-add-device { min-height: 34px; padding: 6px 12px; }
    .rs485-scan-primary { width: 180px; min-width: 180px; min-height: 42px; font-size: 14px; }
    .rs485-scan-primary.scanning { width: 180px; min-width: 180px; }
    @media (max-width: 620px) { .rs485-scan-primary, .rs485-scan-primary.scanning, .rs485-scan-primary.stop { width: 100%; min-width: 0; } }
    .rs485-manage-templates { min-height: 36px; padding: 8px 14px; border: 1px solid var(--ha-border); border-radius: 8px; background: var(--ha-card); color: var(--ha-text); font-weight: 800; }
    .rs485-scan-service { padding: 0; overflow: hidden; }
    .rs485-scan-service summary { min-height: 44px; box-sizing: border-box; padding: 11px 14px; }
    .rs485-scan-service summary { border: 0; outline: none; }
    .rs485-scan-service summary:focus { outline: none; }
    .rs485-scan-service summary:focus-visible { outline: none; color: var(--ha-primary); }
    .rs485-scan-service .rs485-panel-head { margin: 0; padding: 10px 14px 8px; }
    .rs485-scan-service .rs485-panel-title { font-size: 11px; }
    .rs485-scan-service .rs485-clear-button { min-height: 34px; padding: 6px 12px; }
    .rs485-scan-service > .rs485-table { padding: 0 0 14px; }
    .rs485-template-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 16px; align-items: center; min-height: 54px; border: 1px solid var(--ha-row-border); border-radius: 8px; padding: 9px 12px; background: var(--ha-row); }
    .rs485-template-row strong { display: block; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 15px; }
    .rs485-template-row .rs485-device-meta { margin-top: 6px; gap: 6px 12px; flex-wrap: wrap; font-size: 12px; }
    .rs485-template-row .rs485-device-meta span { color: var(--ha-secondary); }
    .rs485-template-row .rs485-device-meta span + span::before { content: '·'; margin-right: 12px; color: var(--ha-border); }
    .rs485-template-menu { position: relative; }
    .rs485-template-menu summary { color: var(--ha-primary); }
    .rs485-template-menu > summary::before { content: none !important; display: none !important; }
    .rs485-template-menu .rs485-menu-dots { color: var(--ha-primary); }
    .rs485-template-menu summary::-webkit-details-marker { display: none; }
    .rs485-template-menu summary:hover, .rs485-template-menu[open] summary { color: var(--ha-primary); }
    .rs485-template-menu-panel { position: absolute; right: 0; bottom: 38px; z-index: 10; min-width: 120px; padding: 5px; border: 1px solid var(--ha-border); border-radius: 8px; background: var(--ha-card); box-shadow: 0 8px 20px rgba(0,0,0,.28); }
    .rs485-template-menu-panel button { width: 100%; min-height: 32px; padding: 6px 9px; text-align: left; }
    .rs485-capability-grid { display: grid; grid-template-columns: minmax(220px, 1fr) minmax(220px, 1fr) 150px; gap: 16px; align-items: start; }
    .rs485-capability-group { min-width: 0; }
    .rs485-panel-head { display: flex; align-items: baseline; justify-content: space-between; gap: 10px; margin-bottom: 8px; }
    .rs485-panel-head .rs485-panel-title { margin: 0; }
    .rs485-polling-caption { color: var(--ha-secondary); font-size: 11px; font-weight: 800; letter-spacing: .04em; text-transform: uppercase; white-space: nowrap; }
    .rs485-polling-strip { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 16px; align-items: center; border: 1px solid var(--ha-row-border); border-radius: 10px; padding: 10px 12px; background: var(--ha-row); }
    .rs485-polling-title { color: var(--ha-primary); font-size: 12px; font-weight: 900; letter-spacing: .14em; text-transform: uppercase; margin-bottom: 6px; }
    .rs485-polling-line { display: flex; gap: 12px; flex-wrap: wrap; color: var(--ha-secondary); font-size: 13px; line-height: 1.45; }
    .rs485-polling-line strong { color: var(--ha-text); }
    .rs485-polling-controls { display: grid; grid-template-columns: repeat(2, minmax(180px, 1fr)); gap: 12px; }
    .rs485-polling-settings-block { display: grid; gap: 10px; border: 1px solid var(--ha-row-border); border-radius: 10px; padding: 12px; background: var(--ha-row); }
    .rs485-tab-panel[data-rs485-panel="settings"] > .rs485-runtime-config-head { margin-top: 16px; }
    .rs485-tab-panel[data-rs485-panel="settings"] > .rs485-polling-settings-block { margin-top: 0; }
    .rs485-tab-panel[data-rs485-panel="settings"] > .rs485-device-settings-head { margin-top: 16px; }
    .rs485-polling-field label { display: block; color: var(--ha-secondary); font-size: 11px; font-weight: 800; margin-bottom: 4px; }
    .rs485-polling-field input, .rs485-polling-field select { width: 100%; height: 42px; box-sizing: border-box; border: 1px solid var(--ha-row-border); border-radius: 8px; background: var(--ha-field); color: var(--ha-text); padding: 0 10px; font-weight: 800; }
    .rs485-polling-field input:focus, .rs485-polling-field input:focus-visible,
    .rs485-polling-field select:focus, .rs485-polling-field select:focus-visible { outline: none; box-shadow: none; border-color: var(--ha-primary); }
    .rs485-polling-toggle { display: flex; align-items: center; justify-content: flex-end; gap: 8px; min-width: 92px; padding-bottom: 1px; }
    .rs485-stale { opacity: .62; }
    .rs485-capability-group .relay-row + .relay-row { margin-top: 10px; }
    .rs485-capability-group .rs485-mode-row + .rs485-mode-row { margin-top: 10px; }
    .rs485-device-settings { --rs485-label-height: 16px; --rs485-control-height: 42px; display: grid; grid-template-columns: repeat(4, minmax(130px, 1fr)) auto; gap: 12px; align-items: end; border: 1px solid var(--ha-row-border); border-radius: 10px; padding: 12px; background: var(--ha-row); }
    .rs485-device-settings .rs485-actions { justify-content: flex-end; }
    .rs485-device-settings .rs485-actions button { min-width: 64px; }
    .rs485-device-settings .rs485-field input[readonly] { color: var(--ha-secondary); }
    .rs485-mode-row { height: 42px; min-height: 42px; display: flex; align-items: center; }
    .rs485-mode-row .mode-select { width: 100%; min-width: 0; flex: 1 1 auto; }
    .rs485-mode-row .mode-trigger { width: 100%; height: 42px; min-height: 42px; box-sizing: border-box; display: flex; align-items: center; line-height: 42px; padding: 0 28px 0 10px; }
    .rs485-select-row { grid-template-columns: minmax(0, 1fr) minmax(130px, .7fr); }
    .rs485-select-row select {
      width: 100%;
      height: 34px;
      border: 1px solid var(--ha-row-border);
      border-radius: 8px;
      background: var(--ha-field);
      color: var(--ha-text);
      padding: 0 8px;
      font-weight: 700;
      min-width: 0;
    }
    .modules { display: grid; grid-template-columns: 1fr; gap: 16px; margin-top: 18px; }
    .module-card { border: 1px solid var(--ha-card-border); border-radius: 12px; background: var(--ha-surface); padding: 18px; }
    .module-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 14px; margin-bottom: 18px; }
    .module-actions { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; justify-content: flex-end; }
    .status-pill { min-height: 30px; padding: 6px 10px; border-radius: 999px; border: 1px solid var(--ha-success-border); color: var(--ha-success-text); background: var(--ha-success-bg); font-size: 12px; font-weight: 800; letter-spacing: .04em; text-transform: uppercase; }
    .status-pill.offline { border-color: var(--ha-danger-border); color: var(--ha-danger-text); background: var(--ha-danger-bg); }
    .module-title { font-size: 17px; font-weight: 800; margin-bottom: 6px; }
    .module-meta { color: var(--ha-secondary); font-size: 13px; }
    .danger-button { border-color: #5f3434; color: #ffb4ab; background: transparent; min-height: 34px; padding: 6px 12px; }
    #rs485-section .danger-button:hover,
    #rs485-section .danger-button:focus-visible {
      border-color: #ff8a80 !important;
      background: rgba(244, 67, 54, .14) !important;
      color: #ffd4cf !important;
      box-shadow: inset 0 0 0 1px #ff8a80 !important;
    }
    .danger-button:hover, .danger-button:focus-visible { border-color: #ff8a80; background: rgba(244, 67, 54, .14); color: #ffd4cf; outline: none; }
    .relay-grid { display: grid; grid-template-columns: repeat(8, minmax(120px, 1fr)); gap: 10px 14px; align-items: stretch; }
    .relay-row { display: grid; grid-template-columns: minmax(58px, 1fr) auto auto; align-items: center; gap: 10px; color: var(--ha-text); min-height: 42px; border: 0; border-radius: 0; padding: 0; background: transparent; }
    .module-card .relay-row, .module-card .relay-row:hover, .module-card .relay-row:focus-within { border: 1px solid var(--ha-row-border) !important; border-radius: 8px !important; padding: 8px 10px !important; background: transparent !important; box-shadow: none !important; }
    .relay-row span { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .state-text { min-width: 30px; text-align: right; color: var(--ha-secondary); font-size: 12px; font-weight: 800; letter-spacing: .04em; }
    .state-text.on { color: var(--ha-primary); }
    /* Toggle buttons carry their own ON/OFF label; avoid rendering a duplicate
       status when a legacy row still contains a separate state element. */
    .state-text:has(+ .toggle) { display: none; }
    .rs485-mode-row .mode-trigger::after {
      right: 12px;
      top: 16px;
      width: 7px;
      height: 7px;
    }
    .carrier-io-grid { display: grid; grid-template-columns: repeat(3, minmax(220px, 1fr)); gap: 16px; margin-top: 18px; }
    .carrier-io-card { border: 1px solid var(--ha-card-border); border-radius: 12px; background: var(--ha-surface); padding: 16px; min-width: 0; }
    .carrier-io-title { font-size: 17px; font-weight: 800; margin-bottom: 14px; }
    .carrier-io-row { display: grid; grid-template-columns: minmax(0, 1fr) auto auto; align-items: center; gap: 10px; min-height: 42px; border: 1px solid var(--ha-row-border); border-radius: 8px; padding: 8px 10px; background: var(--ha-row); }
    .carrier-io-row + .carrier-io-row { margin-top: 10px; }
    .carrier-io-row span { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .buzzer-panel {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 260px;
      gap: 16px;
      margin-top: 18px;
      align-items: stretch;
      min-width: 0;
    }
    .buzzer-group { border: 1px solid var(--ha-card-border); border-radius: 10px; background: var(--ha-surface); padding: 16px; min-width: 0; }
    .buzzer-group-title { color: var(--ha-secondary); font-size: 12px; font-weight: 800; letter-spacing: .12em; text-transform: uppercase; margin-bottom: 14px; }
    .diagnostic-led-grid { display: grid; grid-template-columns: repeat(3, minmax(220px, 1fr)); gap: 16px; margin-top: 18px; }
    .diagnostic-led-row { display: grid; grid-template-columns: minmax(0, 1fr) auto auto; align-items: center; gap: 10px; min-height: 42px; border: 1px solid var(--ha-row-border); border-radius: 8px; padding: 8px 10px; background: var(--ha-row); }
    .diagnostic-led-row span { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .diagnostic-led-note { margin-top: 10px; color: var(--ha-secondary); font-size: 13px; overflow-wrap: anywhere; }
    .diagnostic-rtc-card { margin-top: 18px; }
    .diagnostic-rtc-grid { display: grid; grid-template-columns: repeat(2, minmax(220px, 1fr)); gap: 12px 16px; }
    .diagnostic-rtc-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: center; gap: 10px; min-height: 42px; border: 1px solid var(--ha-row-border); border-radius: 8px; padding: 8px 10px; background: var(--ha-row); }
    .diagnostic-rtc-row span { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .diagnostic-rtc-error { min-height: 18px; margin-top: 10px; color: var(--ha-secondary); font-size: 13px; overflow-wrap: anywhere; }
    .buzzer-settings { display: grid; gap: 12px; }
    .buzzer-row { display: grid; grid-template-columns: minmax(100px, 150px) minmax(0, 1fr); align-items: center; gap: 14px; min-height: 40px; }
    .buzzer-row label,
    .buzzer-volume-label { color: var(--ha-text); font-size: 13px; font-weight: 700; }
    .buzzer-input-wrap { display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: center; gap: 8px; }
    .buzzer-input-wrap span { color: var(--ha-secondary); font-size: 12px; font-weight: 800; min-width: 26px; }
    .buzzer-field input { width: 100%; height: 38px; border: 1px solid var(--ha-row-border); border-radius: 8px; background: var(--ha-field); color: var(--ha-text); padding: 0 10px; font-weight: 600; }
    .buzzer-volume-control { min-width: 0; display: grid; grid-template-columns: minmax(0, 1fr) 72px; align-items: center; gap: 12px; }
    .buzzer-volume-control input { height: 38px; margin: 0; }
    .buzzer-volume-control strong { color: var(--ha-strong); text-align: right; font-size: 13px; white-space: nowrap; }
    .buzzer-toggle-control { display: flex; justify-content: flex-end; align-items: center; gap: 14px; min-height: 38px; }
    .buzzer-toggle-control strong { min-width: 28px; color: var(--ha-strong); font-size: 12px; text-align: right; }
    .buzzer-actions { display: grid; grid-template-rows: auto 40px 40px; gap: 12px; align-content: start; }
    .buzzer-actions button { width: 100%; height: 40px; }
    .buzzer-status { margin-top: 14px; color: var(--ha-secondary); font-size: 13px; overflow-wrap: anywhere; max-width: 980px; }
    .buzzer-detail-line { color: var(--ha-secondary); font-family: ui-monospace, "SFMono-Regular", Consolas, monospace; }
    .toolbar { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 18px; }
    button:not(.toggle):not(.mode-trigger):not(.mode-option) {
      border: 1px solid var(--ha-primary);
      background: transparent;
      color: var(--ha-primary);
      border-radius: 8px;
      min-height: 38px;
      padding: 8px 14px;
      cursor: pointer;
      font-weight: 700;
      transition: background .15s ease, border-color .15s ease, color .15s ease;
    }
    button:not(.toggle):not(.mode-trigger):not(.mode-option):hover,
    button:not(.toggle):not(.mode-trigger):not(.mode-option):focus-visible {
      border-color: var(--ha-primary);
      background: var(--ha-action-hover);
      color: var(--ha-primary);
      outline: none;
    }
    button:not(.toggle):not(.mode-trigger):not(.mode-option):disabled {
      border-color: #4a4a4a;
      color: var(--ha-secondary);
      cursor: default;
      opacity: .7;
    }
    button:not(.toggle):not(.mode-trigger):not(.mode-option):disabled:hover {
      background: transparent;
      color: var(--ha-secondary);
    }
    pre { display: none; min-height: 180px; max-height: 360px; overflow: auto; border: 1px solid var(--ha-card-border); border-radius: 8px; padding: 14px; background: var(--ha-pre); color: #e5edf7; font-size: 13px; }
    pre.visible { display: block; }
    @media (max-width: 1300px) { .relay-grid { grid-template-columns: repeat(4, minmax(140px, 1fr)); } .rs485-toolbar { grid-template-columns: repeat(3, minmax(140px, 1fr)); } .rs485-actions { justify-content: flex-start; } }
    @media (max-width: 1300px) { .overview-panel { grid-template-columns: 1fr; } .overview-identity { border-right: 0; border-bottom: 1px solid var(--ha-card-border); min-height: 260px; } }
    @media (max-width: 1100px) { .ports, .carrier-io-grid { grid-template-columns: repeat(2, minmax(220px, 1fr)); } .xport-group-toolbar, .xport-group-toolbar.config-hidden { grid-template-columns: repeat(2, minmax(220px, 1fr)); } .xport-group-toolbar .bus-note, .xport-group-toolbar.config-hidden .bus-note { grid-column: 1 / -1; } .relay-grid { grid-template-columns: repeat(2, minmax(150px, 1fr)); } .rs485-layout { grid-template-columns: 1fr; } .rs485-capability-grid, .rs485-device-settings { grid-template-columns: repeat(2, minmax(130px, 1fr)); } .rs485-polling-strip { grid-template-columns: 1fr auto; } .rs485-io-head, .rs485-io-row { grid-template-columns: 52px minmax(150px, 1fr) minmax(150px, 1fr) minmax(180px, .9fr); } }
    @media (max-width: 900px) { .diagnostic-led-grid, .diagnostic-rtc-grid, .buzzer-panel { grid-template-columns: 1fr; } .buzzer-actions { grid-template-columns: repeat(2, minmax(120px, 1fr)); grid-template-rows: auto; } .buzzer-actions .buzzer-group-title { grid-column: 1 / -1; } }
    @media (max-width: 620px) { body { padding: 10px; } .app-toolbar { justify-content: stretch; } .theme-switcher { width: 100%; justify-content: space-between; } .theme-choice { flex: 1; } .overview-identity { min-height: 260px; padding: 22px 18px; } .overview-wordmark { font-size: 22px; letter-spacing: .12em; } .overview-title-row { align-items: flex-start; flex-direction: column; gap: 14px; } .overview-title h1 { font-size: 34px; } .overview-title .subtitle { font-size: 16px; } .overview-facts { grid-template-columns: 88px minmax(0, 1fr); } .overview-facts dt, .overview-facts dd { font-size: 14px; } .overview-health { grid-template-columns: 1fr; gap: 18px; } .overview-health-item + .overview-health-item { border-left: 0; padding-left: 0; } .overview-metrics { grid-template-columns: 1fr; } .overview-metric, .overview-metric:nth-child(2n), .overview-metric:nth-last-child(-n+3) { border-right: 0; border-bottom: 1px solid var(--ha-card-border); } .overview-metric:nth-of-type(4) { border-bottom: 0; } .xport-panel { padding: 18px 14px; border-radius: 12px; } .module-row { align-items: flex-start; } .transport { margin-top: 0; } .xport-group-toolbar, .xport-group-toolbar.config-hidden { grid-template-columns: 1fr; } .xport-group-toolbar .bus-note, .xport-group-toolbar.config-hidden .bus-note { grid-column: 1; } .ports, .carrier-io-grid, .relay-grid, .rs485-toolbar { grid-template-columns: 1fr; } .rs485-table-row, .rs485-table-row.scan, .rs485-table-row.configured { grid-template-columns: 1fr; align-items: stretch; } .rs485-device-summary { align-items: flex-start; flex-wrap: wrap; gap: 4px 10px; } .rs485-device-meta { flex-wrap: wrap; } .rs485-device-filters, .rs485-device-facts { grid-template-columns: repeat(2, minmax(0, 1fr)); } .rs485-row-actions { justify-content: flex-start; } .rs485-detail-head { flex-direction: column; } .rs485-device-identity { align-items: flex-start; } .rs485-detail-meta { position: static; flex-wrap: wrap; } .rs485-detail-last-seen { position: static; } .rs485-detail-actions { justify-content: flex-start; } .module-head { flex-direction: column; } .module-actions { justify-content: flex-start; } .buzzer-row { grid-template-columns: 1fr; gap: 6px; } }
    @media (max-width: 620px) { .global-nav .theme-switcher { width: auto; justify-content: initial; } .global-nav .theme-switcher span { display: none; } .global-nav .theme-choice { flex: 0 0 auto; } }
  </style>
</head>
<body>
  <main>
    <nav class="global-nav" id="global-nav" aria-label="Application sections">
      <button type="button" data-ui-nav-target="overview-section">Overview</button>
      <button type="button" data-ui-nav-target="xport-section">X-Port</button>
      <button type="button" data-ui-nav-target="xbus-section">X-Bus</button>
      <button type="button" data-ui-nav-target="rs485-section">RS-485</button>
      <button type="button" data-ui-nav-target="onewire-section">1-Wire</button>
      <button type="button" data-ui-nav-target="diagnostics-section">Functions</button>
      <button type="button" data-ui-nav-target="rtc-section">RTC</button>
      <button type="button" data-ui-nav-target="controls-section">Controls</button>
      <button type="button" data-ui-nav-target="tools-section">Tools</button>
      <div class="theme-switcher" role="group" aria-label="Theme">
        <span>Theme</span>
        <button class="theme-choice" type="button" data-theme-choice="auto" onclick="setTheme('auto')">Auto</button>
        <button class="theme-choice" type="button" data-theme-choice="light" onclick="setTheme('light')">Light</button>
        <button class="theme-choice" type="button" data-theme-choice="dark" onclick="setTheme('dark')">Dark</button>
      </div>
    </nav>
    <section id="overview-section" class="extension-panel overview-panel ui-nav-section">
      <div class="overview-identity">
        <div class="overview-badge-row">
          <div class="overview-wordmark" aria-label="IntellegyHUB">INTELLEGY<span>HUB</span></div>
          <img class="overview-logo-mark" src="logo.png" alt="" aria-hidden="true">
        </div>
        <div class="overview-title-row">
          <div class="overview-title">
            <h1 id="carrier-identity-product">--</h1>
            <div id="carrier-identity-subtitle" class="subtitle">--</div>
          </div>
        </div>
        <div class="overview-title-divider"></div>
        <dl class="overview-facts">
          <dt>Serial</dt>
          <dd id="carrier-identity-serial">--</dd>
          <dt>Hardware</dt>
          <dd id="carrier-identity-hardware">--</dd>
          <dt>Software</dt>
          <dd id="carrier-identity-software">--</dd>
          <dt>Variant</dt>
          <dd id="carrier-identity-variant">--</dd>
        </dl>
        <div class="overview-divider"></div>
        <dl class="overview-health">
          <div class="overview-health-item">
            <dt>Health</dt>
            <dd class="overview-health-value">
              <span id="carrier-health-dot" class="overview-sync-dot"></span>
              <span id="carrier-health-state">Loading</span>
            </dd>
          </div>
          <div class="overview-health-item">
            <dt>Uptime</dt>
            <dd id="carrier-uptime">--</dd>
          </div>
        </dl>
      </div>
      <div class="overview-metrics">
        <article class="overview-metric">
          <div class="overview-metric-label">Board Temp</div>
          <div id="metric-board-temperature" class="overview-metric-value">--</div>
          <div id="metric-board-temperature-meta" class="overview-metric-meta">Loading</div>
        </article>
        <article class="overview-metric">
          <div class="overview-metric-label">+5 V Rail</div>
          <div id="metric-5v" class="overview-metric-value">--</div>
          <div id="metric-5v-meta" class="overview-metric-meta">Loading</div>
        </article>
        <article class="overview-metric">
          <div class="overview-metric-label">+3.3 V Rail</div>
          <div id="metric-3v3" class="overview-metric-value">--</div>
          <div id="metric-3v3-meta" class="overview-metric-meta">Loading</div>
        </article>
        <article class="overview-metric">
          <div class="overview-metric-label">Input Voltage</div>
          <div id="metric-vin" class="overview-metric-value">--</div>
          <div id="metric-vin-meta" class="overview-metric-meta">Loading</div>
        </article>
        <div id="overview-updated" class="overview-updated">Updated --</div>
      </div>
    </section>
    <section id="xport-section" class="extension-panel xport-panel ui-nav-section">
      <div class="module-row">
        <div>
          <div class="eyebrow">Optional Module</div>
          <h1>X-PORT</h1>
          <div class="subtitle">Multi-function expansion ports X1-X4</div>
          <div id="xport-status" class="status">X-PORT: loading...</div>
        </div>
        <div class="transport">REST</div>
      </div>
      <p id="xport-mode-notice" class="notice">When a port mode changes, the previous channel mode is safely shut down first. Selected modes are stored on the Hardware Host and restored after restart.</p>
      <div class="extension-actions bus-toolbar xport-group-toolbar">
        <div class="bus-power-control">
          <label>Profile</label>
          <div id="xport-group-mode"></div>
        </div>
        <div id="xport-configuration-control" class="bus-power-control">
          <label>Mode</label>
          <div id="xport-configuration"></div>
        </div>
        <div id="xport-group-note" class="bus-note">RGBW Dimmer assigns X1-X4 as Red, Green, Blue, and White channels.</div>
      </div>
      <div id="xport-unavailable" class="xport-unavailable hidden"></div>
      <div id="ports" class="ports"></div>
    </section>
    <section id="xbus-section" class="extension-panel ui-nav-section">
      <div class="module-row">
        <div>
          <div class="eyebrow">Hardware Bus</div>
          <h1>X-BUS</h1>
          <div class="subtitle">External expansion bus for xDO-8 / xDI-16 modules</div>
          <div id="extension-status" class="status">X-BUS: loading...</div>
        </div>
        <div class="transport">I2C-1</div>
      </div>
      <p class="notice">X-BUS is the external I2C expansion bus. Detected modules stay stored until removed.</p>
      <div class="extension-actions bus-toolbar">
        <div class="bus-power-control">
          <span class="metric">Bus power</span>
          <button id="extension-power-toggle" class="toggle" type="button" onclick="toggleExtensionPower()"><span>OFF</span></button>
        </div>
        <button class="bus-action" onclick="scanExtensions()">Scan modules</button>
        <div class="bus-note">Power control: MCP23017 / I2C-10 / 0x20 / GPB7</div>
      </div>
      <div id="modules" class="modules"></div>
    </section>
    <!-- RS485_START -->
    <section id="rs485-section" class="extension-panel ui-nav-section">
      <div class="module-row">
        <div>
          <div class="eyebrow">Interface</div>
          <h1>RS-485</h1>
          <div class="subtitle">Template-driven Modbus RTU modules</div>
          <div id="rs485-status" class="status">RS-485: loading...</div>
        </div>
        <div class="rs485-header-actions">
          <button type="button" class="rs485-manage-templates" onclick="openRs485Templates()">Templates</button>
        </div>
      </div>
      <div id="rs485-local-mode" class="rs485-local-mode">
        <span class="rs485-label">Local</span>
        <button id="rs485-local-mode-toggle" type="button" onclick="toggleRs485LocalMode()">Mock</button>
      </div>
      <p class="notice">RS-485 devices are configured from device templates. Scan results are not added until the device is explicitly configured.</p>
      <div class="extension-actions bus-toolbar rs485-toolbar">
        <div class="rs485-connection-intro">
          <div class="rs485-bus-icon">RS&#8209;485</div>
          <div><strong>Connection</strong><span>Select port and parameters<br>to scan or work with devices.</span></div>
        </div>
        <div class="rs485-field rs485-serial-field">
          <label>Serial port</label>
          <div id="rs485-serial-port" class="mode-select" data-value="/dev/ttyAMA3"></div>
        </div>
        <div class="rs485-field">
          <label>Baudrate</label>
          <div id="rs485-baudrate" class="mode-select" data-value="9600"></div>
        </div>
        <div class="rs485-field">
          <label>Parity</label>
          <div id="rs485-parity" class="mode-select" data-value="None"></div>
        </div>
        <div class="rs485-field">
          <label>Stop bits</label>
          <div id="rs485-stopbits" class="mode-select" data-value="1"></div>
        </div>
        <div class="rs485-field">
          <label>Device template</label>
          <div id="rs485-template" class="mode-select" data-value="mio-8"></div>
        </div>
        <div class="rs485-actions">
          <div class="rs485-connection-polling" aria-label="Selected RS-485 interface polling">
            <span class="rs485-polling-title">Bus polling</span>
            <span class="state-text ${(rs485ApiState?.bus?.enabled !== false) ? 'on' : ''}" data-rs485-bus-state>${(rs485ApiState?.bus?.enabled !== false) ? 'ON' : 'OFF'}</span>
            <button class="toggle ${(rs485ApiState?.bus?.enabled !== false) ? 'on' : ''}" type="button" data-rs485-port-control="port" onclick="toggleRs485Port()" aria-label="Toggle selected RS-485 interface polling"><span>${(rs485ApiState?.bus?.enabled !== false) ? 'ON' : 'OFF'}</span></button>
          </div>
          <button id="rs485-scan-button" class="rs485-scan-primary" type="button" onclick="scanRs485Mock()">Scan devices</button>
        </div>
      </div>
      <div class="rs485-global-scan-log">
        <details id="rs485-global-scan-log" class="rs485-device-log">
          <summary>Scan log <span id="rs485-device-scan-summary">Last scan: --:--:--</span></summary>
          <button type="button" class="rs485-clear-button" data-rs485-clear-log="true">Clear</button>
          <div id="rs485-device-scan-log" class="rs485-console-log"><div class="rs485-empty">No scan log yet.</div></div>
        </details>
        <div id="rs485-device-scan-footer" class="rs485-log-footer"></div>
      </div>
      <div class="rs485-layout">
        <div>
          <article id="rs485-scan-results-panel" class="rs485-panel">
            <div class="rs485-panel-head">
              <div class="rs485-panel-title">Scan Results</div>
              <button type="button" class="rs485-clear-button" onclick="clearRs485MockScan()">Clear</button>
            </div>
            <div id="rs485-scan-results" class="rs485-table">
              <div class="rs485-table-row">
                <span>Address</span>
                <span>Template</span>
                <span>Status</span>
                <button type="button" disabled>Add</button>
              </div>
              <div class="rs485-empty">No scan has been run. Press Scan to search the selected RS-485 bus.</div>
            </div>
          </article>
          <details id="rs485-left-scan-service" class="rs485-panel rs485-service-panel">
            <summary>Scan Log <span>View communication log</span></summary>
            <div class="rs485-panel-head">
              <div class="rs485-panel-title">Scan Log</div>
              <button type="button" class="rs485-clear-button" onclick="clearRs485ScanLog()">Clear</button>
            </div>
            <div id="rs485-scan-progress" class="rs485-empty">Scan is idle.</div>
            <pre id="rs485-scan-log" class="rs485-console-log">No scan log yet.</pre>
          </details>
          <article id="rs485-device-list-panel" class="rs485-panel">
            <div class="rs485-panel-head">
              <div id="rs485-device-list-title" class="rs485-device-heading">Devices</div>
            </div>
            <div class="rs485-device-list-toolbar">
              <input id="rs485-device-search" type="search" placeholder="Search devices" oninput="setRs485DeviceQuery(this.value)">
              <div id="rs485-device-filters" class="rs485-device-filters" role="tablist">
                <button type="button" data-filter="all" onclick="setRs485DeviceFilter('all')">All <span>0</span></button>
                <button type="button" data-filter="configured" onclick="setRs485DeviceFilter('configured')">Configured <span>0</span></button>
                <button type="button" data-filter="discovered" onclick="setRs485DeviceFilter('discovered')">Discovered <span>0</span></button>
                <button type="button" data-filter="online" onclick="setRs485DeviceFilter('online')">Online <span>0</span></button>
                <button type="button" data-filter="offline" onclick="setRs485DeviceFilter('offline')">Offline <span>0</span></button>
              </div>
            </div>
            <div class="rs485-device-list-head"><span>#</span><span>Device name</span><span>Slave ID</span><span>Status</span><span></span></div>
            <div id="rs485-configured-devices" class="rs485-table">
              <div class="rs485-table-row">
                <span>MIO-8 #1</span>
                <span>MIO-8</span>
                <span>Offline</span>
                <button type="button" disabled>Remove</button>
              </div>
              <div class="rs485-empty">No configured devices yet. Scan and add one or more modules.</div>
            </div>
          </article>
          <details id="rs485-template-service" class="rs485-panel rs485-service-panel rs485-scan-service">
            <summary>Device Templates</summary>
            <div class="rs485-panel-head">
              <div class="rs485-panel-title">Installed templates</div>
              <div class="rs485-row-actions">
                <button type="button" class="rs485-clear-button" onclick="document.getElementById('rs485-template-upload').click()">Upload</button>
              </div>
            </div>
            <input id="rs485-template-upload" type="file" accept=".yaml,.yml,application/x-yaml,text/yaml,text/plain" hidden onchange="uploadRs485Template(this)">
            <div id="rs485-device-templates" class="rs485-table">
              <div class="rs485-empty">Templates are loading...</div>
            </div>
          </details>
        </div>
        <article id="rs485-device-detail" class="rs485-panel">
          <div class="rs485-empty">Select or add an RS-485 device to view its relay outputs, digital inputs, modes, and device settings.</div>
        </article>
      </div>
    </section>
    <!-- RS485_END -->
    <section id="onewire-section" class="extension-panel ui-nav-section">
      <div class="module-row">
        <div>
          <div class="eyebrow">Interface</div>
          <h1>1-WIRE</h1>
          <div class="subtitle">DS2482S-100 bridges and DS18B20 sensors</div>
          <div id="onewire-status" class="status">1-WIRE: loading...</div>
        </div>
        <div class="transport">I2C-10</div>
      </div>
      <p class="notice">The 1-Wire sensor bus is powered through the controller GPIO extender MCP23017 at I2C-10 address 0x20, pin GPB6. DS2482S-100 bridges are scanned on I2C-10 at addresses 0x1A and 0x1B.</p>
      <div class="extension-actions bus-toolbar">
        <div class="bus-power-control">
          <span class="metric">Bus power</span>
          <button id="onewire-power-toggle" class="toggle" type="button" onclick="toggleOneWirePower()"><span>OFF</span></button>
        </div>
        <button class="bus-action" onclick="scanOneWire()">Scan sensors</button>
        <button class="bus-action" onclick="refreshOneWire()">Refresh temperatures</button>
        <div class="bus-note">Power control: MCP23017 / I2C-10 / 0x20 / GPB6</div>
      </div>
      <div id="onewire-modules" class="modules"></div>
    </section>
    <section id="diagnostics-section" class="extension-panel ui-nav-section">
      <div class="module-row">
        <div>
          <div class="eyebrow">Board Services</div>
          <h1>FUNCTIONS</h1>
          <div class="subtitle">Onboard status indicators and GPIO18 hardware PWM test</div>
          <div id="buzzer-status" class="status">BUZZER: loading...</div>
        </div>
        <div class="transport">GPIO18 / GPIO19 / GPIO21</div>
      </div>
      <div id="buzzer-detail" class="buzzer-status"></div>
      <div class="buzzer-panel">
        <div class="buzzer-group">
          <div class="buzzer-group-title">Buzzer Parameters</div>
          <div class="buzzer-settings">
            <div class="buzzer-row">
              <label for="buzzer-frequency">Frequency</label>
              <div class="buzzer-volume-control">
                <input id="buzzer-frequency" type="range" min="300" max="2800" step="10" value="2000" oninput="updateBuzzerSettingLabels()">
                <strong id="buzzer-frequency-value">2000 Hz</strong>
              </div>
            </div>
            <div class="buzzer-row">
              <label for="buzzer-duration">Duration</label>
              <div class="buzzer-volume-control">
                <input id="buzzer-duration" type="range" min="10" max="1000" step="10" value="300" oninput="updateBuzzerSettingLabels()">
                <strong id="buzzer-duration-value">300 ms</strong>
              </div>
            </div>
            <div class="buzzer-row">
              <span class="buzzer-volume-label">Volume</span>
              <div class="buzzer-volume-control">
                <input id="buzzer-volume" type="range" min="0" max="100" step="1" value="50" oninput="updateBuzzerSettingLabels()">
                <strong id="buzzer-volume-value">50%</strong>
              </div>
            </div>
            <div class="buzzer-row">
              <span class="buzzer-volume-label">Enabled</span>
              <div class="buzzer-toggle-control">
                <button id="buzzer-power-toggle" class="toggle on" type="button" onclick="toggleBuzzerPower()"><span>ON</span></button>
              </div>
            </div>
          </div>
        </div>
        <div class="buzzer-group buzzer-actions">
          <div class="buzzer-group-title">Buzzer Command</div>
          <button onclick="playBuzzer()">Play</button>
          <button onclick="stopBuzzer()">Stop</button>
        </div>
      </div>
      <div class="diagnostic-led-grid">
        <div class="buzzer-group">
          <div class="buzzer-group-title">STE LED</div>
          <div class="diagnostic-led-row">
            <span>Heartbeat</span>
            <strong id="ste-heartbeat-state" class="state-text">OFF</strong>
            <button id="ste-heartbeat-toggle" class="toggle" type="button" onclick="toggleSteHeartbeat()"><span>OFF</span></button>
          </div>
          <div class="diagnostic-led-note">Blinks the STE LED while the add-on is running.</div>
        </div>
        <div class="buzzer-group">
          <div class="buzzer-group-title">NET LED</div>
          <div class="diagnostic-led-row">
            <span>Connection indicator</span>
            <strong id="net-led-state" class="state-text">OFF</strong>
            <button id="net-led-toggle" class="toggle" type="button" onclick="toggleNetLed()"><span>OFF</span></button>
          </div>
          <div class="diagnostic-led-note">Shows WebSocket client status: on when connected, blinking while disconnected.</div>
        </div>
        <div class="buzzer-group">
          <div class="buzzer-group-title">ERR LED</div>
          <div class="diagnostic-led-row">
            <span>Error indicator</span>
            <strong id="err-led-state" class="state-text">OFF</strong>
            <button id="err-led-toggle" class="toggle" type="button" onclick="toggleErrLed()"><span>OFF</span></button>
          </div>
          <div class="diagnostic-led-note">Reserved for add-on and hardware error indication.</div>
        </div>
      </div>
    </section>
    <section id="rtc-section" class="extension-panel ui-nav-section">
      <div class="module-row">
        <div>
          <div class="eyebrow">Board Services</div>
          <h1>RTC</h1>
          <div class="subtitle">Real-time clock status and synchronization</div>
        </div>
        <div class="transport">HOST RTC</div>
      </div>
      <div class="buzzer-group diagnostic-rtc-card">
        <div class="buzzer-group-title">RTC Clock</div>
        <div class="diagnostic-rtc-grid">
          <div class="diagnostic-rtc-row">
            <span>RTC time</span>
            <strong id="rtc-time">Not available</strong>
          </div>
          <div class="diagnostic-rtc-row">
            <span>System time</span>
            <strong id="rtc-system-time">Loading</strong>
          </div>
          <div class="diagnostic-rtc-row">
            <span>Difference</span>
            <strong id="rtc-difference">Unknown</strong>
          </div>
          <div class="diagnostic-rtc-row">
            <span>Status</span>
            <strong id="rtc-status" class="state-text">PENDING</strong>
          </div>
        </div>
        <div class="toolbar">
          <button type="button" id="rtc-sync-button" onclick="syncRtc()">Sync RTC</button>
          <button type="button" onclick="refreshRtc()">Refresh</button>
        </div>
        <div id="rtc-error" class="diagnostic-rtc-error"></div>
        <div class="diagnostic-led-note">RTC is handled by the host kernel driver. The add-on does not access the RTC I2C address directly.</div>
      </div>
    </section>
    <section id="controls-section" class="extension-panel ui-nav-section">
      <div class="module-row">
        <div>
          <div class="eyebrow">Carrier Board I/O</div>
          <h1>CONTROLS</h1>
          <div class="subtitle">MCP23017 controlled carrier outputs</div>
          <div id="carrier-io-status" class="status">CONTROLS: loading...</div>
        </div>
        <div class="transport">I2C-10</div>
      </div>
      <div id="carrier-io" class="carrier-io-grid"></div>
    </section>
    <section id="tools-section" class="extension-panel ui-nav-section">
      <div class="module-row">
        <div>
          <div class="eyebrow">Service Tools</div>
          <h1>TOOLS</h1>
          <div class="subtitle">Quick access to health, state, device discovery and I2C diagnostics.</div>
        </div>
        <div class="transport">REST</div>
      </div>
      <div class="toolbar">
        <button onclick="callApi('health')">Health</button>
        <button onclick="callApi('api/v1/state')">State</button>
        <button onclick="showXPortDiagnostics()">X-Port</button>
        <button onclick="showExtensionDiagnostics()">Expansion</button>
        <button onclick="showOneWireDiagnostics()">1-Wire</button>
        <button onclick="callApi('api/v1/diagnostics/devices')">Devices</button>
        <button onclick="callApi('api/v1/i2c/scan/1')">Scan I2C-1</button>
        <button onclick="callApi('api/v1/i2c/scan/10')">Scan I2C-10</button>
      </div>
      <pre id="output" class="diagnostics-output">Ready.</pre>
    </section>
  </main>
  <script>
    const labels = {
      Disabled: 'Off',
      AnalogInput: 'AI',
      DigitalOutput: 'DO',
      PwmOutput: 'PWM',
      DigitalInputExternalVoltage: 'DI PNP/+24V',
      DigitalInputInternalPullUp: 'DI NPN/COM',
      PulseCounterExternalVoltage: 'CNT PNP/+24V',
      PulseCounterInternalPullUp: 'CNT NPN/COM'
    };
    const modeOrder = [
      'Disabled',
      'AnalogInput',
      'DigitalOutput',
      'PwmOutput',
      'DigitalInputExternalVoltage',
      'DigitalInputInternalPullUp',
      'PulseCounterExternalVoltage',
      'PulseCounterInternalPullUp'
    ];
    const groupModeLabels = {
      'Universal I/O': 'Universal I/O',
      'RGBW Dimmer': 'RGBW Dimmer',
      'RGB + W': 'RGB + W',
      '2xW + 2xW': '2×W + 2×W',
      '2XW + 2XW': '2×W + 2×W',
      '2*W + 2*W': '2×W + 2×W',
      '2×W + 2×W': '2×W + 2×W',
      '2xW + W + W': '2×W + W + W',
      '2XW + W + W': '2×W + W + W',
      '2*W + W + W': '2×W + W + W',
      '2×W + W + W': '2×W + W + W',
      'W + W + 2xW': 'W + W + 2×W',
      'W + W + 2XW': 'W + W + 2×W',
      'W + W + 2*W': 'W + W + 2×W',
      'W + W + 2×W': 'W + W + 2×W',
      '4xW': '4×W',
      '4XW': '4×W',
      '4*W': '4×W',
      '4×W': '4×W',
      Independent: 'Universal I/O',
      RgbwDimmer: 'RGBW Dimmer',
      RgbPlusW: 'RGB + W',
      RGBPlusW: 'RGB + W',
      TwoWPlusTwoW: '2×W + 2×W',
      TwoWPlusWPlusW: '2×W + W + W',
      WPlusWPlusTwoW: 'W + W + 2×W'
    };
    const profileOrder = ['Universal I/O', 'LED Dimmer'];
    const configurationOrder = ['RGBW Dimmer', 'RGB + W', 'W + W + W + W', '2×W + 2×W', '2×W + W + W', 'W + W + 2×W', '4×W'];
    const profileLabels = {
      'Universal I/O': 'Universal I/O',
      'LED Dimmer': 'LED Dimmer'
    };
    const configurationLabels = {
      'RGBW Dimmer': 'RGBW',
      'RGB + W': 'RGB + W',
      'W + W + W + W': 'W + W + W + W',
      '2×W + 2×W': '2×W + 2×W',
      '2×W + W + W': '2×W + W + W',
      'W + W + 2×W': 'W + W + 2×W',
      '4×W': '4×W'
    };
    const rgbwRoles = {
      1: 'Red',
      2: 'Green',
      3: 'Blue',
      4: 'White'
    };
    let latestAppInfo = { uptime_seconds: 0 };
    let latestXPort = null;
    let selectedTheme = 'auto';
    const themeMedia = window.matchMedia('(prefers-color-scheme: dark)');
    function resolvedTheme(theme) {
      return theme === 'auto' ? (themeMedia.matches ? 'dark' : 'light') : theme;
    }
    function applyTheme(theme) {
      selectedTheme = ['auto', 'light', 'dark'].includes(theme) ? theme : 'auto';
      document.documentElement.dataset.themeChoice = selectedTheme;
      document.documentElement.dataset.theme = resolvedTheme(selectedTheme);
      document.querySelectorAll('.theme-choice').forEach((button) => {
        button.classList.toggle('active', button.dataset.themeChoice === selectedTheme);
      });
    }
    async function loadTheme() {
      try {
        const payload = await requestJson('api/v1/ui/settings');
        applyTheme(payload.theme);
      } catch (error) {
        applyTheme('auto');
      }
    }
    async function setTheme(theme) {
      applyTheme(theme);
      try {
        const payload = await requestJson('api/v1/ui/theme', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ theme })
        });
        applyTheme(payload.theme);
      } catch (error) {
        await loadTheme();
      }
    }
    themeMedia.addEventListener('change', () => {
      if (selectedTheme === 'auto') {
        applyTheme('auto');
      }
    });
    function apiUrl(path) {
      const basePath = window.location.pathname.endsWith('/') ? window.location.pathname : window.location.pathname + '/';
      return new URL(path, window.location.origin + basePath);
    }
    function websocketUrl(path) {
      const url = apiUrl(path);
      url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
      return url;
    }
    async function requestJson(path, options = {}) {
      const response = await fetch(apiUrl(path), options);
      const text = await response.text();
      let payload;
      try { payload = JSON.parse(text); } catch { payload = text; }
      if (!response.ok) { throw payload; }
      return payload;
    }
    function errorDetail(error) {
      if (!error) return 'unknown error';
      if (typeof error === 'string') return error;
      if (error.detail) return error.detail;
      return JSON.stringify(error);
    }
    async function renderXPort() {
      const output = document.getElementById('output');
      try {
        const payload = await requestJson('api/v1/xport');
        paintXPort(payload);
      } catch (error) {
        document.getElementById('xport-status').textContent = 'X-PORT: Error';
        output.textContent = JSON.stringify(error, null, 2);
        output.classList.add('visible');
      }
    }
    function paintXPort(payload) {
      latestXPort = payload || null;
      refreshCarrierEdition();
      document.getElementById('xport-status').textContent = formatXPortStatus(payload);
      updateXPortGroupMode(payload);
      const ports = document.getElementById('ports');
      const unavailable = payload.availability !== 'Available';
      updateXPortUnavailableNotice(payload, unavailable);
      document.getElementById('xport-mode-notice')?.classList.toggle('hidden', unavailable);
      ports.classList.toggle('hidden', unavailable);
      if (unavailable) {
        ports.replaceChildren();
        return;
      }
      const seen = new Set();
      for (const channel of payload.channels) {
        const key = String(channel.channel);
        seen.add(key);
        let card = ports.querySelector(`[data-xport-channel="${key}"]`);
        if (!card) {
          card = renderXPortCard(channel, payload.modes);
          ports.appendChild(card);
        }
        updateXPortCard(card, channel, payload.modes, payload.group_mode);
      }
      ports.querySelectorAll('[data-xport-channel]').forEach((card) => {
        if (!seen.has(card.dataset.xportChannel)) { card.remove(); }
      });
    }
    function formatXPortStatus(payload) {
      if (payload.topology === 'NotInstalled' && payload.availability === 'OptionalMissing') {
        return 'X-PORT: Module not installed';
      }
      if (payload.availability === 'InvalidConfiguration') {
        return 'X-PORT: Invalid configuration';
      }
      if (payload.availability === 'Faulted') {
        return 'X-PORT: Faulted';
      }
      if (payload.availability === 'Available') {
        return `X-PORT: ${payload.topology} - Available`;
      }
      return `X-PORT: ${payload.topology || 'Unknown'}`;
    }
    function updateXPortGroupMode(payload) {
      const target = document.getElementById('xport-group-mode');
      const currentMode = normalizeGroupMode(payload.group_mode);
      const profile = groupModeToProfile(currentMode);
      const unavailable = payload.availability !== 'Available';
      const toolbar = target.closest('.xport-group-toolbar');
      if (toolbar) {
        toolbar.classList.toggle('hidden', unavailable);
      }
      const key = `${profile}|${currentMode}|${unavailable}`;
      if (target.dataset.key !== key) {
        target.replaceChildren(renderProfileSelect(profile, unavailable));
        target.dataset.key = key;
      }
      const configControl = document.getElementById('xport-configuration-control');
      const configTarget = document.getElementById('xport-configuration');
      if (configControl && configTarget) {
        const showConfig = profile === 'LED Dimmer';
        configTarget.dataset.currentMode = currentMode;
        if (toolbar) {
          toolbar.classList.toggle('config-hidden', !showConfig);
        }
        configControl.classList.toggle('hidden', !showConfig);
        const configKey = `${currentMode}|${showConfig}|${unavailable}`;
        if (showConfig && configTarget.dataset.key !== configKey) {
          configTarget.replaceChildren(renderConfigurationSelect(currentMode, unavailable));
          configTarget.dataset.key = configKey;
        }
        if (!showConfig) {
          configTarget.replaceChildren();
          configTarget.dataset.key = configKey;
        }
      }
      const note = document.getElementById('xport-group-note');
      if (note) {
        if (currentMode === 'RGB + W') {
          note.textContent = 'RGB + W creates an RGB dimmer on X1-X3 and an independent white dimmer on X4.';
        } else if (currentMode === 'W + W + W + W') {
          note.textContent = 'W + W + W + W creates four independent white dimmers on X1-X4.';
        } else if (currentMode === '2×W + 2×W') {
          note.textContent = '2×W + 2×W links X1-X2 as one white dimmer and X3-X4 as another.';
        } else if (currentMode === '2×W + W + W') {
          note.textContent = '2×W + W + W links X1-X2 and keeps X3 and X4 independent.';
        } else if (currentMode === 'W + W + 2×W') {
          note.textContent = 'W + W + 2×W keeps X1 and X2 independent and links X3-X4.';
        } else if (currentMode === '4×W') {
          note.textContent = '4×W links X1-X4 as one white dimmer.';
        } else if (currentMode === 'RGBW Dimmer') {
          note.textContent = 'RGBW Dimmer assigns X1-X4 as Red, Green, Blue, and White channels.';
        } else {
          note.textContent = 'Universal I/O lets each X-Port channel be configured independently.';
        }
      }
    }
    function updateXPortUnavailableNotice(payload, unavailable) {
      const notice = document.getElementById('xport-unavailable');
      if (!notice) { return; }
      notice.classList.toggle('hidden', !unavailable);
      if (!unavailable) {
        notice.replaceChildren();
        return;
      }
      const title = document.createElement('strong');
      title.textContent = payload.availability === 'OptionalMissing'
        ? 'Module not installed'
        : 'Module unavailable';
      const detail = document.createElement('span');
      if (payload.availability === 'OptionalMissing') {
        detail.textContent = 'The X-Port slot is empty. Install the optional module to configure X1-X4.';
      } else if (payload.error) {
        detail.textContent = payload.error;
      } else {
        detail.textContent = 'X-Port controls are disabled until the module becomes available.';
      }
      notice.replaceChildren(title, detail);
    }
    function renderXPortCard(channel, modes) {
      const card = document.createElement('article');
      card.className = 'port-card';
      card.dataset.xportChannel = String(channel.channel);
      const title = document.createElement('div');
      title.className = 'port-title';
      title.textContent = `X${channel.channel}`;
      const label = document.createElement('label');
      label.textContent = 'Mode';
      const modeSelect = renderModeSelect(channel, modes);
      const activeRow = document.createElement('div');
      activeRow.className = 'active-row';
      const active = document.createElement('div');
      active.className = 'active-mode';
      const activeLabel = document.createElement('span');
      activeLabel.textContent = 'Active mode:';
      const activeValue = document.createElement('strong');
      activeValue.className = 'active-mode-value';
      active.append(activeLabel, activeValue);
      const actionSlot = document.createElement('div');
      actionSlot.className = 'xport-action-slot';
      activeRow.append(active, actionSlot);
      const modeBody = document.createElement('div');
      modeBody.className = 'mode-body';
      card.append(title, label, modeSelect, activeRow, modeBody);
      return card;
    }
    function updateXPortCard(card, channel, modes, groupMode = 'Universal I/O') {
      groupMode = normalizeGroupMode(groupMode);
      const locked = groupMode && groupMode !== 'Universal I/O';
      card.classList.toggle('locked', locked);
      const desiredLabel = locked ? groupChannelModeLabel(groupMode, channel.channel) : labels[channel.desired_mode] || channel.desired_mode;
      const trigger = card.querySelector('.mode-trigger');
      if (trigger && trigger.textContent !== desiredLabel) {
        trigger.textContent = desiredLabel;
      }
      if (trigger) {
        trigger.disabled = locked;
        trigger.title = locked ? 'X1-X4 are assigned by the active X-Port profile' : '';
      }
      const modeSelect = card.querySelector('.mode-select');
      if (modeSelect) {
        modeSelect.classList.toggle('locked', locked);
      }
      card.querySelectorAll('.mode-option').forEach((option) => {
        option.classList.toggle('active', option.dataset.mode === channel.desired_mode);
      });
      const activeValue = card.querySelector('.active-mode-value');
      const confirmedLabel = locked ? groupChannelActiveLabel(groupMode, channel.channel) : labels[channel.confirmed_mode] || channel.confirmed_mode;
      if (activeValue && activeValue.textContent !== confirmedLabel) {
        activeValue.textContent = confirmedLabel;
        activeValue.title = confirmedLabel;
      }
      updateXPortAction(card, channel, locked);
      const body = card.querySelector('.mode-body');
      const bodyKey = [
        groupMode || '',
        channel.confirmed_mode,
        channel.error || '',
        String(channel.value ?? ''),
        String(channel.counter ?? '')
      ].join('|');
      if (body && body.dataset.bodyKey !== bodyKey) {
        body.replaceChildren(locked ? renderLockedXPortBody(channel, groupMode) : renderModeBody(channel));
        body.dataset.bodyKey = bodyKey;
      }
    }
    function updateXPortAction(card, channel, locked = false) {
      const actionSlot = card.querySelector('.xport-action-slot');
      if (!actionSlot) { return; }
      if (locked) {
        actionSlot.replaceChildren();
        return;
      }
      const needsReset = channel.confirmed_mode === 'PulseCounterExternalVoltage' || channel.confirmed_mode === 'PulseCounterInternalPullUp';
      const needsPwmToggle = channel.confirmed_mode === 'PwmOutput';
      if (!needsReset && !needsPwmToggle) {
        actionSlot.replaceChildren();
        return;
      }
      if (needsPwmToggle) {
        const enabled = Number(channel.value || 0) > 0;
        let state = actionSlot.querySelector('.state-text');
        let toggle = actionSlot.querySelector('button.toggle');
        if (!state) {
          state = document.createElement('span');
          state.className = 'state-text';
        }
        if (!toggle) {
          toggle = document.createElement('button');
          toggle.type = 'button';
        }
        state.className = `state-text ${enabled ? 'on' : ''}`;
        state.textContent = enabled ? 'ON' : 'OFF';
        toggle.className = `toggle ${enabled ? 'on' : ''}`;
        toggle.title = enabled ? 'Turn PWM off' : 'Turn PWM on';
        toggle.innerHTML = `<span>${enabled ? 'ON' : 'OFF'}</span>`;
        toggle.onclick = () => setPwmEnabled(channel, !enabled);
        actionSlot.replaceChildren(state, toggle);
        return;
      }
      let reset = actionSlot.querySelector('button');
      if (!reset) {
        reset = document.createElement('button');
        reset.className = 'reset-button';
        reset.type = 'button';
        reset.textContent = 'Reset';
        actionSlot.replaceChildren(reset);
      }
      reset.onclick = () => resetCounter(channel.channel);
    }
    async function setMode(channel, mode) {
      const output = document.getElementById('output');
      output.classList.remove('visible');
      try {
        await requestJson(`api/v1/xport/channels/${channel}/mode`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ mode }) });
        await renderXPort();
      } catch (error) {
        output.textContent = JSON.stringify(error, null, 2);
        output.classList.add('visible');
        await renderXPort();
      }
    }
    async function setGroupMode(mode) {
      const output = document.getElementById('output');
      output.classList.remove('visible');
      try {
        await requestJson('api/v1/xport/group-mode', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ mode }) });
        await renderXPort();
      } catch (error) {
        output.textContent = JSON.stringify(error, null, 2);
        output.classList.add('visible');
        await renderXPort();
      }
    }
    async function setGroupChannelValue(channel, value) {
      const output = document.getElementById('output');
      output.classList.remove('visible');
      const mode = normalizeGroupMode(document.getElementById('xport-configuration')?.dataset.currentMode);
      const linkedChannels = linkedWhiteChannels(mode, channel);
      if (linkedChannels.length > 1) {
        syncLinkedWhiteSliders(linkedChannels, value);
      }
      try {
        await requestJson(`api/v1/xport/group/channels/${channel}/value`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ value })
        });
        await renderXPort();
      } catch (error) {
        output.textContent = JSON.stringify(error, null, 2);
        output.classList.add('visible');
        await renderXPort();
      }
    }
    function syncLinkedWhiteSliders(channels, value) {
      const percent = Math.round(Number(value || 0) * 100);
      document.querySelectorAll('[data-xport-linked-white="true"]').forEach((wrap) => {
        if (!channels.includes(Number(wrap.dataset.xportChannel || 0))) { return; }
        const slider = wrap.querySelector('input[type="range"]');
        const valueEl = wrap.querySelector('strong');
        if (slider) { slider.value = String(percent); }
        if (valueEl) { valueEl.textContent = `${percent}%`; }
      });
    }
    function renderModeBody(channel) {
      if (channel.error) { return readoutRow(channel.error, ''); }
      switch (channel.confirmed_mode) {
        case 'AnalogInput': return readoutRow('Measured voltage', `${Number(channel.value || 0).toFixed(3)} V`);
        case 'DigitalOutput': return digitalOutput(channel);
        case 'PwmOutput': return pwmOutput(channel);
        case 'DigitalInputExternalVoltage':
        case 'DigitalInputInternalPullUp': return digitalInput(channel);
        case 'PulseCounterExternalVoltage':
        case 'PulseCounterInternalPullUp': return counterOutput(channel);
        default: return readoutRow('State', 'Disabled');
      }
    }
    function readoutRow(label, value) {
      const row = document.createElement('div');
      row.className = 'xport-control-row readout';
      const labelEl = document.createElement('span');
      labelEl.textContent = label;
      const valueEl = document.createElement('strong');
      valueEl.textContent = value;
      row.append(labelEl, valueEl);
      return row;
    }
    function renderLockedXPortBody(channel, groupMode) {
      const wrap = document.createElement('div');
      wrap.className = 'xport-control-row pwm';
      if (linkedWhiteChannels(groupMode, channel.channel).length > 1) {
        wrap.dataset.xportLinkedWhite = 'true';
        wrap.dataset.xportChannel = String(channel.channel);
      }
      const percent = Math.round(Number(channel.value || 0) * 100);
      const label = document.createElement('span');
      label.textContent = groupChannelSliderLabel(groupMode, channel.channel);
      const value = document.createElement('strong');
      value.textContent = `${percent}%`;
      const slider = document.createElement('input');
      slider.type = 'range';
      slider.min = '0';
      slider.max = '100';
      slider.value = String(percent);
      slider.onchange = () => setGroupChannelValue(channel.channel, Number(slider.value) / 100);
      wrap.append(label, slider, value);
      return wrap;
    }
    function digitalOutput(channel) {
      const row = document.createElement('div');
      row.className = 'xport-control-row';
      const value = Number(channel.value || 0) > 0;
      const label = document.createElement('span');
      label.textContent = 'Discrete output';
      const state = document.createElement('span');
      state.className = `state-text ${value ? 'on' : ''}`;
      state.textContent = value ? 'ON' : 'OFF';
      const toggle = document.createElement('button');
      toggle.className = `toggle ${value ? 'on' : ''}`;
      toggle.type = 'button';
      toggle.innerHTML = `<span>${value ? 'ON' : 'OFF'}</span>`;
      toggle.onclick = () => setValue(channel.channel, value ? 0 : 1);
      row.append(label, state, toggle);
      return row;
    }
    function digitalInput(channel) {
      const row = document.createElement('div');
      row.className = 'xport-control-row';
      const value = Boolean(channel.value);
      const label = document.createElement('span');
      label.textContent = 'Input';
      const state = document.createElement('span');
      state.className = `state-text ${value ? 'on' : ''}`;
      state.textContent = value ? 'CLOSED' : 'OPEN';
      const indicator = document.createElement('button');
      indicator.className = `toggle readonly ${value ? 'on' : ''}`;
      indicator.type = 'button';
      indicator.tabIndex = -1;
      indicator.innerHTML = `<span>${value ? 'CLOSED' : 'OPEN'}</span>`;
      row.append(label, state, indicator);
      return row;
    }
    function pwmOutput(channel) {
      const wrap = document.createElement('div');
      wrap.className = 'xport-control-row pwm';
      const percent = Math.round(Number(channel.value || 0) * 100);
      const label = document.createElement('span');
      label.textContent = 'PWM';
      const value = document.createElement('strong');
      value.textContent = `${percent}%`;
      const slider = document.createElement('input');
      slider.type = 'range';
      slider.min = '0';
      slider.max = '100';
      slider.value = String(percent);
      slider.onchange = () => setValue(channel.channel, Number(slider.value) / 100);
      wrap.append(label, slider, value);
      return wrap;
    }
    function counterOutput(channel) {
      const row = document.createElement('div');
      row.className = 'xport-control-row readout';
      const label = document.createElement('span');
      label.textContent = 'Counted pulses';
      const value = document.createElement('strong');
      value.textContent = String(Number(channel.counter || 0));
      row.append(label, value);
      return row;
    }
    function renderModeSelect(channel, modes) {
      const wrap = document.createElement('div');
      wrap.className = 'mode-select';
      const trigger = document.createElement('button');
      trigger.type = 'button';
      trigger.className = 'mode-trigger';
      trigger.textContent = labels[channel.desired_mode] || channel.desired_mode;
      trigger.onclick = () => {
        document.querySelectorAll('.mode-select.open').forEach((item) => {
          if (item !== wrap) { item.classList.remove('open'); }
        });
        wrap.classList.toggle('open');
      };

      const menu = document.createElement('div');
      menu.className = 'mode-menu';
      const orderedModes = modeOrder.filter((mode) => modes.includes(mode));
      for (const mode of orderedModes) {
        const option = document.createElement('button');
        option.type = 'button';
        option.dataset.mode = mode;
        option.className = `mode-option ${mode === channel.desired_mode ? 'active' : ''}`;
        option.textContent = labels[mode] || mode;
        option.onclick = () => {
          wrap.classList.remove('open');
          setMode(channel.channel, mode);
        };
        menu.appendChild(option);
      }
      wrap.append(trigger, menu);
      return wrap;
    }
    function normalizeGroupMode(mode) {
      if (mode === 'Independent') { return 'Universal I/O'; }
      if (mode === 'RgbwDimmer') { return 'RGBW Dimmer'; }
      if (mode === 'RgbPlusW' || mode === 'RGBPlusW') { return 'RGB + W'; }
      if (mode === 'WPlusWPlusWPlusW' || mode === 'WWWW') { return 'W + W + W + W'; }
      if (mode === 'TwoWPlusTwoW' || mode === '2xW + 2xW' || mode === '2XW + 2XW' || mode === '2*W + 2*W' || mode === '2×W + 2×W') { return '2×W + 2×W'; }
      if (mode === 'TwoWPlusWPlusW' || mode === '2xW + W + W' || mode === '2XW + W + W' || mode === '2*W + W + W' || mode === '2×W + W + W') { return '2×W + W + W'; }
      if (mode === 'WPlusWPlusTwoW' || mode === 'W + W + 2xW' || mode === 'W + W + 2XW' || mode === 'W + W + 2*W' || mode === 'W + W + 2×W') { return 'W + W + 2×W'; }
      if (mode === 'FourW' || mode === '4xW' || mode === '4XW' || mode === '4*W' || mode === '4×W') { return '4×W'; }
      return mode || 'Universal I/O';
    }
    function linkedWhiteChannels(groupMode, channel) {
      if (groupMode === '4×W') { return [1, 2, 3, 4]; }
      if (groupMode === '2×W + 2×W') { return channel <= 2 ? [1, 2] : [3, 4]; }
      if (groupMode === '2×W + W + W') { return channel <= 2 ? [1, 2] : [channel]; }
      if (groupMode === 'W + W + 2×W') { return channel >= 3 ? [3, 4] : [channel]; }
      return [channel];
    }
    function groupModeToProfile(mode) {
      return normalizeGroupMode(mode) === 'Universal I/O' ? 'Universal I/O' : 'LED Dimmer';
    }
    function groupChannelActiveLabel(groupMode, channel) {
      if (groupMode === 'RGB + W') {
        return channel === 4 ? 'White' : 'RGB';
      }
      if (groupMode === 'W + W + W + W' || groupMode === '4×W' || groupMode === '2×W + 2×W' || groupMode === '2×W + W + W' || groupMode === 'W + W + 2×W') {
        return 'White';
      }
      if (groupMode === 'RGBW Dimmer') {
        return 'RGBW';
      }
      return groupMode;
    }
    function groupChannelModeLabel(groupMode, channel) {
      if (groupMode === 'W + W + W + W' || groupMode === '4×W' || groupMode === '2×W + 2×W' || groupMode === '2×W + W + W' || groupMode === 'W + W + 2×W') {
        return 'White channel';
      }
      return `${rgbwRoles[channel] || 'RGBW'} channel`;
    }
    function groupChannelSliderLabel(groupMode, channel) {
      if (groupMode === 'W + W + W + W' || groupMode === '4×W' || groupMode === '2×W + 2×W' || groupMode === '2×W + W + W' || groupMode === 'W + W + 2×W') {
        return 'White';
      }
      return rgbwRoles[channel] || 'Channel';
    }
    function renderProfileSelect(currentProfile, disabled = false) {
      const wrap = document.createElement('div');
      wrap.className = `mode-select ${disabled ? 'locked' : ''}`;
      const trigger = document.createElement('button');
      trigger.type = 'button';
      trigger.className = 'mode-trigger';
      trigger.textContent = profileLabels[currentProfile] || currentProfile;
      trigger.disabled = disabled;
      trigger.title = disabled ? 'X-Port module is not installed' : '';
      trigger.onclick = () => {
        if (disabled) { return; }
        document.querySelectorAll('.mode-select.open').forEach((item) => {
          if (item !== wrap) { item.classList.remove('open'); }
        });
        wrap.classList.toggle('open');
      };

      const menu = document.createElement('div');
      menu.className = 'mode-menu';
      for (const profile of profileOrder) {
        const option = document.createElement('button');
        option.type = 'button';
        option.dataset.mode = profile;
        option.className = `mode-option ${profile === currentProfile ? 'active' : ''}`;
        option.textContent = profileLabels[profile] || profile;
        option.onclick = () => {
          wrap.classList.remove('open');
          setGroupMode(profile === 'LED Dimmer' ? 'RGBW Dimmer' : 'Universal I/O');
        };
        menu.appendChild(option);
      }
      wrap.append(trigger, menu);
      return wrap;
    }
    function renderConfigurationSelect(currentMode, disabled = false) {
      const wrap = document.createElement('div');
      wrap.className = `mode-select ${disabled ? 'locked' : ''}`;
      const trigger = document.createElement('button');
      trigger.type = 'button';
      trigger.className = 'mode-trigger';
      trigger.textContent = configurationLabels[currentMode] || groupModeLabels[currentMode] || currentMode;
      trigger.disabled = disabled;
      trigger.title = disabled ? 'X-Port module is not installed' : '';
      trigger.onclick = () => {
        if (disabled) { return; }
        document.querySelectorAll('.mode-select.open').forEach((item) => {
          if (item !== wrap) { item.classList.remove('open'); }
        });
        wrap.classList.toggle('open');
      };

      const menu = document.createElement('div');
      menu.className = 'mode-menu';
      for (const mode of configurationOrder) {
        const option = document.createElement('button');
        option.type = 'button';
        option.dataset.mode = mode;
        option.className = `mode-option ${mode === currentMode ? 'active' : ''}`;
        option.textContent = configurationLabels[mode] || groupModeLabels[mode] || mode;
        option.onclick = () => {
          wrap.classList.remove('open');
          setGroupMode(mode);
        };
        menu.appendChild(option);
      }
      wrap.append(trigger, menu);
      return wrap;
    }
    async function setValue(channel, value) {
      const output = document.getElementById('output');
      output.classList.remove('visible');
      try {
        await requestJson(`api/v1/xport/channels/${channel}/value`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ value })
        });
        await renderXPort();
      } catch (error) {
        output.textContent = JSON.stringify(error, null, 2);
        output.classList.add('visible');
      }
    }
    async function setPwmEnabled(channel, enabled) {
      const current = Number(channel.value || 0);
      await setValue(channel.channel, enabled ? (current > 0 ? current : 1) : 0);
    }
    async function resetCounter(channel) {
      const output = document.getElementById('output');
      output.classList.remove('visible');
      try {
        await requestJson(`api/v1/xport/channels/${channel}/counter/reset`, { method: 'POST' });
        await renderXPort();
      } catch (error) {
        output.textContent = JSON.stringify(error, null, 2);
        output.classList.add('visible');
      }
    }
    async function callApi(path) {
      const output = document.getElementById('output');
      output.classList.add('visible');
      output.textContent = 'Loading ' + path + ' ...';
      try {
        const payload = await requestJson(path);
        output.textContent = typeof payload === 'string' ? payload : JSON.stringify(payload, null, 2);
      } catch (error) {
        output.textContent = JSON.stringify(error, null, 2);
      }
    }
    async function showXPortDiagnostics() {
      const output = document.getElementById('output');
      output.classList.add('visible');
      output.textContent = 'Loading api/v1/xport ...';
      try {
        const payload = await requestJson('api/v1/xport');
        paintXPort(payload);
        output.textContent = JSON.stringify(payload, null, 2);
      } catch (error) {
        document.getElementById('xport-status').textContent = 'X-PORT: Error';
        output.textContent = JSON.stringify(error, null, 2);
      }
    }
    async function showExtensionDiagnostics() {
      const output = document.getElementById('output');
      output.classList.add('visible');
      output.textContent = 'Loading api/v1/extensions ...';
      try {
        const payload = await requestJson('api/v1/extensions');
        paintExtensions(payload);
        output.textContent = JSON.stringify(payload, null, 2);
      } catch (error) {
        document.getElementById('extension-status').textContent = 'X-BUS: Error';
        output.textContent = JSON.stringify(error, null, 2);
      }
    }
    async function showOneWireDiagnostics() {
      const output = document.getElementById('output');
      output.classList.add('visible');
      output.textContent = 'Loading api/v1/onewire ...';
      try {
        const payload = await requestJson('api/v1/onewire');
        paintOneWire(payload);
        output.textContent = JSON.stringify(payload, null, 2);
      } catch (error) {
        document.getElementById('onewire-status').textContent = '1-WIRE: Error';
        output.textContent = JSON.stringify(error, null, 2);
      }
    }
    function paintCarrier(carrier, appInfo = latestAppInfo) {
      latestAppInfo = appInfo || latestAppInfo;
      const online = Boolean(carrier && carrier.available);
      const healthDot = document.getElementById('carrier-health-dot');
      healthDot.classList.toggle('online', online);
      healthDot.classList.toggle('offline', !online);
      document.getElementById('carrier-health-state').textContent = online ? 'Normal' : 'Offline';
      const identity = carrier && carrier.identity ? carrier.identity : {};
      const model = cleanIdentityValue(identity.model || identity.product);
      document.getElementById('carrier-identity-product').textContent = model;
      document.getElementById('carrier-identity-subtitle').textContent = model === '--' ? '--' : 'Automation Controller';
      document.getElementById('carrier-identity-serial').textContent = cleanIdentityValue(identity.serial_number);
      document.getElementById('carrier-identity-hardware').textContent = formatHardware(identity.hardware_version, identity.hardware_revision);
      document.getElementById('carrier-identity-software').textContent = formatVersion(identity.software_version);
      refreshCarrierEdition(identity);
      document.getElementById('carrier-uptime').textContent = formatUptime(appInfo && appInfo.uptime_seconds);
      const monitoring = carrier && carrier.monitoring ? carrier.monitoring : {};
      const temperature = monitoring.temperature || {};
      const rails = {};
      for (const rail of monitoring.rails || []) {
        rails[rail.id] = rail;
      }
      const updatedAt = [
        paintMetric('board-temperature', temperature, 1, ' \u00B0C'),
        paintMetric('5v', rails['5v'], 2, ' V'),
        paintMetric('3v3', rails['3v3'], 2, ' V'),
        paintMetric('vin', rails.vin, 2, ' V')
      ].find(Boolean);
      document.getElementById('overview-updated').textContent = updatedAt ? `Updated ${updatedAt}` : 'Updated --';
    }
    async function renderCarrier() {
      try {
        const payload = await requestJson('api/v1/state');
        paintCarrier(payload.carrier, payload.app);
      } catch (error) {
        const health = document.getElementById('carrier-health-state');
        if (health) health.textContent = 'Unavailable';
      }
    }
    function formatVersion(value) {
      const text = String(value || '').trim();
      if (!text) return '--';
      return !text.toLowerCase().startsWith('v') ? `v${text}` : text;
    }
    function formatHardware(version, revision) {
      const versionText = formatVersion(version);
      const revisionText = String(revision || '').trim();
      const formattedRevision = revisionText
        ? (revisionText.toLowerCase().startsWith('rev.') ? revisionText : `Rev.${revisionText}`)
        : '';
      if (versionText === '--' && !formattedRevision) return '--';
      return [versionText === '--' ? '' : versionText, formattedRevision].filter(Boolean).join(' ');
    }
    function cleanIdentityValue(value) {
      const text = String(value || '').trim();
      return text || '--';
    }
    function refreshCarrierEdition(identity = null) {
      const target = document.getElementById('carrier-identity-variant');
      if (!target) { return; }
      target.textContent = formatCarrierEdition(identity);
    }
    function formatCarrierEdition(identity = null) {
      if (latestXPort) {
        const missing = latestXPort.topology === 'NotInstalled' || latestXPort.availability === 'OptionalMissing';
        return missing ? 'Standard' : 'Universal';
      }
      return cleanIdentityValue(identity && identity.variant);
    }
    function formatUptime(seconds) {
      const total = Math.max(0, Number(seconds || 0));
      const days = Math.floor(total / 86400);
      const hours = Math.floor((total % 86400) / 3600);
      const minutes = Math.floor((total % 3600) / 60);
      const secs = Math.floor(total % 60);
      const time = `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}:${String(secs).padStart(2, '0')}`;
      if (days > 0) return `${days} ${days === 1 ? 'day' : 'days'}, ${time}`;
      return time;
    }
    function paintMetric(elementId, metric, precision, unit) {
      const value = document.getElementById(`metric-${elementId}`);
      const meta = document.getElementById(`metric-${elementId}-meta`);
      if (!metric || !metric.available || metric.value === null || metric.value === undefined) {
        value.textContent = '--';
        meta.textContent = metric && metric.error ? `Error - ${metric.error}` : 'Unavailable';
        return '';
      }
      value.textContent = `${Number(metric.value).toFixed(precision)}${unit}`;
      meta.textContent = 'Normal';
      return metric.last_read_utc || '';
    }
    const carrierGroups = [
      {
        title: 'RS-485',
        outputs: ['rs485_ch1_termination', 'rs485_ch2_termination']
      },
      {
        title: 'X-Mod1',
        outputs: ['xmod1_flash_enable', 'xmod1_reset']
      },
      {
        title: 'X-Mod2',
        outputs: ['xmod2_flash_enable', 'xmod2_reset']
      },
      {
        title: 'USB',
        outputs: ['usb12_reset', 'usb3_reset', 'usb4_reset', 'usb_hub_reset']
      }
    ];
    const hostOutputOrder = ['user_led'];
    const hostButtonOrder = ['power', 'fn1', 'fn2'];
    function carrierOutputsById(carrier) {
      const result = {};
      for (const output of (carrier && carrier.outputs) || []) {
        result[output.id] = output;
      }
      return result;
    }
    async function renderCarrierIO() {
      const output = document.getElementById('output');
      try {
        const payload = await requestJson('api/v1/state');
        paintCarrierIO(payload.carrier, payload.outputs || {}, payload.buttons || {});
      } catch (error) {
        document.getElementById('carrier-io-status').textContent = 'CONTROLS: Error';
        output.textContent = JSON.stringify(error, null, 2);
        output.classList.add('visible');
      }
    }
    function paintCarrierIO(carrier, hostOutputs = {}, hostButtons = {}) {
      const online = Boolean(carrier && carrier.available);
      document.getElementById('carrier-io-status').textContent = online ? 'CONTROLS: online' : 'CONTROLS: offline';
      const byId = carrierOutputsById(carrier);
      const root = document.getElementById('carrier-io');
      root.innerHTML = '';
      const hostOutputCard = document.createElement('article');
      hostOutputCard.className = 'carrier-io-card';
      const hostOutputTitle = document.createElement('div');
      hostOutputTitle.className = 'carrier-io-title';
      hostOutputTitle.textContent = 'Host Outputs';
      hostOutputCard.appendChild(hostOutputTitle);
      for (const outputId of hostOutputOrder) {
        const item = hostOutputs[outputId] || { id: outputId, name: outputId, on: false };
        hostOutputCard.appendChild(hostOutputRow(item));
      }
      root.appendChild(hostOutputCard);
      const hostInputCard = document.createElement('article');
      hostInputCard.className = 'carrier-io-card';
      const hostInputTitle = document.createElement('div');
      hostInputTitle.className = 'carrier-io-title';
      hostInputTitle.textContent = 'Host Inputs';
      hostInputCard.appendChild(hostInputTitle);
      for (const buttonId of hostButtonOrder) {
        const item = hostButtons[buttonId] || { id: buttonId, name: buttonId.toUpperCase(), pressed: false };
        hostInputCard.appendChild(hostButtonRow(item));
      }
      root.appendChild(hostInputCard);
      for (const group of carrierGroups) {
        const card = document.createElement('article');
        card.className = 'carrier-io-card';
        const title = document.createElement('div');
        title.className = 'carrier-io-title';
        title.textContent = group.title;
        card.appendChild(title);
        for (const outputId of group.outputs) {
          const item = byId[outputId] || { id: outputId, name: outputId, on: false };
          const row = document.createElement('div');
          row.className = 'carrier-io-row';
          row.dataset.carrierOutputId = item.id;
          const label = document.createElement('span');
          label.textContent = carrierGroupItemLabel(group.title, item.name);
          const state = document.createElement('span');
          state.className = `state-text ${item.on ? 'on' : ''}`;
          state.textContent = item.on ? 'ON' : 'OFF';
          const toggle = document.createElement('button');
          toggle.className = `toggle ${item.on ? 'on' : ''}`;
          toggle.type = 'button';
          toggle.disabled = !online;
          toggle.innerHTML = `<span>${item.on ? 'ON' : 'OFF'}</span>`;
          toggle.onclick = () => setCarrierOutput(item.id, !item.on);
          row.append(label, state, toggle);
          card.appendChild(row);
        }
        root.appendChild(card);
      }
    }
    function carrierGroupItemLabel(groupTitle, itemName) {
      const prefix = `${groupTitle} `;
      return String(itemName || '').startsWith(prefix) ? String(itemName).slice(prefix.length) : itemName;
    }
    function hostOutputRow(item) {
      const row = document.createElement('div');
      row.className = 'carrier-io-row';
      row.dataset.hostOutputId = item.id;
      const label = document.createElement('span');
      label.textContent = item.name;
      const state = document.createElement('span');
      state.className = `state-text ${item.on ? 'on' : ''}`;
      state.textContent = item.on ? 'ON' : 'OFF';
      const toggle = document.createElement('button');
      toggle.className = `toggle ${item.on ? 'on' : ''}`;
      toggle.type = 'button';
      toggle.innerHTML = `<span>${item.on ? 'ON' : 'OFF'}</span>`;
      toggle.onclick = () => setHostOutput(item.id, !item.on);
      row.append(label, state, toggle);
      return row;
    }
    function hostButtonRow(item) {
      const row = document.createElement('div');
      row.className = 'carrier-io-row';
      row.dataset.hostButtonId = item.id;
      const label = document.createElement('span');
      label.textContent = item.name;
      const state = document.createElement('span');
      state.className = `state-text ${item.pressed ? 'on' : ''}`;
      state.textContent = item.pressed ? 'PRESSED' : 'OPEN';
      const indicator = document.createElement('button');
      indicator.className = `toggle readonly ${item.pressed ? 'on' : ''}`;
      indicator.type = 'button';
      indicator.disabled = true;
      indicator.innerHTML = `<span>${item.pressed ? 'ON' : 'OFF'}</span>`;
      row.append(label, state, indicator);
      return row;
    }
    function patchControlRow(attribute, id, active, action) {
      const row = [...document.querySelectorAll(`[${attribute}]`)].find((item) => item.getAttribute(attribute) === id);
      if (!row) return;
      const state = row.querySelector('.state-text');
      const toggle = row.querySelector('.toggle');
      state.classList.toggle('on', active);
      state.textContent = row.dataset.hostButtonId ? (active ? 'PRESSED' : 'OPEN') : (active ? 'ON' : 'OFF');
      toggle.classList.toggle('on', active);
      toggle.querySelector('span').textContent = active ? 'ON' : 'OFF';
      if (action) toggle.onclick = () => action(!active);
    }
    function patchCarrierOutput(item) {
      patchControlRow('data-carrier-output-id', item.id, Boolean(item.on), (on) => setCarrierOutput(item.id, on));
    }
    function patchHostOutput(item) {
      patchControlRow('data-host-output-id', item.id, Boolean(item.on), (on) => setHostOutput(item.id, on));
    }
    function patchHostButton(item) {
      patchControlRow('data-host-button-id', item.id, Boolean(item.pressed));
    }
    async function setHostOutput(outputId, on) {
      const output = document.getElementById('output');
      output.classList.remove('visible');
      try {
        const item = await requestJson(`api/v1/outputs/${outputId}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ on })
        });
        patchHostOutput(item);
      } catch (error) {
        output.textContent = JSON.stringify(error, null, 2);
        output.classList.add('visible');
      }
    }
    async function setCarrierOutput(outputId, on) {
      const output = document.getElementById('output');
      output.classList.remove('visible');
      try {
        const item = await requestJson(`api/v1/carrier/outputs/${outputId}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ on })
        });
        patchCarrierOutput(item);
      } catch (error) {
        output.textContent = JSON.stringify(error, null, 2);
        output.classList.add('visible');
      }
    }
    async function renderExtensions() {
      const output = document.getElementById('output');
      try {
        const payload = await requestJson('api/v1/extensions');
        paintExtensions(payload);
      } catch (error) {
        document.getElementById('extension-status').textContent = 'X-BUS: Error';
        output.textContent = JSON.stringify(error, null, 2);
        output.classList.add('visible');
      }
    }
    let extensionState = null;
    function paintExtensions(payload) {
      extensionState = payload;
      const powerOn = Boolean(payload.power && payload.power.on);
      document.getElementById('extension-status').textContent = `X-BUS: ${payload.modules.length} module${payload.modules.length === 1 ? '' : 's'} detected`;
      const powerToggle = document.getElementById('extension-power-toggle');
      powerToggle.className = `toggle ${powerOn ? 'on' : ''}`;
      powerToggle.querySelector('span').textContent = powerOn ? 'ON' : 'OFF';
      const modules = document.getElementById('modules');
      modules.innerHTML = '';
      if (!payload.modules.length) {
        const empty = document.createElement('div');
        empty.className = 'notice';
        empty.textContent = powerOn ? 'No X-BUS modules detected.' : 'Turn on X-BUS power, then scan modules.';
        modules.appendChild(empty);
        return;
      }
      for (const module of payload.modules) {
        const card = document.createElement('article');
        card.className = 'module-card';
        card.dataset.moduleId = module.id;
        const title = document.createElement('div');
        title.className = 'module-title';
        title.textContent = module.name;
        const meta = document.createElement('div');
        meta.className = 'module-meta';
        const isInputModule = module.kind === 'digital_input';
        const isRelayModule = module.kind === 'relay_output';
        const grid = document.createElement('div');
        grid.className = 'relay-grid';
        meta.textContent = `X-BUS - I2C-${module.bus} - Address ${module.address} - ${module.chip} - ${module.channels} ${isInputModule ? 'digital inputs' : 'relays'}`;
        const text = document.createElement('div');
        text.append(title, meta);
        const status = document.createElement('div');
        status.className = `status-pill ${module.available && powerOn ? '' : 'offline'}`;
        status.textContent = module.available && powerOn ? 'Online' : 'Offline';
        const remove = document.createElement('button');
        remove.className = 'danger-button';
        remove.type = 'button';
        remove.textContent = 'Remove';
        remove.onclick = () => removeExtensionModule(module.id);
        const actions = document.createElement('div');
        actions.className = 'module-actions';
        actions.append(status, remove);
        const head = document.createElement('div');
        head.className = 'module-head';
        head.append(text, actions);
        if (isRelayModule) {
        (module.relays || []).forEach((on, index) => {
          const row = document.createElement('div');
          row.className = 'relay-row';
          const label = document.createElement('span');
          label.textContent = `Relay ${index + 1}`;
          const state = document.createElement('span');
          state.className = `state-text ${on ? 'on' : ''}`;
          state.textContent = on ? 'ON' : 'OFF';
          const toggle = document.createElement('button');
          toggle.className = `toggle ${on ? 'on' : ''}`;
          toggle.type = 'button';
          toggle.innerHTML = `<span>${on ? 'ON' : 'OFF'}</span>`;
          toggle.disabled = !powerOn || !module.available;
          toggle.onclick = () => setExtensionRelay(module.id, index + 1, !on);
          row.append(label, state, toggle);
          grid.appendChild(row);
        });
        } else if (isInputModule) {
        (module.inputs || []).forEach((active, index) => {
          const row = document.createElement('div');
          row.className = 'relay-row';
          const label = document.createElement('span');
          label.textContent = `DI ${index + 1}`;
          const state = document.createElement('span');
          state.className = `state-text ${active ? 'on' : ''}`;
          state.textContent = active ? 'ON' : 'OFF';
          const indicator = document.createElement('button');
          indicator.className = `toggle readonly ${active ? 'on' : ''}`;
          indicator.type = 'button';
          indicator.disabled = true;
          indicator.innerHTML = `<span>${active ? 'ON' : 'OFF'}</span>`;
          row.append(label, state, indicator);
          grid.appendChild(row);
        });
        }
        card.append(head, grid);
        modules.appendChild(card);
      }
    }
    async function toggleExtensionPower() {
      const on = document.getElementById('extension-power-toggle').classList.contains('on');
      const payload = await requestJson('api/v1/extensions/power', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ on: !on })
      });
      paintExtensions(payload);
    }
    async function scanExtensions() {
      const payload = await requestJson('api/v1/extensions/scan', { method: 'POST' });
      paintExtensions(payload);
    }
    async function setExtensionRelay(moduleId, channel, on) {
      const started = performance.now();
      const payload = await requestJson(`api/v1/extensions/modules/${moduleId}/relays/${channel}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ on })
      });
      console.info('XDO8_UI_TRACE', { trace_id: payload.trace_id, module_id: moduleId, channel, on, request_ms: Math.round((performance.now() - started) * 100) / 100, backend_ms: payload.timing_ms });
      if (payload.module) {
        applyExtensionModule(payload.module);
      } else {
        await renderExtensions();
      }
      return payload;
    }
    function applyExtensionModule(module) {
      if (!extensionState || !Array.isArray(extensionState.modules)) {
        renderExtensions();
        return;
      }
      const modules = extensionState.modules.slice();
      const index = modules.findIndex((item) => item.id === module.id);
      if (index >= 0) {
        modules[index] = module;
      } else {
        modules.push(module);
      }
      patchExtensions({ ...extensionState, modules });
    }
    function patchExtensions(payload) {
      if (!extensionState || !Array.isArray(extensionState.modules) ||
          Boolean(extensionState.power && extensionState.power.on) !== Boolean(payload.power && payload.power.on) ||
          extensionState.modules.length !== payload.modules.length) {
        paintExtensions(payload);
        return;
      }
      const cards = new Map([...document.querySelectorAll('#modules .module-card')].map((card) => [card.dataset.moduleId, card]));
      if (cards.size !== payload.modules.length || payload.modules.some((module) => !cards.has(module.id))) {
        paintExtensions(payload);
        return;
      }
      extensionState = payload;
      for (const module of payload.modules) {
        const card = cards.get(module.id);
        const online = Boolean(module.available && payload.power.on);
        const status = card.querySelector('.status-pill');
        status.classList.toggle('offline', !online);
        status.textContent = online ? 'Online' : 'Offline';
        const values = module.kind === 'relay_output' ? module.relays : module.inputs;
        const rows = card.querySelectorAll('.relay-row');
        if (!Array.isArray(values) || rows.length !== values.length) {
          paintExtensions(payload);
          return;
        }
        for (let index = 0; index < rows.length; index++) {
          const on = Boolean(values[index]);
          const state = rows[index].querySelector('.state-text');
          const toggle = rows[index].querySelector('.toggle');
          state.classList.toggle('on', on);
          state.textContent = on ? 'ON' : 'OFF';
          toggle.classList.toggle('on', on);
          toggle.querySelector('span').textContent = on ? 'ON' : 'OFF';
          toggle.disabled = module.kind !== 'relay_output' || !payload.power.on || !module.available;
          if (module.kind === 'relay_output') toggle.onclick = () => setExtensionRelay(module.id, index + 1, !on);
        }
      }
    }
    async function removeExtensionModule(moduleId) {
      const payload = await requestJson(`api/v1/extensions/modules/${moduleId}`, { method: 'DELETE' });
      paintExtensions(payload.extensions);
    }
    async function renderOneWire() {
      const output = document.getElementById('output');
      try {
        const payload = await requestJson('api/v1/onewire');
        paintOneWire(payload);
      } catch (error) {
        document.getElementById('onewire-status').textContent = '1-WIRE: Error';
        output.textContent = JSON.stringify(error, null, 2);
        output.classList.add('visible');
      }
    }
    function paintOneWire(payload) {
      const powerOn = Boolean(payload.power && payload.power.on);
      const sensors = payload.sensors || [];
      const bridges = payload.bridges || [];
      document.getElementById('onewire-status').textContent = `1-WIRE: ${bridges.length} bridge${bridges.length === 1 ? '' : 's'}, ${sensors.length} sensor${sensors.length === 1 ? '' : 's'}`;
      const powerToggle = document.getElementById('onewire-power-toggle');
      powerToggle.className = `toggle ${powerOn ? 'on' : ''}`;
      powerToggle.querySelector('span').textContent = powerOn ? 'ON' : 'OFF';
      const modules = document.getElementById('onewire-modules');
      modules.innerHTML = '';
      if (!sensors.length && !bridges.length) {
        const empty = document.createElement('div');
        empty.className = 'notice';
        empty.textContent = powerOn ? 'No DS2482/DS18B20 devices detected on I2C-10.' : 'Turn on 1-Wire bus power, then scan sensors.';
        modules.appendChild(empty);
        return;
      }
      for (const bridge of bridges) {
        const card = document.createElement('article');
        card.className = 'module-card';
        const title = document.createElement('div');
        title.className = 'module-title';
        title.textContent = bridge.name;
        const meta = document.createElement('div');
        meta.className = 'module-meta';
        const enabled = bridge.enabled !== false;
        meta.textContent = `Bus I2C-${bridge.bus} - Address ${bridge.address} - ${bridge.chip}${enabled ? '' : ' - polling off'}${bridge.available ? '' : ' - unavailable'}`;
        const toggle = document.createElement('button');
        toggle.className = `toggle ${enabled ? 'on' : ''}`;
        toggle.type = 'button';
        toggle.title = enabled ? 'Disable bridge polling' : 'Enable bridge polling';
        toggle.innerHTML = `<span>${enabled ? 'ON' : 'OFF'}</span>`;
        toggle.disabled = !powerOn;
        toggle.onclick = () => setOneWireBridgeEnabled(bridge.id, !enabled);
        const head = document.createElement('div');
        head.className = 'module-head';
        const text = document.createElement('div');
        text.append(title, meta);
        head.append(text, toggle);
        card.append(head);
        modules.appendChild(card);
      }
      for (const sensor of sensors) {
        const card = document.createElement('article');
        card.className = 'module-card';
        const title = document.createElement('div');
        title.className = 'module-title';
        title.textContent = sensor.name;
        const meta = document.createElement('div');
        meta.className = 'module-meta';
        const remove = document.createElement('button');
        remove.className = 'danger-button';
        const cleanValue = sensor.temperature_c === null || sensor.temperature_c === undefined ? 'Unavailable' : `${Number(sensor.temperature_c).toFixed(3)} C`;
        meta.textContent = `Bridge ${sensor.address} - ROM ${sensor.rom} - ${cleanValue}${sensor.available ? '' : ' - unavailable'}`;
        remove.type = 'button';
        remove.textContent = sensor.added ? 'Remove' : 'Add';
        remove.onclick = () => sensor.added ? removeOneWireSensor(sensor.id) : addOneWireSensor(sensor.id);
        const head = document.createElement('div');
        head.className = 'module-head';
        const text = document.createElement('div');
        text.append(title, meta);
        head.append(text, remove);
        card.append(head);
        modules.appendChild(card);
      }
    }
    async function toggleOneWirePower() {
      const on = document.getElementById('onewire-power-toggle').classList.contains('on');
      const payload = await requestJson('api/v1/onewire/power', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ on: !on })
      });
      paintOneWire(payload);
    }
    async function scanOneWire() {
      const payload = await requestJson('api/v1/onewire/scan', { method: 'POST' });
      paintOneWire(payload);
    }
    async function refreshOneWire() {
      const payload = await requestJson('api/v1/onewire/refresh', { method: 'POST' });
      paintOneWire(payload);
    }
    async function setOneWireBridgeEnabled(bridgeId, enabled) {
      const payload = await requestJson(`api/v1/onewire/bridges/${bridgeId}/enabled`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled })
      });
      paintOneWire(payload);
    }
    async function addOneWireSensor(sensorId) {
      const payload = await requestJson(`api/v1/onewire/sensors/${sensorId}/add`, { method: 'POST' });
      paintOneWire(payload.onewire);
    }
    async function removeOneWireSensor(sensorId) {
      const payload = await requestJson(`api/v1/onewire/sensors/${sensorId}`, { method: 'DELETE' });
      paintOneWire(payload.onewire);
    }
    let uiInteractionDepth = 0;
    let uiScheduledFrame = 0;
    let uiPendingRenders = [];
    function uiInteractionStart() { uiInteractionDepth += 1; }
    function uiInteractionEnd() {
      uiInteractionDepth = Math.max(0, uiInteractionDepth - 1);
      if (!uiInteractionDepth && uiPendingRenders.length && !uiScheduledFrame) uiScheduleRender();
    }
    function uiScheduleRender() {
      if (uiScheduledFrame || !uiPendingRenders.length || uiInteractionDepth) return;
      uiScheduledFrame = window.requestAnimationFrame(() => {
        uiScheduledFrame = 0;
        if (uiInteractionDepth) return;
        const renders = uiPendingRenders;
        uiPendingRenders = [];
        for (const render of renders) render();
        if (uiPendingRenders.length) uiScheduleRender();
      });
    }
    function uiScheduleStateRender(render) {
      uiPendingRenders.push(render);
      uiScheduleRender();
    }
    document.addEventListener('pointerdown', uiInteractionStart, true);
    document.addEventListener('pointerup', uiInteractionEnd, true);
    document.addEventListener('pointercancel', uiInteractionEnd, true);
    function connectEvents() {
      const url = websocketUrl('ws');
      const socket = new WebSocket(url);
      socket.onmessage = (event) => {
        uiScheduleStateRender(() => {
        const message = JSON.parse(event.data);
        if (message.type === 'state') {
          applyTheme(message.ui && message.ui.theme ? message.ui.theme : selectedTheme);
          paintCarrier(message.carrier, message.app);
          paintCarrierIO(message.carrier, message.outputs || {}, message.buttons || {});
          paintXPort(message.xport);
          paintExtensions(message.extensions);
          paintOneWire(message.onewire);
          paintDiagnosticIndicators(message.diagnostic_indicators);
        } else if (message.type === 'carrier_changed') {
          paintCarrier(message.carrier);
          renderCarrierIO();
        } else if (message.type === 'carrier_output_changed') {
          paintCarrier(message.carrier);
          patchCarrierOutput(message.output);
        } else if (message.type === 'output_changed' || message.type === 'button_changed') {
          if (message.type === 'output_changed') patchHostOutput(message.output);
          else patchHostButton(message);
        } else if (message.type === 'xport_changed') {
          paintXPort(message.xport);
        } else if (message.type === 'xport_channel_changed') {
          renderXPort();
        } else if (message.type === 'extensions_changed') {
          patchExtensions(message.extensions);
        } else if (message.type === 'extension_module_changed') {
          applyExtensionModule(message.module);
        } else if (message.type === 'rs485_device_changed') {
          if (message.device) {
            rs485ApiState = mergeRs485CommandPayload({ device: message.device, selected_id: message.device.id });
            patchRs485Live({ devices: [message.device] });
          }
        } else if (message.type === 'extension_module_removed') {
          renderExtensions();
        } else if (message.type === 'extension_power_changed') {
          renderExtensions();
        } else if (message.type === 'onewire_changed') {
          paintOneWire(message.onewire);
        } else if (message.type === 'onewire_sensor_removed') {
          renderOneWire();
        } else if (message.type === 'onewire_power_changed') {
          renderOneWire();
        } else if (message.type === 'buzzer_changed') {
          paintBuzzer(message.buzzer);
        } else if (message.type === 'diagnostic_indicators_changed') {
          paintDiagnosticIndicators(message.diagnostic_indicators);
        } else if (message.type === 'ui_changed') {
          applyTheme(message.ui && message.ui.theme ? message.ui.theme : 'auto');
        }
        });
      };
      socket.onopen = () => console.info('IntellegyHUB WebSocket connected', url.href);
      socket.onerror = (event) => console.warn('IntellegyHUB WebSocket error', url.href, event);
      socket.onclose = () => {
        console.warn('IntellegyHUB WebSocket closed; reconnecting', url.href);
        window.setTimeout(connectEvents, 2000);
      };
    }
    function rs485ControlValue(id) {
      const element = document.getElementById(id);
      return element ? (element.dataset.value || element.value || '') : '';
    }
    function closeModeSelects(except) {
      document.querySelectorAll('.mode-select.open').forEach((item) => {
        if (item !== except) { item.classList.remove('open'); }
      });
    }
    function renderRs485Select(id, options, value, onChange) {
      const wraps = [...document.querySelectorAll(`#${id}`)];
      const wrap = wraps.find((item) => !item.closest('.hidden')) || wraps[wraps.length - 1];
      if (!wrap || !options.length) return;
      const selected = options.find((option) => String(option.value) === String(value)) || options[0];
      wrap.className = 'mode-select';
      wrap.dataset.value = selected.value;
      wrap.innerHTML = '';
      const trigger = document.createElement('button');
      trigger.type = 'button';
      trigger.className = 'mode-trigger';
      trigger.textContent = selected.label;
      trigger.onclick = (event) => {
        event.preventDefault();
        event.stopPropagation();
        closeModeSelects(wrap);
        wrap.classList.add('open');
      };
      const menu = document.createElement('div');
      menu.className = 'mode-menu';
      options.forEach((option) => {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = `mode-option ${String(option.value) === String(selected.value) ? 'active' : ''}`;
        button.textContent = option.label;
        button.onclick = (event) => {
          event.preventDefault();
          event.stopPropagation();
          wrap.dataset.value = option.value;
          wrap.classList.remove('open');
          renderRs485Select(id, options, option.value, onChange);
          if (onChange) onChange(option.value);
        };
        menu.appendChild(button);
      });
      wrap.append(trigger, menu);
    }
    function rs485InlineSelect(options, value, onChangeCall, disabled = false) {
      if (!options.length) return '<div class="rs485-empty">No options</div>';
      const selected = options.find((option) => String(option.value) === String(value)) || options[0];
      const buttons = options.map((option) => `
        <button type="button" class="mode-option ${String(option.value) === String(selected.value) ? 'active' : ''}" onclick="${onChangeCall(option.value)}" ${disabled ? 'disabled' : ''}>${option.label}</button>
      `).join('');
      return `
        <div class="mode-select">
          <button type="button" class="mode-trigger" onclick="closeModeSelects(this.parentElement); this.parentElement.classList.toggle('open')" ${disabled ? 'disabled' : ''}>${selected.label}</button>
          <div class="mode-menu">${buttons}</div>
        </div>`;
    }

    let rs485ApiState = null;
    let rs485SelectedId = null;
    let rs485DeviceQuery = '';
    let rs485DeviceFilter = 'all';
    let rs485DevicePage = 1;
    let rs485DeviceListSignature = '';
    const rs485DevicePageSize = 16;
    let rs485DetailTab = 'io';
    let rs485DiagnosticsPaused = false;
    let rs485DiagnosticsCommandInFlight = false;
    let rs485ManualCommandOpen = false;
    const rs485ManualCommandState = (() => {
      try { return JSON.parse(localStorage.getItem('intellegyhub.rs485.manual-command') || '{}') || {}; }
      catch (_error) { return {}; }
    })();
    function persistRs485ManualCommandState() {
      try { localStorage.setItem('intellegyhub.rs485.manual-command', JSON.stringify(rs485ManualCommandState)); }
      catch (_error) { /* Browser storage may be disabled. */ }
    }
    let rs485DiagnosticsFilter = 'all';
    let rs485DiagnosticsPortFilter = 'all';
    let rs485DiagnosticsGroupFilter = 'all';
    let rs485DiagnosticsOperationFilter = 'all';
    const RS485_DIAGNOSTICS_BUFFER_KEY = 'intellegyhub.rs485.diagnostics-buffer';
    function rs485DiagnosticsBufferSize() {
      const stored = Number(localStorage.getItem(RS485_DIAGNOSTICS_BUFFER_KEY) || 1000);
      return Math.min(1000, Math.max(100, Number.isFinite(stored) ? Math.round(stored) : 1000));
    }
    function saveRs485DiagnosticsBuffer(value) {
      const size = Math.min(1000, Math.max(100, Math.round(Number(value) || 1000)));
      localStorage.setItem(RS485_DIAGNOSTICS_BUFFER_KEY, String(size));
      const input = document.getElementById('rs485-diagnostics-buffer');
      if (input) input.value = String(size);
    }
    let rs485DiagnosticsSlaveFilter = 'all';
    let rs485DiagnosticsFunctionFilter = 'all';
    const rs485DiagnosticsTraffic = {};
    let rs485ScanPollTimer = null;
    let rs485ScanPollInFlight = false;
    let rs485ScanRequestVersion = 0;
    let rs485ScanLogSignature = '';
    let rs485ScanStartedAt = 0;
    let rs485LivePollTimer = null;
    let rs485DiagnosticsLiveInFlight = false;
    let rs485LivePollInFlight = false;
    let rs485LiveRequestVersion = 0;
    let rs485DetailSignature = '';
    let rs485CommandDepth = 0;
    const rs485PendingSettings = {};
    const rs485PendingCapabilityCommands = new Set();
    const rs485AddingScanIds = new Set();
    const rs485RemovingDeviceIds = new Set();
    const rs485InitialSettingsRead = new Set();
    let rs485LocalMode = localStorage.getItem('rs485LocalMode') === 'usb_real' ? 'usb_real' : 'mock';
    const RS485_DEFAULT_SERIAL_OPTIONS = [
      { value: '/dev/ttyAMA3', label: 'RS-485 CH1 (/dev/ttyAMA3)' },
      { value: '/dev/ttyAMA5', label: 'RS-485 CH2 (/dev/ttyAMA5)' }
    ];
    const RS485_BAUDRATE_OPTIONS = ['4800', '9600', '19200', '38400', '57600', '115200', '128000', '256000'].map((value) => ({ value, label: value }));
    const RS485_PARITY_OPTIONS = [
      { value: 'none', label: 'None' },
      { value: 'even', label: 'Even' },
      { value: 'odd', label: 'Odd' }
    ];
    const RS485_STOP_BITS_OPTIONS = ['1', '2'].map((value) => ({ value, label: value }));

    function rs485Template(templateId) {
      const fullTemplates = rs485ApiState && rs485ApiState.template_details || [];
      const summaries = rs485ApiState && rs485ApiState.templates || [];
      return fullTemplates.find((template) => template.template_id === templateId) || summaries.find((template) => template.template_id === templateId) || null;
    }
    function rs485TemplateLabel(templateId) {
      const template = rs485Template(templateId);
      return template ? template.model : templateId;
    }
    async function rs485Command(operation) {
      rs485CommandDepth += 1;
      try {
        return await operation();
      } finally {
        rs485CommandDepth = Math.max(0, rs485CommandDepth - 1);
      }
    }
    function rs485CurrentSerialPort() {
      const busValue = rs485ApiState && rs485ApiState.bus && rs485ApiState.bus.serial_port;
      const value = rs485ControlValue('rs485-serial-port');
      return value || busValue || '/dev/ttyAMA3';
    }
    function rs485BusPayload() {
      const bus = rs485ApiState && rs485ApiState.bus ? rs485ApiState.bus : {};
      return {
        serial_port: rs485CurrentSerialPort(),
        baudrate: Number(rs485ControlValue('rs485-baudrate') || 9600),
        parity: rs485ControlValue('rs485-parity') || 'none',
        stop_bits: Number(rs485ControlValue('rs485-stopbits') || 1),
        mode: rs485LocalMode === 'usb_real' ? 'usb_real' : 'mock',
        template_id: rs485ControlValue('rs485-template') || 'mio-8',
        enabled: bus.enabled !== false,
      };
    }
    function paintRs485PortControls() {
      const bus = rs485ApiState && rs485ApiState.bus || {};
      [['port', bus.enabled !== false, 'Port']].forEach(([kind, enabled, label]) => {
        document.querySelectorAll(`[data-rs485-port-control="${kind}"]`).forEach((button) => {
          button.classList.toggle('on', Boolean(enabled));
          button.innerHTML = `<span>${kind === 'port' ? '' : `${label} `}${enabled ? 'ON' : 'OFF'}</span>`;
          if (kind === 'port') {
            const state = button.closest('.rs485-connection-polling')?.querySelector('[data-rs485-bus-state]') || button.closest('.rs485-polling-toggle')?.querySelector('.state-text');
            if (state) {
              state.textContent = enabled ? 'ON' : 'OFF';
              state.classList.toggle('on', Boolean(enabled));
            }
          }
        });
      });
    }
    async function updateRs485PortSettings(changes) {
      const payload = { enabled: Boolean(changes.enabled) };
      try { paintRs485(await rs485Command(() => requestJson('api/v1/rs485/bus', { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) }))); } catch (error) { document.getElementById('rs485-status').textContent = `RS-485: port setting failed - ${errorDetail(error)}`; }
    }
    function toggleRs485Port() { updateRs485PortSettings({ enabled: !(rs485ApiState?.bus?.enabled !== false) }); }
    function rs485TemplateOptions() {
      const templates = rs485ApiState && rs485ApiState.templates && rs485ApiState.templates.length ? rs485ApiState.templates : [{ template_id: 'mio-8', model: 'MIO-8' }];
      return templates.map((template) => ({ value: template.template_id, label: template.model }));
    }
    function isRs485WindowsPort(value) {
      return /^COM\d+$/i.test(String(value || ''));
    }
    function isRs485LinuxPort(value) {
      return String(value || '').startsWith('/dev/');
    }
    function rs485LocalRealEnabled() {
      return rs485LocalMode === 'usb_real';
    }
    function rs485SerialOptions() {
      const detected = rs485ApiState && Array.isArray(rs485ApiState.serial_ports) && rs485ApiState.serial_ports.length
        ? rs485ApiState.serial_ports
        : RS485_DEFAULT_SERIAL_OPTIONS;
      if (!rs485LocalRealEnabled()) {
        return RS485_DEFAULT_SERIAL_OPTIONS;
      }
      const options = detected.filter((option) => isRs485WindowsPort(option.value));
      const baseOptions = options.length ? options : [];
      const current = rs485ApiState && rs485ApiState.bus && rs485ApiState.bus.serial_port;
      let result = baseOptions;
      if (current && !result.some((option) => option.value === current) && isRs485WindowsPort(current)) {
        result = result.concat([{ value: current, label: current }]);
      }
      return result;
    }
    function paintRs485LocalModeToggle() {
      const wrap = document.getElementById('rs485-local-mode');
      const target = document.getElementById('rs485-local-mode-toggle');
      if (!wrap || !target) return;
      const visible = Boolean(rs485ApiState && rs485ApiState.mock);
      wrap.classList.toggle('visible', visible);
      target.textContent = rs485LocalMode === 'usb_real' ? 'USB/Real' : 'Mock';
      target.title = rs485LocalMode === 'usb_real' ? 'Use local mock RS-485 data' : 'Use a real local USB RS-485 adapter';
    }
    async function toggleRs485LocalMode() {
      rs485LocalMode = rs485LocalMode === 'usb_real' ? 'mock' : 'usb_real';
      localStorage.setItem('rs485LocalMode', rs485LocalMode);
      paintRs485LocalModeToggle();
      initRs485MockControls();
      if (rs485LocalMode !== 'usb_real') {
        const target = document.getElementById('rs485-serial-port');
        if (target) target.dataset.value = '/dev/ttyAMA3';
      }
      rs485SelectedId = null;
      await saveRs485BusSettings();
    }
    async function saveRs485SerialPortSettings(value) {
      rs485ScanRequestVersion += 1;
      try {
        const payload = await rs485Command(() => requestJson('api/v1/rs485/bus', {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ serial_port: value })
        }));
        paintRs485(payload);
      } catch (error) {
        document.getElementById('rs485-status').textContent = `RS-485: port change failed - ${errorDetail(error)}`;
        renderRs485Mock();
      }
    }
    function initRs485MockControls() {
      let bus = rs485ApiState && rs485ApiState.bus ? rs485ApiState.bus : {
        serial_port: '/dev/ttyAMA3',
        baudrate: 9600,
        parity: 'none',
        stop_bits: 1,
        mode: 'mock'
      };
      if (bus.mode === 'usb_real') {
        rs485LocalMode = 'usb_real';
        localStorage.setItem('rs485LocalMode', rs485LocalMode);
      }
      const serialOptions = rs485SerialOptions();
      const fallbackSerial = (serialOptions[0] && serialOptions[0].value) || bus.serial_port || '/dev/ttyAMA3';
      const currentSerial = serialOptions.some((option) => option.value === bus.serial_port) ? bus.serial_port : fallbackSerial;
      paintRs485LocalModeToggle();
      renderRs485Select('rs485-serial-port', serialOptions, currentSerial || '/dev/ttyAMA3', saveRs485SerialPortSettings);
      renderRs485Select('rs485-baudrate', RS485_BAUDRATE_OPTIONS, String(bus.baudrate || 9600), saveRs485BusSettings);
      renderRs485Select('rs485-parity', RS485_PARITY_OPTIONS, bus.parity || 'none', saveRs485BusSettings);
      renderRs485Select('rs485-stopbits', RS485_STOP_BITS_OPTIONS, String(bus.stop_bits || 1), saveRs485BusSettings);
      const templates = rs485TemplateOptions();
      renderRs485Select('rs485-template', templates, templates[0] ? templates[0].value : '', renderRs485Mock);
    }
    function loadRs485Mock() {
      return renderRs485Mock();
    }
    async function saveRs485BusSettings() {
      try {
        const payload = await rs485Command(() => requestJson('api/v1/rs485/bus', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(rs485BusPayload())
        }));
        paintRs485(payload);
      } catch (error) {
        document.getElementById('rs485-status').textContent = 'RS-485: bus settings save failed';
      }
    }
    async function scanRs485Mock() {
      try {
        rs485ScanRequestVersion += 1;
        rs485ScanStartedAt = Date.now();
        const payload = await rs485Command(() => requestJson('api/v1/rs485/scan?compact=1', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(rs485BusPayload())
        }));
        patchRs485ScanSnapshot(payload);
        startRs485ScanPolling();
      } catch (error) {
        document.getElementById('rs485-status').textContent = 'RS-485: scan failed';
      }
    }
    async function stopRs485Scan() {
      try {
        rs485ScanRequestVersion += 1;
        const payload = await rs485Command(() => requestJson('api/v1/rs485/scan/stop?compact=1', { method: 'POST' }));
        patchRs485ScanSnapshot(payload);
        startRs485ScanPolling();
      } catch (error) {
        document.getElementById('rs485-status').textContent = `RS-485: stop failed - ${errorDetail(error)}`;
      }
    }
    function startRs485ScanPolling() {
      if (rs485ScanPollTimer) return;
      rs485ScanPollTimer = window.setInterval(refreshRs485ScanProgress, 500);
      refreshRs485ScanProgress();
    }
    function patchRs485ScanSnapshot(payload) {
      if (!rs485ApiState || !payload || !payload.scan_state) return;
      for (const key of ['scan_state', 'scan_log', 'scanned', 'scan_errors', 'status']) {
        if (Object.prototype.hasOwnProperty.call(payload, key)) rs485ApiState[key] = payload[key];
      }
      paintRs485ConfiguredDevices();
      paintRs485ScanResults();
      paintRs485ScanLog();
    }
    async function refreshRs485ScanProgress() {
      if (rs485ScanPollInFlight || rs485CommandDepth) return;
      rs485ScanPollInFlight = true;
      const version = rs485ScanRequestVersion;
      try {
        const payload = await requestJson('api/v1/rs485/scan/progress');
        if (version !== rs485ScanRequestVersion) return;
        patchRs485ScanSnapshot(payload);
        if (!payload.scan_state.running && rs485ScanPollTimer) {
          window.clearInterval(rs485ScanPollTimer);
          rs485ScanPollTimer = null;
        }
      } catch (error) {
        document.getElementById('rs485-status').textContent = `RS-485: scan progress failed - ${errorDetail(error)}`;
      } finally {
        rs485ScanPollInFlight = false;
      }
    }
    function stopRs485LivePolling() {
      if (rs485LivePollTimer) {
        window.clearInterval(rs485LivePollTimer);
        rs485LivePollTimer = null;
      }
    }
    function syncRs485LivePolling(payload = rs485ApiState) {
      const busEnabled = payload && (!payload.bus || payload.bus.enabled !== false);
      const hasDevices = Boolean(payload && Array.isArray(payload.devices) && payload.devices.length);
      if (busEnabled && hasDevices) startRs485LivePolling();
      else stopRs485LivePolling();
    }
    function startRs485LivePolling() {
      if (rs485LivePollTimer) return;
      rs485LivePollTimer = window.setInterval(async () => {
        if (!rs485ApiState || rs485ApiState.bus && rs485ApiState.bus.enabled === false || !Array.isArray(rs485ApiState.devices) || !rs485ApiState.devices.length) {
          stopRs485LivePolling();
          return;
        }
        if (document.hidden) return;
        if (rs485LivePollInFlight) return;
        if (rs485CommandDepth) return;
        if (rs485UserEditing()) return;
        const state = rs485ApiState && rs485ApiState.scan_state || {};
        if (state.running) return;
        rs485LivePollInFlight = true;
        const requestVersion = rs485LiveRequestVersion;
        try {
          const payload = await requestJson('api/v1/rs485/live');
          if (requestVersion === rs485LiveRequestVersion) {
            patchRs485Live(payload);
          }
        } catch (error) {
          document.getElementById('rs485-status').textContent = 'RS-485: backend unavailable';
        } finally {
          rs485LivePollInFlight = false;
        }
      }, 100);
    }
    function rs485UserEditing() {
      const section = document.getElementById('rs485-section');
      const active = document.activeElement;
      if (document.querySelector('.mode-select.open')) return true;
      if (rs485CommandDepth) return true;
      if (Object.values(rs485PendingSettings).some((item) => item && Object.keys(item).length)) return true;
      if (rs485AddingScanIds.size) return true;
      if (rs485RemovingDeviceIds.size) return true;
      const detail = document.getElementById('rs485-device-detail');
      return Boolean(active && detail && detail.contains(active) && ['INPUT', 'SELECT', 'TEXTAREA'].includes(active.tagName));
    }
    async function refreshRs485Devices() {
      try {
        const payload = await rs485Command(() => requestJson('api/v1/rs485/refresh', { method: 'POST' }));
        paintRs485(payload);
      } catch (error) {
        document.getElementById('rs485-status').textContent = 'RS-485: refresh failed';
      }
    }
    async function reloadRs485Templates() {
      try {
        await rs485Command(() => requestJson('api/v1/rs485/templates/reload', { method: 'POST' }));
        await renderRs485Mock();
        document.getElementById('rs485-status').textContent = 'RS-485: templates reloaded';
      } catch (error) {
        document.getElementById('rs485-status').textContent = 'RS-485: template reload failed';
      }
    }
    function openRs485Templates() {
      const panel = document.getElementById('rs485-template-service');
      if (!panel) return;
      panel.open = true;
      panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
    async function uploadRs485Template(input) {
      const file = input.files && input.files[0];
      if (!file) return;
      try {
        const body = await file.arrayBuffer();
        const payload = await rs485Command(() => requestJson(`api/v1/rs485/templates/upload?filename=${encodeURIComponent(file.name)}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/x-yaml' },
          body
        }));
        input.value = '';
        paintRs485(payload);
      } catch (error) {
        input.value = '';
        document.getElementById('rs485-status').textContent = `RS-485: template upload failed - ${errorDetail(error)}`;
      }
    }
    async function deleteRs485Template(templateId) {
      try {
        const payload = await rs485Command(() => requestJson(`api/v1/rs485/templates/${encodeURIComponent(templateId)}`, { method: 'DELETE' }));
        paintRs485(payload);
      } catch (error) {
        document.getElementById('rs485-status').textContent = `RS-485: template delete failed - ${errorDetail(error)}`;
      }
    }
    async function clearRs485MockScan() {
      try {
        const payload = await rs485Command(() => requestJson('api/v1/rs485/scan/clear', { method: 'POST' }));
        paintRs485(payload);
      } catch (error) {
        document.getElementById('rs485-status').textContent = `RS-485: clear failed - ${errorDetail(error)}`;
      }
    }
    async function clearRs485ScanLog() {
      try {
        // Ignore a live response that was started before Clear.
        const clearVersion = ++rs485LiveRequestVersion;
        if (rs485ApiState) {
          rs485ApiState.scan_log = [];
          rs485ApiState.scan_state = {
            ...(rs485ApiState.scan_state || {}),
            running: false,
            stop_requested: false,
            current_address: null,
            scanned: 0,
            found: 0,
            last_error: null,
            started_at: null,
          };
          rs485ScanStartedAt = 0;
          paintRs485ScanLog();
        }
        const payload = await rs485Command(() => requestJson('api/v1/rs485/scan/clear', { method: 'POST' }));
        if (clearVersion !== rs485LiveRequestVersion) return;
        rs485ScanStartedAt = 0;
        paintRs485(payload);
      } catch (error) {
        document.getElementById('rs485-status').textContent = `RS-485: clear failed - ${errorDetail(error)}`;
      }
    }
    async function addRs485Device(scanId) {
      rs485AddingScanIds.add(scanId);
      paintRs485ScanResults();
      try {
        const payload = await rs485Command(() => requestJson(`api/v1/rs485/devices/${encodeURIComponent(scanId)}/add`, { method: 'POST' }));
        rs485AddingScanIds.delete(scanId);
        paintRs485(payload);
      } catch (error) {
        rs485AddingScanIds.delete(scanId);
        document.getElementById('rs485-status').textContent = `RS-485: add failed - ${errorDetail(error)}`;
        renderRs485Mock();
      }
    }
    async function removeRs485MockDevice(deviceId) {
      if (rs485RemovingDeviceIds.has(deviceId)) return;
      rs485RemovingDeviceIds.add(deviceId);
      paintRs485ConfiguredDevices();
      try {
        const payload = await rs485Command(() => requestJson(`api/v1/rs485/devices/${encodeURIComponent(deviceId)}`, { method: 'DELETE' }));
        rs485RemovingDeviceIds.delete(deviceId);
        if (rs485SelectedId === deviceId) rs485SelectedId = null;
        paintRs485(payload);
      } catch (error) {
        rs485RemovingDeviceIds.delete(deviceId);
        document.getElementById('rs485-status').textContent = `RS-485: remove failed - ${errorDetail(error)}`;
        renderRs485Mock();
      }
    }
    async function renameRs485Device(deviceId) {
      const device = (rs485ApiState && rs485ApiState.devices || []).find((item) => item.id === deviceId);
      const name = window.prompt('Device name', device ? device.name : '');
      if (name === null || !String(name).trim()) return;
      try {
        const payload = await rs485Command(() => requestJson(`api/v1/rs485/devices/${encodeURIComponent(deviceId)}/name`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }) }));
        paintRs485(payload);
      } catch (error) {
        document.getElementById('rs485-status').textContent = `RS-485: rename failed - ${errorDetail(error)}`;
      }
    }
    function selectRs485MockDevice(deviceId) {
      rs485SelectedId = deviceId;
      paintRs485(rs485ApiState);
    }
    function setRs485DeviceQuery(value) {
      rs485DeviceQuery = String(value || '').trim().toLowerCase();
      rs485DevicePage = 1;
      paintRs485ConfiguredDevices();
    }
    function setRs485DeviceFilter(value) {
      rs485DeviceFilter = ['all', 'configured', 'discovered', 'online', 'offline'].includes(value) ? value : 'all';
      rs485DevicePage = 1;
      paintRs485ConfiguredDevices();
    }
    function setRs485DevicePage(page) {
      rs485DevicePage = Math.max(1, Number(page) || 1);
      paintRs485ConfiguredDevices();
    }
    function setRs485DetailTab(tab) {
      rs485DetailTab = ['settings', 'diagnostics', 'logs'].includes(tab) ? tab : 'io';
      const detail = document.getElementById('rs485-device-detail');
      if (!detail || !detail.querySelector('.rs485-detail-stack')) {
        rs485DetailSignature = '';
        paintRs485Detail();
        return;
      }
      detail.querySelectorAll('.rs485-detail-tabs button').forEach((button, index) => {
        const target = index === 0 ? 'io' : index === 1 ? 'settings' : index === 2 ? 'diagnostics' : 'logs';
        const active = target === rs485DetailTab;
        button.classList.toggle('active', active);
        button.setAttribute('aria-selected', String(active));
      });
      detail.querySelectorAll('[data-rs485-panel]').forEach((panel) => {
        panel.classList.toggle('hidden', panel.dataset.rs485Panel !== rs485DetailTab);
      });
      const polling = detail.querySelector('.rs485-polling-strip');
      if (polling) polling.classList.toggle('hidden', ['diagnostics', 'logs'].includes(rs485DetailTab));
      if (rs485DetailTab === 'diagnostics') refreshRs485DiagnosticsPanel(detail);
      if (rs485DetailTab === 'logs') refreshRs485LogsPanel(detail);
    }
    async function setRs485PollingEnabled(deviceId, enabled) {
      try {
        const payload = await rs485Command(() => requestJson(`api/v1/rs485/devices/${encodeURIComponent(deviceId)}/enabled`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ enabled })
        }));
        paintRs485(payload);
      } catch (error) {
        document.getElementById('rs485-status').textContent = `RS-485: polling toggle failed - ${errorDetail(error)}`;
      }
    }
    async function setRs485DeviceAccess(deviceId, level, enabled) {
      const body = level === 'read' ? { read_enabled: Boolean(enabled) } : { write_enabled: Boolean(enabled) };
      try {
        const payload = await rs485Command(() => requestJson(`api/v1/rs485/devices/${encodeURIComponent(deviceId)}/access`, {
          method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
        }));
        paintRs485(payload);
      } catch (error) {
        document.getElementById('rs485-status').textContent = `RS-485: ${level} toggle failed - ${errorDetail(error)}`;
      }
    }
    function mergeRs485CommandPayload(payload) {
      if (!payload || Array.isArray(payload.devices) || !payload.device) return payload;
      const current = rs485ApiState || {};
      const devices = (current.devices || []).map((device) => {
        if (device.id !== payload.device.id) return device;
        const merged = { ...device, ...payload.device };
        if (device.diagnostics && payload.device.diagnostics && !Array.isArray(payload.device.diagnostics.entries)) {
          merged.diagnostics = { ...device.diagnostics, ...payload.device.diagnostics, entries: device.diagnostics.entries };
        }
        return merged;
      });
      if (!devices.some((device) => device.id === payload.device.id)) devices.push(payload.device);
      return { ...current, ...payload, devices };
    }
    async function setRs485PollingSettings(deviceId) {
      const inputsInput = document.getElementById(`rs485-poll-inputs-${deviceId}`);
      const outputsInput = document.getElementById(`rs485-poll-outputs-${deviceId}`);
      const inputsMs = Math.max(50, Number(inputsInput && inputsInput.value || 100));
      const outputsMs = Math.max(50, Number(outputsInput && outputsInput.value || 250));
      try {
        const payload = await rs485Command(() => requestJson(`api/v1/rs485/devices/${encodeURIComponent(deviceId)}/polling`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            groups: {
              inputs: { mode: 'polling', interval_ms: inputsMs },
              outputs: { mode: 'polling', interval_ms: outputsMs }
            }
          })
        }));
        paintRs485(mergeRs485CommandPayload(payload));
      } catch (error) {
        document.getElementById('rs485-status').textContent = `RS-485: polling settings failed - ${errorDetail(error)}`;
      }
    }
    async function toggleRs485MockCollapse(deviceId) {
      try {
        const payload = await requestJson(`api/v1/rs485/devices/${encodeURIComponent(deviceId)}/collapse`, { method: 'POST' });
        paintRs485(mergeRs485CommandPayload(payload));
      } catch (error) {
        document.getElementById('rs485-status').textContent = 'RS-485: collapse failed';
      }
    }
    async function setRs485Capability(deviceId, capabilityId, value) {
      const started = performance.now();
      const commandKey = `${deviceId}:${capabilityId}`;
      if (rs485PendingCapabilityCommands.has(commandKey)) return;
      rs485PendingCapabilityCommands.add(commandKey);
      const row = [...document.querySelectorAll('[data-rs485-live-value]')].find((item) => item.dataset.rs485LiveValue === capabilityId);
      const toggle = row && row.querySelector('.toggle');
      if (toggle) toggle.disabled = true;
      try {
        const payload = await rs485Command(() => requestJson(`api/v1/rs485/devices/${encodeURIComponent(deviceId)}/capabilities/${encodeURIComponent(capabilityId)}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ value })
        }));
        if (typeof value === 'boolean') console.info('RS485_UI_TRACE', { trace_id: payload.trace_id, device_id: deviceId, capability_id: capabilityId, value, request_ms: Math.round((performance.now() - started) * 100) / 100, backend_ms: payload.timing_ms });
        if (typeof value === 'boolean' && payload.device) {
          rs485PendingCapabilityCommands.delete(commandKey);
          rs485ApiState = mergeRs485CommandPayload(payload);
          patchRs485Live({ devices: [payload.device] });
        } else {
          paintRs485(mergeRs485CommandPayload(payload));
        }
      } catch (error) {
        document.getElementById('rs485-status').textContent = 'RS-485: capability update failed';
        renderRs485Mock();
      } finally {
        rs485PendingCapabilityCommands.delete(commandKey);
        if (toggle && toggle.isConnected) toggle.disabled = toggle.dataset.rs485Writable === 'false';
      }
    }
    function setRs485PendingSetting(deviceId, capabilityId, value) {
      if (!rs485PendingSettings[deviceId]) rs485PendingSettings[deviceId] = {};
      rs485PendingSettings[deviceId][capabilityId] = value;
      closeModeSelects();
      document.getElementById('rs485-status').textContent = 'RS-485: device settings pending';
      paintRs485Detail();
    }
    async function applyRs485DeviceSettings(deviceId) {
      const originalDeviceId = deviceId;
      const pending = rs485PendingSettings[deviceId] || {};
      const entries = Object.entries(pending);
      if (!entries.length) {
        document.getElementById('rs485-status').textContent = 'RS-485: no pending device settings';
        return;
      }
      try {
        let payload = rs485ApiState;
        for (const [capabilityId, value] of entries) {
          payload = await rs485Command(() => requestJson(`api/v1/rs485/devices/${encodeURIComponent(deviceId)}/capabilities/${encodeURIComponent(capabilityId)}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ value })
          }));
          payload = mergeRs485CommandPayload(payload);
          deviceId = payload.selected_id || deviceId;
        }
        delete rs485PendingSettings[originalDeviceId];
        delete rs485PendingSettings[deviceId];
        paintRs485(payload);
      } catch (error) {
        delete rs485PendingSettings[originalDeviceId];
        delete rs485PendingSettings[deviceId];
        document.getElementById('rs485-status').textContent = `RS-485: apply failed - ${errorDetail(error)}`;
        renderRs485Mock();
      }
    }
    async function readRs485Device(deviceId, groupId = null) {
      try {
        const query = groupId ? `?group=${encodeURIComponent(groupId)}` : '';
        const payload = await rs485Command(() => requestJson(`api/v1/rs485/devices/${encodeURIComponent(deviceId)}/read${query}`, { method: 'POST' }));
        if (!groupId || groupId === 'settings') {
          delete rs485PendingSettings[deviceId];
          rs485InitialSettingsRead.add(deviceId);
        }
        paintRs485(mergeRs485CommandPayload(payload));
      } catch (error) {
        document.getElementById('rs485-status').textContent = `RS-485: read failed - ${errorDetail(error)}`;
      }
    }
    async function renderRs485Mock() {
      try {
        const payload = await requestJson('api/v1/rs485');
        paintRs485(payload);
      } catch (error) {
        document.getElementById('rs485-status').textContent = 'RS-485: backend unavailable';
      }
    }
    function paintRs485(payload, options = {}) {
      if (!payload) return;
      rs485ApiState = payload;
      paintRs485PortControls();
      const currentPort = rs485CurrentSerialPort();
      const devices = (payload.devices || []).filter((device) => device.serial_port === currentPort);
      if (!rs485SelectedId || !devices.some((device) => device.id === rs485SelectedId)) {
        const selectedOnPort = devices.find((device) => device.id === payload.selected_id);
        rs485SelectedId = (selectedOnPort && selectedOnPort.id) || (devices[0] && devices[0].id) || null;
      }
      if (!options.preserveStatus) {
        document.getElementById('rs485-status').textContent = payload.status || rs485ModeStatus(payload);
      }
      initRs485MockControls();
      paintRs485Templates();
      paintRs485ConfiguredDevices();
      // Scan results arrive while the scan is running; keep this list live.
      paintRs485ScanResults();
      paintRs485ScanLog();
      paintRs485Detail();
      syncRs485LivePolling(payload);
      if (payload.scan_state && payload.scan_state.running) startRs485ScanPolling();
    }
    function patchRs485Live(payload) {
      if (!payload) return;
      const liveDevices = Array.isArray(payload.devices) ? payload.devices : [];
      const byId = new Map(liveDevices.map((device) => [device.id, device]));
      rs485ApiState = {
        ...(rs485ApiState || {}),
        devices: (rs485ApiState && rs485ApiState.devices || []).map((device) => ({
          ...device,
          ...(byId.get(device.id) || {}),
        })),
      };
      const currentPort = rs485CurrentSerialPort();
      const devices = (rs485ApiState.devices || []).filter((device) => device.serial_port === currentPort);
      if (!rs485SelectedId || !devices.some((device) => device.id === rs485SelectedId)) {
        const selectedOnPort = devices.find((device) => device.id === payload.selected_id);
        rs485SelectedId = (selectedOnPort && selectedOnPort.id) || (devices[0] && devices[0].id) || null;
      }
      patchRs485LiveStatus(devices);
      if (rs485DetailTab === 'logs') patchRs485LogsTable();
      if (rs485DetailTab === 'diagnostics') patchRs485DiagnosticsLog();
      refreshRs485DiagnosticsLive();
      syncRs485LivePolling(rs485ApiState);
    }
    async function refreshRs485DiagnosticsLive() {
      if (!['diagnostics', 'logs'].includes(rs485DetailTab) || rs485DiagnosticsLiveInFlight || !rs485SelectedId) return;
      rs485DiagnosticsLiveInFlight = true;
      try {
        const payload = await requestJson(`api/v1/rs485/devices/${encodeURIComponent(rs485SelectedId)}/diagnostics/live`);
        const device = (rs485ApiState && rs485ApiState.devices || []).find((item) => item.id === payload.device_id);
        if (device && payload.diagnostics) {
          device.diagnostics = payload.diagnostics;
          if (rs485DetailTab === 'logs') patchRs485LogsTable();
          else patchRs485DiagnosticsLog();
        }
      } catch (_error) {
        // The normal full snapshot remains the fallback if diagnostics refresh fails.
      } finally {
        rs485DiagnosticsLiveInFlight = false;
      }
    }
    function patchRs485LiveStatus(devices) {
      const byId = new Map(devices.map((device) => [device.id, device]));
      document.querySelectorAll('[data-rs485-live-status]').forEach((node) => {
        const device = byId.get(node.dataset.rs485LiveStatus);
        if (!device) return;
        const status = rs485CommunicationStatus(device);
        node.className = `rs485-device-status ${status.toLowerCase()}`;
        node.textContent = status;
      });
      const selected = byId.get(rs485SelectedId);
      const detail = document.getElementById('rs485-device-detail');
      if (!selected || !detail) return;
      patchRs485LiveIoValues(detail, selected);
      const status = rs485CommunicationStatus(selected);
      const badge = detail.querySelector('[data-rs485-live-detail-status]');
      if (badge) {
        const isDetailBadge = badge.classList.contains('rs485-detail-status');
        badge.className = `${isDetailBadge ? 'rs485-detail-status' : 'status-pill'} ${status.toLowerCase()}`;
        badge.textContent = status;
      }
      const runtime = rs485Runtime(selected);
      const lastSeen = detail.querySelector('.rs485-detail-last-seen');
      if (lastSeen) lastSeen.innerHTML = `Last seen&nbsp;&nbsp; ${rs485LogTime(runtime.last_seen)}<br>Last update&nbsp;&nbsp; ${rs485LogTime(runtime.last_update || runtime.last_seen)}`;
      const pollingLine = detail.querySelector('.rs485-polling-line');
      if (pollingLine) pollingLine.innerHTML = rs485RuntimeSummary(selected);
      const allDevices = (rs485ApiState.devices || []).filter((device) => device.serial_port === rs485CurrentSerialPort());
      const online = allDevices.filter((device) => rs485CommunicationStatus(device) === 'ONLINE').length;
      const discovered = (rs485ApiState.scanned || []).filter((item) => item.serial_port === rs485CurrentSerialPort() && !item.configured).length;
      const counts = { all: allDevices.length + discovered, configured: allDevices.length, discovered, online, offline: allDevices.length - online };
      document.querySelectorAll('#rs485-device-filters [data-filter]').forEach((button) => { const count = button.querySelector('span'); if (count) count.textContent = counts[button.dataset.filter] || 0; });
    }
    function patchRs485LiveIoValues(detail, device) {
      const values = device.values || {};
      detail.querySelectorAll('[data-rs485-live-value]').forEach((row) => {
        const capabilityId = row.dataset.rs485LiveValue;
        if (rs485PendingCapabilityCommands.has(`${device.id}:${capabilityId}`)) return;
        if (!Object.prototype.hasOwnProperty.call(values, capabilityId)) return;
        const value = values[capabilityId];
        const error = values[`${capabilityId}__error`];
        const active = Boolean(value);
        const text = row.querySelector('.state-text');
        if (text) {
          text.textContent = active ? 'ON' : 'OFF';
          text.classList.toggle('on', active);
        }
        const toggle = row.querySelector('.toggle');
        if (toggle) {
          toggle.classList.toggle('on', active);
          const label = toggle.querySelector('span');
          if (label) label.textContent = active ? 'ON' : 'OFF';
          if (row.dataset.rs485LiveType === 'binary_input') toggle.setAttribute('aria-label', `${toggle.closest('.relay-row')?.querySelector('span')?.textContent || 'Input'} ${active ? 'ON' : 'OFF'}`);
          if (row.dataset.rs485LiveType === 'switch') {
            toggle.removeAttribute('onclick');
            toggle.onclick = () => setRs485Capability(device.id, capabilityId, !active);
            toggle.disabled = toggle.dataset.rs485Writable === 'false';
          }
        }
        row.classList.toggle('rs485-stale', Boolean(error));
        row.title = error || '';
      });
    }
    function rs485HexPayload(entry) {
      const raw = String(entry.rx_hex || entry.tx_hex || entry.payload || '').trim();
      const bytes = raw.match(/[0-9a-f]{2}/gi);
      if (!bytes || bytes.length < 2) return entry.payload || entry.message || '—';
      const exception = bytes[1] && (parseInt(bytes[1], 16) & 0x80);
      const classes = bytes.map(() => 'hex-data');
      classes[0] = 'hex-slave';
      classes[1] = exception ? 'hex-exception' : 'hex-function';
      if (bytes.length >= 8) {
        classes[2] = classes[3] = 'hex-address';
        classes[4] = classes[5] = 'hex-quantity';
        classes[bytes.length - 2] = classes[bytes.length - 1] = 'hex-crc';
      } else if (bytes.length >= 5) {
        classes[2] = classes[3] = 'hex-address';
        classes[bytes.length - 2] = classes[bytes.length - 1] = 'hex-crc';
      }
      return bytes.map((byte, index) => `<span class="hex-token ${classes[index]}">${byte.toUpperCase()}</span>`).join('');
    }
    function rs485DiagnosticsTrafficRows(device) {
      const traffic = ((device.diagnostics && device.diagnostics.entries) || []).slice(-rs485DiagnosticsBufferSize());
      const filteredTraffic = traffic.filter((entry) => {
        if (rs485DiagnosticsFilter === 'errors' && entry.result !== 'error') return false;
        if (rs485DiagnosticsFilter === 'master' && entry.direction !== 'MASTER') return false;
        if (rs485DiagnosticsFilter === 'slave' && entry.direction !== 'SLAVE') return false;
        if (rs485DiagnosticsSlaveFilter !== 'all' && String(entry.slave_address) !== rs485DiagnosticsSlaveFilter) return false;
        if (rs485DiagnosticsFunctionFilter !== 'all' && String(entry.function) !== rs485DiagnosticsFunctionFilter) return false;
        if (rs485DiagnosticsPortFilter !== 'all' && String(entry.serial_port) !== rs485DiagnosticsPortFilter) return false;
        if (rs485DiagnosticsGroupFilter !== 'all' && String(entry.group) !== rs485DiagnosticsGroupFilter) return false;
        if (rs485DiagnosticsOperationFilter !== 'all' && String(entry.operation) !== rs485DiagnosticsOperationFilter) return false;
        return true;
      });
      return filteredTraffic.map((entry) => {
        const id = String(entry.transaction_id || entry.seq || '');
        const raw = String(entry.rx_hex || entry.tx_hex || entry.payload || '').trim();
        const frameBytes = raw.match(/[0-9a-f]{2}/gi) || [];
        const functionCode = frameBytes[1] ? frameBytes[1].toUpperCase() : '—';
        const payload = rs485DiagnosticsView === 'raw' ? (raw || '—') : rs485HexPayload(entry);
        const frame = raw.replaceAll("'", '').replaceAll('"', '');
        const decoded = rs485DecodeFrame(raw, entry);
        return `<div class="rs485-traffic-entry ${entry.result === 'error' ? 'error' : ''}" data-rs485-entry="${id}"><span>${entry.seq || '—'}</span><span>${rs485RuntimeTime(entry.ts)}</span><span>${entry.response_ms ? '+' + entry.response_ms + ' ms' : '+0 ms'}</span><span class="traffic-direction ${entry.direction === 'SLAVE' ? 'slave' : ''}">${entry.direction || 'MASTER'}</span><span>${entry.slave_address}</span><span>${entry.function || '—'}</span><span>${functionCode}</span><span class="rs485-traffic-payload">${payload}</span><span>${entry.bytes || '—'}</span><span class="rs485-traffic-crc">${entry.crc || 'OK'}</span></div><div class="rs485-traffic-detail hidden" data-rs485-detail="${id}"><div><strong>Transaction ${id}</strong><span>Port: ${entry.serial_port || '—'} · ${entry.group || 'unknown'} · ${entry.operation || 'READ'}</span><span>TX ${entry.tx_ts || entry.ts || '—'}</span><span>RX ${entry.rx_ts || (entry.result === 'response' ? entry.ts : '—')}</span></div><div><span>TX: ${entry.tx_hex || '—'}</span><span>RX: ${entry.rx_hex || '—'}</span><span>Decoded: ${decoded}</span><span>CRC: ${entry.crc || '—'}</span><span>Online: ${entry.online == null ? '—' : entry.online ? 'yes' : 'no'} · Backoff: ${entry.backoff_ms || 0} ms · WebSocket: ${entry.ws_published ? 'published' : 'pending'}</span><span>Exception: ${rs485ExceptionLabel(raw) || '—'}</span></div><button type="button" onclick="copyRs485Frame('${frame}')">Copy frame</button></div>`;
      }).join('');
    }
    const RS485_EXCEPTION_CODES = { '01': 'Illegal Function', '02': 'Illegal Data Address', '03': 'Illegal Data Value', '04': 'Server Device Failure', '06': 'Server Device Busy', '0A': 'Gateway Path Unavailable', '0B': 'Gateway Target Device Failed to Respond' };
    function rs485ExceptionLabel(raw) {
      const bytes = String(raw || '').trim().split(/\s+/).filter(Boolean);
      if (bytes.length < 3 || !(parseInt(bytes[1], 16) & 0x80)) return '';
      const code = bytes[2].toUpperCase();
      return `${code} ${RS485_EXCEPTION_CODES[code] || 'Unknown exception'}`;
    }
    function rs485DecodeFrame(raw, entry) {
      const bytes = String(raw || '').trim().split(/\s+/).filter(Boolean).map((value) => parseInt(value, 16));
      if (bytes.length < 2 || bytes.some(Number.isNaN)) return entry.message || 'Malformed frame';
      const fn = bytes[1];
      if (fn & 0x80) return `Exception ${rs485ExceptionLabel(raw)}`;
      if ([1, 2, 3, 4].includes(fn) && bytes.length >= 3) return `FC${String(fn).padStart(2, '0')} byte count ${bytes[2]}`;
      if ([5, 6, 15, 16].includes(fn) && bytes.length >= 6) return `FC${String(fn).padStart(2, '0')} address ${((bytes[2] << 8) | bytes[3])} value/quantity ${((bytes[4] << 8) | bytes[5])}`;
      return entry.message || `FC${String(fn).padStart(2, '0')}`;
    }
    let rs485DiagnosticsView = 'decoded';
    function toggleRs485DiagnosticsView() { rs485DiagnosticsView = rs485DiagnosticsView === 'decoded' ? 'raw' : 'decoded'; paintRs485Detail(); }
    async function copyRs485Frame(frame) { if (frame && navigator.clipboard) await navigator.clipboard.writeText(frame); }
    function exportRs485Diagnostics(format) {
      const device = (rs485ApiState && rs485ApiState.devices || []).find((item) => item.id === rs485SelectedId);
      if (!device) return;
      const entries = (device.diagnostics && device.diagnostics.entries) || [];
      const content = format === 'json' ? JSON.stringify({ device: device.name, exported_at: new Date().toISOString(), entries }, null, 2) : [
        ['seq','timestamp','direction','slave','function','bytes','crc','response_ms','result','message'],
        ...entries.map((entry) => [entry.seq, entry.ts, entry.direction, entry.slave_address, entry.function, entry.bytes, entry.crc, entry.response_ms, entry.result, entry.message])
      ].map((row) => row.map((value) => `"${String(value ?? '').replaceAll('"', '""')}"`).join(',')).join('\\n');
      const blob = new Blob([content], { type: format === 'json' ? 'application/json' : 'text/csv;charset=utf-8' });
      const link = document.createElement('a');
      link.href = URL.createObjectURL(blob);
      link.download = `${device.name || 'rs485'}-diagnostics.${format}`;
      link.click();
      URL.revokeObjectURL(link.href);
    }
    function setRs485DiagnosticsFilter(value) { rs485DiagnosticsFilter = value; patchRs485DiagnosticsLog(); }
    function setRs485DiagnosticsSlaveFilter(value) { rs485DiagnosticsSlaveFilter = value; patchRs485DiagnosticsLog(); }
    function setRs485DiagnosticsFunctionFilter(value) { rs485DiagnosticsFunctionFilter = value; patchRs485DiagnosticsLog(); }
    function patchRs485DiagnosticsLog() {
      const panel = document.querySelector(`#rs485-device-detail [data-rs485-panel="${rs485DetailTab === 'logs' ? 'logs' : 'diagnostics'}"]`);
      if (!panel) return;
      const device = (rs485ApiState && rs485ApiState.devices || []).find((item) => item.id === rs485SelectedId);
      if (!device) return;
      const body = panel.querySelector('.rs485-traffic-empty');
      if (!body) return;
      const entries = (device.diagnostics && device.diagnostics.entries) || [];
      body.classList.toggle('no-entries', !entries.length);
      body.innerHTML = entries.length ? rs485DiagnosticsTrafficRows(device) : 'No Modbus traffic recorded yet.';
      const footer = panel.querySelector('.rs485-diagnostics-footer');
      if (footer) footer.innerHTML = renderRs485DiagnosticsFooter(device);
    }
    function paintRs485ScanResults() {
      const scanResults = document.getElementById('rs485-scan-results');
      if (!scanResults || !rs485ApiState) return;
      const currentPort = rs485CurrentSerialPort();
      const allDevices = (rs485ApiState.devices || []).filter((device) => device.serial_port === currentPort);
      const listTitle = document.getElementById('rs485-device-list-title');
      if (listTitle) listTitle.textContent = 'Devices';
      const rows = (rs485ApiState.scanned || []).filter((item) => !item.configured && item.serial_port === currentPort);
      const errors = (rs485ApiState.scan_errors || []).filter((item) => item.serial_port === currentPort);
      const running = Boolean(rs485ApiState.scan_state && rs485ApiState.scan_state.running && rs485ApiState.scan_state.serial_port === currentPort);
      const markup = rows.length
        ? rows.map((item) => `
            <div class="rs485-table-row scan">
              <span><span class="rs485-label">Slave</span> <span class="rs485-primary">${item.slave_address}</span></span>
              <span>${rs485TemplateLabel(item.template_id)}</span>
              <span>${rs485ScanResultLabel(item)}</span>
              <div class="rs485-row-actions">
                <button type="button" data-rs485-add-id="${item.id}">Add</button>
              </div>
            </div>`).join('')
        : (running
            ? '<div class="rs485-empty">Scanning selected RS-485 bus. Found devices will appear here immediately.</div>'
            : errors.length && !(rs485ApiState.devices || []).some((device) => device.serial_port === currentPort)
            ? `<div class="rs485-empty">No devices found on the selected bus. ${errors.length} address(es) did not respond. See Scan Log for details.</div>`
            : '<div class="rs485-empty">No scan results. Press Scan to search the Modbus slave address range on the selected RS-485 bus.</div>');
      if (scanResults.innerHTML !== markup) scanResults.innerHTML = markup;
    }
    function rs485ScanResultLabel(item) {
      const confidence = String(item.confidence || '').toLowerCase();
      const label = confidence === 'template probe' || confidence === 'mock match'
        ? 'Found'
        : (item.status || item.confidence || 'Found');
      const baudrate = item.baudrate || (rs485ApiState && rs485ApiState.bus && rs485ApiState.bus.baudrate);
      return baudrate ? `${label} @ ${baudrate} baud` : label;
    }
    function paintRs485ScanLog() {
      const progress = document.getElementById('rs485-scan-progress');
      const logTarget = document.getElementById('rs485-scan-log');
      const detailLogTarget = document.getElementById('rs485-device-scan-log');
      const detailLogSummary = document.getElementById('rs485-device-scan-summary');
      const detailLogFooter = document.getElementById('rs485-device-scan-footer');
      const scanButton = document.getElementById('rs485-scan-button');
      if (!rs485ApiState) return;
      const currentPort = rs485CurrentSerialPort();
      const state = rs485ApiState.scan_state || {};
      const running = Boolean(state.running && state.serial_port === currentPort);
      const busyElsewhere = Boolean(state.running && state.serial_port !== currentPort);
      const stopping = running && Boolean(state.stop_requested);
      if (scanButton) {
        scanButton.disabled = busyElsewhere || stopping;
        scanButton.textContent = stopping ? 'Stopping...' : busyElsewhere ? 'Scan busy' : running ? 'Stop' : 'Scan devices';
        scanButton.onclick = running ? stopRs485Scan : scanRs485Mock;
        scanButton.setAttribute('aria-label', stopping ? 'Stopping RS-485 scan' : busyElsewhere ? `Scanning ${state.serial_port}` : running ? 'Stop RS-485 scan' : 'Start RS-485 scan');
      }
      const scanned = Number(state.scanned || 0);
      const total = Math.max(1, Number(state.total || 255));
      const found = Number(state.found || 0);
      const percent = Math.min(100, Math.round((scanned / total) * 100));
      const rows = (rs485ApiState.scan_log || []).filter((item) => item.serial_port === currentPort).slice(-512).reverse();
      const progressText = running
        ? `Scanning ${state.serial_port}: slave ${state.current_address || '--'} / ${total} | scanned ${scanned} | found ${found}${state.last_error ? ` | ${state.last_error}` : ''}`
        : 'Scan is idle.';
      if (progress) progress.textContent = progressText;
      if (scanButton) {
        scanButton.classList.toggle('scanning', running);
        scanButton.classList.toggle('stop', running);
        const buttonMarkup = stopping ? 'Stopping...' : busyElsewhere ? 'Scan busy' : running ? '<span class="rs485-stop-square"></span>Stop' : 'Scan devices';
        if (scanButton.innerHTML !== buttonMarkup) scanButton.innerHTML = buttonMarkup;
      }
      const logSignature = currentPort + ':' + rows.length + ':' + (rows[0]?.seq ?? rows[0]?.ts ?? '');
      if (logSignature !== rs485ScanLogSignature) {
        if (logTarget) logTarget.textContent = rows.length ? rows.map(rs485ScanLogLine).join('\\n') : 'No scan log yet.';
        if (detailLogTarget) detailLogTarget.innerHTML = rows.length ? rows.map(rs485ScanLogEntry).join('') : '<div class="rs485-empty">No scan log yet.</div>';
        rs485ScanLogSignature = logSignature;
      }
      if (detailLogSummary) detailLogSummary.textContent = running ? `(Scanning addresses 1 - ${state.total || 255})` : `Last scan: ${rs485LogTime(rows[0] && rows[0].ts)}`;
      if (detailLogFooter) {
        const total = Math.max(1, Number(state.total || 255));
        const scanned = Number(state.scanned || 0);
        const progress = running ? `
          <div class="rs485-progress-track"><span class="rs485-progress-fill" style="width:${percent}%"></span></div>
          <strong>${scanned} / ${total} (${percent}%)</strong>` : '';
        detailLogFooter.innerHTML = `
          <div class="rs485-log-status"><span class="active">${running ? 'Scan in progress' : 'Scan complete'}</span>
          <span>Current address: <strong>${state.current_address || (running ? '--' : scanned || '--')} / ${total}</strong></span>
          ${running ? progress + `<span>Elapsed: <strong>${rs485ScanElapsed(state, rows)}</strong></span><span>Est. remaining: <strong>${rs485ScanRemaining(state)}</strong></span><span>Devices found: <strong>${found}</strong></span>` : `<span>Elapsed: <strong>${rs485ScanElapsed(state, rows)}</strong></span><span>Est. remaining: <strong>00:00:00</strong></span><span>Devices found: <strong>${Number(state.found || 0)}</strong></span>`}</div>`;
      }
    }
    function rs485ScanLogEntry(item) {
      const result = String(item.result || 'event').toLowerCase();
      const icon = result === 'matched' ? '✓' : result === 'timeout' ? '!' : result === 'progress' ? '◌' : result === 'stopped' ? '■' : 'i';
      const message = rs485ScanLogLine(item).replace(/^\S+ \| /, '');
      return `<div class="rs485-log-entry ${result}"><span class="rs485-log-time">${rs485LogTime(item.ts)}</span><span class="rs485-log-icon">${icon}</span><span class="rs485-log-message">${message}</span></div>`;
    }
    function rs485ScanElapsed(state, rows = []) {
      if (!state.running && !rows.length && !state.started_at) return '00:00:00';
      const started = rs485ScanStartedAt || Date.parse(state.started_at || (rows.length ? rows[rows.length - 1].ts : ''));
      const ended = state.running ? Date.now() : (rows.length ? Date.parse(rows[0].ts) : Date.now());
      const seconds = started && ended >= started ? Math.round((ended - started) / 1000) : 0;
      return rs485FormatDuration(seconds);
    }
    function rs485ScanRemaining(state) {
      const scanned = Number(state.scanned || 0);
      const total = Number(state.total || 255);
      if (!scanned || scanned >= total) return '00:00:00';
      return rs485FormatDuration(Math.max(1, Math.round((total - scanned) * 0.25)));
    }
    function rs485FormatDuration(seconds) {
      const value = Math.max(0, Number(seconds) || 0);
      const hours = Math.floor(value / 3600);
      const minutes = Math.floor((value % 3600) / 60);
      const secs = value % 60;
      return `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}:${String(secs).padStart(2, '0')}`;
    }
    function rs485ScanLogLine(item) {
      const result = String(item.result || '').toLowerCase();
      const address = item.slave_address == null ? '--' : item.slave_address;
      if (result === 'matched') {
        const templateId = item.template_id || (rs485ApiState && rs485ApiState.scan_state && rs485ApiState.scan_state.template_id) || '';
        const template = rs485TemplateLabel(templateId) || item.message || 'device';
        return `Found device at address ${address}: ${template}`;
      }
      if (result === 'timeout') {
        return `Address ${address}: timeout (no response)`;
      }
      if (result === 'stopped') {
        return `Scan stopped at address ${address}`;
      }
      if (result === 'progress') {
        return item.message || `Scanning... address ${address}`;
      }
      if (item.message) return item.message;
      return `Address ${address}: ${item.result || 'scan event'}`;
    }
    function rs485ModeStatus(payload) {
      const mode = payload && payload.effective_mode;
      const bus = payload && payload.bus || {};
      const port = bus.serial_port;
      const link = `${port || 'serial port'} ${bus.baudrate || 9600} ${bus.parity || 'none'} ${bus.stop_bits || 1} stop`;
      if (mode === 'usb_real') return `RS-485: USB/Real on ${link}`;
      if (mode === 'mock') return 'RS-485: template-driven mock';
      return `RS-485: Modbus on ${link}`;
    }
    function rs485LogTime(value) {
      if (!value) return '--:--:--';
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return value;
      return date.toLocaleTimeString([], { hour12: false });
    }
    function paintRs485ConfiguredDevices() {
      const configured = document.getElementById('rs485-configured-devices');
      if (!configured || !rs485ApiState) return;
      const currentPort = rs485CurrentSerialPort();
      const allDevices = (rs485ApiState.devices || []).filter((device) => device.serial_port === currentPort);
      const discoveredDevices = (rs485ApiState.scanned || []).filter((item) => item.serial_port === currentPort && !item.configured);
      const onlineCount = allDevices.filter((device) => rs485CommunicationStatus(device) === 'ONLINE').length;
      const offlineCount = allDevices.length - onlineCount;
      const filterButtons = document.querySelectorAll('#rs485-device-filters [data-filter]');
      const filterCounts = { all: allDevices.length + discoveredDevices.length, configured: allDevices.length, discovered: discoveredDevices.length, online: onlineCount, offline: offlineCount };
      filterButtons.forEach((button) => {
        const key = button.dataset.filter;
        button.classList.toggle('active', key === rs485DeviceFilter);
        const countNode = button.querySelector('span');
        if (countNode) countNode.textContent = filterCounts[key] || 0;
      });
      const devices = (rs485ApiState.devices || []).filter((device) => {
        if (device.serial_port !== currentPort) return false;
        const runtime = rs485Runtime(device);
        const haystack = `${device.name} ${device.template_id} ${device.serial_port} ${device.slave_address}`.toLowerCase();
        if (rs485DeviceQuery && !haystack.includes(rs485DeviceQuery)) return false;
        if (rs485DeviceFilter === 'discovered') return false;
        if (rs485DeviceFilter === 'online' && rs485CommunicationStatus(device) !== 'ONLINE') return false;
        if (rs485DeviceFilter === 'offline' && rs485CommunicationStatus(device) !== 'OFFLINE') return false;
        return true;
      });
      const count = document.getElementById('rs485-device-count');
      if (count) count.textContent = allDevices.length + discoveredDevices.length;
      const search = document.getElementById('rs485-device-search');
      if (search && search.value !== rs485DeviceQuery) search.value = rs485DeviceQuery;
      const discovered = (rs485ApiState.scanned || []).filter((item) => {
        if (item.serial_port !== currentPort || item.configured) return false;
        const haystack = `${item.template_id} ${item.serial_port} ${item.slave_address} ${item.model || ''}`.toLowerCase();
        return (!rs485DeviceQuery || haystack.includes(rs485DeviceQuery)) && (rs485DeviceFilter === 'all' || rs485DeviceFilter === 'discovered');
      });
      const combined = [
        ...devices.map((device) => ({ type: 'configured', device })),
        ...discovered.map((item) => ({ type: 'discovered', item }))
      ];
      const pageCount = Math.max(1, Math.ceil(combined.length / rs485DevicePageSize));
      rs485DevicePage = Math.min(rs485DevicePage, pageCount);
      const pageItems = combined.slice((rs485DevicePage - 1) * rs485DevicePageSize, rs485DevicePage * rs485DevicePageSize);
      const listSignature = JSON.stringify({
        filter: rs485DeviceFilter,
        query: rs485DeviceQuery,
        page: rs485DevicePage,
        items: combined.map((entry) => entry.type === 'configured'
          ? ['configured', entry.device.id, entry.device.name, entry.device.slave_address, rs485Runtime(entry.device).online]
          : ['discovered', entry.item.id, entry.item.slave_address, entry.item.template_id, rs485AddingScanIds.has(entry.item.id)])
      });
      if (listSignature === rs485DeviceListSignature) return;
      rs485DeviceListSignature = listSignature;
      configured.innerHTML = pageItems.length
        ? pageItems.map((entry, index) => entry.type === 'configured' ? (() => { const device = entry.device; return `
            <div class="rs485-table-row configured ${device.id === rs485SelectedId ? 'selected' : ''}" role="button" tabindex="0" aria-selected="${device.id === rs485SelectedId ? 'true' : 'false'}" onclick="selectRs485MockDevice('${device.id}')" onkeydown="if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); selectRs485MockDevice('${device.id}'); }">
              <div class="rs485-device-index">${(rs485DevicePage - 1) * rs485DevicePageSize + index + 1}</div>
              <div class="rs485-device-title"><img class="rs485-list-icon" src="rs485_assets/${device.template_id}.png" alt="">${device.name}</div>
              <div class="rs485-list-address">${device.slave_address}</div>
              <span class="rs485-device-status ${rs485CommunicationStatus(device).toLowerCase()}" data-rs485-live-status="${device.id}">${rs485CommunicationStatus(device)}</span>
              <span aria-hidden="true"></span>
            </div>`; })() : (() => { const item = entry.item; return `
        <div class="rs485-table-row configured discovered">
          <div class="rs485-device-index">${(rs485DevicePage - 1) * rs485DevicePageSize + index + 1}</div>
          <div class="rs485-device-title" title="${rs485TemplateLabel(item.template_id) || item.model || 'Discovered device'}"><span class="rs485-discovered-icon" aria-hidden="true"></span>${rs485TemplateLabel(item.template_id) || item.model || 'Discovered device'}</div>
          <div class="rs485-list-address">${item.slave_address}</div>
              <span class="rs485-device-status discovered" title="Discovered">Discovered</span>
          <button type="button" class="rs485-row-add" aria-label="Add discovered device" data-rs485-add-id="${item.id}">Add</button>
        </div>`; })()).join('')
        : '<div class="rs485-empty">No devices match the current filter.</div>';
      configured.querySelectorAll('[data-rs485-add-id]').forEach((button) => {
        button.onclick = (event) => {
          event.preventDefault();
          event.stopPropagation();
          addRs485Device(button.dataset.rs485AddId);
        };
      });
      if (combined.length > rs485DevicePageSize) configured.innerHTML += `<div class="rs485-pagination"><button type="button" onclick="setRs485DevicePage(${rs485DevicePage - 1})" ${rs485DevicePage <= 1 ? 'disabled' : ''}>Previous</button><span>Page ${rs485DevicePage} / ${pageCount}</span><button type="button" onclick="setRs485DevicePage(${rs485DevicePage + 1})" ${rs485DevicePage >= pageCount ? 'disabled' : ''}>Next</button></div>`;
    }
    function paintRs485Templates() {
      const target = document.getElementById('rs485-device-templates');
      if (!target || !rs485ApiState) return;
      const templates = rs485ApiState.template_details || [];
      const errors = rs485ApiState.template_errors || [];
      const rows = templates.map((template) => {
        return `
          <div class="rs485-template-row">
            <div>
              <strong>${template.model}</strong>
              <div class="rs485-device-meta">
                <span>${template.template_id}</span>
                <span>${template.manufacturer}</span>
                <span>${template.protocol}</span>
                <span>${template.source || 'built-in'}</span>
              </div>
            </div>
            <details class="rs485-template-menu">
              <summary aria-label="Template actions" title="Template actions"><span class="rs485-menu-dots" aria-hidden="true"><i></i><i></i><i></i></span></summary>
              <div class="rs485-template-menu-panel">
                <button type="button" class="danger-button" onclick="deleteRs485Template('${template.template_id}'); this.closest('details').removeAttribute('open')">Delete</button>
              </div>
            </details>
          </div>`;
      });
      const errorRows = errors.map((error) => `
        <div class="rs485-empty">Template ${error.template}: ${error.error}</div>
      `);
      target.innerHTML = rows.concat(errorRows).join('') || '<div class="rs485-empty">No RS-485 templates loaded.</div>';
    }
    function renderRs485IoTable(selected, inputs, outputs, modes) {
      const count = Math.max(inputs.length, outputs.length, modes.length);
      const rows = Array.from({ length: count }, (_, index) => `
          <div class="rs485-io-row">
          <div class="rs485-io-channel">${index + 1}</div>
          <div>${inputs[index] ? renderRs485RowCapability(selected, inputs[index], `Input ${index + 1}`) : '<span class="rs485-empty-cell">--</span>'}</div>
          <div>${outputs[index] ? renderRs485RowCapability(selected, outputs[index]) : '<span class="rs485-empty-cell">--</span>'}</div>
          <div>${modes[index] ? renderRs485ModeCapability(selected, modes[index]) : '<span class="rs485-empty-cell">--</span>'}</div>
        </div>`).join('');
      return `<div class="rs485-io-table">
        <div class="rs485-io-head"><span>CH</span><span>Digital Input (DI)</span><span>Relay Output</span><span>Mode</span></div>
        ${rows || '<div class="rs485-empty">No I/O capabilities.</div>'}
      </div>`;
    }
    function paintRs485Detail() {
      const detail = document.getElementById('rs485-device-detail');
      if (!detail || !rs485ApiState) return;
      const currentPort = rs485CurrentSerialPort();
      const devices = (rs485ApiState.devices || []).filter((device) => device.serial_port === currentPort);
      const selected = devices.find((device) => device.id === rs485SelectedId) || devices[0];
      if (!selected) {
        rs485DetailSignature = '';
        detail.innerHTML = '<div class="rs485-empty">Select or add an RS-485 device to view its template-rendered capabilities.</div>';
        return;
      }
      rs485SelectedId = selected.id;
      const template = rs485Template(selected.template_id);
      if (!template) {
        rs485DetailSignature = '';
        detail.innerHTML = `<div class="rs485-empty">Template ${selected.template_id} is not loaded.</div>`;
        return;
      }
      const nextSignature = rs485DetailPaintSignature(selected);
      rs485DetailSignature = nextSignature;
      const grouped = rs485GroupedCapabilities(template);
      const allCapabilities = Object.values(grouped).flat();
      const deviceSettings = grouped.device_settings || [];
      const inputs = (grouped.inputs || allCapabilities).filter((item) => item.type === 'binary_input' || item.type === 'sensor');
      const outputs = (grouped.outputs || allCapabilities).filter((item) => item.type === 'switch');
      const modes = (grouped.control_modes || allCapabilities).filter((item) => item.type === 'select' && item.group !== 'device_settings');
      const softwareVersion = allCapabilities.find((item) => item.id === 'software_version' || item.source === 'software_version');
      const settingsFields = softwareVersion && !deviceSettings.some((item) => item.id === softwareVersion.id)
        ? deviceSettings.concat([softwareVersion])
        : deviceSettings;
      const runtime = rs485Runtime(selected);
      const writable = rs485DeviceWritable(selected);
      const settingLabel = rs485DeviceSettingLabel(selected);
      const identity = selected.identity || {};
      const firmwareValue = identity.firmware_version === null || identity.firmware_version === undefined
        ? null
        : (typeof identity.firmware_version === 'number' ? `v${identity.firmware_version.toFixed(1)}` : (String(identity.firmware_version).startsWith('v') ? String(identity.firmware_version) : `v${identity.firmware_version}`));
      const identityFacts = [
        ['Manufacturer', identity.manufacturer],
        ['Hardware', identity.hardware_version],
        ['Firmware', firmwareValue],
        ['Serial', identity.serial_number],
        ['Slave ID', selected.slave_address]
      ].filter((item) => item[1] !== null && item[1] !== undefined && String(item[1]).trim() !== '')
        .map((item) => `<span><span>${item[0]}:</span><strong>${item[1]}</strong></span>`).join('');
      const parityCode = String(selected.parity || 'none').toLowerCase() === 'even' ? 'E' : String(selected.parity || 'none').toLowerCase() === 'odd' ? 'O' : 'N';
      detail.innerHTML = `
        <div class="rs485-detail-head">
          <div class="rs485-device-identity">
            <img class="rs485-device-icon" src="rs485_assets/${selected.template_id}.png" alt="">
            <div class="rs485-device-identity-copy">
            <div class="rs485-device-title-row"><div class="module-title">${selected.name}</div></div>
            <div class="rs485-device-subtitle"></div>
            ${identityFacts ? `<div class="rs485-device-identity-facts">${identityFacts}</div>` : ''}
            <div class="rs485-device-connection">
              <span class="meta-port">${selected.serial_port}</span>
              <span class="meta-protocol">${selected.mock ? 'Mock' : 'Modbus RTU'}</span>
              <span class="meta-communication">${selected.baudrate} 8${parityCode}${selected.stop_bits || 1}</span>
            </div>
            </div>
          </div>
          <div class="rs485-detail-actions">
            <span class="rs485-detail-status ${rs485CommunicationStatus(selected).toLowerCase()}" data-rs485-live-detail-status title="Communication status based on the last valid Modbus RTU response">${rs485CommunicationStatus(selected)}</span>
            <details class="rs485-detail-action-menu">
              <summary aria-label="Device actions" title="Device actions"><span class="rs485-menu-dots" aria-hidden="true"><i></i><i></i><i></i></span></summary>
              <div class="rs485-detail-action-menu-panel">
                <button type="button" onclick="renameRs485Device('${selected.id}'); this.closest('details').removeAttribute('open')">Rename</button>
                <button type="button" onclick="readRs485Device('${selected.id}', 'settings'); this.closest('details').removeAttribute('open')">Read device information</button>
                <button type="button" class="danger-button" onclick="if (window.confirm('Remove this device?')) removeRs485MockDevice('${selected.id}'); this.closest('details').removeAttribute('open')">Remove device</button>
              </div>
            </details>
          </div>
          <div class="rs485-detail-tabs" role="tablist">
            <button type="button" class="${rs485DetailTab === 'io' ? 'active' : ''}" onclick="setRs485DetailTab('io')" role="tab" aria-selected="${rs485DetailTab === 'io'}">I/O Channels</button>
            <button type="button" class="${rs485DetailTab === 'settings' ? 'active' : ''}" onclick="setRs485DetailTab('settings')" role="tab" aria-selected="${rs485DetailTab === 'settings'}">Settings</button>
            <button type="button" class="${rs485DetailTab === 'diagnostics' ? 'active' : ''}" onclick="setRs485DetailTab('diagnostics')" role="tab" aria-selected="${rs485DetailTab === 'diagnostics'}">Diagnostics</button>
            <button type="button" class="${rs485DetailTab === 'logs' ? 'active' : ''}" onclick="setRs485DetailTab('logs')" role="tab" aria-selected="${rs485DetailTab === 'logs'}">Logs</button>
          </div>
        </div>
        <div class="rs485-detail-stack">
          ${settingsFields.length ? `
            <div data-rs485-panel="settings" class="rs485-capability-group rs485-tab-panel ${rs485DetailTab === 'settings' ? '' : 'hidden'}">
              <div class="rs485-settings-polling-controls rs485-device-access-controls" aria-label="Device controls">
                <span class="polling-control-label">Device control</span>
                <div class="rs485-polling-group-toggle"><span>Device polling<small>Automatic device updates</small></span><button class="toggle ${selected.enabled ? 'on' : ''}" type="button" data-rs485-device-control="device" onclick="setRs485PollingEnabled('${selected.id}', ${!selected.enabled})" aria-label="Toggle device polling"><span>${selected.enabled ? 'ON' : 'OFF'}</span></button></div>
                <div class="rs485-polling-group-toggle"><span>Read access<small>Read inputs and state</small></span><button class="toggle ${selected.read_enabled !== false ? 'on' : ''}" type="button" data-rs485-device-control="read" onclick="setRs485DeviceAccess('${selected.id}', 'read', ${selected.read_enabled === false})"><span>${selected.read_enabled !== false ? 'ON' : 'OFF'}</span></button></div>
                <div class="rs485-polling-group-toggle"><span>Write access<small>Control relay outputs</small></span><button class="toggle ${selected.write_enabled !== false ? 'on' : ''}" type="button" data-rs485-device-control="write" onclick="setRs485DeviceAccess('${selected.id}', 'write', ${selected.write_enabled === false})"><span>${selected.write_enabled !== false ? 'ON' : 'OFF'}</span></button></div>
              </div>
              <div class="rs485-panel-head rs485-runtime-config-head">
                <div class="rs485-panel-title">Read polling intervals</div>
              </div>
              <div class="rs485-polling-settings-block">
                ${renderRs485PollingControls(selected)}
                <label class="rs485-polling-field">
                  <span>Traffic log buffer</span>
                  <input id="rs485-diagnostics-buffer" type="number" min="100" max="1000" step="100" value="${rs485DiagnosticsBufferSize()}" onchange="saveRs485DiagnosticsBuffer(this.value)">
                </label>
              </div>
              <div class="rs485-panel-head rs485-device-settings-head">
                <div class="rs485-panel-title">Device Settings</div>
                <div class="rs485-polling-caption">${rs485PollingCaption(selected, 'settings', 'On demand')}</div>
              </div>
              <div class="rs485-device-settings">
                ${settingsFields.map((capability) => renderRs485FieldCapability(selected, capability)).join('')}
                <div class="rs485-actions">
                  <button type="button" onclick="applyRs485DeviceSettings('${selected.id}')" ${writable ? '' : 'disabled'}>Apply</button>
                  <button type="button" onclick="readRs485Device('${selected.id}', 'settings')">Read</button>
                </div>
              </div>
            </div>` : ''}
          <div data-rs485-panel="io" class="rs485-tab-panel ${rs485DetailTab === 'io' ? '' : 'hidden'}">
            ${renderRs485IoTable(selected, inputs, outputs, modes)}
          </div>
          <div data-rs485-panel="diagnostics" class="rs485-tab-panel ${rs485DetailTab === 'diagnostics' ? '' : 'hidden'}">
            ${renderRs485Diagnostics(selected)}
          </div>
          <div data-rs485-panel="logs" class="rs485-tab-panel ${rs485DetailTab === 'logs' ? '' : 'hidden'}">
            ${renderRs485Logs(selected)}
          </div>
        </div>`;
      detail.dataset.rs485DeviceId = selected.id;
      bindRs485DiagnosticsActions(detail);
      bindRs485LogsActions(detail);
    }
    function refreshRs485LogsPanel(detail) {
      const device = (rs485ApiState.devices || []).find((item) => item.id === rs485SelectedId);
      const panel = detail.querySelector('[data-rs485-panel="logs"]');
      if (!device || !panel) return;
      panel.innerHTML = renderRs485Logs(device);
      bindRs485LogsActions(detail);
    }
    function bindRs485LogsActions(detail) {
      const panel = detail.querySelector('[data-rs485-panel="logs"]');
      if (!panel) return;
      const toolbar = panel.querySelector('.rs485-logs-toolbar');
      if (toolbar && !toolbar.querySelector('.rs485-logs-actions')) {
        const actions = document.createElement('div');
        actions.className = 'rs485-logs-actions';
        toolbar.querySelectorAll('label, button').forEach((node) => actions.appendChild(node));
        toolbar.appendChild(actions);
      }
      panel.querySelectorAll('[data-rs485-log-filter]').forEach((select) => { select.onchange = () => patchRs485LogsTable(); });
      panel.querySelector('[data-rs485-log-clear]')?.addEventListener('click', () => rs485DiagnosticsClear(panel.querySelector('[data-rs485-log-clear]').dataset.rs485LogClear));
      panel.querySelector('[data-rs485-log-capture]')?.addEventListener('click', () => rs485DiagnosticsTogglePause(panel.querySelector('[data-rs485-log-capture]').dataset.rs485LogCapture));
      panel.querySelectorAll('[data-rs485-log-export]').forEach((button) => { button.onclick = () => exportRs485Logs(button.dataset.rs485LogExport); });
    }
    function patchRs485LogsTable() {
      const panel = document.querySelector('#rs485-device-detail [data-rs485-panel="logs"]');
      const device = (rs485ApiState.devices || []).find((item) => item.id === rs485SelectedId);
      const body = panel && panel.querySelector('[data-rs485-logs-body]');
      if (!panel || !device || !body) return;
      const values = Object.fromEntries([...panel.querySelectorAll('[data-rs485-log-filter]')].map((node) => [node.dataset.rs485LogFilter, node.value]));
      const errors = panel.querySelector('[data-rs485-log-errors]')?.checked;
      const entries = (device.diagnostics && device.diagnostics.entries || []).slice(-rs485DiagnosticsBufferSize()).filter((entry) => (!errors || entry.result === 'error') && (!values.port || values.port === 'all' || String(entry.serial_port) === values.port) && (!values.slave || values.slave === 'all' || String(entry.slave_address) === values.slave) && (!values.group || values.group === 'all' || String(entry.group) === values.group) && (!values.operation || values.operation === 'all' || String(entry.operation) === values.operation) && (!values.function || values.function === 'all' || String(entry.function) === values.function));
      body.innerHTML = entries.length ? entries.map((entry) => `<div class="rs485-log-row"><span>${entry.seq ?? '—'}</span><span>${rs485RuntimeTime(entry.ts)}</span><span>${entry.serial_port || '—'}</span><span>${entry.slave_address ?? '—'}</span><span>${entry.group || '—'}</span><span>${entry.operation || '—'}</span><span>${entry.direction || '—'}</span><span>${entry.function || '—'}</span><span>${entry.response_ms == null ? '—' : `${entry.response_ms} ms`}</span><span class="${entry.result === 'error' ? 'error' : ''}">${entry.result || '—'}</span><span title="${entry.message || ''}">${entry.error_type || entry.message || 'details'}</span></div>`).join('') : '<div class="rs485-log-empty">No matching transactions.</div>';
    }
    function exportRs485Logs(format) { exportRs485Diagnostics(format); }
    function refreshRs485DiagnosticsPanel(detail) {
      const device = (rs485ApiState.devices || []).find((item) => item.id === rs485SelectedId);
      const panel = detail.querySelector('[data-rs485-panel="diagnostics"]');
      if (!device || !panel) return;
      panel.innerHTML = renderRs485Diagnostics(device);
      const entries = (device.diagnostics && device.diagnostics.entries) || [];
      const optionValues = (key) => [...new Set(entries.map((entry) => String(entry[key] ?? '')).filter(Boolean))].sort();
      const slaveValues = optionValues('slave_address').map((value) => ({ value, label: `Slave ${value}` }));
      const functionValues = optionValues('function').map((value) => ({ value, label: value }));
      const portValues = optionValues('serial_port').map((value) => ({ value, label: value }));
      const groupValues = optionValues('group').map((value) => ({ value, label: value }));
      const operationValues = optionValues('operation').map((value) => ({ value, label: value }));
      renderRs485Select('rs485-diagnostics-filter', [{ value: 'all', label: 'All traffic' }, { value: 'master', label: 'Master TX' }, { value: 'slave', label: 'Slave RX' }, { value: 'errors', label: 'Errors only' }], rs485DiagnosticsFilter, setRs485DiagnosticsFilter);
      renderRs485Select('rs485-diagnostics-port-filter', [{ value: 'all', label: 'All ports' }, ...portValues], rs485DiagnosticsPortFilter, (value) => { rs485DiagnosticsPortFilter = value; patchRs485DiagnosticsLog(); });
      renderRs485Select('rs485-diagnostics-group-filter', [{ value: 'all', label: 'All groups' }, ...groupValues], rs485DiagnosticsGroupFilter, (value) => { rs485DiagnosticsGroupFilter = value; patchRs485DiagnosticsLog(); });
      renderRs485Select('rs485-diagnostics-operation-filter', [{ value: 'all', label: 'All operations' }, ...operationValues], rs485DiagnosticsOperationFilter, (value) => { rs485DiagnosticsOperationFilter = value; patchRs485DiagnosticsLog(); });
      renderRs485Select('rs485-diagnostics-slave-filter', [{ value: 'all', label: 'All slaves' }, ...slaveValues], rs485DiagnosticsSlaveFilter, setRs485DiagnosticsSlaveFilter);
      renderRs485Select('rs485-diagnostics-function-filter', [{ value: 'all', label: 'All functions' }, ...functionValues], rs485DiagnosticsFunctionFilter, setRs485DiagnosticsFunctionFilter);
      bindRs485DiagnosticsActions(detail);
    }
    function bindRs485DiagnosticsActions(detail) {
      const diagnosticsFilters = detail.querySelector('[data-rs485-panel="diagnostics"] .rs485-traffic-filters');
      const diagnosticsActions = detail.querySelector('[data-rs485-panel="diagnostics"] .rs485-traffic-actions');
      if (diagnosticsFilters && diagnosticsActions) {
        diagnosticsFilters.querySelectorAll(':scope > button').forEach((button) => {
          const action = button.getAttribute('onclick') || '';
          button.classList.add(action.includes('exportRs485Diagnostics') ? 'rs485-export-action' : 'rs485-primary-action');
          if (action.includes("'csv'")) button.classList.add('rs485-export-csv');
          if (action.includes("'json'")) button.classList.add('rs485-export-json');
          diagnosticsActions.appendChild(button);
        });
        if (!diagnosticsActions.querySelector('.rs485-export-actions-row')) {
          const buttons = [...diagnosticsActions.querySelectorAll(':scope > button')];
          const topRow = document.createElement('div');
          const bottomRow = document.createElement('div');
          topRow.className = 'rs485-export-actions-row';
          bottomRow.className = 'rs485-primary-actions-row';
          buttons.forEach((button) => (button.classList.contains('rs485-export-action') ? topRow : bottomRow).appendChild(button));
          diagnosticsActions.append(topRow, bottomRow);
        }
      }
      const device = (rs485ApiState.devices || []).find((item) => item.id === rs485SelectedId);
      if (device) {
        const entries = (device.diagnostics && device.diagnostics.entries) || [];
        const values = (key) => [...new Set(entries.map((entry) => String(entry[key] ?? '')).filter(Boolean))].sort();
        renderRs485Select('rs485-diagnostics-filter', [{ value: 'all', label: 'All traffic' }, { value: 'master', label: 'Master TX' }, { value: 'slave', label: 'Slave RX' }, { value: 'errors', label: 'Errors only' }], rs485DiagnosticsFilter, setRs485DiagnosticsFilter);
        renderRs485Select('rs485-diagnostics-slave-filter', [{ value: 'all', label: 'All slaves' }, ...values('slave_address').map((value) => ({ value, label: `Slave ${value}` }))], rs485DiagnosticsSlaveFilter, setRs485DiagnosticsSlaveFilter);
        const knownFunctions = ['Read', 'Write', 'Write Single Coil', 'Write Single Register', 'Write Multiple Coils', 'Write Multiple Registers'];
        const functionLabels = [...new Set([...knownFunctions, ...values('function')])];
        renderRs485Select('rs485-diagnostics-function-filter', [{ value: 'all', label: 'All functions' }, ...functionLabels.map((value) => ({ value, label: value }))], rs485DiagnosticsFunctionFilter, setRs485DiagnosticsFunctionFilter);
        renderRs485Select('rs485-diagnostics-port-filter', [{ value: 'all', label: 'All ports' }, ...values('serial_port').map((value) => ({ value, label: value }))], rs485DiagnosticsPortFilter, (value) => { rs485DiagnosticsPortFilter = value; patchRs485DiagnosticsLog(); });
        renderRs485Select('rs485-diagnostics-group-filter', [{ value: 'all', label: 'All groups' }, ...values('group').map((value) => ({ value, label: value }))], rs485DiagnosticsGroupFilter, (value) => { rs485DiagnosticsGroupFilter = value; patchRs485DiagnosticsLog(); });
        renderRs485Select('rs485-diagnostics-operation-filter', [{ value: 'all', label: 'All operations' }, ...values('operation').map((value) => ({ value, label: value }))], rs485DiagnosticsOperationFilter, (value) => { rs485DiagnosticsOperationFilter = value; patchRs485DiagnosticsLog(); });
      }
      const diagnosticsClear = detail.querySelector('[data-rs485-diagnostics-clear]');
      const diagnosticsReset = detail.querySelector('[data-rs485-diagnostics-reset]');
      const diagnosticsToggle = detail.querySelector('[data-rs485-diagnostics-toggle]');
      if (diagnosticsClear) diagnosticsClear.onclick = (event) => {
        event.preventDefault();
        event.stopPropagation();
        rs485DiagnosticsClear(diagnosticsClear.dataset.rs485DiagnosticsClear);
      };
      if (diagnosticsReset) diagnosticsReset.onclick = (event) => {
        event.preventDefault();
        event.stopPropagation();
        rs485DiagnosticsReset(diagnosticsReset.dataset.rs485DiagnosticsReset);
      };
      if (diagnosticsToggle) diagnosticsToggle.onclick = (event) => {
        event.preventDefault();
        event.stopPropagation();
        rs485DiagnosticsTogglePause(diagnosticsToggle.dataset.rs485DiagnosticsToggle);
      };
      const manualPanel = detail.querySelector('[data-rs485-manual-panel]');
      const manualOpen = detail.querySelector('[data-rs485-manual-open]');
      const manualClose = detail.querySelector('[data-rs485-manual-close]');
      if (manualOpen && manualPanel) manualOpen.onclick = () => { rs485ManualCommandOpen = true; manualPanel.classList.add('open'); updateRs485ManualPreview(manualPanel); };
      if (manualClose && manualPanel) manualClose.onclick = () => { rs485ManualCommandOpen = false; manualPanel.classList.remove('open'); };
      if (manualPanel) {
        setupRs485ManualFunctionSelect(manualPanel);
        updateRs485ManualFields(manualPanel);
        manualPanel.querySelectorAll('input').forEach((field) => field.addEventListener('input', () => updateRs485ManualPreview(manualPanel)));
        updateRs485ManualPreview(manualPanel);
        const send = manualPanel.querySelector('[data-rs485-manual-send]');
        if (send) send.onclick = () => sendRs485ManualCommand(manualPanel, send.dataset.rs485ManualSend);
      }
      detail.querySelectorAll('[data-rs485-entry]').forEach((row) => {
        row.onclick = () => {
          const detailRow = detail.querySelector(`[data-rs485-detail="${row.dataset.rs485Entry}"]`);
          if (detailRow) detailRow.classList.toggle('hidden');
        };
      });
    }
    function setupRs485ManualFunctionSelect(panel) {
      const wrap = panel.querySelector('[data-rs485-manual-function]');
      if (!wrap) return;
      const options = [
        { value: 3, label: 'FC03 - Read Holding Registers' },
        { value: 1, label: 'FC01 - Read Coils' },
        { value: 2, label: 'FC02 - Read Discrete Inputs' },
        { value: 4, label: 'FC04 - Read Input Registers' },
        { value: 5, label: 'FC05 - Write Single Coil' },
        { value: 6, label: 'FC06 - Write Single Register' }
      ];
      const selected = options.find((option) => String(option.value) === String(wrap.dataset.value)) || options[0];
      wrap.className = 'mode-select rs485-manual-function-select';
      wrap.dataset.value = selected.value;
      wrap.innerHTML = `<button type="button" class="mode-trigger">${selected.label}</button><div class="mode-menu">${options.map((option) => `<button type="button" class="mode-option ${option.value === selected.value ? 'active' : ''}" data-value="${option.value}">${option.label}</button>`).join('')}</div>`;
      const trigger = wrap.querySelector('.mode-trigger');
      trigger.onclick = (event) => { event.preventDefault(); event.stopPropagation(); closeModeSelects(wrap); wrap.classList.add('open'); };
      wrap.querySelectorAll('.mode-option').forEach((option) => option.onclick = (event) => {
        event.preventDefault();
        event.stopPropagation();
        wrap.dataset.value = option.dataset.value;
        setupRs485ManualFunctionSelect(panel);
        updateRs485ManualFields(panel);
        updateRs485ManualPreview(panel);
      });
    }
    function updateRs485ManualFields(panel) {
      const fn = rs485ManualFunctionValue(panel);
      const write = fn >= 5;
      panel.querySelector('[data-rs485-manual-count-field]')?.classList.toggle('hidden', write);
      panel.querySelector('[data-rs485-manual-value-field]')?.classList.toggle('hidden', !write);
      const valueLabel = panel.querySelector('[data-rs485-manual-value-label]');
      if (valueLabel) valueLabel.textContent = fn === 5 ? 'Coil state' : 'Value';
    }
    function rs485ManualFunctionValue(panel) {
      return Number(panel.querySelector('[data-rs485-manual-function]')?.dataset.value || 3);
    }
    function rs485ManualCrc(bytes) {
      let crc = 0xFFFF;
      bytes.forEach((byte) => {
        crc ^= byte;
        for (let bit = 0; bit < 8; bit += 1) crc = (crc & 1) ? ((crc >> 1) ^ 0xA001) : (crc >> 1);
      });
      return [crc & 0xFF, (crc >> 8) & 0xFF];
    }
    function updateRs485ManualPreview(panel) {
      const fn = rs485ManualFunctionValue(panel);
      const address = Number(panel.querySelector('[data-rs485-manual-address]').value || 0);
      const count = Number(panel.querySelector('[data-rs485-manual-count]').value || 1);
      const value = Number(panel.querySelector('[data-rs485-manual-value]').value || 0);
      const slave = Number(panel.querySelector('[data-rs485-manual-slave]').value || 1);
      const data = fn === 5 ? (value ? 0xFF00 : 0x0000) : fn === 6 ? value : count;
      const frame = [slave, fn, (address >> 8) & 0xFF, address & 0xFF, (data >> 8) & 0xFF, data & 0xFF];
      const crc = rs485ManualCrc(frame);
      panel.querySelector('[data-rs485-manual-preview]').textContent = `Preview: ${[...frame, ...crc].map((byte) => byte.toString(16).padStart(2, '0').toUpperCase()).join(' ')}`;
      const deviceId = panel.dataset.rs485ManualPanel;
      if (deviceId) {
        rs485ManualCommandState[deviceId] = { slave_id: slave, function: fn, address, count, value };
        persistRs485ManualCommandState();
      }
    }
    async function sendRs485ManualCommand(panel, deviceId) {
      const fn = rs485ManualFunctionValue(panel);
      const body = { slave_id: Number(panel.querySelector('[data-rs485-manual-slave]').value || 1), function: fn, address: Number(panel.querySelector('[data-rs485-manual-address]').value || 0), count: Number(panel.querySelector('[data-rs485-manual-count]').value || 1), value: Number(panel.querySelector('[data-rs485-manual-value]').value || 0) };
      const error = body.slave_id < 1 || body.slave_id > 247 ? 'Slave ID must be 1-247' : body.address < 0 || body.address > 65535 ? 'Address must be 0-65535' : fn < 5 && (body.count < 1 || body.count > 125) ? 'Count must be 1-125' : fn === 5 && ![0, 1].includes(body.value) ? 'Coil state must be 0 or 1' : fn === 6 && (body.value < 0 || body.value > 65535) ? 'Value must be 0-65535' : '';
      const result = panel.querySelector('[data-rs485-manual-result]');
      if (error) { if (result) result.textContent = error; return; }
      const send = panel.querySelector('[data-rs485-manual-send]');
      if (send) { send.disabled = true; send.textContent = 'Sending...'; }
      rs485ManualCommandState[deviceId] = body;
      try {
        const payload = await rs485Command(() => requestJson(`api/v1/rs485/devices/${encodeURIComponent(deviceId)}/manual-command`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }));
        rs485ManualCommandOpen = true;
        paintRs485(payload);
        const refreshedPanel = document.querySelector(`[data-rs485-manual-panel="${deviceId}"]`);
        if (refreshedPanel) {
          refreshedPanel.classList.add('open');
          updateRs485ManualPreview(refreshedPanel);
        }
      } catch (error) {
        document.getElementById('rs485-status').textContent = `RS-485 manual command failed: ${errorDetail(error)}`;
      } finally {
        const refreshedSend = document.querySelector(`[data-rs485-manual-send="${deviceId}"]`);
        if (refreshedSend) { refreshedSend.disabled = false; refreshedSend.textContent = 'Send packet'; }
      }
    }
    function rs485DeviceSettingLabel(device) {
      const pending = rs485PendingSettings[device.id] || {};
      const baudrate = Object.prototype.hasOwnProperty.call(pending, 'device_baudrate') ? `${device.baudrate} -> ${pending.device_baudrate} pending` : device.baudrate;
      const parity = Object.prototype.hasOwnProperty.call(pending, 'device_parity') ? `${device.parity} -> ${pending.device_parity} pending` : device.parity;
      const address = Object.prototype.hasOwnProperty.call(pending, 'device_address') ? `${device.slave_address} -> ${pending.device_address} pending` : device.slave_address;
      return `${baudrate} ${parity} ${device.stop_bits} stop${String(address) !== String(device.slave_address) ? ` / slave ${address}` : ''}`;
    }
    function rs485DetailPaintSignature(device) {
      const runtime = device.runtime || {};
      const values = device.values || {};
      const comparableRuntime = {
        online: runtime.online,
        polling: runtime.polling,
        last_error: runtime.last_error,
        groups: Object.fromEntries(Object.entries(runtime.groups || {}).map(([groupId, group]) => [
          groupId,
          {
            mode: group.mode,
            interval_ms: group.interval_ms,
            status: group.status,
            last_error: group.last_error
          }
        ]))
      };
      return JSON.stringify({
        id: device.id,
        enabled: device.enabled,
        baudrate: device.baudrate,
        parity: device.parity,
        stop_bits: device.stop_bits,
        slave_address: device.slave_address,
        identity: device.identity || {},
        values,
        runtime: comparableRuntime,
        pending: rs485PendingSettings[device.id] || {}
        ,diagnostics: device.diagnostics || {}
      });
    }
    function rs485GroupedCapabilities(template) {
      const groups = {};
      Object.entries(template.capabilities || {})
        .map(([id, capability]) => ({ id, ...capability }))
        .sort((a, b) => (a.order || 0) - (b.order || 0))
        .forEach((capability) => {
          const group = capability.group || 'main';
          if (!groups[group]) groups[group] = [];
          groups[group].push(capability);
        });
      return groups;
    }
    function rs485TemplateStats(template) {
      return {
        groups: Object.keys(template.groups || {}).length,
        capabilities: Object.keys(template.capabilities || {}).length,
        points: Object.keys(template.points || {}).length
      };
    }
    function rs485Runtime(device) {
      return device.runtime || { online: true, polling: 'live', groups: {} };
    }
    function rs485CommunicationStatus(device) {
      const runtime = rs485Runtime(device);
      return String(runtime.communication_status || (runtime.online === false ? 'OFFLINE' : device.mock ? 'ONLINE' : 'UNKNOWN')).toUpperCase();
    }
    function rs485RuntimeGroup(device, groupId) {
      const runtime = rs485Runtime(device);
      return (runtime.groups && runtime.groups[groupId]) || {};
    }
    async function rs485DiagnosticsTogglePause(deviceId) {
      if (rs485DiagnosticsCommandInFlight) return;
      const device = (rs485ApiState && rs485ApiState.devices || []).find((item) => item.id === deviceId);
      const paused = !(device && device.diagnostics && device.diagnostics.paused);
      if (!device) return;
      rs485DiagnosticsCommandInFlight = true;
      device.diagnostics = { ...(device.diagnostics || {}), paused };
      rs485DetailSignature = '';
      paintRs485Detail();
      rs485LiveRequestVersion += 1;
      try {
        const payload = await rs485Command(() => requestJson(`api/v1/rs485/devices/${encodeURIComponent(deviceId)}/diagnostics/pause`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ paused }) }));
        paintRs485(payload, { preserveStatus: true });
      } catch (error) {
        document.getElementById('rs485-status').textContent = `RS-485 diagnostics: ${errorDetail(error)}`;
        device.diagnostics.paused = !paused;
        rs485DetailSignature = '';
        paintRs485Detail();
      } finally {
        rs485DiagnosticsCommandInFlight = false;
      }
    }
    async function rs485DiagnosticsClear(deviceId) {
      if (rs485DiagnosticsCommandInFlight) return;
      rs485DiagnosticsCommandInFlight = true;
      rs485LiveRequestVersion += 1;
      try {
        const payload = await rs485Command(() => requestJson(`api/v1/rs485/devices/${encodeURIComponent(deviceId)}/diagnostics/clear`, { method: 'POST' }));
        paintRs485(payload, { preserveStatus: true });
      } catch (error) {
        document.getElementById('rs485-status').textContent = `RS-485 diagnostics: ${errorDetail(error)}`;
      } finally {
        rs485DiagnosticsCommandInFlight = false;
      }
    }
    async function rs485DiagnosticsReset(deviceId) {
      if (rs485DiagnosticsCommandInFlight) return;
      rs485DiagnosticsCommandInFlight = true;
      rs485LiveRequestVersion += 1;
      try {
        const payload = await rs485Command(() => requestJson(`api/v1/rs485/devices/${encodeURIComponent(deviceId)}/diagnostics/reset`, { method: 'POST' }));
        paintRs485(payload, { preserveStatus: true });
      } catch (error) {
        document.getElementById('rs485-status').textContent = `RS-485 diagnostics: ${errorDetail(error)}`;
      } finally {
        rs485DiagnosticsCommandInFlight = false;
      }
    }
    function renderRs485Diagnostics(device) {
      return renderRs485LegacyDiagnostics(device);
    }
    function rs485LogFilterOptions(entries, key, allLabel) {
      const values = [...new Set(entries.map((entry) => String(entry[key] ?? '')).filter(Boolean))].sort();
      return `<option value="all">${allLabel}</option>${values.map((value) => `<option value="${value}">${value}</option>`).join('')}`;
    }
    function renderRs485Logs(device) {
      const entries = (device.diagnostics && device.diagnostics.entries || []).slice(-rs485DiagnosticsBufferSize());
      const id = String(device.id).replace(/[^a-zA-Z0-9_-]/g, '_');
      const rowMarkup = entries.length ? entries.map((entry) => `<div class="rs485-log-row" data-rs485-log-row data-result="${entry.result || ''}" data-port="${entry.serial_port || ''}" data-group="${entry.group || ''}" data-operation="${entry.operation || ''}" data-slave="${entry.slave_address || ''}" data-function="${entry.function || ''}"><span>${entry.seq ?? '—'}</span><span>${rs485RuntimeTime(entry.ts)}</span><span>${entry.serial_port || '—'}</span><span>${entry.slave_address ?? '—'}</span><span>${entry.group || '—'}</span><span>${entry.operation || '—'}</span><span>${entry.direction || '—'}</span><span>${entry.function || '—'}</span><span>${entry.response_ms == null ? '—' : `${entry.response_ms} ms`}</span><span class="${entry.result === 'error' ? 'error' : ''}">${entry.result || '—'}</span><details><summary>details</summary><div>TX: ${entry.tx_hex || '—'} · RX: ${entry.rx_hex || '—'} · Error: ${entry.error_type || entry.message || '—'} · ${entry.online == null ? '—' : entry.online ? 'ONLINE' : 'OFFLINE'} · Backoff: ${entry.backoff_ms || 0} ms · WebSocket: ${entry.ws_published ? 'published' : 'pending'}</div></details></div>`).join('') : '<div class="rs485-log-empty" data-rs485-log-empty>No Modbus transactions recorded yet.</div>';
      return `<section class="rs485-logs-component" data-rs485-logs-component="${id}"><div class="rs485-logs-toolbar"><select data-rs485-log-filter="port"><option value="all">All ports</option>${rs485LogFilterOptions(entries, 'serial_port', '').replace('<option value="all"></option>', '')}</select><select data-rs485-log-filter="slave"><option value="all">All slaves</option>${rs485LogFilterOptions(entries, 'slave_address', '').replace('<option value="all"></option>', '')}</select><select data-rs485-log-filter="group"><option value="all">All groups</option>${rs485LogFilterOptions(entries, 'group', '').replace('<option value="all"></option>', '')}</select><select data-rs485-log-filter="operation"><option value="all">All operations</option>${rs485LogFilterOptions(entries, 'operation', '').replace('<option value="all"></option>', '')}</select><select data-rs485-log-filter="function"><option value="all">All functions</option>${rs485LogFilterOptions(entries, 'function', '').replace('<option value="all"></option>', '')}</select><label><input type="checkbox" data-rs485-log-errors> Errors</label><button type="button" data-rs485-log-export="csv">Export CSV</button><button type="button" data-rs485-log-export="json">Export JSON</button><button type="button" data-rs485-log-clear="${device.id}">Clear logs</button><button type="button" data-rs485-log-capture="${device.id}">${(device.diagnostics || {}).paused ? 'Start' : 'Stop'}</button></div><div class="rs485-logs-table"><div class="rs485-logs-head"><span>#</span><span>Timestamp</span><span>Port</span><span>Slave</span><span>Group</span><span>Operation</span><span>Direction</span><span>Function</span><span>Duration</span><span>Result</span><span>Details</span></div><div class="rs485-logs-body" data-rs485-logs-body>${rowMarkup}</div></div></section>`;
    }
    function renderRs485LegacyDiagnostics(device, logsOnly = false) {
      const manual = rs485ManualCommandState[device.id] || { slave_id: device.slave_address, function: 3, address: 0, count: 1, value: 0 };
      const runtime = rs485Runtime(device);
      const state = rs485RuntimeGroup(device, 'state');
      const diagnostics = device.diagnostics || {};
      const communicationStatus = rs485CommunicationStatus(device);
      const online = communicationStatus === 'ONLINE';
      const communicationDescription = communicationStatus === 'ONLINE' ? 'Device is responding normally' : communicationStatus === 'OFFLINE' ? 'No valid response from the device' : 'Waiting for the first valid response';
      const traffic = diagnostics.entries || [];
      const trafficRows = rs485DiagnosticsTrafficRows(device);
      const lastError = runtime.last_error || state.last_error || '—';
      const mode = state.mode === 'on_demand' ? 'On demand' : (state.mode === 'disabled' ? 'Disabled' : 'Continuous');
      const interval = state.interval_ms ? `${state.interval_ms} ms` : '—';
      const inputs = rs485RuntimeGroup(device, 'inputs');
      const outputs = rs485RuntimeGroup(device, 'outputs');
      const responseTimes = traffic.filter((entry) => entry.result === 'response').map((entry) => Number(entry.response_ms)).filter((value) => Number.isFinite(value) && value >= 0);
      const averageResponse = responseTimes.length ? Math.round(responseTimes.reduce((sum, value) => sum + value, 0) / responseTimes.length) : null;
      const minResponse = responseTimes.length ? Math.min(...responseTimes) : null;
      const maxResponse = responseTimes.length ? Math.max(...responseTimes) : null;
      const totalRequests = Number(diagnostics.total_requests || 0);
      const successfulResponses = Number(diagnostics.successful_responses || 0);
      const successRate = totalRequests ? `${Math.round((successfulResponses / totalRequests) * 100)}%` : '—';
      const lastTransaction = [...traffic].reverse().find((entry) => entry.result === 'response' || entry.result === 'error') || traffic[traffic.length - 1];
      const bus = device.serial_port ? `${device.serial_port} · ${device.baudrate || '—'} baud · ${String(device.parity || 'none').toUpperCase()} · ${device.stop_bits || 1} stop` : '—';
      const lastSeen = rs485RuntimeTime(runtime.last_seen);
      const lastUpdate = rs485RuntimeTime(runtime.last_update);
      const dataAgeSeconds = diagnostics.last_valid_response_at ? Math.max(0, Math.round((Date.now() - Date.parse(diagnostics.last_valid_response_at)) / 1000)) : null;
      const dataAge = dataAgeSeconds == null ? '—' : `${dataAgeSeconds} s${dataAgeSeconds > 5 ? ' · STALE' : ''}`;
      const latencyAvg = totalRequests ? Math.round(Number(diagnostics.latency_total_ms || 0) / totalRequests) : null;
      return `<div class="rs485-diagnostics-shell${logsOnly ? ' rs485-logs-panel' : ''}">
        <div class="rs485-diagnostics-cards">
          <section class="rs485-diagnostics-card">
            <div class="rs485-diagnostics-card-head"><span class="diag-icon">[ ]</span>Communication Status</div>
            <div class="rs485-diagnostics-list">
              <div class="rs485-diagnostics-status-row">
              <div class="rs485-diagnostics-online ${online ? '' : 'offline'}">${online ? 'ONLINE' : 'OFFLINE'}</div>
              <div class="rs485-diagnostics-subtitle">${communicationDescription}</div>
              </div>
              <div><span>Last seen</span><strong>${lastSeen}</strong></div>
              <div><span>Data age</span><strong>${dataAge}</strong></div>
              <div><span>Last update</span><strong>${lastUpdate}</strong></div>
              <div><span>Average response time</span><strong>${averageResponse == null ? '—' : `${averageResponse} ms`}</strong></div>
              <div><span>RTU link</span><strong title="${bus}">${bus}</strong></div>
              <div><span>Response latency</span><strong>${averageResponse == null ? '—' : `${averageResponse} ms avg · ${minResponse}/${maxResponse} ms min/max`}</strong></div>
              <div><span>Success rate</span><strong>${successRate} · ${successfulResponses}/${totalRequests}</strong></div>
              <div><span>TX / RX</span><strong>${diagnostics.tx_count || 0} / ${diagnostics.rx_count || 0}</strong></div>
              <div><span>Timeout count</span><strong>${diagnostics.timeout_count || 0}</strong></div>
              <div><span>CRC / exception / malformed</span><strong>${diagnostics.crc_errors || 0} / ${diagnostics.exception_count || 0} / ${diagnostics.malformed_count || 0}</strong></div>
              <div><span>Wrong slave / function</span><strong>${diagnostics.wrong_slave_count || 0} / ${diagnostics.wrong_function_count || 0}</strong></div>
              ${diagnostics.possible_bus_conflict ? '<div class="rs485-diagnostics-warning"><span>Bus warning</span><strong>Possible multiple masters</strong></div>' : ''}
            </div>
          </section>
          <section class="rs485-diagnostics-card">
            <div class="rs485-diagnostics-card-head"><span class="diag-icon">~</span>Polling Status</div>
            <div class="rs485-diagnostics-list">
              <div><span>Current polling mode</span><strong>${mode}</strong></div>
              <div><span>DI / relay intervals</span><strong>${inputs.interval_ms || '—'} / ${outputs.interval_ms || '—'} ms</strong></div>
              <div><span>Next poll in</span><strong>—</strong></div>
              <div><span>Last poll result</span><strong>${lastError === '—' ? 'Success' : 'Error'}</strong></div>
              <div><span>Last update / seen</span><strong>${lastUpdate} / ${lastSeen}</strong></div>
              <div><span>Last transaction</span><strong>${lastTransaction ? `${lastTransaction.direction || '—'} · FC ${lastTransaction.function || '—'} · ${lastTransaction.response_ms ?? '—'} ms` : '—'}</strong></div>
              <div><span>Consecutive failures</span><strong>${diagnostics.consecutive_failures || 0}</strong></div>
              <div><span>Last error</span><strong>${lastError}</strong></div>
              <div><span>Requests processed</span><strong>${totalRequests}</strong></div>
              <div><span>Last response</span><strong>${lastTransaction && lastTransaction.response_ms != null ? `${lastTransaction.response_ms} ms` : '—'}</strong></div>
              <div><span>Data age</span><strong>${dataAge}</strong></div>
              <div><span>TX / RX</span><strong>${diagnostics.tx_count || 0} / ${diagnostics.rx_count || 0}</strong></div>
              <div><span>Success rate</span><strong>${successRate}</strong></div>
              <div><span>Latency avg / min / max</span><strong>${latencyAvg == null ? '—' : `${latencyAvg} / ${diagnostics.latency_min_ms ?? '—'} / ${diagnostics.latency_max_ms ?? '—'} ms`}</strong></div>
              <div><span>Protocol errors</span><strong>${diagnostics.protocol_errors || 0}</strong></div>
            </div>
          </section>
        </div>
        <section class="rs485-traffic-panel">
          <div class="rs485-traffic-filters"><div id="rs485-diagnostics-filter" class="mode-select"></div><div id="rs485-diagnostics-port-filter" class="mode-select"></div><div id="rs485-diagnostics-group-filter" class="mode-select"></div><div id="rs485-diagnostics-operation-filter" class="mode-select"></div><div id="rs485-diagnostics-slave-filter" class="mode-select"></div><div id="rs485-diagnostics-function-filter" class="mode-select"></div><button type="button" onclick="toggleRs485DiagnosticsView()">${rs485DiagnosticsView === 'decoded' ? 'Show raw' : 'Show decoded'}</button><button type="button" onclick="exportRs485Diagnostics('csv')">Export CSV</button><button type="button" onclick="exportRs485Diagnostics('json')">Export JSON</button></div>
          <div class="rs485-traffic-head"><div class="rs485-traffic-title">Modbus Traffic Log</div><div class="rs485-traffic-actions"><button type="button" data-rs485-manual-open="${device.id}">Send packet</button><button type="button" data-rs485-diagnostics-reset="${device.id}">Reset counters</button><button type="button" class="danger-button" data-rs485-diagnostics-clear="${device.id}">Clear log</button><button type="button" data-rs485-diagnostics-toggle="${device.id}">${diagnostics.paused ? 'Start' : 'Stop'}</button></div></div>
          <div class="rs485-manual-command ${rs485ManualCommandOpen ? 'open' : ''}" data-rs485-manual-panel="${device.id}">
            <div class="rs485-manual-command-head"><span>Send packet</span><button type="button" data-rs485-manual-close="${device.id}" aria-label="Close">&times;</button></div>
            <div class="rs485-manual-command-body">
              <div class="rs485-manual-command-fields">
                <label>Slave ID<input type="number" min="1" max="247" value="${manual.slave_id}" data-rs485-manual-slave></label>
                <label>Function code<div class="mode-select rs485-manual-function-select" data-rs485-manual-function data-value="${manual.function}"></div></label>
                <label>Start address<input type="number" min="0" max="65535" value="${manual.address}" data-rs485-manual-address></label>
                <label data-rs485-manual-count-field>Count<input type="number" min="1" max="125" value="${manual.count}" data-rs485-manual-count></label>
                <label class="hidden" data-rs485-manual-value-field><span data-rs485-manual-value-label>Value</span><input type="number" min="0" max="65535" value="${manual.value}" data-rs485-manual-value></label>
              </div>
              <div class="rs485-manual-preview" data-rs485-manual-preview>Preview: enter command values</div>
              <div class="rs485-manual-result" data-rs485-manual-result role="status"></div>
              <button type="button" class="rs485-manual-command-submit" data-rs485-manual-send="${device.id}">Send packet</button>
            </div>
          </div>
          <div class="rs485-traffic-table-head"><span>#</span><span>Time</span><span>Delta</span><span>Direction</span><span>Slave</span><span>Function</span><span>FC</span><span>Payload</span><span>Byte</span><span>CRC</span></div>
          <div class="rs485-traffic-empty ${traffic.length ? '' : 'no-entries'}">${traffic.length ? trafficRows : 'No Modbus traffic recorded yet.'}</div>
          <div class="rs485-diagnostics-footer">${renderRs485DiagnosticsFooter(device)}</div>
        </section>
      </div>`;
    }
    function renderRs485DiagnosticsFooter(device) {
      const runtime = rs485Runtime(device);
      const diagnostics = device.diagnostics || {};
      const entries = (diagnostics.entries || []).slice(-rs485DiagnosticsBufferSize());
      const last = entries[entries.length - 1];
      const failures = Number(diagnostics.timeout_count || 0) + Number(diagnostics.protocol_errors || 0);
      const success = Number(diagnostics.successful_responses || 0);
      const total = Number(diagnostics.total_requests || 0);
      const rate = total ? `${Math.min(100, Math.round((success / total) * 100))}%` : '—';
      return `<div><span class="rs485-diagnostics-online ${runtime.online === false ? 'offline' : ''}"></span><strong>${diagnostics.paused ? 'Traffic capture off' : 'Traffic capture active'}</strong></div><div><strong>Requests: ${total || '—'}</strong></div><div><strong>Success: ${rate}</strong><span>${failures ? ` / Errors: ${failures}` : ''}</span></div><div><strong>Last response: ${last && last.response_ms != null ? `${last.response_ms} ms` : '—'}</strong></div>`;
    }
    function renderRs485PollingControls(device) {
      const inputs = rs485RuntimeGroup(device, 'inputs');
      const outputs = rs485RuntimeGroup(device, 'outputs');
      return `
        <div class="rs485-polling-field">
          <label>DI interval ms</label>
          <input id="rs485-poll-inputs-${device.id}" type="number" min="50" max="600000" step="50" value="${inputs.interval_ms || 100}" onchange="setRs485PollingSettings('${device.id}')">
        </div>
        <div class="rs485-polling-field">
          <label>Relay state read interval ms</label>
          <input id="rs485-poll-outputs-${device.id}" type="number" min="50" max="600000" step="50" value="${outputs.interval_ms || 250}" onchange="setRs485PollingSettings('${device.id}')">
        </div>`;
    }
    function rs485PollingCaption(device, groupId, fallback) {
      const group = rs485RuntimeGroup(device, groupId);
      if (group.mode === 'on_demand') return 'On demand';
      if (group.interval_ms) return `Live ${group.interval_ms} ms`;
      return fallback;
    }
    function rs485RuntimeTime(value) {
      if (!value) return 'never';
      const date = new Date(value);
      return Number.isNaN(date.getTime()) ? value : date.toLocaleTimeString();
    }
    function rs485RuntimeSummary(device) {
      const runtime = rs485Runtime(device);
      const state = runtime.online ? (runtime.polling || 'live') : 'offline';
      const parts = [
        `<span>Polling: <strong>${state}</strong></span>`,
        `<span>DI: <strong>${rs485RuntimeGroup(device, 'inputs').interval_ms || 100} ms</strong></span>`,
        `<span>Relay: <strong>${rs485RuntimeGroup(device, 'outputs').interval_ms || 250} ms</strong></span>`,
        `<span>Last update: <strong>${rs485RuntimeTime(runtime.last_update)}</strong></span>`
      ];
      if (runtime.last_error) parts.push(`<span>Error: <strong>${runtime.last_error}</strong></span>`);
      return parts.join('');
    }
    function rs485DeviceWritable(device) {
      // Writes are command transactions (for example FC05), not device polling.
      // They only require the selected bus and write access to be enabled.
      return Boolean(device && device.write_enabled !== false && rs485ApiState?.bus?.enabled !== false);
    }
    function renderRs485RowCapability(device, capability, labelOverride = '') {
      const displayName = labelOverride || capability.name;
      const value = device.values ? device.values[capability.id] : undefined;
      const error = device.values ? device.values[`${capability.id}__error`] : null;
      const writable = rs485DeviceWritable(device);
      const staleClass = writable ? '' : ' rs485-stale';
      if (capability.type === 'switch') {
        const on = Boolean(value);
        return `
          <div class="relay-row${staleClass}" data-rs485-live-value="${capability.id}" data-rs485-live-type="switch" title="${error || ''}">
            <span>${displayName}</span>
            <span class="state-text ${on ? 'on' : ''}">${on ? 'ON' : 'OFF'}</span>
            <button class="toggle ${on ? 'on' : ''}" type="button" data-rs485-writable="${writable}" onclick="setRs485Capability('${device.id}', '${capability.id}', ${!on})" ${writable ? '' : 'disabled'}><span>${on ? 'ON' : 'OFF'}</span></button>
          </div>`;
      }
      const on = Boolean(value);
      if (capability.type === 'binary_input') {
        return `
          <div class="relay-row${error ? ' rs485-stale' : ''}" data-rs485-live-value="${capability.id}" data-rs485-live-type="binary_input" title="${error || ''}">
            <span>${displayName}</span>
            <span class="state-text ${on ? 'on' : ''}">${on ? 'ON' : 'OFF'}</span>
            <button class="toggle rs485-input-toggle ${on ? 'on' : ''}" type="button" disabled aria-label="${displayName} ${on ? 'ON' : 'OFF'}"><span>${on ? 'ON' : 'OFF'}</span></button>
          </div>`;
      }
      return `
        <div class="relay-row">
          <span>${displayName}</span>
          <span class="state-text">${value ?? 'Mock'}${capability.unit ? ' ' + capability.unit : ''}</span>
        </div>`;
    }
    function renderRs485ModeCapability(device, capability) {
      const value = device.values ? device.values[capability.id] : undefined;
      const error = device.values ? device.values[`${capability.id}__error`] : null;
      const options = (capability.options || []).map((option) => ({ value: option.id, label: option.name }));
      const writable = rs485DeviceWritable(device);
      return `<div class="rs485-mode-row${writable && !error ? '' : ' rs485-stale'}" title="${error || ''}">${rs485InlineSelect(options, value || (options[0] && options[0].value), (nextValue) => `setRs485Capability('${device.id}', '${capability.id}', '${nextValue}')`, !writable)}</div>`;
    }
    function renderRs485FieldCapability(device, capability) {
      const pending = rs485PendingSettings[device.id] || {};
      const canonicalValues = {
        device_baudrate: device.baudrate,
        device_parity: device.parity,
        device_address: device.slave_address
      };
      const storedValue = Object.prototype.hasOwnProperty.call(canonicalValues, capability.id)
        ? canonicalValues[capability.id]
        : (device.values ? device.values[capability.id] : '');
      const value = Object.prototype.hasOwnProperty.call(pending, capability.id) ? pending[capability.id] : storedValue;
      const error = device.values ? device.values[`${capability.id}__error`] : null;
      if (capability.type === 'select') {
        const options = (capability.options || []).map((option) => ({ value: option.id, label: option.name }));
        return `
          <div class="rs485-field${error ? ' rs485-stale' : ''}" title="${error || ''}">
            <label>${capability.name}</label>
            ${rs485InlineSelect(options, value || (options[0] && options[0].value), (nextValue) => `setRs485PendingSetting('${device.id}', '${capability.id}', '${nextValue}')`, !rs485DeviceWritable(device))}
          </div>`;
      }
      if (capability.type === 'number') {
        return `
          <div class="rs485-field${error ? ' rs485-stale' : ''}" title="${error || ''}">
            <label>${capability.name}</label>
            <input type="number" min="${capability.min || 1}" max="${capability.max || 247}" value="${value}" onchange="setRs485PendingSetting('${device.id}', '${capability.id}', this.value)" ${rs485DeviceWritable(device) ? '' : 'disabled'}>
          </div>`;
      }
      return `
        <div class="rs485-field">
          <label>${capability.name}</label>
          <input type="text" value="${value ?? 'Mock'}" readonly>
        </div>`;
    }

    let diagnosticIndicators = {
      ste: { heartbeat_enabled: false },
      net: { indicator_enabled: false },
      err: { indicator_enabled: false }
    };
    async function toggleSteHeartbeat() {
      const enabled = !(diagnosticIndicators.ste && diagnosticIndicators.ste.heartbeat_enabled);
      const payload = await requestJson('api/v1/diagnostic-indicators/ste-heartbeat', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled })
      });
      paintDiagnosticIndicators(payload);
    }
    async function toggleNetLed() {
      const enabled = !(diagnosticIndicators.net && diagnosticIndicators.net.indicator_enabled);
      const payload = await requestJson('api/v1/diagnostic-indicators/net', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled })
      });
      paintDiagnosticIndicators(payload);
    }
    async function toggleErrLed() {
      const enabled = !(diagnosticIndicators.err && diagnosticIndicators.err.indicator_enabled);
      const payload = await requestJson('api/v1/diagnostic-indicators/err', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled })
      });
      paintDiagnosticIndicators(payload);
    }
    function paintDiagnosticIndicators(payload) {
      if (!payload) return;
      diagnosticIndicators = payload;
      paintDiagnosticLed(
        'ste-heartbeat-state',
        'ste-heartbeat-toggle',
        Boolean(payload.ste && payload.ste.heartbeat_enabled),
        'Toggle STE LED heartbeat'
      );
      paintDiagnosticLed(
        'net-led-state',
        'net-led-toggle',
        Boolean(payload.net && payload.net.indicator_enabled),
        'Toggle NET LED connection indication'
      );
      paintDiagnosticLed(
        'err-led-state',
        'err-led-toggle',
        Boolean(payload.err && payload.err.indicator_enabled),
        'Toggle ERR LED error indication'
      );
    }
    function paintDiagnosticLed(stateId, toggleId, enabled, title) {
      const state = document.getElementById(stateId);
      const toggle = document.getElementById(toggleId);
      if (!state || !toggle) return;
      state.textContent = enabled ? 'ON' : 'OFF';
      state.className = `state-text ${enabled ? 'on' : ''}`;
      toggle.className = `toggle ${enabled ? 'on' : ''}`;
      toggle.innerHTML = `<span>${enabled ? 'ON' : 'OFF'}</span>`;
      toggle.title = title;
    }
    function formatRtcTime(value) {
      if (!value) return 'Not available';
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return value;
      return date.toLocaleString();
    }
    function paintRtcStatus(payload) {
      const systemTime = document.getElementById('rtc-system-time');
      const rtcTime = document.getElementById('rtc-time');
      const difference = document.getElementById('rtc-difference');
      const status = document.getElementById('rtc-status');
      const errorText = document.getElementById('rtc-error');
      const syncButton = document.getElementById('rtc-sync-button');
      if (!systemTime || !rtcTime || !difference || !status || !syncButton || !errorText) return;
      const systemDate = new Date(payload.system_time);
      const rtcValue = payload.rtc_local_time || payload.rtc_time;
      const rtcDate = rtcValue ? new Date(rtcValue) : new Date(Number.NaN);
      systemTime.textContent = formatRtcTime(payload.system_time);
      rtcTime.textContent = payload.rtc_local_time ? formatRtcTime(payload.rtc_local_time) : formatRtcTime(payload.rtc_time);
      difference.textContent = Number.isNaN(rtcDate.getTime()) || Number.isNaN(systemDate.getTime())
        ? 'Unknown'
        : `${((rtcDate.getTime() - systemDate.getTime()) / 1000).toFixed(3)}s`;
      status.textContent = payload.status || 'UNKNOWN';
      status.className = `state-text ${payload.available ? 'on' : ''}`;
      syncButton.disabled = !payload.available;
      syncButton.title = payload.error || 'Write current system time to RTC';
      errorText.textContent = payload.error ? `RTC error: ${payload.error}` : '';
    }
    async function refreshRtc() {
      try {
        paintRtcStatus(await requestJson('api/v1/rtc'));
      } catch (error) {
        paintRtcStatus({
          available: false,
          rtc_time: null,
          system_time: new Date().toISOString(),
          difference_seconds: null,
          status: 'ERROR',
          error: JSON.stringify(error)
        });
      }
    }
    async function syncRtc() {
      try {
        paintRtcStatus(await requestJson('api/v1/rtc/sync', { method: 'POST' }));
      } catch (error) {
        paintRtcStatus({
          available: false,
          rtc_time: null,
          system_time: new Date().toISOString(),
          difference_seconds: null,
          status: 'ERROR',
          error: JSON.stringify(error)
        });
      }
    }
    let buzzerLastVolumePercent = 50;
    function buzzerPayload() {
      const frequency = clampNumber(document.getElementById('buzzer-frequency').value, 300, 2800);
      const duration = clampNumber(document.getElementById('buzzer-duration').value, 10, 1000);
      const volume = clampNumber(document.getElementById('buzzer-volume').value, 0, 100);
      return {
        frequency,
        duration_ms: duration,
        volume_percent: volume
      };
    }
    function clampNumber(value, min, max) {
      const parsed = Number(value);
      if (Number.isNaN(parsed)) return min;
      return Math.max(min, Math.min(max, parsed));
    }
    async function saveBuzzerSettings() {
      const requested = buzzerPayload();
      try {
        await requestJson('api/v1/buzzer/settings', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(requested)
        });
      } catch (error) {
        document.getElementById('buzzer-status').textContent = 'BUZZER: settings failed';
        document.getElementById('buzzer-detail').textContent = JSON.stringify(error);
      }
    }
    function updateBuzzerSettingLabels() {
      const frequency = clampNumber(document.getElementById('buzzer-frequency').value, 300, 2800);
      const duration = clampNumber(document.getElementById('buzzer-duration').value, 10, 1000);
      const value = clampNumber(document.getElementById('buzzer-volume').value, 0, 100);
      document.getElementById('buzzer-frequency-value').textContent = `${frequency} Hz`;
      document.getElementById('buzzer-duration-value').textContent = `${duration} ms`;
      document.getElementById('buzzer-volume-value').textContent = `${value}%`;
      if (value > 0) buzzerLastVolumePercent = value;
      paintBuzzerPower(value, buzzerLastVolumePercent);
    }
    function paintBuzzerPower(volume, lastVolume) {
      const enabled = Number(volume) > 0;
      const toggle = document.getElementById('buzzer-power-toggle');
      if (!toggle) return;
      toggle.className = `toggle ${enabled ? 'on' : ''}`;
      toggle.innerHTML = `<span>${enabled ? 'ON' : 'OFF'}</span>`;
      toggle.title = enabled ? 'Turn buzzer volume off' : `Restore buzzer volume to ${lastVolume || 50}%`;
    }
    async function renderBuzzerStatus() {
      try {
        const payload = await requestJson('api/v1/buzzer/status');
        paintBuzzer(payload);
        paintBuzzerAvailability(payload);
      } catch (error) {
        document.getElementById('buzzer-status').textContent = 'BUZZER: Error';
        document.getElementById('buzzer-detail').textContent = JSON.stringify(error);
      }
    }
    function paintBuzzerAvailability(payload) {
      const detail = document.getElementById('buzzer-detail');
      if (!detail || !payload) return;
      const backendAvailable = Boolean((payload.pigpio && payload.pigpio.connected) || (payload.pwm && payload.pwm.available));
      const gpioAvailable = Boolean(payload.gpio && payload.gpio.available);
      detail.textContent = `GPIO18 PWM: ${backendAvailable && gpioAvailable ? 'available' : 'not available'}`;
    }
    function paintBuzzer(payload) {
      if (!payload) return;
      if (payload.frequency !== undefined) {
        document.getElementById('buzzer-frequency').value = Number(payload.frequency);
      }
      if (payload.duration_ms !== undefined) {
        document.getElementById('buzzer-duration').value = Number(payload.duration_ms);
      }
      const volume = Number(payload.volume_percent ?? 50);
      buzzerLastVolumePercent = Number(payload.last_volume_percent ?? (volume > 0 ? volume : buzzerLastVolumePercent));
      document.getElementById('buzzer-volume').value = volume;
      updateBuzzerSettingLabels();
      paintBuzzerPower(volume, buzzerLastVolumePercent);
      const state = payload.active && payload.active.running ? `running ${payload.active.backend}` : 'stopped';
      document.getElementById('buzzer-status').textContent = `BUZZER: ${state}`;
    }
    async function toggleBuzzerPower() {
      const current = clampNumber(document.getElementById('buzzer-volume').value, 0, 100);
      const requested = current > 0 ? 0 : clampNumber(buzzerLastVolumePercent, 1, 100);
      try {
        const payload = await requestJson('api/v1/buzzer/volume', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ volume_percent: requested })
        });
        paintBuzzer(payload);
      } catch (error) {
        document.getElementById('buzzer-status').textContent = 'BUZZER: settings failed';
        document.getElementById('buzzer-detail').textContent = JSON.stringify(error);
      }
    }
    async function playBuzzer() {
      const detail = document.getElementById('buzzer-detail');
      const requested = buzzerPayload();
      detail.textContent = 'Playing buzzer...';
      try {
        const payload = await requestJson('api/v1/buzzer/play-pwm', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(requested)
        });
        document.getElementById('buzzer-status').textContent = `BUZZER: running ${payload.backend}`;
        paintBuzzerAvailability(payload);
        window.setTimeout(renderBuzzerStatus, requested.duration_ms + 250);
      } catch (error) {
        document.getElementById('buzzer-status').textContent = 'BUZZER: Test failed';
        detail.textContent = JSON.stringify(error);
      }
    }
    async function stopBuzzer() {
      try {
        const payload = await requestJson('api/v1/buzzer/stop', { method: 'POST' });
        document.getElementById('buzzer-status').textContent = 'BUZZER: stopped';
        paintBuzzerAvailability(payload);
      } catch (error) {
        document.getElementById('buzzer-status').textContent = 'BUZZER: stop failed';
        document.getElementById('buzzer-detail').textContent = JSON.stringify(error);
      }
    }
    window.RS485_ENABLED = __RS485_ENABLED__;
    const rs485NavButton = document.querySelector('[data-ui-nav-target="rs485-section"]');
    if (rs485NavButton && !window.RS485_ENABLED) rs485NavButton.hidden = true;
    document.getElementById('buzzer-frequency').addEventListener('change', saveBuzzerSettings);
    document.getElementById('buzzer-duration').addEventListener('change', saveBuzzerSettings);
    document.getElementById('buzzer-volume').addEventListener('change', saveBuzzerSettings);
    document.addEventListener('click', (event) => {
      document.querySelectorAll('.rs485-detail-action-menu[open], .rs485-template-menu[open]').forEach((menu) => {
        if (!menu.contains(event.target)) menu.removeAttribute('open');
      });
      const clearLogButton = event.target.closest('[data-rs485-clear-log]');
      if (clearLogButton) {
        event.preventDefault();
        event.stopPropagation();
        clearRs485ScanLog();
        return;
      }
      const addButton = event.target.closest('[data-rs485-add-id]');
      if (addButton) {
        event.preventDefault();
        event.stopPropagation();
        addRs485Device(addButton.dataset.rs485AddId);
        return;
      }
      if (!event.target.closest('.mode-select')) {
        document.querySelectorAll('.mode-select.open').forEach((item) => item.classList.remove('open'));
      }
    }, true);
    function initGlobalNavigation() {
      const nav = document.getElementById('global-nav');
      if (!nav) return;
      let navigationLockUntil = 0;
      const buttons = [...nav.querySelectorAll('[data-ui-nav-target]')];
      const sections = buttons.map((button) => document.getElementById(button.dataset.uiNavTarget)).filter(Boolean);
      buttons.forEach((button) => {
        const target = document.getElementById(button.dataset.uiNavTarget);
        if (!target) { button.hidden = true; return; }
        button.onclick = () => {
          setActive(target.id);
          navigationLockUntil = Date.now() + 1200;
          localStorage.setItem('intellegyhub.ui.section', target.id);
          window.scrollTo({ top: Math.max(0, target.getBoundingClientRect().top + window.scrollY - 74), behavior: 'smooth' });
        };
      });
      const setActive = (id) => buttons.forEach((button) => button.classList.toggle('active', button.dataset.uiNavTarget === id));
      if ('IntersectionObserver' in window) {
        const observer = new IntersectionObserver((entries) => {
          if (Date.now() < navigationLockUntil) return;
          const visible = entries.filter((entry) => entry.isIntersecting).sort((a, b) => b.intersectionRatio - a.intersectionRatio)[0];
          if (visible) { setActive(visible.target.id); localStorage.setItem('intellegyhub.ui.section', visible.target.id); }
        }, { rootMargin: '-74px 0px -55% 0px', threshold: [0.1, 0.35, 0.6] });
        sections.forEach((section) => observer.observe(section));
      }
      const saved = localStorage.getItem('intellegyhub.ui.section');
      setActive(sections.some((section) => section.id === saved) ? saved : 'overview-section');
    }
    applyTheme('auto');
    loadTheme();
    initGlobalNavigation();
    renderCarrier();
    renderXPort();
    renderCarrierIO();
    renderExtensions();
    if (window.RS485_ENABLED) {
      loadRs485Mock();
      initRs485MockControls();
      renderRs485Mock();
    } else {
      const rs485Section = document.getElementById('rs485-section');
      if (rs485Section) rs485Section.remove();
    }
    renderOneWire();
    renderBuzzerStatus();
    refreshRtc();
    connectEvents();
  </script>
</body>
</html>
        """
        if not app.state.rs485_enabled:
            start = html.find("    <!-- RS485_START -->")
            end = html.find("    <!-- RS485_END -->")
            if start != -1 and end != -1:
                end += len("    <!-- RS485_END -->")
                if end < len(html) and html[end : end + 1] == "\n":
                    end += 1
                html = html[:start] + html[end:]
        html = html.replace("__RS485_ENABLED__", "true" if app.state.rs485_enabled else "false")
        return HTMLResponse(html, headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"})

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> FileResponse:
        return FileResponse(Path(__file__).resolve().parents[1] / "icon.png")

    @app.get("/logo.png", include_in_schema=False)
    async def logo() -> FileResponse:
        return FileResponse(Path(__file__).resolve().parents[1] / "logo.png")

    @app.get("/rs485-icons/{template_id}.png", include_in_schema=False)
    async def rs485_icon(template_id: str) -> FileResponse:
        icon = Path(__file__).resolve().parents[1] / "rs485_assets" / f"{template_id}.png"
        if not icon.is_file():
            raise HTTPException(status_code=404, detail="RS-485 device icon not found")
        return FileResponse(icon)

    @app.get("/rs485_assets/{template_id}.png", include_in_schema=False)
    async def rs485_asset(template_id: str) -> FileResponse:
        asset = Path(__file__).resolve().parents[1] / "rs485_assets" / f"{template_id}.png"
        if not asset.is_file():
            raise HTTPException(status_code=404, detail="RS-485 device asset not found")
        return FileResponse(asset)

    @app.get("/api/v1/state")
    async def get_state() -> dict:
        return await runtime_or_503().async_snapshot()

    @app.get("/api/v1/ui/settings")
    async def get_ui_settings() -> dict:
        return runtime_or_503().ui_settings.snapshot()

    @app.put("/api/v1/diagnostic-indicators/ste-heartbeat")
    async def put_ste_heartbeat(payload: SteHeartbeatPayload) -> dict:
        try:
            return await runtime_or_503().set_ste_heartbeat(
                payload.enabled,
                payload.on_seconds,
                payload.off_seconds,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.put("/api/v1/diagnostic-indicators/net")
    async def put_net_indicator(payload: DiagnosticIndicatorPayload) -> dict:
        return await runtime_or_503().set_net_indicator(payload.enabled)

    @app.put("/api/v1/diagnostic-indicators/err")
    async def put_err_indicator(payload: DiagnosticIndicatorPayload) -> dict:
        return await runtime_or_503().set_err_indicator(payload.enabled)

    @app.put("/api/v1/ui/theme")
    async def put_ui_theme(payload: UiThemePayload) -> dict:
        return await runtime_or_503().set_ui_theme(payload.theme)

    @app.get("/api/v1/rs485/templates")
    async def get_rs485_templates() -> dict:
        return rs485_or_404().templates_snapshot()

    @app.get("/api/v1/rs485/templates/{template_id}")
    async def get_rs485_template(template_id: str) -> dict:
        try:
            return rs485_or_404().template_snapshot(template_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="template not found") from exc

    @app.post("/api/v1/rs485/templates/reload")
    async def post_rs485_templates_reload() -> dict:
        return rs485_or_404().registry.reload()

    @app.post("/api/v1/rs485/templates/upload")
    async def post_rs485_template_upload(request: Request, filename: str = "template.yaml") -> dict:
        try:
            data = await request.body()
            return await rs485_or_404().upload_template(filename, data)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.delete("/api/v1/rs485/templates/{template_id}")
    async def delete_rs485_template(template_id: str) -> dict:
        try:
            return await rs485_or_404().delete_template(template_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/v1/rs485")
    async def get_rs485() -> dict:
        return rs485_or_404().snapshot()

    @app.get("/api/v1/rs485/live")
    async def get_rs485_live() -> dict:
        return rs485_or_404().live_snapshot()

    @app.get("/api/v1/rs485/devices/{device_id}/diagnostics/live")
    async def get_rs485_diagnostics_live(device_id: str) -> dict:
        try:
            return rs485_or_404().diagnostics_live_snapshot(device_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.put("/api/v1/rs485/bus")
    async def put_rs485_bus(payload: Rs485BusPayload) -> dict:
        result = await rs485_or_404().save_bus(payload.model_dump())
        app.state.runtime.broadcast_nowait({"type": "rs485_changed", "rs485": result})
        return result

    @app.patch("/api/v1/rs485/bus")
    async def patch_rs485_bus(payload: Rs485BusPatchPayload) -> dict:
        result = await rs485_or_404().patch_bus(payload.model_dump(exclude_none=True))
        app.state.runtime.broadcast_nowait({"type": "rs485_changed", "rs485": result})
        return result

    @app.post("/api/v1/rs485/scan")
    async def post_rs485_scan(payload: Rs485BusPayload, compact: bool = False) -> dict:
        manager = rs485_or_404()
        return await manager.scan(payload.model_dump(), compact=compact)

    @app.post("/api/v1/rs485/scan/stop")
    async def post_rs485_scan_stop(compact: bool = False) -> dict:
        manager = rs485_or_404()
        return await manager.stop_scan(compact=compact)

    @app.get("/api/v1/rs485/scan/progress")
    async def get_rs485_scan_progress() -> dict:
        return rs485_or_404().scan_progress_snapshot()

    @app.post("/api/v1/rs485/scan/clear")
    async def post_rs485_scan_clear() -> dict:
        return await rs485_or_404().clear_scan_results()

    @app.post("/api/v1/rs485/devices/{device_id}/diagnostics/clear")
    async def post_rs485_diagnostics_clear(device_id: str) -> dict:
        try:
            return rs485_or_404().clear_device_diagnostics(device_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/v1/rs485/devices/{device_id}/diagnostics/reset")
    async def post_rs485_diagnostics_reset(device_id: str) -> dict:
        try:
            return rs485_or_404().reset_device_diagnostic_counters(device_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/v1/rs485/devices/{device_id}/diagnostics/pause")
    async def post_rs485_diagnostics_pause(device_id: str, request: Request) -> dict:
        try:
            payload = await request.json()
            return rs485_or_404().set_device_diagnostics_paused(device_id, bool(payload.get("paused")))
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/v1/rs485/refresh")
    async def post_rs485_refresh() -> dict:
        return await rs485_or_404().refresh()

    @app.post("/api/v1/rs485/devices/{scan_id}/add")
    async def post_rs485_device_add(scan_id: str) -> dict:
        LOGGER.info("RS485_ADD request scan_id=%s", scan_id)
        try:
            result = await rs485_or_404().add_device(scan_id)
            await app.state.runtime.broadcast({"type": "rs485_changed", "rs485": result})
            LOGGER.info("RS485_ADD committed scan_id=%s selected_id=%s devices=%s scanned=%s", scan_id, result.get("selected_id"), len(result.get("devices", [])), len(result.get("scanned", [])))
            return result
        except ValueError as exc:
            LOGGER.warning("RS485_ADD rejected scan_id=%s error=%s", scan_id, exc)
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.delete("/api/v1/rs485/devices/{device_id}")
    async def delete_rs485_device(device_id: str) -> dict:
        try:
            result = await rs485_or_404().remove_device(device_id)
            app.state.runtime.broadcast_nowait({"type": "rs485_changed", "rs485": result})
            return result
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/v1/rs485/devices/{device_id}/read")
    async def post_rs485_device_read(device_id: str, group: str | None = None) -> dict:
        try:
            return await rs485_or_404().read_device(device_id, group)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/v1/rs485/devices/{device_id}/manual-command")
    async def post_rs485_manual_command(device_id: str, payload: Rs485ManualCommandPayload) -> dict:
        if not 0 <= payload.address <= 0xFFFF or not 1 <= payload.count <= 125:
            raise HTTPException(status_code=400, detail="address/count out of range")
        if payload.function in {5, 6} and payload.count != 1:
            raise HTTPException(status_code=400, detail="write commands use count 1")
        try:
            return await rs485_or_404().manual_command(device_id, payload.function, payload.address, payload.count, payload.value, payload.slave_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.put("/api/v1/rs485/devices/{device_id}/name")
    async def put_rs485_device_name(device_id: str, payload: Rs485DeviceNamePayload) -> dict:
        try:
            result = await rs485_or_404().rename_device(device_id, payload.name)
            await app.state.runtime.broadcast({"type": "rs485_changed", "rs485": result})
            return result
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.put("/api/v1/rs485/devices/{device_id}/capabilities/{capability_id}")
    async def put_rs485_capability(device_id: str, capability_id: str, payload: Rs485CapabilityPayload) -> dict:
        try:
            result = await rs485_or_404().set_capability(device_id, capability_id, payload.value)
            app.state.runtime.broadcast_nowait({"type": "rs485_device_changed", "device": result["device"]})
            return result
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/v1/rs485/devices/{device_id}/collapse")
    async def post_rs485_device_collapse(device_id: str) -> dict:
        try:
            return await rs485_or_404().toggle_collapsed(device_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.put("/api/v1/rs485/devices/{device_id}/enabled")
    async def put_rs485_device_enabled(device_id: str, payload: Rs485DeviceEnabledPayload) -> dict:
        try:
            result = await rs485_or_404().set_device_enabled(device_id, payload.enabled)
            await app.state.runtime.broadcast({"type": "rs485_changed", "rs485": result})
            return result
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.put("/api/v1/rs485/devices/{device_id}/access")
    async def put_rs485_device_access(device_id: str, payload: Rs485DeviceAccessPayload) -> dict:
        try:
            result = await rs485_or_404().set_device_access(
                device_id,
                read_enabled=payload.read_enabled,
                write_enabled=payload.write_enabled,
            )
            await app.state.runtime.broadcast({"type": "rs485_changed", "rs485": result})
            return result
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.put("/api/v1/rs485/devices/{device_id}/polling")
    async def put_rs485_device_polling(device_id: str, payload: Rs485PollingPayload) -> dict:
        try:
            result = await rs485_or_404().set_device_polling(device_id, payload.model_dump(exclude_none=True))
            await app.state.runtime.broadcast({"type": "rs485_changed", "rs485": result})
            return result
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/v1/xport")
    async def get_xport() -> dict:
        return runtime_or_503().xport.snapshot()

    @app.get("/api/v1/extensions")
    async def get_extensions() -> dict:
        return runtime_or_503().extensions.snapshot()

    @app.get("/api/v1/carrier")
    async def get_carrier() -> dict:
        return runtime_or_503().carrier.snapshot()

    @app.put("/api/v1/carrier/outputs/{output_id}")
    async def put_carrier_output(output_id: str, payload: OutputPayload) -> dict:
        try:
            return (await runtime_or_503().carrier.set_output(output_id, payload.on)).__dict__
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.put("/api/v1/extensions/power")
    async def put_extension_power(payload: ExtensionPowerPayload) -> dict:
        try:
            return await runtime_or_503().extensions.set_power(payload.on)
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/api/v1/extensions/scan")
    async def post_extension_scan() -> dict:
        try:
            return await runtime_or_503().extensions.scan()
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.put("/api/v1/extensions/modules/{module_id}/relays/{channel}")
    async def put_extension_relay(module_id: str, channel: int, payload: ExtensionRelayPayload) -> dict:
        try:
            return await runtime_or_503().extensions.set_relay(module_id, channel, payload.on)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.delete("/api/v1/extensions/modules/{module_id}")
    async def delete_extension_module(module_id: str) -> dict:
        try:
            return await runtime_or_503().extensions.delete_module(module_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/v1/onewire")
    async def get_onewire() -> dict:
        return runtime_or_503().onewire.snapshot()

    @app.put("/api/v1/onewire/power")
    async def put_onewire_power(payload: OneWirePowerPayload) -> dict:
        try:
            return await runtime_or_503().onewire.set_power(payload.on)
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/api/v1/onewire/scan")
    async def post_onewire_scan() -> dict:
        try:
            return await runtime_or_503().onewire.scan()
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/api/v1/onewire/refresh")
    async def post_onewire_refresh() -> dict:
        try:
            return await runtime_or_503().onewire.refresh()
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.put("/api/v1/onewire/bridges/{bridge_id}/enabled")
    async def put_onewire_bridge_enabled(bridge_id: str, payload: OneWireBridgeEnabledPayload) -> dict:
        try:
            return await runtime_or_503().onewire.set_bridge_enabled(bridge_id, payload.enabled)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/api/v1/onewire/sensors/{sensor_id}/add")
    async def post_onewire_sensor_add(sensor_id: str) -> dict:
        try:
            return await runtime_or_503().onewire.add_sensor(sensor_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.delete("/api/v1/onewire/sensors/{sensor_id}")
    async def delete_onewire_sensor(sensor_id: str) -> dict:
        try:
            return await runtime_or_503().onewire.delete_sensor(sensor_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/v1/buzzer/status")
    async def get_buzzer_status() -> dict:
        return runtime_or_503().buzzer.status()

    @app.put("/api/v1/buzzer/volume")
    async def put_buzzer_volume(payload: BuzzerVolumePayload) -> dict:
        try:
            return await runtime_or_503().set_buzzer_volume(payload.volume_percent)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.put("/api/v1/buzzer/settings")
    async def put_buzzer_settings(payload: BuzzerSettingsPayload) -> dict:
        try:
            return await runtime_or_503().set_buzzer_settings(
                frequency=payload.frequency,
                duration_ms=payload.duration_ms,
                volume_percent=payload.volume_percent,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/v1/buzzer/play")
    async def post_buzzer_play() -> dict:
        try:
            current = runtime_or_503()
            result = await current.buzzer.test_configured_pwm()
            await current.buzzer_store.save(current.buzzer.settings())
            await current.broadcast({"type": "buzzer_changed", "buzzer": current.buzzer.status()})
            return result
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/api/v1/buzzer/play-pwm")
    async def post_buzzer_play_pwm(payload: BuzzerTestPayload) -> dict:
        try:
            current = runtime_or_503()
            result = await current.buzzer.play_pwm(
                payload.frequency,
                payload.duration_ms,
                payload.duty,
                payload.volume_percent,
            )
            await current.buzzer_store.save(current.buzzer.settings())
            await current.broadcast({"type": "buzzer_changed", "buzzer": current.buzzer.status()})
            return result
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/api/v1/buzzer/test-configured")
    async def post_buzzer_test_configured() -> dict:
        return await post_buzzer_play()

    @app.post("/api/v1/buzzer/test-pwm")
    async def post_buzzer_test_pwm(payload: BuzzerTestPayload) -> dict:
        return await post_buzzer_play_pwm(payload)

    @app.post("/api/v1/buzzer/test-gpio")
    async def post_buzzer_test_gpio(payload: BuzzerTestPayload) -> dict:
        try:
            current = runtime_or_503()
            result = await current.buzzer.test_gpio(
                payload.frequency,
                payload.duration_ms,
                payload.duty,
                payload.volume_percent,
            )
            await current.buzzer_store.save(current.buzzer.settings())
            await current.broadcast({"type": "buzzer_changed", "buzzer": current.buzzer.status()})
            return result
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/api/v1/buzzer/stop")
    async def post_buzzer_stop() -> dict:
        await runtime_or_503().buzzer.stop()
        return {"status": "ok", **runtime_or_503().buzzer.status()}

    @app.put("/api/v1/xport/channels/{channel}/mode")
    async def put_xport_mode(channel: int, payload: XPortModePayload) -> dict:
        try:
            return await runtime_or_503().xport.configure(channel, payload.mode, payload.revision)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.put("/api/v1/xport/group-mode")
    async def put_xport_group_mode(payload: XPortGroupModePayload) -> dict:
        try:
            return await runtime_or_503().xport.configure_group_mode(payload.mode)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.put("/api/v1/xport/group/channels/{channel}/value")
    async def put_xport_group_channel_value(channel: int, payload: XPortValuePayload) -> dict:
        try:
            return await runtime_or_503().xport.set_group_channel_value(channel, payload.value)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.put("/api/v1/xport/channels/{channel}/value")
    async def put_xport_value(channel: int, payload: XPortValuePayload) -> dict:
        try:
            return await runtime_or_503().xport.set_value(channel, payload.value)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/v1/xport/channels/{channel}/counter/reset")
    async def post_xport_counter_reset(channel: int) -> dict:
        try:
            return await runtime_or_503().xport.reset_counter(channel)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/v1/diagnostics/devices")
    async def get_device_diagnostics() -> dict[str, list[str]]:
        return collect_device_diagnostics()

    @app.get("/api/v1/rtc")
    async def get_rtc_status() -> dict:
        return (await app.state.rtc.status()).__dict__

    @app.post("/api/v1/rtc/sync")
    async def post_rtc_sync() -> dict:
        try:
            return (await app.state.rtc.sync_from_system()).__dict__
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.get("/api/v1/i2c/scan/{bus}")
    async def get_i2c_scan(bus: int) -> dict:
        try:
            return await asyncio.to_thread(scan_i2c_bus, bus)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.put("/api/v1/led")
    async def put_led(payload: LedPayload) -> dict[str, bool]:
        confirmed = await runtime_or_503().set_led(payload.on)
        return {"on": confirmed}

    @app.put("/api/v1/outputs/{output_id}")
    async def put_output(output_id: str, payload: OutputPayload) -> dict:
        try:
            confirmed = await runtime_or_503().set_output(output_id, payload.on)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        snapshot = runtime_or_503().snapshot()["outputs"][output_id]
        return {**snapshot, "on": confirmed}

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        current = runtime_or_503()
        await current.add_client(websocket)
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            current.remove_client(websocket)

    return app


app = create_app()
