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

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from .backends import MockGpioBackend, RealGpiodBackend
from .config import load_config
from .runtime import AppRuntime
from .xport import XPortMode

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


class UiThemePayload(BaseModel):
    theme: Literal["auto", "light", "dark"]


def collect_device_diagnostics() -> dict[str, list[str]]:
    return {
        "gpio": sorted(glob.glob("/dev/gpio*")),
        "i2c": sorted(glob.glob("/dev/i2c*")),
    }


def scan_i2c_bus(bus: int) -> dict:
    if bus < 0 or bus > 255:
        raise ValueError("bus must be between 0 and 255")

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


def create_app(options_path: Path | None = None, runtime: AppRuntime | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if app.state.runtime is None:
            config = load_config(app.state.options_path)
            LOGGER.info(
                "Starting v0.5.120 chip=%s led=%s active_low=%s fn1_gpio=27 fn2_gpio=%s active_low=%s bias=%s debounce_ms=%s startup_buzzer=%s startup_buzzer_frequency=%s startup_buzzer_duration_ms=%s carrier_monitoring_poll_interval_seconds=%s onewire_bridge1_poll_interval_seconds=%s onewire_bridge2_poll_interval_seconds=%s mock=%s port=8098",
                config.chip_path,
                config.led_gpio,
                config.led_active_low,
                config.button_gpio,
                config.button_active_low,
                config.button_bias,
                config.button_debounce_ms,
                config.startup_buzzer_enabled,
                config.startup_buzzer_frequency,
                config.startup_buzzer_duration_ms,
                config.carrier_monitoring_poll_interval_seconds,
                config.onewire_bridge1_poll_interval_seconds,
                config.onewire_bridge2_poll_interval_seconds,
                config.mock,
            )
            LOGGER.info("Visible devices: %s", collect_device_diagnostics())
            backend = MockGpioBackend() if config.mock else RealGpiodBackend(config)
            app.state.runtime = AppRuntime(
                backend,
                startup_buzzer_enabled=config.startup_buzzer_enabled,
                startup_buzzer_frequency=config.startup_buzzer_frequency,
                startup_buzzer_duration_ms=config.startup_buzzer_duration_ms,
                carrier_monitoring_poll_interval_seconds=config.carrier_monitoring_poll_interval_seconds,
                onewire_poll_intervals={
                    "onewire_bus10_addr1a": config.onewire_bridge1_poll_interval_seconds,
                    "onewire_bus10_addr1b": config.onewire_bridge2_poll_interval_seconds,
                },
            )
        await app.state.runtime.start()
        try:
            yield
        finally:
            await app.state.runtime.stop()

    app = FastAPI(title="IntellegyHUB", version="0.5.120", lifespan=lifespan)
    app.state.runtime = runtime
    app.state.options_path = options_path

    def runtime_or_503() -> AppRuntime:
        current: AppRuntime = app.state.runtime
        if not current.ready:
            raise HTTPException(status_code=503, detail={"status": "error", "error": current.error or "not ready"})
        return current

    @app.get("/health")
    async def health() -> dict[str, str]:
        current: AppRuntime = app.state.runtime
        if current.ready:
            return {"status": "ok"}
        raise HTTPException(status_code=503, detail={"status": "error", "error": current.error or "not ready"})

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
    async def index() -> str:
        return """
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
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      padding: 24px;
      background: var(--ha-page);
      color: var(--ha-text);
    }
    main { width: min(100%, 1520px); margin: 0 auto; }
    .app-toolbar { display: flex; justify-content: flex-end; margin-bottom: 10px; }
    .theme-switcher { display: inline-flex; align-items: center; gap: 6px; border: 1px solid var(--ha-card-border); border-radius: 999px; background: var(--ha-card); padding: 4px; }
    .theme-switcher span { color: var(--ha-secondary); font-size: 12px; font-weight: 800; padding: 0 8px; text-transform: uppercase; letter-spacing: .08em; }
    .theme-choice { min-height: 30px !important; border-radius: 999px !important; padding: 4px 12px !important; border-color: transparent !important; color: var(--ha-secondary) !important; }
    .theme-choice.active { border-color: var(--ha-primary) !important; color: var(--ha-primary) !important; background: rgba(3, 169, 244, .12) !important; }
    .overview-panel { display: grid; grid-template-columns: minmax(320px, .9fr) minmax(520px, 1.5fr); gap: 0; margin-top: 0; margin-bottom: 18px; padding: 0; overflow: hidden; }
    .overview-identity { display: flex; flex-direction: column; min-height: 270px; padding: 22px 28px; border-right: 1px solid var(--ha-card-border); font-family: ui-monospace, "SFMono-Regular", Consolas, monospace; }
    .overview-badge-row { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; margin-bottom: 14px; }
    .overview-logo { display: block; width: 42px; height: 42px; object-fit: contain; }
    .overview-status-pill { min-height: 30px; padding: 6px 12px; border-radius: 999px; border: 1px solid var(--ha-success-border); color: var(--ha-success-text); background: var(--ha-success-bg); font-size: 12px; font-weight: 800; letter-spacing: .04em; text-transform: uppercase; }
    .overview-status-pill.offline { border-color: var(--ha-danger-border); color: var(--ha-danger-text); background: var(--ha-danger-bg); }
    .overview-title h1 { font-size: 30px; line-height: 1; margin-bottom: 6px; font-weight: 800; }
    .overview-title .subtitle { color: var(--ha-text); font-size: 13px; margin-bottom: 0; }
    .overview-facts { display: grid; grid-template-columns: 82px minmax(0, 170px); gap: 5px 18px; margin-top: 14px; font-size: 12px; line-height: 1.25; }
    .overview-facts dt { color: var(--ha-text); font-weight: 800; }
    .overview-facts dd { margin: 0; color: var(--ha-text); font-weight: 700; min-width: 0; overflow-wrap: anywhere; }
    .overview-divider { width: min(258px, 100%); height: 1px; background: var(--ha-card-border); margin: 18px 0 14px; }
    .overview-health { display: grid; grid-template-columns: 82px minmax(0, 170px); gap: 5px 18px; font-size: 12px; line-height: 1.25; }
    .overview-health dt { color: var(--ha-text); font-weight: 800; text-transform: uppercase; }
    .overview-health dd { margin: 0; color: var(--ha-text); font-weight: 700; min-width: 0; overflow-wrap: anywhere; }
    .overview-health-value { display: inline-flex; align-items: center; gap: 8px; }
    .overview-sync-dot { width: 8px; height: 8px; border-radius: 50%; background: #448aff; }
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
      min-height: 410px;
    }
    .module-row { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; margin-bottom: 22px; }
    .eyebrow { color: var(--ha-primary); font-size: 12px; font-weight: 700; letter-spacing: 2px; text-transform: uppercase; margin-bottom: 8px; }
    h1 { font-size: 28px; line-height: 1; margin: 0 0 7px; letter-spacing: 0; font-weight: 500; }
    .subtitle { color: var(--ha-secondary); font-size: 14px; margin-bottom: 8px; }
    .status { color: var(--ha-success); font-size: 14px; font-weight: 500; }
    .transport { border: 1px solid var(--ha-primary); color: var(--ha-primary); background: transparent; border-radius: 999px; padding: 6px 14px; font-size: 12px; font-weight: 500; margin-top: 38px; }
    .notice { color: var(--ha-secondary); font-size: 14px; line-height: 1.45; margin: 0 0 20px; overflow-wrap: anywhere; }
    .ports { display: grid; grid-template-columns: repeat(4, minmax(220px, 1fr)); gap: 16px; }
    .port-card { border: 1px solid var(--ha-card-border); border-radius: 12px; background: var(--ha-surface); padding: 20px; min-height: 228px; }
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
    .mode-select.open .mode-trigger { border-color: var(--ha-primary); box-shadow: none; }
    .mode-menu {
      display: none;
      position: absolute;
      z-index: 20;
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
    .xport-action-slot { min-width: 98px; min-height: 38px; display: flex; justify-content: flex-end; align-items: center; }
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
      width: 40px;
      height: 24px;
      min-height: 24px;
      border: 0;
      border-radius: 999px;
      background: #5f6368;
      padding: 0;
      vertical-align: middle;
    }
    .toggle::before {
      content: "";
      position: absolute;
      width: 20px;
      height: 20px;
      left: 2px;
      top: 2px;
      border-radius: 50%;
      background: #ffffff;
      box-shadow: 0 1px 3px rgba(0, 0, 0, .45);
      transition: transform .16s ease;
    }
    .toggle.on { background: var(--ha-primary); }
    .toggle.on::before { transform: translateX(16px); }
    .toggle span { display: none; }
    .toggle.readonly {
      cursor: default;
      pointer-events: none;
    }
    .toggle.readonly,
    .toggle.readonly:hover,
    .toggle.readonly:focus-visible {
      background: #5f6368;
      outline: none;
    }
    .toggle.readonly.on,
    .toggle.readonly.on:hover,
    .toggle.readonly.on:focus-visible {
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
    .bus-note { color: var(--ha-secondary); font-size: 13px; line-height: 1.4; flex: 1 1 360px; align-self: center; }
    .bus-action { min-width: 142px; }
    .modules { display: grid; grid-template-columns: 1fr; gap: 16px; margin-top: 18px; }
    .module-card { border: 1px solid var(--ha-card-border); border-radius: 12px; background: var(--ha-surface); padding: 18px; }
    .module-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 14px; margin-bottom: 18px; }
    .module-actions { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; justify-content: flex-end; }
    .status-pill { min-height: 30px; padding: 6px 10px; border-radius: 999px; border: 1px solid var(--ha-success-border); color: var(--ha-success-text); background: var(--ha-success-bg); font-size: 12px; font-weight: 800; letter-spacing: .04em; text-transform: uppercase; }
    .status-pill.offline { border-color: var(--ha-danger-border); color: var(--ha-danger-text); background: var(--ha-danger-bg); }
    .module-title { font-size: 17px; font-weight: 800; margin-bottom: 6px; }
    .module-meta { color: var(--ha-secondary); font-size: 13px; }
    .danger-button { border-color: #5f3434; color: #ffb4ab; background: transparent; min-height: 34px; padding: 6px 12px; }
    .danger-button:hover, .danger-button:focus-visible { border-color: #ff8a80; background: rgba(244, 67, 54, .14); color: #ffd4cf; outline: none; }
    .relay-grid { display: grid; grid-template-columns: repeat(8, minmax(120px, 1fr)); gap: 10px 14px; align-items: stretch; }
    .relay-row { display: grid; grid-template-columns: minmax(58px, 1fr) auto auto; align-items: center; gap: 10px; color: var(--ha-text); min-height: 42px; border: 1px solid var(--ha-row-border); border-radius: 8px; padding: 8px 10px; background: var(--ha-row); }
    .relay-row span { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .state-text { min-width: 30px; text-align: right; color: var(--ha-secondary); font-size: 12px; font-weight: 800; letter-spacing: .04em; }
    .state-text.on { color: var(--ha-primary); }
    .carrier-io-grid { display: grid; grid-template-columns: repeat(4, minmax(180px, 1fr)); gap: 16px; margin-top: 18px; }
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
    .buzzer-settings { display: grid; gap: 12px; }
    .buzzer-row { display: grid; grid-template-columns: minmax(100px, 150px) minmax(0, 1fr); align-items: center; gap: 14px; min-height: 40px; }
    .buzzer-row label,
    .buzzer-volume-label { color: var(--ha-text); font-size: 13px; font-weight: 700; }
    .buzzer-input-wrap { display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: center; gap: 8px; }
    .buzzer-input-wrap span { color: var(--ha-secondary); font-size: 12px; font-weight: 800; min-width: 26px; }
    .buzzer-field input { width: 100%; height: 38px; border: 1px solid var(--ha-row-border); border-radius: 8px; background: var(--ha-field); color: var(--ha-text); padding: 0 10px; font-weight: 600; }
    .buzzer-volume-control { min-width: 0; display: grid; grid-template-columns: minmax(0, 1fr) 44px; align-items: center; gap: 12px; }
    .buzzer-volume-control input { height: 38px; margin: 0; }
    .buzzer-volume-control strong { color: var(--ha-strong); text-align: right; font-size: 13px; }
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
    @media (max-width: 1300px) { .relay-grid { grid-template-columns: repeat(4, minmax(140px, 1fr)); } }
    @media (max-width: 1300px) { .overview-panel { grid-template-columns: 1fr; } .overview-identity { border-right: 0; border-bottom: 1px solid var(--ha-card-border); min-height: 240px; } }
    @media (max-width: 1100px) { .ports, .carrier-io-grid { grid-template-columns: repeat(2, minmax(220px, 1fr)); } .relay-grid { grid-template-columns: repeat(2, minmax(150px, 1fr)); } }
    @media (max-width: 900px) { .buzzer-panel { grid-template-columns: 1fr; } .buzzer-actions { grid-template-columns: repeat(2, minmax(120px, 1fr)); grid-template-rows: auto; } .buzzer-actions .buzzer-group-title { grid-column: 1 / -1; } }
    @media (max-width: 620px) { body { padding: 10px; } .app-toolbar { justify-content: stretch; } .theme-switcher { width: 100%; justify-content: space-between; } .theme-choice { flex: 1; } .overview-identity { min-height: 220px; padding: 22px 18px; } .overview-title h1 { font-size: 30px; } .overview-metrics { grid-template-columns: 1fr; } .overview-metric, .overview-metric:nth-child(2n), .overview-metric:nth-last-child(-n+3) { border-right: 0; border-bottom: 1px solid var(--ha-card-border); } .overview-metric:nth-of-type(4) { border-bottom: 0; } .xport-panel { padding: 18px 14px; border-radius: 12px; } .module-row { align-items: flex-start; } .transport { margin-top: 0; } .ports, .carrier-io-grid, .relay-grid { grid-template-columns: 1fr; } .module-head { flex-direction: column; } .module-actions { justify-content: flex-start; } .buzzer-row { grid-template-columns: 1fr; gap: 6px; } }
  </style>
</head>
<body>
  <main>
    <div class="app-toolbar">
      <div class="theme-switcher" role="group" aria-label="Theme">
        <span>Theme</span>
        <button class="theme-choice" type="button" data-theme-choice="auto" onclick="setTheme('auto')">Auto</button>
        <button class="theme-choice" type="button" data-theme-choice="light" onclick="setTheme('light')">Light</button>
        <button class="theme-choice" type="button" data-theme-choice="dark" onclick="setTheme('dark')">Dark</button>
      </div>
    </div>
    <section class="extension-panel overview-panel">
      <div class="overview-identity">
        <div class="overview-badge-row">
          <img class="overview-logo" src="logo.png" alt="IntellegyHUB">
          <div id="carrier-overview-status" class="overview-status-pill">Loading</div>
        </div>
        <div class="overview-title">
          <div class="eyebrow">Controller</div>
          <h1 id="carrier-identity-product">IHC-1400</h1>
          <div id="carrier-identity-model" class="subtitle">IntellegyHUB Controller</div>
        </div>
        <dl class="overview-facts">
          <dt>Serial</dt>
          <dd id="carrier-identity-serial">IH1400-00001234</dd>
          <dt>Hardware</dt>
          <dd id="carrier-identity-hardware">v1.4 Rev.A</dd>
          <dt>Software</dt>
          <dd id="carrier-identity-software">v0.5.120</dd>
        </dl>
        <div class="overview-divider"></div>
        <dl class="overview-health">
          <dt>System Health</dt>
          <dd class="overview-health-value">
            <span id="carrier-health-dot" class="overview-sync-dot"></span>
            <span id="carrier-health-state">Loading</span>
          </dd>
          <dt>Uptime</dt>
          <dd id="carrier-uptime">--</dd>
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
    <section class="extension-panel xport-panel">
      <div class="module-row">
        <div>
          <div class="eyebrow">Optional Module</div>
          <h1>X-PORT</h1>
          <div class="subtitle">Multi-function expansion ports X1-X4</div>
          <div id="xport-status" class="status">X-PORT: loading...</div>
        </div>
        <div class="transport">REST</div>
      </div>
      <p class="notice">When a port mode changes, the previous channel mode is safely shut down first. Selected modes are stored on the Hardware Host and restored after restart.</p>
      <div id="ports" class="ports"></div>
    </section>
    <section class="extension-panel">
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
          <span class="metric">Bus power: <strong id="extension-power-label">OFF</strong></span>
          <button id="extension-power-toggle" class="toggle" type="button" onclick="toggleExtensionPower()"><span>OFF</span></button>
        </div>
        <button class="bus-action" onclick="scanExtensions()">Scan modules</button>
        <div class="bus-note">Power control: MCP23017 / I2C-10 / 0x20 / GPB7</div>
      </div>
      <div id="modules" class="modules"></div>
    </section>
    <section class="extension-panel">
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
          <span class="metric">Bus power: <strong id="onewire-power-label">OFF</strong></span>
          <button id="onewire-power-toggle" class="toggle" type="button" onclick="toggleOneWirePower()"><span>OFF</span></button>
        </div>
        <button class="bus-action" onclick="scanOneWire()">Scan sensors</button>
        <button class="bus-action" onclick="refreshOneWire()">Refresh temperatures</button>
        <div class="bus-note">Power control: MCP23017 / I2C-10 / 0x20 / GPB6</div>
      </div>
      <div id="onewire-modules" class="modules"></div>
    </section>
    <section class="extension-panel">
      <div class="module-row">
        <div>
          <div class="eyebrow">Diagnostic</div>
          <h1>BUZZER</h1>
          <div class="subtitle">GPIO18 hardware PWM test</div>
          <div id="buzzer-status" class="status">BUZZER: loading...</div>
        </div>
        <div class="transport">GPIO18</div>
      </div>
      <div class="buzzer-panel">
        <div class="buzzer-group">
          <div class="buzzer-group-title">Parameters</div>
          <div class="buzzer-settings">
            <div class="buzzer-row">
              <label for="buzzer-frequency">Frequency</label>
              <div class="buzzer-input-wrap buzzer-field">
                <input id="buzzer-frequency" type="number" min="20" max="2800" value="2000">
                <span>Hz</span>
              </div>
            </div>
            <div class="buzzer-row">
              <label for="buzzer-duration">Duration</label>
              <div class="buzzer-input-wrap buzzer-field">
                <input id="buzzer-duration" type="number" min="10" max="5000" value="300">
                <span>ms</span>
              </div>
            </div>
            <div class="buzzer-row">
              <span class="buzzer-volume-label">Volume</span>
              <div class="buzzer-volume-control">
                <input id="buzzer-volume" type="range" min="0" max="100" step="1" value="50" oninput="updateBuzzerVolumeLabel()">
                <strong id="buzzer-volume-value">50%</strong>
              </div>
            </div>
          </div>
        </div>
        <div class="buzzer-group buzzer-actions">
          <div class="buzzer-group-title">Command</div>
          <button onclick="playBuzzer()">Play</button>
          <button onclick="stopBuzzer()">Stop</button>
        </div>
      </div>
      <div id="buzzer-detail" class="buzzer-status"></div>
    </section>
    <section class="extension-panel">
      <div class="module-row">
        <div>
          <div class="eyebrow">Carrier I/O</div>
          <h1>CARRIER</h1>
          <div class="subtitle">MCP23017 controlled carrier outputs</div>
          <div id="carrier-io-status" class="status">CARRIER: loading...</div>
        </div>
        <div class="transport">I2C-10</div>
      </div>
      <div id="carrier-io" class="carrier-io-grid"></div>
    </section>
    <section class="extension-panel">
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
    let latestAppInfo = { uptime_seconds: 0 };
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
      document.getElementById('xport-status').textContent = `X-PORT: ${payload.topology} - ${payload.availability}`;
      const ports = document.getElementById('ports');
      const seen = new Set();
      for (const channel of payload.channels) {
        const key = String(channel.channel);
        seen.add(key);
        let card = ports.querySelector(`[data-xport-channel="${key}"]`);
        if (!card) {
          card = renderXPortCard(channel, payload.modes);
          ports.appendChild(card);
        }
        updateXPortCard(card, channel, payload.modes);
      }
      ports.querySelectorAll('[data-xport-channel]').forEach((card) => {
        if (!seen.has(card.dataset.xportChannel)) { card.remove(); }
      });
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
    function updateXPortCard(card, channel, modes) {
      const desiredLabel = labels[channel.desired_mode] || channel.desired_mode;
      const trigger = card.querySelector('.mode-trigger');
      if (trigger && trigger.textContent !== desiredLabel) {
        trigger.textContent = desiredLabel;
      }
      card.querySelectorAll('.mode-option').forEach((option) => {
        option.classList.toggle('active', option.dataset.mode === channel.desired_mode);
      });
      const activeValue = card.querySelector('.active-mode-value');
      const confirmedLabel = labels[channel.confirmed_mode] || channel.confirmed_mode;
      if (activeValue && activeValue.textContent !== confirmedLabel) {
        activeValue.textContent = confirmedLabel;
        activeValue.title = confirmedLabel;
      }
      updateXPortAction(card, channel);
      const body = card.querySelector('.mode-body');
      const bodyKey = [
        channel.confirmed_mode,
        channel.error || '',
        String(channel.value ?? ''),
        String(channel.counter ?? '')
      ].join('|');
      if (body && body.dataset.bodyKey !== bodyKey) {
        body.replaceChildren(renderModeBody(channel));
        body.dataset.bodyKey = bodyKey;
      }
    }
    function updateXPortAction(card, channel) {
      const actionSlot = card.querySelector('.xport-action-slot');
      if (!actionSlot) { return; }
      const needsReset = channel.confirmed_mode === 'PulseCounterExternalVoltage' || channel.confirmed_mode === 'PulseCounterInternalPullUp';
      if (!needsReset) {
        actionSlot.replaceChildren();
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
      const status = document.getElementById('carrier-overview-status');
      const online = Boolean(carrier && carrier.available);
      status.textContent = online ? 'Online' : 'Offline';
      status.classList.toggle('offline', !online);
      const healthDot = document.getElementById('carrier-health-dot');
      healthDot.classList.toggle('online', online);
      healthDot.classList.toggle('offline', !online);
      document.getElementById('carrier-health-state').textContent = online ? 'Normal' : 'Offline';
      const identity = carrier && carrier.identity ? carrier.identity : {};
      document.getElementById('carrier-identity-product').textContent = identity.product || 'IHC-1400';
      document.getElementById('carrier-identity-model').textContent = identity.model || 'IntellegyHUB Controller';
      document.getElementById('carrier-identity-serial').textContent = identity.serial_number || 'IH1400-00001234';
      document.getElementById('carrier-identity-hardware').textContent = formatVersion(identity.hardware_revision || '1.4 Rev.A');
      document.getElementById('carrier-identity-software').textContent = formatVersion(identity.software_version || '0.5.120');
      document.getElementById('carrier-uptime').textContent = formatUptime(appInfo && appInfo.uptime_seconds);
      const monitoring = carrier && carrier.monitoring ? carrier.monitoring : {};
      const temperature = monitoring.temperature || {};
      const rails = {};
      for (const rail of monitoring.rails || []) {
        rails[rail.id] = rail;
      }
      const updatedAt = [
        paintMetric('board-temperature', temperature, 1, ' °C'),
        paintMetric('5v', rails['5v'], 2, ' V'),
        paintMetric('3v3', rails['3v3'], 2, ' V'),
        paintMetric('vin', rails.vin, 2, ' V')
      ].find(Boolean);
      document.getElementById('overview-updated').textContent = updatedAt ? `Updated ${updatedAt}` : 'Updated --';
    }
    function formatVersion(value) {
      const text = String(value || '').trim();
      return text && !text.toLowerCase().startsWith('v') ? `v${text}` : text;
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
        title: 'XMOD',
        outputs: ['xmod1_flash_enable', 'xmod1_reset', 'xmod2_flash_enable', 'xmod2_reset']
      },
      {
        title: 'USB',
        outputs: ['usb12_reset', 'usb3_reset', 'usb4_reset', 'usb_hub_reset']
      }
    ];
    const hostOutputOrder = ['ste', 'err', 'net', 'user_led'];
    const hostButtonOrder = ['fn1', 'fn2'];
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
        document.getElementById('carrier-io-status').textContent = 'CARRIER: Error';
        output.textContent = JSON.stringify(error, null, 2);
        output.classList.add('visible');
      }
    }
    function paintCarrierIO(carrier, hostOutputs = {}, hostButtons = {}) {
      const online = Boolean(carrier && carrier.available);
      document.getElementById('carrier-io-status').textContent = online ? 'CARRIER: online' : 'CARRIER: offline';
      const byId = carrierOutputsById(carrier);
      const root = document.getElementById('carrier-io');
      root.innerHTML = '';
      const hostCard = document.createElement('article');
      hostCard.className = 'carrier-io-card';
      const hostTitle = document.createElement('div');
      hostTitle.className = 'carrier-io-title';
      hostTitle.textContent = 'Host GPIO';
      hostCard.appendChild(hostTitle);
      for (const outputId of hostOutputOrder) {
        const item = hostOutputs[outputId] || { id: outputId, name: outputId, on: false };
        hostCard.appendChild(hostOutputRow(item));
      }
      for (const buttonId of hostButtonOrder) {
        const item = hostButtons[buttonId] || { id: buttonId, name: buttonId.toUpperCase(), pressed: false };
        hostCard.appendChild(hostButtonRow(item));
      }
      root.appendChild(hostCard);
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
          const label = document.createElement('span');
          label.textContent = item.name;
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
    function hostOutputRow(item) {
      const row = document.createElement('div');
      row.className = 'carrier-io-row';
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
    async function setHostOutput(outputId, on) {
      const output = document.getElementById('output');
      output.classList.remove('visible');
      try {
        await requestJson(`api/v1/outputs/${outputId}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ on })
        });
        await renderCarrierIO();
      } catch (error) {
        output.textContent = JSON.stringify(error, null, 2);
        output.classList.add('visible');
      }
    }
    async function setCarrierOutput(outputId, on) {
      const output = document.getElementById('output');
      output.classList.remove('visible');
      try {
        await requestJson(`api/v1/carrier/outputs/${outputId}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ on })
        });
        await renderCarrierIO();
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
      document.getElementById('extension-power-label').textContent = powerOn ? 'ON' : 'OFF';
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
        const title = document.createElement('div');
        title.className = 'module-title';
        title.textContent = module.name;
        const meta = document.createElement('div');
        meta.className = 'module-meta';
        const isInputModule = module.kind === 'digital_input';
        const isRelayModule = module.kind === 'relay_output';
        meta.textContent = `X-BUS · I2C-${module.bus} · Address ${module.address} · ${module.chip} · 8 relays`;
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
          indicator.innerHTML = `<span>${active ? 'ACTIVE' : 'INACTIVE'}</span>`;
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
      const payload = await requestJson(`api/v1/extensions/modules/${moduleId}/relays/${channel}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ on })
      });
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
      paintExtensions({ ...extensionState, modules });
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
      document.getElementById('onewire-power-label').textContent = powerOn ? 'ON' : 'OFF';
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
        meta.textContent = `Bus I2C-${bridge.bus} В· Address ${bridge.address} В· ${bridge.chip}${bridge.available ? '' : ' В· unavailable'}`;
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
        const value = sensor.temperature_c === null || sensor.temperature_c === undefined ? 'Unavailable' : `${Number(sensor.temperature_c).toFixed(3)} В°C`;
        meta.textContent = `Bridge ${sensor.address} В· ROM ${sensor.rom} В· ${value}${sensor.available ? '' : ' В· unavailable'}`;
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
    function connectEvents() {
      const url = websocketUrl('ws');
      const socket = new WebSocket(url);
      socket.onmessage = (event) => {
        const message = JSON.parse(event.data);
        if (message.type === 'state') {
          applyTheme(message.ui && message.ui.theme ? message.ui.theme : selectedTheme);
          paintCarrier(message.carrier, message.app);
          paintCarrierIO(message.carrier, message.outputs || {}, message.buttons || {});
          paintXPort(message.xport);
          paintExtensions(message.extensions);
          paintOneWire(message.onewire);
        } else if (message.type === 'carrier_changed') {
          paintCarrier(message.carrier);
          renderCarrierIO();
        } else if (message.type === 'carrier_output_changed') {
          paintCarrier(message.carrier);
          renderCarrierIO();
        } else if (message.type === 'output_changed' || message.type === 'button_changed') {
          renderCarrierIO();
        } else if (message.type === 'xport_changed') {
          paintXPort(message.xport);
        } else if (message.type === 'xport_channel_changed') {
          renderXPort();
        } else if (message.type === 'extensions_changed') {
          paintExtensions(message.extensions);
        } else if (message.type === 'extension_module_changed') {
          applyExtensionModule(message.module);
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
        } else if (message.type === 'ui_changed') {
          applyTheme(message.ui && message.ui.theme ? message.ui.theme : 'auto');
        }
      };
      socket.onopen = () => console.info('IntellegyHUB WebSocket connected', url.href);
      socket.onerror = (event) => console.warn('IntellegyHUB WebSocket error', url.href, event);
      socket.onclose = () => {
        console.warn('IntellegyHUB WebSocket closed; reconnecting', url.href);
        window.setTimeout(connectEvents, 2000);
      };
    }
    function buzzerPayload() {
      const volume = Number(document.getElementById('buzzer-volume').value);
      return {
        frequency: Number(document.getElementById('buzzer-frequency').value),
        duration_ms: Number(document.getElementById('buzzer-duration').value),
        volume_percent: volume
      };
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
    function updateBuzzerVolumeLabel() {
      const value = Number(document.getElementById('buzzer-volume').value);
      document.getElementById('buzzer-volume-value').textContent = `${value}%`;
    }
    async function renderBuzzerStatus() {
      try {
        const payload = await requestJson('api/v1/buzzer/status');
        paintBuzzer(payload);
        document.getElementById('buzzer-detail').textContent = `pigpio: ${payload.pigpio.connected ? 'connected' : 'offline'} · pwmchip: ${payload.pwm.available ? 'available' : 'missing'} · gpio18: ${payload.gpio.available ? 'available' : 'missing'}`;
      } catch (error) {
        document.getElementById('buzzer-status').textContent = 'BUZZER: Error';
        document.getElementById('buzzer-detail').textContent = JSON.stringify(error);
      }
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
      document.getElementById('buzzer-volume').value = volume;
      updateBuzzerVolumeLabel();
      const state = payload.active && payload.active.running ? `running ${payload.active.backend}` : 'stopped';
      document.getElementById('buzzer-status').textContent = `BUZZER: ${state}`;
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
        detail.textContent = `duration: ${payload.duration_ms} ms · pigpio: ${payload.pigpio.connected ? 'connected' : 'offline'}`;
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
        document.getElementById('buzzer-detail').textContent = `pigpio: ${payload.pigpio.connected ? 'connected' : 'offline'} · pwmchip: ${payload.pwm.available ? 'available' : 'missing'}`;
      } catch (error) {
        document.getElementById('buzzer-status').textContent = 'BUZZER: stop failed';
        document.getElementById('buzzer-detail').textContent = JSON.stringify(error);
      }
    }
    document.getElementById('buzzer-frequency').addEventListener('change', saveBuzzerSettings);
    document.getElementById('buzzer-duration').addEventListener('change', saveBuzzerSettings);
    document.getElementById('buzzer-volume').addEventListener('change', saveBuzzerSettings);
    document.addEventListener('click', (event) => {
      if (!event.target.closest('.mode-select')) {
        document.querySelectorAll('.mode-select.open').forEach((item) => item.classList.remove('open'));
      }
    });
    applyTheme('auto');
    loadTheme();
    renderXPort();
    renderCarrierIO();
    renderExtensions();
    renderOneWire();
    renderBuzzerStatus();
    connectEvents();
  </script>
</body>
</html>
"""

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> FileResponse:
        return FileResponse(Path(__file__).resolve().parents[1] / "icon.png")

    @app.get("/logo.png", include_in_schema=False)
    async def logo() -> FileResponse:
        return FileResponse(Path(__file__).resolve().parents[1] / "logo.png")

    @app.get("/api/v1/state")
    async def get_state() -> dict:
        return await runtime_or_503().async_snapshot()

    @app.get("/api/v1/ui/settings")
    async def get_ui_settings() -> dict:
        return runtime_or_503().ui_settings.snapshot()

    @app.put("/api/v1/ui/theme")
    async def put_ui_theme(payload: UiThemePayload) -> dict:
        return await runtime_or_503().set_ui_theme(payload.theme)

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
