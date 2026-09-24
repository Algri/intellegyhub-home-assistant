# Runtime Notes

Startup reads `/data/options.json`, validates GPIO offsets, rejects identical LED and button lines, requests the LED as output, forces logical LED off, reads the button, and starts an edge listener.

The button edge listener runs outside the main asyncio loop. On each edge it waits for the configured debounce interval, samples the final logical button state, and emits one state transition if the stable value changed.

The fixed Power input on GPIO17 is also exposed as a button state. If
`power_button.shutdown_enabled` is enabled, pressing it starts a cancellable hold
timer. Releasing it before `power_button.shutdown_hold_seconds` does nothing;
holding it for the configured `0.1` to `1.5` seconds optionally plays the
configured GPIO18 buzzer beep at fixed 80% volume, waits for it to finish, and then sends
`POST http://supervisor/host/shutdown` with the add-on `SUPERVISOR_TOKEN`.

`GET /health` returns `200 {"status":"ok"}` when ready and `503` while startup or hardware initialization has failed.

## Local UI Preview On Windows

The add-on web UI can be checked locally without HAOS by running the backend
in mock mode from the repository root:

```powershell
python -m pip install -r requirements-local-ui.txt
$env:INTELLEGY_GPIO_MOCK="1"
$env:INTELLEGY_ADDON_OPTIONS='{"diagnostics":{"carrier_monitoring_poll_interval_seconds":30},"onewire":{"bus1_poll_interval_seconds":30,"bus2_poll_interval_seconds":30}}'
python -m uvicorn addon.app.main:app --host 127.0.0.1 --port 8098 --reload
```

Open:

```text
http://127.0.0.1:8098/
```

This preview uses mock GPIO/I2C data. It validates the UI layout and API shape,
but it does not validate real CM4 hardware.

## CM4 I2C Diagnostics

For the tested CM4 hardware, HAOS exposes the useful I2C buses as `/dev/i2c-1` and `/dev/i2c-10`.
The add-on maps those two buses explicitly through `devices`, together with `/dev/gpiochip0`.

Required add-on metadata:

```yaml
devices:
  - /dev/gpiochip0
  - /dev/i2c-1
  - /dev/i2c-10
  - /dev/mem
  - /dev/vcio
```

The GPIO and I2C paths work with explicit `devices`, but HAOS mounts `/sys/class/pwm` read-only inside add-ons.
The GPIO18 hardware PWM diagnostic therefore starts `pigpiod` and uses `pigpio.hardware_PWM()` through the daemon socket.
This requires `full_access: true`, `gpio: true`, `SYS_RAWIO`, `/dev/mem`, `/dev/vcio`, and disabled Protection Mode.

The add-on Ingress UI includes:

- `Devices` - lists `/dev/gpio*` and `/dev/i2c*` visible inside the add-on container.
- `Scan I2C-1` - scans `/dev/i2c-1`.
- `Scan I2C-10` - scans `/dev/i2c-10`, the VC/left bus on the tested CM4 setup.

HAOS boot configuration must load the Linux I2C character-device module. On the boot partition, create:

```text
CONFIG/modules/rpi-i2c.conf
```

with:

```text
i2c-dev
```

The boot partition `config.txt` must enable the CM4 I2C firmware parameters, for example:

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

After changing boot files, run `ha host reboot` and verify with:

```sh
hwclock -w
ls -la /dev/i2c*
lsmod | grep -i i2c
hwclock -r
cat /sys/class/rtc/rtc0/hctosys
```
