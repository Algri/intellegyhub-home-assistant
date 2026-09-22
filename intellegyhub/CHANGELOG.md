# Changelog

## 0.5.120

- Remove repeated module prefixes from xDO/xDI dashboard picker entity names and use clearer extension module I/O icons.

## 0.5.119

- Extend compact Home Assistant registry names to X-Port, xDO/xDI module, and 1-Wire entities for cleaner dashboard picker labels.

## 0.5.118

- Apply compact Home Assistant entity registry names for Hardware Host entities so dashboard pickers no longer show repeated Hardware Host prefixes.

## 0.5.117

- Keep host entities on the Hardware Host device while exposing compact dashboard names without the repeated Hardware Host prefix.

## 0.5.116

- Restore Home Assistant device-scoped entity naming and document explicit Lovelace tile names for compact dashboard cards.

## 0.5.115

- Clear stale Hardware Host name overrides from Home Assistant entity registry so dashboard tiles use the corrected short names.

## 0.5.114

- Use dashboard-friendly Home Assistant entity names without the repeated Hardware Host prefix.

## 0.5.113

- Add dashboard-friendly icons to Home Assistant buzzer number entities.

## 0.5.112

- Limit buzzer PWM frequency to the verified stable range and map UI volume linearly into the calibrated physical duty range.

## 0.5.111

- Fall back to sysfs PWM when pigpio is unavailable so the buzzer can still play if `pigpiod` fails to start.

## 0.5.110

- Restore the previous `pigpiod` startup flags while keeping buzzer status polling from opening pigpio socket connections.

## 0.5.109

- Stop buzzer status polling from opening pigpio socket connections.

## 0.5.108

- Rebuild release archives after the X-Port mode-control UI polish and release checklist documentation updates.

## 0.5.107

- Align X-Port mode controls with the shared row style, including counter reset placement, read-only DI indicators, and compact PWM slider layout.

## 0.5.106

- Keep X-Port cards stable while live telemetry updates so mode dropdowns stay open in AI and other modes.

## 0.5.105

- Polish add-on UI consistency across action bars, hover states, 1-Wire controls, and buzzer command layout.

## 0.5.104

- Refine the controller overview card labels, metric units, metric status text, and shared monitoring update time.

## 0.5.103

- Skip reserved low I2C addresses during service-tool scans so false responses like `0x03` are not reported as devices.

## 0.5.102

- Keep the Service Tools diagnostics output open while background UI refreshes run.

## 0.5.101

- Make 1-Wire bus power toggles return immediately while sensor scanning runs after power settles.
- Restore persisted X-Port writable output values after add-on restart, including PWM duty and DO state.

## 0.5.100

- Improve light theme status contrast and make controller uptime text human-readable.

## 0.5.99

- Bump packaged add-on and integration version after the persisted theme switcher change.

## 0.5.98

- Add a persisted add-on theme switcher backed by the existing SQLite state database.
- Support Auto, Light, and Dark add-on UI themes without using browser localStorage.

## 0.5.97

- Fix X-Port service diagnostics by adding the missing shared X-Port paint function.

## 0.5.96

- Remove the unused `Scan I2C-0` service tool button.
- Make the X-Port, Expansion, and 1-Wire service tool buttons print their JSON diagnostics output while still refreshing their UI sections.

## 0.5.95

- Keep the controller passport free of placeholder Network/IP data and compact the mobile title sizing.

## 0.5.94

- Copy add-on logo/icon assets into the runtime image, compact the controller passport height, and remove the placeholder Network row until real interface data is available.

## 0.5.93

- Rework only the left controller overview area into a controller passport with logo, serial placeholder, hardware/software, health, and uptime rows.

## 0.5.92

- Keep X-Port mode dropdown styling separate from the global add-on action button style.

## 0.5.91

- Restyle add-on UI command buttons with the compact blue-outline action style used by X-Port reset controls.

## 0.5.90

- Fit the four Carrier I/O cards in one horizontal row on wide add-on UI screens.

## 0.5.89

- Add FN1 GPIO27 and FN2 GPIO26 function button reporting to the add-on API, WebSocket events, add-on UI, and Home Assistant integration.
- Rename host GPIO outputs to `STE LED`, `ERR LED`, `NET LED`, and `USR LED`, and show them in the add-on Carrier I/O panel.

