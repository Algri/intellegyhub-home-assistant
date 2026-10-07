# Changelog

## 0.5.201

- Stop RS-485 live UI refresh when the selected bus is disabled, even if devices remain saved.
- Prevent the disabled RS-485 bus from competing with other add-on controls for browser time.

## 0.5.200

- Replace the heavy RS-485 live snapshot polling with a compact live-state endpoint.
- Keep templates, diagnostics, scan logs and full device metadata out of the high-frequency UI refresh path.

## 0.5.199

- Stop RS-485 UI live-refresh when no devices are configured.
- Resume live-refresh automatically when an RS-485 device becomes available.

## 0.5.198

- Reduce RS-485 live UI refresh frequency from 200 ms to 1 second.
- Avoid rebuilding device and scan lists on every live state update to keep the add-on UI responsive.

## 0.5.197

- Publish the separate public GitHub README from `docs/public/README.md`.
- Keep the internal root README outside the public repository package.

## 0.5.196

- Refresh the public add-on description with the full IntellegyHUB platform capabilities.
- Document independent RS-485 and 1-Wire channels, X-Port, XDO-8, XDI-16 and Zigbee expansion.
- Link the Home Assistant add-on page to https://intellegyhub.com.

## 0.5.195

- Keep FC05/FC06/FC15/FC16 write commands independent from read and device polling toggles.
- Route RS-485 command validation through the selected device port configuration.
- Preserve visible ON/OFF status inside all project toggles without duplicate external labels.

## 0.5.194

- Separate RTC Clock into its own navigation card while keeping Functions focused on buzzer and LED services.
- Unify project toggles with visible ON/OFF labels and remove duplicated external status text.
- Clarify RS-485 bus and device control naming, including independent per-port state and device read/write access.

## 0.5.193

- Ensure relay FC05 commands enter the per-port priority queue without waiting behind mode updates.
- Keep mode writes ahead of polling while preserving one uninterrupted Modbus transaction at a time.
- Maintain independent RS-485 port scheduling and automatic polling recovery after queued work is canceled.

## 0.5.192

- Add explicit per-port priority queues so relay commands always run before pending polling jobs.
- Preserve FIFO ordering within command and polling priorities while keeping RS-485 interfaces isolated.
- Cancel stale queued polling jobs when a command arrives to reduce relay response latency.

## 0.5.191

- Bind RS-485 WebSocket publishing to the lazily-created production runtime.
- Deliver detected MIO-8 DI changes to Home Assistant immediately after polling.

## 0.5.190

- Publish MIO-8 DI changes immediately over WebSocket after Modbus detection.
- Keep DI/DO runtime state in memory without SQLite writes during polling.
- Preserve XDI-16 input icon behavior for RS-485 binary sensors.

## 0.5.189

- Separate RS-485 polling, command, scan, and manual transactions per serial interface.
- Route RS-485 live state and configuration lifecycle updates through WebSocket events.
- Remove the shared transaction fallback so every Modbus operation requires its serial port.
- Fix DI live updates and relay command confirmation in Home Assistant.

## 0.5.188

- Add isolated dynamic Home Assistant entities for RS-485 devices.
- Support RS485-1/RS485-2 device naming with Slave ID in DeviceInfo.
- Add dynamic relay switches, digital input sensors, and mode selects.
- Preserve X-BUS isolation and remove stale RS-485 registry devices.


## 0.5.187

- Fix rounded corners and clipping for RS-485 I/O and traffic panels.
- Keep traffic panel scrolling and controls inside the corrected frame.


## 0.5.186

- Align the HA add-on navigation with the host header and refine spacing.
- Improve RS-485 toolbar layout at medium widths.
- Hide the RS-485 navigation tab when RS-485 is disabled.


## 0.5.185

- Add configurable RS-485 traffic log buffer with a default of 1000 entries.
- Keep Clear log independent from traffic capture Stop/Start state.
- Improve RS-485 navigation, settings UI, and digital input ON/OFF status display.

## 0.5.183

