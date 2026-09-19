DOMAIN = "intellegyhub"
NAME = "IntellegyHUB"
DEFAULT_URL = "http://local-intellegyhub:8098"

CONF_URL = "url"
DEVICE_IDENTIFIER = "mainboard"

UNIQUE_ID_LED = "intellegyhub_led"
UNIQUE_ID_BUTTON = "intellegyhub_button"

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