## 0.5.88

- Stretch the Buzzer parameter group across the available diagnostic card width so no dead space remains beside the controls.

## 0.5.87

- Rework the Buzzer add-on UI into compact Parameters and Command groups that fit cleanly inside the diagnostic card.

## 0.5.86

- Move Carrier I/O below Buzzer so service controls stay close to low-level tools.
- Restyle the quick action area as a Service Tools panel using the add-on section layout.

## 0.5.85

- Add Carrier I/O retained toggle controls for RS-485 120Ω termination, XMOD Flash/Enable and Reset, and USB reset lines.
- Expose Carrier I/O outputs through the add-on API, add-on UI, WebSocket events, and Home Assistant switch entities.
- Document that Carrier MCP23017 `0x20` writes must use the shared shadow so changing one output does not clear other outputs.

## 0.5.84

- Rename the Home Assistant host device to `Hardware Host`.
- Rename the physical button entity to `User Defined Button`.
- Rename the buzzer action entity to `Buzzer Play`.

## 0.5.83

- Rename the user-facing buzzer command API to `/api/v1/buzzer/play` and use `/api/v1/buzzer/play-pwm` from the add-on UI.
- Add Home Assistant Buzzer Frequency and Buzzer Duration settings.
- Add a Home Assistant Play Buzzer button that triggers the buzzer with the configured settings.

## 0.5.82

- Remove the nested BUZZER control card and use a flat aligned control row with fixed field widths.

## 0.5.81

- Rework the BUZZER add-on UI into a compact control card so the volume slider and action buttons no longer stretch across the whole diagnostic panel.

## 0.5.80

- Clean up the BUZZER add-on UI layout: group frequency/duration, keep Volume as the main slider, and align action buttons as a compact control block.

## 0.5.79

- Replace the BUZZER duty input with a 0-100% Volume slider in the add-on UI.
- Convert user volume linearly to the calibrated GPIO18 physical PWM duty range.
- Expose Buzzer Volume as a Home Assistant number slider on the IntellegyHUB device.

## 0.5.78

- Document the local mock-mode add-on UI preview command in the packaged add-on docs.

## 0.5.77

- Restore the Carrier Overview split layout while keeping the add-on's existing card, border, color, and spacing style.

## 0.5.76

- Restore the normal section gap below the Carrier Overview panel so it does not touch X-PORT.

## 0.5.75

- Align the Carrier Overview card with the existing add-on UI style, reusing the same section, card, typography, and color treatment as the rest of the add-on.
- Keep the Carrier identity placeholder in the backend model so EEPROM-backed identity can replace it later without changing the UI contract.

## 0.5.74

- Add Carrier board monitoring for MCP9808 `0x18` board temperature and ADS1115 `0x48` Vin/+5V/+3.3V rails.
- Add a top add-on UI controller overview card with the latest monitoring values and timestamps.
- Expose Carrier monitoring values as Home Assistant diagnostic sensors.
- Add `carrier_monitoring_poll_interval_seconds`, defaulting to 30 seconds.

## 0.5.73

- Shorten X-Port mode labels in the add-on UI and Home Assistant integration to `Off`, `AI`, `DO`, `PWM`, `DI PNP/+24V`, `DI NPN/COM`, `CNT PNP/+24V`, and `CNT NPN/COM`.
- Keep the internal X-Port API mode values unchanged for compatibility.

## 0.5.72

- Shorten Home Assistant child device names by removing repeated `IntellegyHUB` prefixes from X-Port and X-Bus modules.
- Shorten 1-Wire bridge device names by hiding the bus/address suffix from the primary display name.

## 0.5.71

- Replace the host `Button` occupancy device class with a dedicated button icon so Home Assistant does not show the occupancy/house icon.

## 0.5.70

- Rename the host `LED` Home Assistant switch to `USR`.
- Add host output switches for `STE` GPIO19, `ERR` GPIO20, and `NET` GPIO21.
- Add the `/api/v1/outputs/{output_id}` API and `output_changed` WebSocket events while keeping `/api/v1/led` compatible.

## 0.5.69

