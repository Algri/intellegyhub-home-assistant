# IntellegyHub Home Assistant GPIO MVP

This repository contains two parts:

- `addon/` - the Home Assistant App/Add-on that owns `/dev/gpiochip0` and maps the CM4 I2C buses used for diagnostics.
- `custom_components/intellegyhub/` - the Home Assistant integration that creates native IntellegyHUB entities.

## What You Can Check Locally On This PC

This proves the Python API works in mock mode. It does not prove real GPIO.

1. Install dev dependencies:

```powershell
python -m pip install -r requirements-dev.txt
```

2. Run automated checks:

```powershell
python -m compileall addon custom_components tests scripts
python -m pytest
python scripts/verify_mvp_repo.py
```

Expected result:

```text
9 passed
MVP repository static verification passed.
```

If Windows denies access to the default pytest temp folder, run pytest with a temp directory inside this repo:

```powershell
New-Item -ItemType Directory -Force .tmp
$env:TMP="$PWD\.tmp"
$env:TEMP="$PWD\.tmp"
python -m pytest
```

3. Run the backend locally in mock mode:

```powershell
python -m pip install -r requirements-local-ui.txt
$env:INTELLEGY_GPIO_MOCK="1"
$env:INTELLEGY_ADDON_OPTIONS='{"led_gpio":22,"led_active_low":false,"button_gpio":26,"button_active_low":true,"button_bias":"pull_up","button_debounce_ms":50}'
python -m uvicorn addon.app.main:app --host 127.0.0.1 --port 8098 --reload
```

On Windows, do not install `addon/requirements.txt` just to preview the local UI. That file contains the Linux-only `gpiod` dependency used inside the HAOS container. Use `requirements-local-ui.txt` for the mock UI/API preview.

Open:

```text
http://127.0.0.1:8098/
```

4. In another PowerShell window, check the REST API:

```powershell
Invoke-RestMethod http://127.0.0.1:8098/health
Invoke-RestMethod http://127.0.0.1:8098/api/v1/state
Invoke-RestMethod -Method Put -Uri http://127.0.0.1:8098/api/v1/led -ContentType "application/json" -Body '{"on":true}'
Invoke-RestMethod http://127.0.0.1:8098/api/v1/state
```

Expected behavior:

- `/health` returns `status: ok`.
- initial LED state is `on: false`.
- after the PUT command, LED state becomes `on: true`.

## What Requires Home Assistant OS

You need HAOS/Supervisor on the target device to test:

- add-on install/build;
- `/dev/gpiochip0` mapping;
- native Home Assistant entities;
- physical LED/button behavior;
- unavailable/reconnect behavior.

## HAOS Boot Config For CM4 I2C

Future agents should also read `docs/18-HAOS-CM4-I2C-BRINGUP.md`; it records the exact HAOS symptoms, wrong turns, and working solution from real CM4 bring-up.

On Home Assistant OS, enabling I2C on Raspberry Pi CM4 needs both firmware config and the Linux `i2c-dev` module.
Edit the boot partition `config.txt`, not `/config/configuration.yaml`.

For the tested CM4 setup, the relevant `config.txt` lines are:

```ini
arm_64bit=1
dtparam=i2c_arm=on

[cm4]
otg_mode=1

[all]
enable_uart=1
dtoverlay=uart3
dtoverlay=uart5
dtparam=i2c1=on
dtparam=i2c_vc=on
dtparam=spi=off
gpio=16=ip,np
gpio=23=ip,np
dtoverlay=gpio-shutdown,gpio_pin=17,active_low=1,gpio_pull=up,debounce=1000
dtoverlay=pwm,pin=18,func=2
dtparam=ant2
dtoverlay=i2c-rtc,pcf85063a,i2c_csi_dsi
```

HAOS also needs this file on the boot partition:

```text
CONFIG/modules/rpi-i2c.conf
```

with this content:

```text
i2c-dev
```

On Windows, write that file as ASCII or UTF-8 without BOM. PowerShell's
default `Set-Content` can write UTF-16, which HAOS will not import as the
plain module file expected here.

This repository includes a Windows helper script for the tested CM4 setup:

