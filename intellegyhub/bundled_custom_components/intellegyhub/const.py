DOMAIN = "intellegyhub"
NAME = "IntellegyHUB"
DEFAULT_URL = "http://local-intellegyhub:8098"

CONF_URL = "url"
DEVICE_IDENTIFIER = "mainboard"

UNIQUE_ID_LED = "intellegyhub_led"
UNIQUE_ID_BUTTON = "intellegyhub_button"
OUTPUTS = {
    "ste": {"name": "STE LED", "unique_id": "intellegyhub_output_ste"},
    "err": {"name": "ERR LED", "unique_id": "intellegyhub_output_err"},
    "net": {"name": "NET LED", "unique_id": "intellegyhub_output_net"},
    "user_led": {"name": "USR LED", "unique_id": UNIQUE_ID_LED},
}
BUTTONS = {
    "fn1": {"name": "FN1", "unique_id": "intellegyhub_button_fn1"},
    "fn2": {"name": "FN2", "unique_id": UNIQUE_ID_BUTTON},
}

CARRIER_OUTPUTS = {
    "rs485_ch1_termination": {
        "name": "RS-485 CH1 120Ω Termination",
        "unique_id": "intellegyhub_carrier_rs485_ch1_termination",
    },
    "rs485_ch2_termination": {
        "name": "RS-485 CH2 120Ω Termination",
        "unique_id": "intellegyhub_carrier_rs485_ch2_termination",
    },
    "xmod1_flash_enable": {
        "name": "XMOD1 Flash / Enable",
        "unique_id": "intellegyhub_carrier_xmod1_flash_enable",
    },
    "xmod1_reset": {
        "name": "XMOD1 Reset",
        "unique_id": "intellegyhub_carrier_xmod1_reset",
    },
    "xmod2_flash_enable": {
        "name": "XMOD2 Flash / Enable",
        "unique_id": "intellegyhub_carrier_xmod2_flash_enable",
    },
    "xmod2_reset": {
        "name": "XMOD2 Reset",
        "unique_id": "intellegyhub_carrier_xmod2_reset",
    },
    "usb12_reset": {
        "name": "USB1/2 Reset",
        "unique_id": "intellegyhub_carrier_usb12_reset",
    },
    "usb3_reset": {
        "name": "USB3 Reset",
        "unique_id": "intellegyhub_carrier_usb3_reset",
    },
    "usb4_reset": {
        "name": "USB4 Reset",
        "unique_id": "intellegyhub_carrier_usb4_reset",
    },
    "usb_hub_reset": {
        "name": "USB Hub Reset",
        "unique_id": "intellegyhub_carrier_usb_hub_reset",
    },
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

XPORT_MODE_LABELS = {
    "Disabled": "Off",
    "AnalogInput": "AI",
    "DigitalOutput": "DO",
    "PwmOutput": "PWM",
    "DigitalInputExternalVoltage": "DI PNP/+24V",
    "DigitalInputInternalPullUp": "DI NPN/COM",
    "PulseCounterExternalVoltage": "CNT PNP/+24V",
    "PulseCounterInternalPullUp": "CNT NPN/COM",
}
XPORT_MODE_LABEL_OPTIONS = [
    "Off",
    "AI",
    "DO",
    "PWM",
    "DI PNP/+24V",
    "DI NPN/COM",
    "CNT PNP/+24V",
    "CNT NPN/COM",
]
XPORT_MODE_VALUES_BY_LABEL = {label: mode for mode, label in XPORT_MODE_LABELS.items()}

XPORT_MODE_DO = "DigitalOutput"
XPORT_MODE_PWM = "PwmOutput"
XPORT_MODE_AI = "AnalogInput"
XPORT_MODE_DI = {"DigitalInputExternalVoltage", "DigitalInputInternalPullUp"}
XPORT_MODE_COUNTER = {"PulseCounterExternalVoltage", "PulseCounterInternalPullUp"}
