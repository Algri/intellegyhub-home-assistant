from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


DEFAULT_OPTIONS_PATH = Path("/data/options.json")
VALID_BIASES = {"pull_up", "pull_down", "disabled"}
FIXED_LED_GPIO = 22
FIXED_BUTTON_GPIO = 26
FIXED_BUTTON_BIAS = "pull_up"
FIXED_BUTTON_DEBOUNCE_MS = 50
FIXED_OUTPUT_GPIOS = {
    "ste_gpio": 19,
    "err_gpio": 20,
    "net_gpio": 21,
}
FIXED_INPUT_GPIOS = {
    "fn1_gpio": 27,
}


@dataclass(frozen=True)
class AppConfig:
    led_gpio: int = 22
    led_active_low: bool = False
    button_gpio: int = 26
    button_active_low: bool = True
    button_bias: str = "pull_up"
    button_debounce_ms: int = 50
    startup_buzzer_enabled: bool = False
    startup_buzzer_frequency: int = 2000
    startup_buzzer_duration_ms: int = 200
    shutdown_buzzer_enabled: bool = True
    carrier_monitoring_poll_interval_seconds: int = 30
    onewire_bridge1_poll_interval_seconds: int = 30
    onewire_bridge2_poll_interval_seconds: int = 30
    power_button_shutdown_enabled: bool = True
    power_button_shutdown_hold_seconds: float = 1.0
    chip_path: str = "/dev/gpiochip0"
    mock: bool = False


def _read_options(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def _bool_option(options: dict, name: str, default: bool) -> bool:
    value = options.get(name, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return bool(value)


def _option(options: dict, name: str, default, *legacy_names: str):
    if name in options:
        return options[name]
    for legacy_name in legacy_names:
        if legacy_name in options:
            return options[legacy_name]
    return default


def _group(options: dict, name: str) -> dict:
    value = options.get(name, {})
    return value if isinstance(value, dict) else {}


def load_config(path: Path | None = None) -> AppConfig:
    inline_options = os.environ.get("INTELLEGY_ADDON_OPTIONS")
    options = json.loads(inline_options) if inline_options else _read_options(path or DEFAULT_OPTIONS_PATH)
    if not isinstance(options, dict):
        raise ValueError("INTELLEGY_ADDON_OPTIONS must contain a JSON object")
    mock = (
        os.environ.get("INTELLEGY_GPIO_MOCK", "").lower() in {"1", "true", "yes"}
        or os.environ.get("INTELLEGY_MOCK_GPIO", "").lower() in {"1", "true", "yes"}
    )
    diagnostics = _group(options, "diagnostics")
    onewire = _group(options, "onewire")
    power_button = _group(options, "power_button")
    buzzer = _group(options, "buzzer")
    config = AppConfig(
        led_gpio=FIXED_LED_GPIO,
        led_active_low=False,
        button_gpio=FIXED_BUTTON_GPIO,
        button_active_low=True,
        button_bias=FIXED_BUTTON_BIAS,
        button_debounce_ms=FIXED_BUTTON_DEBOUNCE_MS,
        startup_buzzer_enabled=_bool_option(buzzer, "startup_enabled", _bool_option(options, "startup_buzzer_enabled", False)),
        startup_buzzer_frequency=int(_option(buzzer, "frequency", options.get("startup_buzzer_frequency", 2000))),
        startup_buzzer_duration_ms=int(_option(buzzer, "duration_ms", options.get("startup_buzzer_duration_ms", 200))),
        shutdown_buzzer_enabled=_bool_option(buzzer, "shutdown_enabled", _bool_option(options, "shutdown_buzzer_enabled", True)),
        carrier_monitoring_poll_interval_seconds=int(
            _option(diagnostics, "carrier_monitoring_poll_interval_seconds", options.get("carrier_monitoring_poll_interval_seconds", 30))
        ),
        onewire_bridge1_poll_interval_seconds=int(
            _option(
                onewire,
                "bus1_poll_interval_seconds",
                _option(options, "onewire_bus1_poll_interval_seconds", 30, "onewire_bridge1_poll_interval_seconds"),
            )
        ),
        onewire_bridge2_poll_interval_seconds=int(
            _option(
                onewire,
                "bus2_poll_interval_seconds",
                _option(options, "onewire_bus2_poll_interval_seconds", 30, "onewire_bridge2_poll_interval_seconds"),
            )
        ),
        power_button_shutdown_enabled=_bool_option(
            power_button,
            "shutdown_enabled",
            _bool_option(options, "power_button_shutdown_enabled", True),
        ),
        power_button_shutdown_hold_seconds=float(
            _option(power_button, "shutdown_hold_seconds", options.get("power_button_shutdown_hold_seconds", 1.0))
        ),
        mock=mock,
    )
    validate_config(config)
    return config


def validate_config(config: AppConfig) -> None:
    gpio_values = {
        "led_gpio": config.led_gpio,
        "button_gpio": config.button_gpio,
        **FIXED_OUTPUT_GPIOS,
        **FIXED_INPUT_GPIOS,
    }
    for name, value in gpio_values.items():
        if not 0 <= value <= 53:
            raise ValueError(f"{name} must be between 0 and 53")
    seen: dict[int, str] = {}
    for name, value in gpio_values.items():
        if value in seen:
            raise ValueError(f"{name} and {seen[value]} must be different")
        seen[value] = name
    if config.button_bias not in VALID_BIASES:
        raise ValueError(f"button_bias must be one of {sorted(VALID_BIASES)}")
    if not 0 <= config.button_debounce_ms <= 1000:
        raise ValueError("button_debounce_ms must be between 0 and 1000")
    if not 20 <= config.startup_buzzer_frequency <= 2800:
        raise ValueError("startup_buzzer_frequency must be between 20 and 2800")
    if not 10 <= config.startup_buzzer_duration_ms <= 5000:
        raise ValueError("startup_buzzer_duration_ms must be between 10 and 5000")
    if not 1 <= config.carrier_monitoring_poll_interval_seconds <= 3600:
        raise ValueError("carrier_monitoring_poll_interval_seconds must be between 1 and 3600")
    if not 1 <= config.onewire_bridge1_poll_interval_seconds <= 3600:
        raise ValueError("onewire_bus1_poll_interval_seconds must be between 1 and 3600")
    if not 1 <= config.onewire_bridge2_poll_interval_seconds <= 3600:
        raise ValueError("onewire_bus2_poll_interval_seconds must be between 1 and 3600")
    if not 0.1 <= config.power_button_shutdown_hold_seconds <= 1.5:
        raise ValueError("power_button_shutdown_hold_seconds must be between 0.1 and 1.5")
