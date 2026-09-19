# Runtime Notes

Startup reads `/data/options.json`, validates GPIO offsets, rejects identical LED and button lines, requests the LED as output, forces logical LED off, reads the button, and starts an edge listener.

The button edge listener runs outside the main asyncio loop. On each edge it waits for the configured debounce interval, samples the final logical button state, and emits one state transition if the stable value changed.

`GET /health` returns `200 {"status":"ok"}` when ready and `503` while startup or hardware initialization has failed.

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
dtparam=i2c_vc=on
dtparam=spi=off
```

After changing boot files, run `ha host reboot` and verify with:

```sh
ls -la /dev/i2c*
lsmod | grep -i i2c
```
