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

When installed from a Home Assistant Store repository, the add-on includes a
bundled copy of the `intellegyhub` custom integration. On startup it compares the
bundled integration version with `/config/custom_components/intellegyhub` and
copies the files only when the installed integration is missing or older.

After the add-on installs or updates the integration, restart Home Assistant
Core so the integration appears under `Settings -> Devices & services -> Add
integration`.

## Dashboard Cards

Home Assistant entity names are device-scoped by design, so an automatically
added tile can show names such as `Hardware Host Buzzer Volume`. For dashboard
cards that should use a compact label, set the Lovelace card `name` explicitly.

Recommended buzzer volume tile:

```yaml
type: tile
entity: number.intellegyhub_buzzer_volume
name: Buzzer Volume
icon: mdi:volume-high
features:
  - type: numeric-input
    style: slider
features_position: inline
grid_options:
  columns: 12
  rows: 1
```

Recommended compact buzzer setting tiles:

```yaml
type: grid
columns: 2
square: false
cards:
  - type: tile
    entity: number.intellegyhub_buzzer_duration
    name: Buzzer Duration
    icon: mdi:timer-outline
  - type: tile
    entity: number.intellegyhub_buzzer_frequency
    name: Buzzer Frequency
    icon: mdi:sine-wave
  - type: tile
    entity: number.intellegyhub_buzzer_volume
    name: Buzzer Volume
    icon: mdi:volume-high
    features:
      - type: numeric-input
        style: slider
    features_position: inline
```

## Version Bump And Release Archive Checklist

Before building release archives, bump the same version in every packaged
surface. Keep these files in sync:

```text
addon/config.yaml
addon/Dockerfile
custom_components/intellegyhub/manifest.json
addon/app/main.py
addon/app/carrier.py
addon/CHANGELOG.md
```

Use the changelog entry to describe the user-visible change. Example:

```text
## 0.5.107

- Align X-Port mode controls with the shared row style, including counter reset placement, read-only DI indicators, and compact PWM slider layout.
```

Run the local checks before packaging:

```powershell
python -m compileall addon custom_components tests
python -m pytest tests/test_addon_api.py
```

Build all release archives from the repository root:

```powershell
python scripts/build_haos_deploy.py
```

The build script validates that the add-on config, Docker label, integration
manifest, app version, and changelog all use the same version. It also rebuilds:

```text
dist/intellegyhub_haos_deploy.zip
dist/intellegyhub_ha_repository.zip
dist/intellegyhub_ha_integration.zip
```

Use a commit title that starts with the release version and names the change,
for example:

```text
0.5.107: align X-Port mode controls
```

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