- Refresh discovered RS-485 devices while scanning instead of waiting for scan completion.
- Compact the RS-485 connection toolbar and discovered-device rows for narrow layouts.

## 0.5.182

- Include and serve the MIO-8 device icon from the repository and App image.
- Validate required device assets before repository deployment.

## 0.5.181

- Use the existing root `logo.png` for RS-485 device cards, matching the main card asset path.
- Remove the unavailable `rs485_assets` Docker build dependency.

## 0.5.180

- Fix RS-485 device image URLs for Home Assistant ingress by using relative asset paths.

## 0.5.179

- Fix RS-485 device logo delivery in the Home Assistant App bundle.
- Serve device assets through the same root application path as the main logo.

## 0.5.178

- Include RS-485 image assets in all HAOS release bundles.
- Refresh the release version after the corrected deployment packaging.

## 0.5.177

- Refine RS-485 device, template, diagnostics, and polling configuration UI.
- Keep Modbus traffic capture stopped by default and persist diagnostic logs safely across reloads.
- Prevent automatic device reads from adding traffic after a page refresh.

## 0.5.176

- Add a central RS-485 enable flag so production can hide the RS-485 card and prevent RS-485 scan, polling, and UART worker startup when disabled.
- Default RS-485 to disabled in add-on options until real-hardware validation is complete.

## 0.5.175

- Add RS-485 scan stop/progress handling and a console-style scan log that shows Modbus probe TX/RX frames.
- Keep the RS-485 scan log visible at a minimum five-line height in the add-on UI.

## 0.5.174

- Add the missing `pyserial` runtime dependency required by `pymodbus` for real RS-485 Modbus RTU serial scans.

## 0.5.173

- Improve RS-485 scan diagnostics and support both `pymodbus` `device_id` and `slave` keyword variants so MIO-8 discovery probes are sent on real hardware.

## 0.5.172

- Show the RS-485 devices panel in production add-on UI and remove stale placeholder text now that the MIO-8 backend is connected.

## 0.5.171

- Add RS-485 MIO-8 template-driven Modbus RTU backend polling, scan, relay write/read-back, DI polling, relay mode control, template CRUD, and `/dev/ttyAMA3`/`/dev/ttyAMA5` add-on device mappings.

## 0.5.170

- Align the X-Port mode toolbar dropdowns with the channel cards and restore spacing above the X1-X4 cards.

## 0.5.169

- Show the controller Variant as `Standard` without X-Port and `Universal` when X-Port is installed.
- Collapse the X-Port panel height when the optional module is not installed.

## 0.5.168

- Show a user-friendly X-Port status in the add-on UI when the optional module is not installed.

## 0.5.167

- Hide unavailable X-Port channel controls when the optional module is missing and clean stale X-Port devices from Home Assistant.

## 0.5.166

- Rename the main Home Assistant controller device to `IHC-1400 Controller` and simplify its model metadata to `IHC-1400`.

## 0.5.165

- Add missing Home Assistant number sliders for linked X-Port white dimmer modes.

## 0.5.164

- Add Home Assistant and backend support for linked X-Port white dimmer modes: `4×W`, `2×W + 2×W`, `2×W + W + W`, and `W + W + 2×W`.

## 0.5.163

- Fix RGB + W Home Assistant RGB light handling so logical color and brightness stay separate and color changes no longer dim RGB channels repeatedly.

## 0.5.162

- Polish X-Port LED Dimmer UI labels so `RGB + W` shows the white channel as `White` instead of `W`.
- Keep the X-Port mode selector naming and layout aligned with the card grid.

## 0.5.161

- Add the working X-Port `W + W + W + W` LED Dimmer mode with four independent white PWM channels.
- Rename X-Port Home Assistant selectors to `X-PORT 1 Profile` and `X-PORT 2 Mode`, and align add-on mode labels with the X-Port card layout.

## 0.5.160

- Add the X-Port LED Dimmer configuration selector in the add-on UI and keep RGBW displayed as a configuration rather than a profile.
- Expose RGB + W in Home Assistant as separate X-Port RGB Dimmer and X-Port W Dimmer devices with independent controls.