```powershell
cd C:\Users\Algri\Documents\iot\IntellegyHub_HA_GPIO
.\scripts\prepare_haos_boot_windows.ps1 -BootDrive E:
```

Preview the generated files without writing to the mounted boot partition:

```powershell
cd C:\Users\Algri\Documents\iot\IntellegyHub_HA_GPIO
.\scripts\prepare_haos_boot_windows.ps1 -BootDrive E: -Preview
```

It updates the mounted HAOS boot partition `config.txt`, including the
I2C/UART lines and the GPIO16/GPIO23 no-pull fault inputs, creates
`E:\CONFIG\modules\rpi-i2c.conf`. To also write `E:\CONFIG\authorized_keys`
from `$env:USERPROFILE\.ssh\id_ed25519.pub`, pass `-SshKey`:

```powershell
.\scripts\prepare_haos_boot_windows.ps1 -BootDrive E: -SshKey
```

The script creates a timestamped `config.txt.bak.*` backup before editing.

On macOS and Linux, mount the HAOS boot partition and pass its mountpoint:

```sh
cd /path/to/IntellegyHub_HA_GPIO
chmod +x scripts/prepare_haos_boot_unix.sh
scripts/prepare_haos_boot_unix.sh --boot /mnt/boot --preview
scripts/prepare_haos_boot_unix.sh --boot /mnt/boot
```

On macOS the mountpoint is commonly under `/Volumes`, for example:

```sh
cd /path/to/IntellegyHub_HA_GPIO
chmod +x scripts/prepare_haos_boot_unix.sh
scripts/prepare_haos_boot_unix.sh --boot /Volumes/hassos-boot
```

To also write host SSH access from `~/.ssh/id_ed25519.pub`:

```sh
scripts/prepare_haos_boot_unix.sh --boot /mnt/boot --ssh-key
```

```powershell
New-Item -ItemType Directory -Force -Path E:\CONFIG\modules | Out-Null
[System.IO.File]::WriteAllText("E:\CONFIG\modules\rpi-i2c.conf", "i2c-dev`n", [System.Text.Encoding]::ASCII)
Format-Hex E:\CONFIG\modules\rpi-i2c.conf
```

After changing boot files, boot HAOS and import the boot `CONFIG` folder:

```sh
ha os import
ha host reboot
hwclock -w
hwclock -r
cat /sys/class/rtc/rtc0/hctosys
```

If the Supervisor is not reachable immediately after import, use host SSH or a
full power cycle; the `ha` CLI depends on the Supervisor API.

## HAOS Host SSH For Development

Root SSH into the HAOS host is separate from the Terminal & SSH add-on. It uses
port `22222` and imports keys from the boot partition.

On the Windows boot partition, create:

```text
E:\CONFIG\authorized_keys
```

The file must contain the public key from:

```text
C:\Users\<USER>\.ssh\id_ed25519.pub
```

Write it as one ASCII/LF line:

```powershell
$key = (Get-Content -Raw "$env:USERPROFILE\.ssh\id_ed25519.pub").Trim()
New-Item -ItemType Directory -Force -Path E:\CONFIG | Out-Null
[System.IO.File]::WriteAllText("E:\CONFIG\authorized_keys", "$key`n", [System.Text.Encoding]::ASCII)
```

Boot HAOS and import:

```sh
ha os import
ha host reboot
```

Connect from Windows:

```powershell
ssh -p 22222 root@192.168.0.25
```

Check from the HAOS SSH shell:

```sh
ls -la /dev/i2c*
lsmod | grep -i i2c
dmesg | grep -i i2c
```

On the tested CM4 hardware, the useful buses are:

```text
/dev/i2c-1   # ARM/user I2C bus; may be empty depending on wiring
/dev/i2c-10  # VC/left bus on this CM4 setup
```

Do not assume that "I2C0" appears as `/dev/i2c-0`; with the CM4 device tree it can appear as `/dev/i2c-10`.

## Controller Identity EEPROM

The controller identity EEPROM is a 24LC32 on `/dev/i2c-10` at address `0x50`.
Normal add-on/runtime code must treat it as read-only. Factory writes are done
with the standalone script:

```text
scripts/write_controller_identity_eeprom.py
```

Copy it to HAOS, for example:

```bash
cp scripts/write_controller_identity_eeprom.py /mnt/data/write_controller_identity_eeprom.py
```

Preview without writing:

```bash
python3 /mnt/data/write_controller_identity_eeprom.py write --dry-run
```

Write the default controller identity:

```bash
python3 /mnt/data/write_controller_identity_eeprom.py write
```

Read and verify after writing:

```bash
python3 /mnt/data/write_controller_identity_eeprom.py read
python3 /mnt/data/write_controller_identity_eeprom.py read --json
python3 /mnt/data/write_controller_identity_eeprom.py verify
```

Expected verify result:

```text
VERIFY OK
```

Full EEPROM format, safety policy, status meanings, and factory checklist are in
[`docs/21-EEPROM-IDENTITY.md`](docs/21-EEPROM-IDENTITY.md).

## Deploy The Add-on To HAOS

The confirmed local path is using the Home Assistant Studio Code Server add-on.
In that terminal, `~` contains these HAOS-visible folders:

```text
addon_configs  addons  backup  config  homeassistant  media  share  ssl
```

The local add-on must be deployed to:

```text
~/addons/intellegyhub
```

The custom integration must be deployed to:

```text
~/config/custom_components/intellegyhub
```

The generated ZIP already uses this layout:

```text
addons/intellegyhub/config.yaml
config/custom_components/intellegyhub/manifest.json
```

Do not deploy this project to these stale/wrong paths:

```text
~/config/addons/intellegyhub
~/homeassistant/addons/intellegyhub
~/addons/local/intellegyhub
~/local_apps/intellegyhub
```

After extracting or copying files, open Home Assistant UI:

```text
Settings -> Add-ons -> Add-on Store
```

Open the three-dot menu and choose:

```text
Check for updates
```

Find `IntellegyHUB` under local add-ons and install it. Open the add-on
configuration tab and set:

```yaml
led_gpio: 22
led_active_low: false
button_gpio: 26
button_active_low: true
button_bias: pull_up
button_debounce_ms: 50
```

8. Start the add-on.
9. Open the add-on logs and confirm the app reports hardware ready.

For the current CM4 diagnostic build, the add-on maps:

```yaml
devices:
  - /dev/gpiochip0
  - /dev/i2c-1
  - /dev/i2c-10
  - /dev/mem
  - /dev/vcio
