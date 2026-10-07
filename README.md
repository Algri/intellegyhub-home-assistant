<p align="center">
  <img src="intellegyhub/logo.png" alt="IntellegyHUB" width="420">
</p>

<h1 align="center">IntellegyHUB for Home Assistant</h1>

<p align="center">
  <strong>Reliable local automation for spaces that should take care of themselves.</strong>
</p>

<p align="center">
  <a href="https://intellegyhub.com">Website</a> ·
  <a href="intellegyhub/README.md">Platform overview</a> ·
  <a href="intellegyhub/CHANGELOG.md">Changelog</a> ·
  <a href="https://github.com/Algri/intellegyhub-home-assistant/issues">Issues</a>
</p>

---

**IntellegyHUB** is an expandable hardware platform for Home Assistant that brings wired sensors, Modbus devices, relays, lighting, digital signals, expansion modules, and wireless devices into one local automation system.

It is built around a simple idea: **people should define the outcome, not repeat the same actions every day.**

Home Assistant provides the intelligence. IntellegyHUB connects that intelligence to the physical world.

> **Wired where reliability matters. Wireless where flexibility matters. Home Assistant brings it all together.**

## Why IntellegyHUB

| | |
|---|---|
| **Local by design** | Core control stays on the installation. No mandatory cloud service is required for connected equipment to operate with Home Assistant. |
| **Wired-first reliability** | Stable wired interfaces provide a dependable foundation for devices that need predictable, continuous operation. |
| **One automation layer** | Industrial devices, sensors, I/O, lighting, expansion modules, and wireless devices can participate in the same Home Assistant logic. |
| **Built to expand** | Start with what the project needs today and add more I/O or communication capabilities as the installation grows. |

The goal is not to create more buttons or another isolated smart-home ecosystem.

The goal is to build a space that can **observe, react, handle routine work automatically, and ask for human attention only when it is actually needed.**

---

## Connect the physical world

### RS-485 / Modbus RTU

Two independent **RS-485 channels** connect Modbus RTU equipment such as meters, sensors, relay modules, actuators, I/O modules, and other automation devices.

Each channel operates independently, allowing equipment to be separated between different parts of the installation and managed according to the needs of the project.

IntellegyHUB handles device polling, state updates, commands, and availability monitoring so Modbus equipment becomes part of the wider Home Assistant environment instead of a separate subsystem.

### 1-Wire

Two independent **1-Wire channels** provide a reliable wired foundation for temperature monitoring across rooms, heating systems, technical areas, and equipment.

### X-Port

**X-Port** is designed to adapt the controller to the project instead of forcing the project to adapt to fixed I/O.

Depending on the task, an X-Port can be used for buttons and sensors, equipment-state monitoring, load and lighting control, dimming, analog signals, or pulse counting.

**The same controller can serve very different spaces without being locked into one predefined I/O layout.**

---

## Expand when the project grows

IntellegyHUB is designed to grow with the installation.

- **XDO-8** adds eight relay outputs for lighting, valves, actuators, contactors, and other equipment.
- **XDI-16** adds sixteen digital inputs for buttons, switches, status contacts, sensors, and engineering-system signals.
- **Communication expansion** allows additional communication capabilities to be added, including a Zigbee coordinator.

This makes it possible to combine reliable wired infrastructure with the flexibility of wireless devices inside one system.

---

## Built around Home Assistant

IntellegyHUB does not replace Home Assistant with another closed control environment.

It gives Home Assistant reliable access to the physical world.

Connected equipment is exposed through familiar **native Home Assistant entities**, allowing devices to participate naturally in:

- dashboards;
- automations;
- scenes;
- history and graphs;
- notifications;
- climate and energy logic;
- presence-based behavior;
- cross-device scenarios.

A relay remains a relay.  
A temperature sensor remains a temperature sensor.  
Lighting remains lighting.

The user works with **what a device does**, not with the protocol behind it.

The IntellegyHUB app and Home Assistant integration communicate locally through **REST and WebSocket**, combining configuration, control, and real-time state updates.

---

## Local first. Cloud optional.

Essential automation should not stop because an internet connection or an external service becomes unavailable.

IntellegyHUB is designed so that communication with connected equipment and its integration with Home Assistant remain **local**.

Cloud services can still be used for remote access or additional functionality when desired, but they are not a mandatory link between Home Assistant and the equipment controlled by IntellegyHUB.

That means **fast local response, fewer external dependencies, and greater control over the automation system itself.**

---

## A system that watches itself

A useful automation platform should do more than send commands.

IntellegyHUB is designed to continuously monitor connected equipment, maintain current states, and track device availability.

The platform provides:

- automatic device polling;
- independent operation of RS-485 channels;
- independent polling, read, and write behavior for connected devices;
- device availability monitoring;
- configuration persistence and state recovery after restart;
- RS-485 diagnostics;
- Modbus traffic logging for commissioning and troubleshooting.

If something stops responding, the system should know about it.

If communication returns, the system should recover cleanly.

**Automation should reduce the amount of attention a system requires — not create another system that needs constant supervision.**

---

## Built-in operational tools

IntellegyHUB also provides local controller functions for everyday operation and diagnostics:

- **System status LEDs** for immediate visual feedback;
- **Buzzer** with configurable frequency, duration, and volume;
- **RTC clock** with time viewing and synchronization;
- persistent configuration and recovery after restart.

---

## Installation

> **Requirements:** Home Assistant OS, compatible IntellegyHUB hardware, and an `aarch64` system.

### 1. Add the repository

In Home Assistant, open:

`Settings → Apps → Install app → ⋮ → Repositories`

Add:

```text
https://github.com/Algri/intellegyhub-home-assistant
```

### 2. Install IntellegyHUB

After adding the repository, find **IntellegyHUB** in the App Store, install it, and start the app.

### 3. Restart Home Assistant

The IntellegyHUB app contains the companion Home Assistant integration and installs or updates it when the app starts.

Restart Home Assistant after the integration has been installed or updated so Home Assistant can discover it.

### 4. Add the integration

Open:

`Settings → Devices & services → Add integration → IntellegyHUB`

Once configured, supported IntellegyHUB hardware and connected devices become available through native Home Assistant entities.

---

## Documentation

| Resource | Description |
|---|---|
| [Platform overview](intellegyhub/README.md) | IntellegyHUB product philosophy, capabilities, and platform overview |
| [Changelog](intellegyhub/CHANGELOG.md) | Release history and current software changes |
| [Technical runtime notes](intellegyhub/DOCS.md) | Low-level development and runtime information |

For bugs and feature requests, use [GitHub Issues](https://github.com/Algri/intellegyhub-home-assistant/issues).

---

## One space. One automation layer.

The value of automation does not come from the number of connected devices.

It comes from what happens when those devices start working together.

A temperature sensor can influence climate control.  
A window can change heating behavior.  
Presence can control lighting.  
A meter can affect energy logic.  
A button can trigger an entire scenario.  
An engineering system can report a problem before the user notices it.

Wired and wireless devices stop being separate technologies and become **inputs and actions inside the same intelligent space**.

---

<p align="center">
  <strong>IntellegyHUB — build spaces you do not have to constantly manage.</strong>
</p>

<p align="center">
  <a href="https://intellegyhub.com"><strong>intellegyhub.com</strong></a>
</p>