- Reinitialize persisted xDO-8 MCP23008 modules after X-Bus power is restored, then reapply saved relay states.
- Add a regression test that verifies X-Bus power cycling uses the dedicated xDO-8 restore path instead of only rewriting relay shadow state.

## 0.5.68

- Push Carrier FAULT GPIO16/GPIO23 changes over WebSocket so Home Assistant fault sensors update without waiting for a full state refresh.
- Treat Carrier FAULT lines as Active-Low no-pull GPIO inputs and document/add the required HAOS boot `gpio=16=ip,np` and `gpio=23=ip,np` lines.
- Read back MCP23008 GPIO after xDO-8 relay writes so the add-on state follows the actual expander output register.

## 0.5.67

- Add a shared Carrier MCP23017 `0x20` manager so X-Bus and 1-Wire power control use one owner, one lock, and one GPIOA/GPIOB shadow.
- Expose +5V X-Bus and +5V 1-Wire FAULT lines GPIO23/GPIO16 as Home Assistant problem binary sensors.
- Add `docs/tasks/hardware-coverage-backlog.md` for hardware features still not covered from `docs/HARDWARE.md`.

## 0.5.66

- Enable Supervisor API access for the add-on so the bundled integration installer can reliably read its installed slug.
- Include the generated backend URL in `/data/integration_install_status.json` and installer logs for easier HAOS verification.

## 0.5.65

- Have the add-on write the exact installed backend URL into the custom integration as `backend.json`.
- Let the Home Assistant integration setup use that bundled backend URL first, so repository installs do not require users to find the hashed add-on slug manually.
- Keep Supervisor add-on discovery and the backend URL field as fallbacks for systems where the hint file is unavailable.

## 0.5.64

- Bundle the Home Assistant custom integration inside the add-on image.
- Let the add-on install or update `/config/custom_components/intellegyhub` only when the bundled integration version is newer than the installed one.
- Write integration install status to `/data/integration_install_status.json`; Home Assistant Core still needs a restart after integration files are installed or updated.

## 0.5.63

- Add a separate `dist/intellegyhub_ha_integration.zip` package for installing the Home Assistant custom integration under `/config/custom_components/intellegyhub`.
- Document that Home Assistant Store repositories install add-ons only; Home Assistant Core discovers custom integrations from `/config/custom_components` after a Core restart.

## 0.5.62

- Rebuild the local HAOS deploy bundle after adding the separate Home Assistant repository bundle workflow.

## 0.5.61

- Add a separate Home Assistant repository bundle layout under `dist/intellegyhub_ha_repository.zip`.
- Keep the confirmed Studio Code Server local deploy bundle under `dist/intellegyhub_haos_deploy.zip`.
- Validate both generated layouts so local deploy paths and repository install paths cannot drift silently.

## 0.5.60

- Reset existing Home Assistant xDI-16 binary sensor registry entries before recreating them so stale occupancy metadata cannot keep showing house icons.
- Change xDI-16 input entities to the standard `mdi:toggle-switch-outline` icon and keep their binary sensor device class unset.

## 0.5.59

- Stop auto-scanning X-BUS addresses when bus power is turned on; `Scan modules` is now the only action that discovers new modules.
- Keep power-on reinitialization limited to already stored modules so xDO-8 relays and xDI-16 inputs recover after a bus power-cycle.

## 0.5.58

- Force xDI-16 Home Assistant input entities to report no binary sensor device class and an explicit `mdi:electric-switch` icon, overriding stale occupancy icons.

## 0.5.57

- Rebuild package with xDI-16 Home Assistant inputs using `mdi:electric-switch` instead of the occupancy device class icon.

## 0.5.56

- Use an electric switch icon for Home Assistant xDI-16 input entities instead of the occupancy device class icon.

## 0.5.55

- Show explicit ON/OFF text next to each xDO-8 relay and xDI-16 input toggle in the add-on X-BUS UI.

## 0.5.54

- Reinitialize X-BUS modules after bus power is turned back on so MCP23008 relay outputs and MCP23017 xDI-16 input pull-ups/interrupt registers are restored after a power-cycle.
- Reconfigure xDI-16 modules before reading their inputs, preventing all DI channels from sticking ON after bus power is toggled.

## 0.5.53