```

The GPIO18 hardware PWM diagnostic uses `pigpiod` and `pigpio.hardware_PWM()` because HAOS mounts `/sys/class/pwm` read-only inside add-ons.
This diagnostic build sets `full_access: true`, `gpio: true`, and `SYS_RAWIO`; disable Protection Mode for the add-on before testing `BUZZER -> Test PWM`.

If the log says `/dev/gpiochip0 not available`, the add-on is installed but the hardware/device mapping is not usable on that host yet.

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

## Deploy From The ZIP Bundle

The generated HAOS bundle is:

```text
dist/intellegyhub_haos_deploy.zip
```

Build it on the development PC from the repository root:

```powershell
python scripts/build_haos_deploy.py
```

Expected output:

```text
Built dist\intellegyhub_haos_deploy.zip
version config 0.5.12: ok
version docker 0.5.12: ok
no pycache: ok
no backslash paths: ok
```

Copy this file to HAOS as:

```text
/intellegyhub_haos_deploy.zip
```

Common ways to copy it:

- Home Assistant Studio Code Server: upload it into `/homeassistant`, then move it to `/`.
- Samba share: copy it into the Home Assistant config share.
- SCP from the development PC:

```powershell
scp dist/intellegyhub_haos_deploy.zip root@192.168.0.25:/intellegyhub_haos_deploy.zip
```

Then upload the ZIP into the Studio Code Server `CONFIG` folder and unpack it
from the Studio Code Server terminal:

```sh
cd ~
unzip -o config/intellegyhub_haos_deploy.zip -d .
grep -n "version\|full_access\|gpio:\|SYS_RAWIO\|devices\|/dev/i2c\|/dev/gpio\|/dev/mem\|/dev/vcio" addons/intellegyhub/config.yaml
grep -n '"version"' config/custom_components/intellegyhub/manifest.json
```

Expected lines:

```text
version: 0.5.50
full_access: true
gpio: true
  - SYS_RAWIO