## 0.5.159

- Split Board Diagnostics LED behavior so STE is the runtime heartbeat, NET is the WebSocket connection indicator, and ERR no longer reacts to WebSocket disconnects.
- Remove the user-facing WebSocket grace option from add-on configuration while keeping NET retry indication internal.

## 0.5.158

- Keep STE LED off during WebSocket reconnect grace while NET blinks until the connection timeout expires.

## 0.5.157

- Add a configurable WebSocket connection grace period so NET blinks while connecting and ERR starts only after timeout.
- Enable the startup buzzer by default in add-on configuration and local run examples.

## 0.5.156

- Change Board Diagnostics LED behavior so STE heartbeat and NET stay active only while WebSocket is connected, and ERR blinks when the connection is lost.

## 0.5.155

- Normalize RTC Clock display time and provide a usable mock RTC status in add-on mock mode.

## 0.5.154

- Add SYS_TIME privilege for RTC sync and keep the last readable RTC status visible when a sync command fails.

## 0.5.153

- Parse BusyBox hwclock output with variable spacing in the RTC Clock backend.

## 0.5.152

- Pass the host RTC device into the add-on container and use explicit host RTC paths for RTC Clock commands.

## 0.5.151

- Use BusyBox-compatible host RTC commands for the RTC Clock card and show RTC backend errors in the add-on UI.

## 0.5.150

- Add Board Diagnostics backend support for STE heartbeat, NET connection indication, and ERR placeholder state persistence.
- Add host RTC status and sync endpoints for the RTC Clock card without direct I2C access to the RTC address.

## 0.5.149

- Rename the RGBW dimmer control to Dimmer and label RGBW channel sliders as X1 Red, X2 Green, X3 Blue, and X4 White.

## 0.5.148

- Rename the main Home Assistant controller device to Controller while keeping the IHC-1400 model visible in device details.

## 0.5.147

- Remove the legacy X-Port Home Assistant device registry entry when the active profile uses the new X-Port device model.

## 0.5.146

- Rename X-Port group mode to Profile with Universal I/O and RGBW Dimmer profiles.
- Safely reset outputs on manual profile switching while restoring the active profile state after add-on restart.
- Add the Home Assistant X-PORT Profile select entity and keep X1-X4/RGBW entities synchronized with the active profile.

## 0.5.145

- Add X-Port RGBW Dimmer group mode with persisted backend state.
- Expose one Home Assistant RGBW light entity for X1-X4 while hiding the individual X-Port channel entities in group mode.
- Restore the individual X1-X4 entities when X-Port returns to Independent mode.

## 0.5.144

- Group add-on options into Diagnostics, 1-Wire, Power button, and Buzzer sections.
- Use the shared buzzer frequency and duration for startup and shutdown beeps, with shutdown volume fixed at 80%.
- Keep backward compatibility with old flat option names while presenting the cleaned grouped configuration.

## 0.5.143

- Simplify add-on options for IHC-1400 by hiding fixed low-level GPIO settings.
- Rename visible 1-Wire polling options from bridge terminology to Bus 1 and Bus 2.
- Improve option labels and descriptions for diagnostics, Power shutdown, and buzzer behavior.

## 0.5.142

- Play the configured diagnostic buzzer and wait for it to finish before sending the Power button shutdown command.

## 0.5.141

- Add configurable Power button hold-to-shutdown through the Home Assistant Supervisor API.
- Expose add-on options to enable or disable Power shutdown and set the hold time from 0.1 to 1.5 seconds.

## 0.5.140

- Add the physical Power input on GPIO17 to the add-on state, WebSocket events, and Home Assistant binary sensors.
- Rework Carrier Board I/O as Controls with separate Host Outputs, Host Inputs, RS-485, X-Mod1, X-Mod2, and USB cards.
- Shorten RS-485 and X-Mod control labels in the add-on UI and Home Assistant integration.

## 0.5.139

- Fix mock xDO-8 relay state so toggling one relay no longer resets another relay.
- Make Service Tools return realistic mock health, device, and I2C diagnostics instead of empty local data.
- Stop 1-Wire bus power-on from automatically scanning sensors; scans now run only from the explicit scan action.