- Remove the X-BUS polling fallback; the add-on UI must update from WebSocket events.
- Add browser console diagnostics for the add-on UI WebSocket connection path.

## 0.5.52

- Add a quiet X-BUS UI state poll so xDO-8 relay and xDI-16 input toggles update without F5 even when Home Assistant Ingress does not deliver WebSocket events to the add-on page.

## 0.5.51

- Fix the add-on Ingress UI WebSocket URL so X-BUS updates work behind Home Assistant Ingress instead of only at `/ws`.
- Broadcast a full X-BUS snapshot after relay changes so other open UI windows update without F5.
- Apply xDO-8/xDI-16 module WebSocket updates directly in the UI.

## 0.5.50

- Restore the confirmed Studio Code Server local add-on layout: package the add-on under `/addons/intellegyhub`.
- Keep the custom integration under `/config/custom_components/intellegyhub`.
- Document that Home Assistant Store may require the UI action "Check for updates" before `local_intellegyhub` appears.

## 0.5.49

- Restore the original Studio Code Server `/config` deployment layout.
- Package the add-on under `/config/addons/intellegyhub`.
- Package the custom integration under `/config/custom_components/intellegyhub`.

## 0.5.48

- Package the local add-on as a real local repository under `/addons/local`, including `/addons/local/repository.yaml`.
- Place the add-on at `/addons/local/intellegyhub` and keep the install slug `local_intellegyhub`.
- Keep the custom integration under `/homeassistant/custom_components/intellegyhub`.

## 0.5.47

- Restore the HAOS Core SSH layout that worked on the target system: the local add-on is packaged under `/homeassistant/addons/intellegyhub`.
- Remove the broken `/addons/local/intellegyhub` packaging attempt.
- Keep the custom integration under `/homeassistant/custom_components/intellegyhub`.

## 0.5.46

- Package attempt under `/addons/local/intellegyhub`; superseded by `0.5.47`.
- Keep the install slug as `local_intellegyhub`.
- Keep the custom integration under `/homeassistant/custom_components/intellegyhub`.

## 0.5.45

- Package the local add-on under `/addons/intellegyhub`, which is the official HAOS local app path used by SSH/Samba local development.
- Keep the custom integration under `/homeassistant/custom_components/intellegyhub`.
- Keep `repository.yaml` out of the HAOS local `/addons` deploy path; it belongs to the GitHub repository root, not the built-in Local apps folder.

## 0.5.44

- Package the local add-on under `/homeassistant/addons/intellegyhub`.
- Keep the custom integration under `/homeassistant/custom_components/intellegyhub`.

## 0.5.43

- Restore the official HAOS local app repository path: the add-on is packaged under `/addons/intellegyhub`.
- Keep the Home Assistant integration packaged under `/homeassistant/custom_components/intellegyhub`.

## 0.5.42

- Restore the HAOS Apps deploy layout: the add-on is packaged under `/local_apps/intellegyhub`, while the integration is packaged under `/homeassistant/custom_components/intellegyhub`.
- Include `/local_apps/repository.yaml` in the deploy archive so a clean local app install is visible after `ha apps reload`.

## 0.5.41

- Restore the HAOS deploy archive to the `/homeassistant` layout used by HAOS Core SSH: `addons/intellegyhub` and `custom_components/intellegyhub`.
- Keep deploy bundle checks aligned with the `cd /homeassistant; unzip ...` install flow.

## 0.5.40

- Update the add-on UI from backend WebSocket events so X-BUS relay and input states refresh without F5.
- Render xDI-16 inputs as read-only indicators without hover/click highlighting.

## 0.5.39

- Keep X-BUS power control separate from module discovery; turning bus power on no longer scans for new modules.
- Replace stale X-BUS module inventory when the detected module type changes at the same address.
- Sort X-BUS modules by bus address and clean stale Home Assistant entities after X-BUS inventory changes.
- Fix HAOS deploy archive paths for the legacy `cd /; unzip ...` install flow.

## 0.5.38

- Add xDI-16 support on X-BUS with MCP23017 detection, read-only add-on UI inputs, and dynamic Home Assistant binary sensors.
- Handle xDI-16 input updates through the shared GPIO6 interrupt worker instead of periodic polling.

## 0.5.37