devices:
  - /dev/gpiochip0
  - /dev/i2c-1
  - /dev/i2c-10
  - /dev/mem
  - /dev/vcio
"version": "0.5.50"
```

The ZIP must contain Linux-style paths relative to `/`:

```text
addons/intellegyhub/config.yaml
config/custom_components/intellegyhub/manifest.json
```

Do not build the deploy ZIP with Windows backslash paths like
`addons\intellegyhub\config.yaml`, because HAOS `unzip` will not overlay
Linux paths correctly.

For an existing local install, extract the bundle and force Supervisor to re-read
the local add-on metadata:

```sh
ha store reload
ha supervisor reload
ha apps reload
ha apps restart local_intellegyhub
ha core restart
```

If the add-on card still shows the previous version after the files on disk show
the new version, Supervisor is still using the old built local add-on image.
Rebuild the local app image:

```sh
ha apps rebuild local_intellegyhub
ha apps restart local_intellegyhub
```

For a first install on a fresh HAOS device:

```sh
ha store reload
ha supervisor reload
ha apps reload
```

If the local app does not appear immediately, open Home Assistant:

```text
Settings -> Apps -> Store -> three-dot menu -> Check for updates
```

Then install and start:

```sh
ha apps install local_intellegyhub
ha apps start local_intellegyhub
ha core restart
```

Check Supervisor metadata:

```sh
ha apps info local_intellegyhub | grep -E "^(name|version|version_latest|state):"
grep -n '"version"' config/custom_components/intellegyhub/manifest.json
grep -n "version:" addons/intellegyhub/config.yaml
ls -l config/custom_components/intellegyhub/brand/icon.png config/custom_components/intellegyhub/brand/logo.png
```

Expected:

```text
name: IntellegyHUB
state: started
version: 0.5.50
version_latest: 0.5.50
"version": "0.5.50"
```

If the integration icon still shows `icon not available` immediately after update, force-refresh the Home Assistant frontend or open the integration page in a private browser window. Home Assistant serves custom integration brand assets from `/api/brands/integration/intellegyhub/icon.png` and the browser can keep the old placeholder cached.

## Repository Install Bundle

The build script also creates a second archive for a Home Assistant Store
repository and a third archive for the custom integration:

```text
dist/intellegyhub_ha_repository.zip
dist/intellegyhub_ha_integration.zip
```

`dist/intellegyhub_ha_repository.zip` is not for unpacking into Studio Code
Server `~`. It is the layout to put at the root of a git repository that Home
Assistant can add through:

```text
Settings -> Apps/Add-ons -> Store -> Repositories
```

Repository layout:

```text
repository.yaml
intellegyhub/config.yaml
intellegyhub/Dockerfile
intellegyhub/run.sh
intellegyhub/app/
intellegyhub/translations/
intellegyhub/README.md
intellegyhub/DOCS.md
intellegyhub/CHANGELOG.md
intellegyhub/icon.png
intellegyhub/logo.png
intellegyhub/requirements.txt
```

To refresh the local GitHub checkout from the generated repository archive on
Windows, use:

```powershell
python scripts\deploy_ha_repository_zip.py --dry-run
python scripts\deploy_ha_repository_zip.py
```

The script replaces the contents of:

```text
C:\Users\Algri\Documents\GitHub\intellegyhub-home-assistant
```

with `dist/intellegyhub_ha_repository.zip`. It keeps the target `.git`
directory by default so the folder remains a git checkout. Use `--delete-git`
only when intentionally replacing the folder as plain files instead of keeping
the repository history.

Home Assistant Store installs only the add-on from that repository. The add-on
package also contains a bundled copy of the custom integration and installs or
updates it at startup through the mapped Home Assistant config directory:

```text
/config/custom_components/intellegyhub
```

The integration list under:

```text
Settings -> Devices & services -> Add integration
```

is populated by Home Assistant Core. Core discovers custom integrations only
after their files exist under:

```text
/config/custom_components/<domain>
```

and Home Assistant Core has been restarted. The add-on repository does not copy
files there directly; the installed add-on container performs the copy when it
starts.

Repository install/update flow:

1. Publish `dist/intellegyhub_ha_repository.zip` contents to the GitHub add-on
   repository, add that repository in Home Assistant Store, then install
   `IntellegyHUB`.
2. Start the add-on. On startup it compares:

```text
bundled integration version
installed /config/custom_components/intellegyhub/manifest.json version
```

3. If the installed integration is missing or older, the add-on updates:

```text
/config/custom_components/intellegyhub/manifest.json
```

4. Restart Home Assistant Core, then add:

```text
Settings -> Devices & services -> Add integration -> IntellegyHUB
```

The add-on writes its integration installer result to:

```text
/data/integration_install_status.json
```

and exposes the same information at:

```text
GET /api/v1/integration/status
```

`dist/intellegyhub_ha_integration.zip` remains available as a manual fallback
package for copying the custom integration yourself.

For full local development, keep using `dist/intellegyhub_haos_deploy.zip`
because it copies both required parts:

```text
addons/intellegyhub
config/custom_components/intellegyhub
```

Use the add-on web UI to run:

```text
Devices
Scan I2C-1
Scan I2C-10
```

On the tested CM4 hardware, `/dev/i2c-1` opens and may have no devices; `/dev/i2c-10` is the bus expected to show the left-side devices.

## Deploy The Custom Integration To HAOS

1. Open your HAOS config directory.
2. Create:

```text
/config/custom_components/intellegyhub
```

3. Copy all files from this repository folder:

```text
custom_components/intellegyhub/
```

into:

```text
/config/custom_components/intellegyhub/
```

4. Restart Home Assistant Core:

```text
Settings -> System -> Restart Home Assistant
```

5. Add the integration:

```text
Settings -> Devices & services -> Add integration -> IntellegyHUB
```

6. Backend URL for local add-on install:

```text
http://local-intellegyhub:8098
```

7. Submit the form.

Expected result:

- one device: `IntellegyHUB`;
- one LED switch entity;
- one Button binary sensor entity.

## End-To-End Hardware Check

1. Turn the LED switch on in Home Assistant.
2. Confirm the physical LED turns on.
3. Turn the LED switch off.
4. Confirm the physical LED turns off.
5. Press the physical button.
6. Confirm the binary sensor changes to on/pressed.
7. Release the physical button.
8. Confirm the binary sensor changes to off/released.
9. Stop the add-on.
10. Confirm both entities become unavailable.
11. Start the add-on again.
12. Confirm both entities become available again after reconnect.

## Expansion Modules

The add-on exposes a separate `EXPANSION` section below `X-PORT`.

Current implemented scope:

- `xDO-8` relay modules only;
- `xDO-8` uses `MCP23008` on `/dev/i2c-1`;
- supported scan address range: `0x20` through `0x27`;
- each detected `xDO-8` becomes one Home Assistant device with 8 relay switch entities.

Expansion bus power is controlled by the controller-board GPIO extender:

```text
bus: /dev/i2c-10
chip: MCP23017
address: 0x20
pin: GPB7
direction: output
```

In the add-on UI:

1. Open `EXPANSION`.
2. Turn `Bus power` on.
3. Press `Scan modules`.
4. Detected `xDO-8` modules appear as relay cards.
5. Press `Remove` on a relay card to forget a saved module; a later `Scan modules` will add it again if it is present on `/dev/i2c-1`.

In Home Assistant:

- `Expansion Bus Power` controls GPB7 and powers `/dev/i2c-1`.
- `xDO-8` relay entities are added only for detected modules.
- If bus power is off or a module is not available, its relay entities become unavailable.
- Bus power, detected modules, and relay states are stored by the add-on and restored after restart.

## Notes

- The integration does not access GPIO directly.
- The add-on is the only component that opens `/dev/gpiochip0`.
- Mock mode is only for development and tests.
- No LAN port mapping is configured by default; Home Assistant Core talks to the add-on over the internal Supervisor network.

DI PNP · wet · +24V
DI NPN · dry · COM
CNT PNP · wet · +24V
CNT NPN · dry · COM

[BUG] ПРИ ВКЛЮЧЕНИИ ПИТАНИЯ НА X-BUS ЗАПУСКАЕТСЯ ПОИСК ЭТО НЕ КОРЕКТНО
