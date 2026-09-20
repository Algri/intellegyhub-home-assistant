DOMAIN = "intellegyhub"
NAME = "IntellegyHUB"
DEFAULT_URL = "http://local-intellegyhub:8098"

CONF_URL = "url"
DEVICE_IDENTIFIER = "mainboard"

UNIQUE_ID_LED = "intellegyhub_led"
UNIQUE_ID_BUTTON = "intellegyhub_button"
OUTPUTS = {
    "user_led": {"name": "USR", "unique_id": UNIQUE_ID_LED},
    "ste": {"name": "STE", "unique_id": "intellegyhub_output_ste"},
    "err": {"name": "ERR", "unique_id": "intellegyhub_output_err"},
    "net": {"name": "NET", "unique_id": "intellegyhub_output_net"},
}

XPORT_MODES = [
    "Disabled",
    "DigitalInputExternalVoltage",
    "DigitalInputInternalPullUp",
    "DigitalOutput",
    "PwmOutput",
    "AnalogInput",
    "PulseCounterExternalVoltage",
    "PulseCounterInternalPullUp",
]

XPORT_MODE_DO = "DigitalOutput"
XPORT_MODE_PWM = "PwmOutput"
XPORT_MODE_AI = "AnalogInput"
XPORT_MODE_DI = {"DigitalInputExternalVoltage", "DigitalInputInternalPullUp"}
XPORT_MODE_COUNTER = {"PulseCounterExternalVoltage", "PulseCounterInternalPullUp"}