- Rebuild X-Port Home Assistant entities dynamically when a channel mode changes.
- Keep only the active mode entity visible for each X-Port channel.

## 0.5.36

- Show all eight xDO-8 relay toggles in a single row on wide screens.
- Keep responsive fallback layouts for narrower screens.

## 0.5.35

- Rename the expansion add-on UI section to X-BUS and separate bus-level controls from module relay controls.
- Rework xDO-8 module cards with Online/Offline status, safer Remove placement, and a responsive relay toggle grid.

## 0.5.34

- Add human-readable add-on option labels and descriptions for the 1-Wire bridge polling intervals.

## 0.5.33

- Add 1-Wire sensor inventory flow: scan discovers and stores DS18B20 sensors, while Home Assistant entities are created only after the user adds a sensor.
- Add visible Home Assistant bridge devices for both DS2482S-100 buses, including I2C bus and address in the device name.
- Remove stale DS18B20 temperature entities when sensors are removed from the inventory.

## 0.5.32

- Add per-bridge 1-Wire polling intervals in add-on options.
- Make each 1-Wire bridge polling toggle start or stop periodic DS18B20 temperature refreshes for that bridge.

## 0.5.31

- Rename the 1-Wire UI section from optional module to interface.
- Add per-bridge polling toggles for DS2482S-100 bridges and persist their state.

## 0.5.30

- Group DS18B20 temperature entities under 1-Wire Bridge devices instead of creating one device per sensor.

## 0.5.29

- Move 1-Wire DS2482S-100 bridge scan to I2C-10 addresses 0x1A and 0x1B.

## 0.5.28

- Add 1-Wire support through DS2482S-100 bridges.
- Add 1-Wire bus power control through MCP23017 0x20 on I2C-10 GPB6.
- Add add-on UI, REST/WS events, persistence, and Home Assistant DS18B20 temperature sensors.

## 0.5.27

- Apply a perceptual brightness curve before writing X-Port PWM values to the PCA9632.

## 0.5.26

- Remove the experimental power-button shutdown path and its add-on options.
- Add optional startup buzzer beep settings.

## 0.5.25

- Do not restart the power-button hold timer on duplicate press events from contact bounce.
- Add detailed power-button timing logs for press, beep start, beep completion, and shutdown publish.

## 0.5.24

- Use the same `BuzzerManager.test_pwm()` path for the power-button confirmation beep as the working add-on UI button.
- Hold the confirmation beep for one second, stop it through the normal buzzer stop path, then wait one more second before shutdown.

## 0.5.23

- Drive the power-button confirmation beep through the `pigs hp` command path first, with Python pigpio as fallback.
- Log the start and completion of the power-button beep before publishing the HAOS shutdown request.

## 0.5.22

- Make the power-button confirmation beep longer and wait one extra second before publishing the HAOS shutdown request.

## 0.5.21

- Use a dedicated blocking hardware PWM beep path for the power button shutdown confirmation.
- Stop GPIO18 PWM in the same pigpio session before publishing the HAOS shutdown request.

## 0.5.20

- Remove the dedicated power button bias option; GPIO17 now relies on the board's external pull resistor.
- Keep only `power_button_delay_ms` visible for the power button timing; contact debounce stays internal.
- Finish the confirmation beep and explicitly stop the buzzer before sending the HAOS shutdown request.
- Allow the button to be released after the confirmation beep starts without cancelling shutdown.

## 0.5.19

- Expose a single power button timing option named `power_button_delay_ms`.
- Keep power button contact debounce internal at 50 ms and preserve backward compatibility with `power_button_shutdown_delay_ms`.
- Send the HAOS shutdown request immediately after starting the confirmation beep instead of waiting for the beep to finish.

## 0.5.18

- Make the power button shutdown beep easier to hear: 2000 Hz, 300 ms, 60% duty.
- Log when the power button shutdown beep starts so hardware tests can distinguish missed beep from missed button event.

## 0.5.17

- Use `disabled` as the default power button bias for boards with an external resistor.
- Emit a short GPIO18 hardware PWM beep when the power button hold delay completes, before requesting HAOS shutdown.

## 0.5.16

- Add an optional dedicated GPIO power button with a configurable 0-1500 ms hold delay.
- Publish `power_button_shutdown_requested` over WebSocket when the hold delay completes.
- Have the Home Assistant integration call `hassio.host_shutdown` for graceful HAOS host shutdown.

