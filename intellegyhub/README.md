# IntellegyHUB

## Automation that becomes part of the space

**IntellegyHUB** is an expandable hardware platform for Home Assistant, built for reliable intelligent spaces.

A home, apartment, office, workshop, retail space, or technical facility — the location does not matter. The principle is the same: the system should understand what is happening around it, handle repetitive tasks automatically, and involve the user only when a real decision is needed.

Not more buttons.  
Not more apps.  
Not more manual control of every individual device.

**You define how the space should work. IntellegyHUB helps it do the rest.**

---

## From controlling devices to controlling outcomes

Most actions inside a building are predictable.

Lighting is needed under certain conditions.  
Temperature needs to be maintained.  
Equipment should run when it is actually required.  
Engineering systems need to be monitored.  
Events need an appropriate response.

There is no reason for people to repeat the same actions manually every day.

That is where real automation begins.

IntellegyHUB connects Home Assistant to the physical world — sensors, switches, lighting, relays, meters, actuators, and industrial equipment.

Home Assistant provides the logic.

IntellegyHUB connects that logic to real devices.

The user gets what ultimately matters most:

**the desired result without constant interaction.**

This is not simply remote control for a building.

It is a step toward a space that can take care of routine tasks on its own.

---

## A reliable foundation where reliability matters

The more responsibility automation takes on, the more important its foundation becomes.

That is why IntellegyHUB is built around **reliable wired connectivity**.

Not because wireless technology is inherently bad.

But because lighting, climate, engineering systems, and critical sensors should work predictably every day — regardless of Wi-Fi congestion, radio interference, or the state of a battery.

Two independent **RS-485 channels** allow Modbus RTU devices to become part of the system, including meters, sensors, actuators, relay modules, and industrial automation equipment.

Each channel operates independently, allowing devices to be separated across different parts of the installation and configured according to the needs of the project.

Two independent **1-Wire channels** provide a reliable wired foundation for temperature monitoring across rooms, heating systems, and technical equipment.

IntellegyHUB continuously communicates with connected devices, reads their state, sends commands, and monitors availability.

**Wired infrastructure becomes part of one coherent system instead of a collection of isolated integrations.**

---

## Local by design

Your home or building exists here.

Its essential functions should work here too.

IntellegyHUB is designed for **local automation** together with Home Assistant.

Controlling connected equipment does not require a mandatory external cloud service.

The internet can still be used for remote access, external services, and additional functionality — but core automation should not stop simply because a service somewhere outside the building becomes unavailable.

That means:

**fast local response, fewer external dependencies, and greater control over your own system.**

Your equipment, automation logic, and operational data remain where they belong — inside your own infrastructure.

---

## Wired where reliability matters. Wireless where flexibility matters.

A good automation system should not force you to choose one technology for every task.

Each approach has its place.

Permanent equipment can use wired connections for stable communication and minimal maintenance.

Where running cables is impractical, or where devices need to be added quickly, wireless technologies provide flexibility.

IntellegyHUB brings both approaches together.

Internal expansion ports allow additional communication capabilities to be added, including a **Zigbee coordinator**.

As a result, wired sensors, Modbus RTU equipment, physical inputs and outputs, relay modules, and Zigbee devices no longer need to exist as separate systems.

They become parts of **one intelligent space with Home Assistant providing the shared logic**.

The user does not need to care how a device is connected.

What matters is that the system can see it, understand it, and use it in automation.

---

## X-Port — hardware that adapts to the project

No two installations are exactly the same.

One project may need more buttons.

Another may need lighting control.

A third may require sensors, analog signals, or pulse counting.

IntellegyHUB does not try to decide in advance what every channel must be used for.

The universal **X-Port** allows the same hardware resource to serve different purposes depending on the project.

It can work with buttons and sensors, monitor equipment states, control loads and lighting, adjust brightness, process analog signals, or count pulses.

That creates an important advantage:

**the controller adapts to the project — not the project to the controller.**

If the purpose of the space changes, the configuration can change with it.

If a new requirement appears, it can often be handled without replacing the entire hardware platform.