## 0.5.138

- Keep the startup buzzer using add-on config values without overwriting persisted diagnostic buzzer settings after restart.

## 0.5.137

- Persist diagnostic buzzer settings after successful play/test actions so UI changes survive add-on restart.
- Update the hardware coverage backlog to mark completed EEPROM and buzzer work and separate hardware-pending validation.

## 0.5.136

- Show the controller identity passport in Home Assistant device metadata, including model, manufacturer, serial, hardware, and software versions.
- Use lightbulb icons for X-Port PWM controls and DC icons for controller voltage diagnostics.

## 0.5.135

- Update Home Assistant X-Port icons for AI, PWM, DI, and counter modes.
- Rename X-Bus module devices to slot-style names such as `X-Bus0 xDI-16` while keeping the module address in the device model.

## 0.5.134

- Refine the controller overview identity card layout with the restored logo mark, Automation Controller subtitle, and full-width identity divider.

## 0.5.133

- Add complete mock data for the local add-on UI preview, including carrier monitoring, EEPROM identity, X-Port, expansion modules, and 1-Wire sensors.
- Refine the controller overview layout with the IntellegyHUB wordmark, cleaner identity text, and a shorter health label.

## 0.5.132

- Add a runtime EEPROM identity reader that uses the same `IHID` header, CRC, and JSON payload format as the factory writer.
- Expose EEPROM identity read status and errors in the carrier API instead of silently showing empty identity fields.
- Add a guarded factory overwrite path for boards that intentionally need conversion from old non-`IHID` EEPROM data.
- Align the overview identity panel with the agreed Product/Model, Manufacturer, Serial, Hardware, Software, and Variant fields.

## 0.5.131

- Read controller identity from the factory EEPROM and show it on the add-on overview.
- Show `--` for missing EEPROM identity fields instead of stale factory placeholders.
- Fix broken temperature/unit text encoding in the overview and clean related UI metadata strings.

## 0.5.130

- Rename Home Assistant devices to user-facing labels: `Controls`, `1-Wire Bus1`, `1-Wire Bus2`, and address-first xBUS module names.
- Update existing Home Assistant device registry names so already-created devices stop showing stale `Hardware Host` and bridge names.

## 0.5.129

- Persist diagnostic buzzer settings in the add-on database and restore the last non-zero volume when re-enabled.
- Rework the diagnostic buzzer panel so frequency, duration, and volume use aligned sliders with an Enabled toggle.
- Keep startup buzzer add-on configuration fields unchanged while applying the tighter diagnostic buzzer ranges.

## 0.5.128

- Force the Buzzer Volume light entity id to `light.intellegyhub_buzzer_volume_light` so dashboard YAML can reference it reliably.

## 0.5.127

- Add a Home Assistant Buzzer Volume light entity so Tile cards can open the native fullscreen brightness/on-off control.

## 0.5.126

- Add Home Assistant X-Port PWM light entities so PWM channels can use native light brightness tiles and fullscreen on/off controls.

## 0.5.125

- Mark the PCF85063A RTC system integration as covered now that HAOS boot scripts enable the RTC overlay and print setup/verification steps.

## 0.5.124

- Update HAOS boot preparation scripts for the PCF85063A RTC overlay, remove the duplicate SPI-off line, and print RTC setup/verification commands.

## 0.5.123

- Restore xDO-8 relay tile icons to the relay-style toggle variant shown by Home Assistant while keeping compact relay names.

## 0.5.122

- Restore stateful switch-style icons for xDO relay outputs and xDI input states.

## 0.5.121

- Use relay and discrete-input icons for xDO/xDI extension module entities.

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
## 0.5.184

- Add manual Modbus command sending with calculated RTU CRC preview.
- Preserve manual command settings between dialog refreshes and browser reloads.
- Keep diagnostics capture in memory and persist it on Stop or shutdown.
- Align RS-485 device list states and diagnostics controls with the addon UI style.