## 0.5.15

- Make the BUZZER hardware PWM test stoppable immediately by starting PWM and scheduling automatic shutdown asynchronously.
- Simplify the BUZZER UI to hardware PWM only, remove the noisy software GPIO button, and align the controls.

## 0.5.14

- Avoid upstream pigpio `make install` on Alpine/Python 3.12 because it runs a legacy `setup.py` path that can fail when `distutils` is unavailable.
- Install only the needed `pigpiod`, `pigs`, headers, and shared libraries from the patched pigpio build; the Python client still comes from `pip`.

## 0.5.13

- Build pigpio from upstream inside the add-on image, matching the known working HAOS pigpio add-on pattern instead of relying on an Alpine package.
- Start `pigpiod` with the correct foreground flag `-g`; the previous `-f` flag disables FIFO and was not foreground.
- Stop hiding pigpiod startup failures and add an explicit startup connectivity check with `pigs t`.
- Map `/dev/gpiomem` explicitly in addition to `/dev/mem`.

## 0.5.12

- Switch the BUZZER hardware test to `pigpio.hardware_PWM()` instead of the read-only HAOS `/sys/class/pwm` path.
- Start `pigpiod` inside the add-on and add the raw I/O permissions/devices used by existing HAOS pigpio add-ons.
- Keep `/sys/class/pwm` diagnostics visible as fallback information only.

## 0.5.11

- Enable full add-on hardware access so the GPIO18 hardware PWM diagnostic can write `/sys/class/pwm`.
- Keep the buzzer hardware PWM path explicit; Home Assistant Protection Mode must be disabled for this add-on when testing PWM.

## 0.5.10

- Add a BUZZER diagnostic section to the add-on UI for GPIO18 tests.
- Add REST endpoints for buzzer status, hardware PWM test, software GPIO test, and stop.
- Keep hardware PWM best-effort through `/sys/class/pwm` and expose clear errors when sysfs is not writable inside the add-on.

## 0.5.9

- Make extension bus power-on authoritative in the add-on: enabling bus power now scans/restores xDO-8 modules before publishing state.
- Add a short power-settle delay before scanning xDO-8 modules after restored or newly enabled bus power.
- Keep Home Assistant and add-on UI behavior aligned by removing the integration-only extra scan on bus power enable.

## 0.5.8

- Fix xDO-8 Home Assistant device creation on current Home Assistant releases by using the keyword-only device registry helper API.
- Retry xDO-8 relay entity discovery until Home Assistant accepts the new entities instead of marking the module as known too early.

## 0.5.7

- Rework xDO-8 relay discovery in the Home Assistant switch platform to follow the dynamic-device listener pattern from the Home Assistant developer docs.
- Add integration logs showing current, known, and newly discovered xDO-8 module ids during switch discovery.

## 0.5.6

- Add a periodic Home Assistant integration resync so expansion modules found from the add-on UI appear even if the WebSocket event was missed.

## 0.5.5

- Align xDO-8 Home Assistant registry cleanup with the current device registry API.
- Use `via_device_id` for xDO-8 devices instead of deprecated `via_device`.

## 0.5.4

- Remove xDO-8 relay entities and the module device from Home Assistant when an expansion module is removed in the add-on UI.
- Allow removed xDO-8 modules to be recreated in Home Assistant after a later scan finds them again.

## 0.5.3

- Add manual removal for saved expansion modules.
- Keep expansion scan available so removed modules can be rediscovered.
- Disable relay toggles in the add-on UI when a saved module is unavailable.

## 0.5.2

- Persist expansion bus power state across add-on restarts.
- Persist detected xDO-8 modules across add-on restarts.
- Restore persisted xDO-8 relay states when the module is detected again.
- Keep known modules visible as unavailable when expansion bus power is off.

## 0.5.1

- Improve Expansion UI relay layout with compact horizontal toggles.

## 0.5.0

- Add Expansion section for xDO-8 modules on I2C-1.
- Add expansion bus power control through MCP23017 0x20 on I2C-10 GPB7.
- Add Home Assistant entities for expansion bus power and detected xDO-8 relays.