Flexibility remains available not only during design, but throughout the lifetime of the installation.

---

## Expand when the project grows

Automation rarely stays exactly as it was on day one.

New rooms appear.  
More equipment is added.  
New scenarios are introduced.  
The number of inputs and outputs grows.

IntellegyHUB is designed for that from the beginning.

The **XDO-8** expansion module adds eight relay outputs for controlling lighting, valves, actuators, contactors, and other equipment.

The **XDI-16** adds sixteen digital inputs for buttons, switches, status sensors, and signals from engineering systems.

Additional communication capabilities can be added through internal expansion ports.

And the system does not fragment into new isolated islands as it grows.

Expansion modules become part of the same platform and the same Home Assistant environment.

**Start with what you need today. Expand when new requirements appear.**

---

## Home Assistant remains the intelligence layer

IntellegyHUB does not create another closed ecosystem next to Home Assistant.

It gives Home Assistant reliable access to the physical world.

Connected equipment is represented through familiar **native Home Assistant entities**.

A relay appears as a relay.

A digital input appears as a state.

A temperature sensor appears as temperature.

Lighting appears as lighting.

The user does not need to know whether an entity is backed by RS-485, 1-Wire, X-Port, or an expansion module.

They interact with **what the device does**, not with the protocol behind it.

Everything can participate in dashboards, automations, scenes, history, notifications, and the wider Home Assistant logic.

Communication between the IntellegyHUB add-on and the Home Assistant integration stays local through **REST and WebSocket**, providing control and up-to-date device states.

---

## A system that watches itself

Automation should understand more than the current value of a device.

It should also know whether that device is available at all.

IntellegyHUB automatically polls connected equipment, monitors communication, and tracks device availability.

Different devices can use update rates and read/write behavior appropriate to their role.

If a device stops responding, the system knows.

If communication returns, the device can return to normal operation.

After a restart, IntellegyHUB restores its configuration and required operating state.

The user should not have to check every day whether their automation is still working.

**The automation should monitor what it is responsible for.**

---

## Diagnostics without working blind

A reliable system is not one in which nothing ever goes wrong.

A reliable system is one that helps you understand the cause quickly when something does.

IntellegyHUB provides RS-485 diagnostics and a Modbus traffic log that makes communication between the controller and connected devices visible.

This simplifies commissioning new equipment and helps identify communication problems directly on site.

System **LED indicators** provide an immediate view of controller status without additional tools.

The built-in **buzzer** can provide local notifications and confirmations with configurable frequency, duration, and volume.

The **RTC Clock** provides local system time with viewing and synchronization support.

All of these functions serve the same purpose:

**the system should be easy not only to use, but also to operate and maintain.**

---

## Not a collection of devices. One space.

The real value of automation does not appear when many devices are connected.

It appears when those devices **begin to work together**.

A temperature sensor can influence climate control.

A window state can affect heating.

Presence can influence lighting.

Energy tariffs can change equipment behavior.

A single button can trigger an entire scenario instead of one relay.

An engineering system can report a problem before the user notices it.

Wired and wireless devices stop being separate technologies.

They become sources of information and actions inside the shared logic of the space.

That is the moment when automation stops being a collection of gadgets.

**It becomes infrastructure.**

---

## Technology should save attention

Human attention is too valuable to spend every day on the same repetitive actions.

IntellegyHUB is built around a simple idea:

**if an action can be determined reliably and performed automatically, a person should not have to repeat it manually again and again.**

The user defines preferences and boundaries.

Home Assistant provides the intelligence of the space.

IntellegyHUB turns that intelligence into real-world actions.

Lighting.  
Climate.  
Energy.  
Sensors.  
Engineering equipment.  
Security.  
Everyday routines.

Over time, these systems require less and less attention.

The user steps in when they want to change the outcome.

The rest of the time, the system simply works.

---

> # IntellegyHUB
>
> **Reliable wired infrastructure. Local operation. Flexible expansion. Wired and wireless devices working together in one Home Assistant system.**
>
> **Build spaces you do not have to constantly manage.**
