# IntellegyHUB Add-on

This Home Assistant App/Add-on runs the IntellegyHUB hardware runtime.
The current CM4 diagnostic build also maps `/dev/i2c-1` and `/dev/i2c-10`.

Install it from the local repository folder that contains root `repository.yaml` and this `addon/` directory.

Default options:

```yaml
led_gpio: 22
led_active_low: false
button_gpio: 26
button_active_low: true
button_bias: pull_up
button_debounce_ms: 50
startup_buzzer_enabled: false
startup_buzzer_frequency: 2000
startup_buzzer_duration_ms: 200
onewire_bridge1_poll_interval_seconds: 30
onewire_bridge2_poll_interval_seconds: 30
```

When `startup_buzzer_enabled` is true, the add-on emits one short GPIO18
hardware PWM beep after the runtime starts successfully.

The 1-Wire bridge polling toggles in the Ingress UI enable or disable periodic
DS18B20 temperature polling per DS2482S-100 bridge. The interval for each bridge
is configured with `onewire_bridge1_poll_interval_seconds` and
`onewire_bridge2_poll_interval_seconds`.

The service listens inside the Home Assistant internal app network on port `8098`.
For a local repository install, Home Assistant Core should reach it at:

```text
http://local-intellegyhub:8098
```

Mock mode is for local tests only and is enabled with `INTELLEGY_GPIO_MOCK=1`.

For the confirmed Studio Code Server install flow, deploy the add-on to:

```text
~/addons/intellegyhub
```

and deploy the Home Assistant custom integration to:

```text
~/config/custom_components/intellegyhub
```

After copying or extracting files, refresh the Add-on Store with the three-dot
menu `Check for updates`, install `IntellegyHUB`, configure the options, and
start it.

## CM4 I2C Notes

For Home Assistant OS on Raspberry Pi CM4, enable I2C in the boot partition `config.txt`:

```ini
arm_64bit=1
dtparam=i2c_arm=on
dtparam=spi=off

[cm4]
otg_mode=1

[all]
enable_uart=1
dtoverlay=uart3
dtoverlay=uart5
dtparam=i2c1=on
dtparam=i2c_vc=on
```

Also create this boot-partition file:

```text
CONFIG/modules/rpi-i2c.conf
```

with:

```text
i2c-dev
```

On Windows, write that file as ASCII/LF, not UTF-16:

```powershell
New-Item -ItemType Directory -Force -Path E:\CONFIG\modules | Out-Null
[System.IO.File]::WriteAllText("E:\CONFIG\modules\rpi-i2c.conf", "i2c-dev`n", [System.Text.Encoding]::ASCII)
```

Then boot HAOS, import the boot `CONFIG` folder, and reboot:

```sh
ha os import
ha host reboot
```

For HAOS host root SSH, put the Windows public key into:

```text
E:\CONFIG\authorized_keys
```

then run the same `ha os import` flow. Connect to the host on port `22222`,
which is separate from the Terminal & SSH add-on.

The tested CM4 setup exposes the main/user bus as `/dev/i2c-1` and the VC/left bus as `/dev/i2c-10`.
The add-on maps those buses explicitly:

```yaml
devices:
  - /dev/gpiochip0
  - /dev/i2c-1
  - /dev/i2c-10
```

Use the add-on Ingress UI buttons `Devices`, `Scan I2C-1`, and `Scan I2C-10` to verify what the add-on container can see and open.
