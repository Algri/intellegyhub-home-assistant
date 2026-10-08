# Примітки щодо роботи доповнення

Під час запуску доповнення читає `/data/options.json`, перевіряє номери ліній GPIO, відхиляє однакові лінії світлодіода й кнопки, запитує лінію світлодіода як вихід, встановлює його в логічний стан «вимкнено», зчитує стан кнопки та запускає обробник подій зміни сигналу.

Обробник подій кнопки працює поза основним циклом asyncio. Після кожної зміни сигналу він очікує заданий інтервал придушення брязкоту контактів, зчитує остаточний логічний стан кнопки та надсилає одну подію зміни стану, якщо стале значення змінилося.

Фіксований вхід живлення Power на GPIO17 також представлено станом кнопки. Якщо ввімкнено параметр
`power_button.shutdown_enabled`, натискання запускає таймер утримання, який можна скасувати.
Якщо відпустити кнопку раніше часу, заданого параметром `power_button.shutdown_hold_seconds`, нічого не станеться;
утримання кнопки протягом заданого часу від `0.1` до `1.5` секунди за потреби вмикає
налаштований звуковий сигнал зумера GPIO18 із фіксованою гучністю 80%, очікує на його завершення та потім надсилає
`POST http://supervisor/host/shutdown` із токеном доповнення `SUPERVISOR_TOKEN`.

`GET /health` повертає `200 {"status":"ok"}`, коли доповнення готове до роботи, і `503`, якщо запуск або ініціалізація обладнання завершилися помилкою.

## Локальний перегляд інтерфейсу у Windows

Вебінтерфейс доповнення можна перевірити локально без HAOS, запустивши сервер
у режимі емуляції з кореня репозиторію:

```powershell
python -m pip install -r requirements-local-ui.txt
$env:INTELLEGY_GPIO_MOCK="1"
$env:INTELLEGY_ADDON_OPTIONS='{"diagnostics":{"carrier_monitoring_poll_interval_seconds":30},"onewire":{"bus1_poll_interval_seconds":30,"bus2_poll_interval_seconds":30}}'
python -m uvicorn addon.app.main:app --host 127.0.0.1 --port 8098 --reload
```

Відкрийте:

```text
http://127.0.0.1:8098/
```

У цьому режимі використовуються емульовані дані GPIO/I2C. Він дає змогу перевірити розмітку інтерфейсу та структуру API,
але не підтверджує роботу реального обладнання CM4.

## Діагностика I2C на CM4

На перевіреному обладнанні CM4 HAOS надає потрібні шини I2C як `/dev/i2c-1` та `/dev/i2c-10`.
Доповнення явно підключає обидві шини через `devices` разом із `/dev/gpiochip0`.

Необхідні метадані доповнення:

```yaml
devices:
  - /dev/gpiochip0
  - /dev/i2c-1
  - /dev/i2c-10
  - /dev/mem
  - /dev/vcio
```

Шляхи GPIO та I2C працюють за явного зазначення `devices`, але HAOS монтує `/sys/class/pwm` усередині доповнень лише для читання.
Тому діагностика апаратного PWM на GPIO18 запускає `pigpiod` і використовує `pigpio.hardware_PWM()` через сокет демона.
Для цього потрібні `full_access: true`, `gpio: true`, `SYS_RAWIO`, `/dev/mem`, `/dev/vcio` та вимкнений режим захисту.

Вебінтерфейс доповнення через Ingress містить:

- `Devices` — список `/dev/gpio*` та `/dev/i2c*`, доступних усередині контейнера доповнення.
- `Scan I2C-1` — сканування `/dev/i2c-1`.
- `Scan I2C-10` — сканування `/dev/i2c-10`, шини VC/лівої шини в перевіреній конфігурації CM4.

Конфігурація завантаження HAOS має завантажувати модуль символьних пристроїв Linux I2C. У завантажувальному розділі створіть:

```text
CONFIG/modules/rpi-i2c.conf
```

із вмістом:

```text
i2c-dev
```

Файл `config.txt` завантажувального розділу має вмикати параметри прошивки I2C для CM4, наприклад:

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

Після зміни завантажувальних файлів виконайте `ha host reboot` і перевірте:

```sh
hwclock -w
ls -la /dev/i2c*
lsmod | grep -i i2c
hwclock -r
cat /sys/class/rtc/rtc0/hctosys
```
