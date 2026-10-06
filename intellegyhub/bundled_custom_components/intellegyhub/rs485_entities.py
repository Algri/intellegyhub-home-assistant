from __future__ import annotations

from collections.abc import Callable

from homeassistant.const import Platform
from homeassistant.core import callback
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback


def rs485_capabilities(manager, device: dict, kind: str) -> list[dict]:
    rs485 = getattr(manager, "rs485", {})
    template = next((item for item in rs485.get("template_details", []) if item.get("template_id") == device.get("template_id")), {})
    return [
        {"id": capability_id, **capability}
        for capability_id, capability in (template.get("capabilities") or {}).items()
        if capability.get("type") == kind
        and (kind != "select" or capability.get("group") == "control_modes")
    ]


def setup_rs485_dynamic_platform(entry, manager, async_add_entities: AddEntitiesCallback, kind: str, factory: Callable[[str, str], Entity]) -> None:
    # RS-485 is an independent transport. Test doubles and managers that do
    # not expose the RS-485 subsystem must not enter this dynamic flow.
    if not hasattr(manager, "rs485"):
        return
    known: dict[tuple[str, str], Entity] = {}

    @callback
    def sync_entities() -> None:
        desired = {
            (device.get("id"), capability["id"])
            for device in getattr(manager, "rs485", {}).get("devices", [])
            if isinstance(device.get("id"), str)
            for capability in rs485_capabilities(manager, device, kind)
        }
        for key in sorted(desired - set(known)):
            entity = factory(*key)
            known[key] = entity
            async_add_entities([entity])
        for key in sorted(set(known) - desired):
            entity = known.pop(key)
            manager.hass.async_create_task(entity.async_remove(force_remove=True), f"intellegyhub_remove_rs485_{key[0]}_{key[1]}")

    entry.async_on_unload(manager.async_add_listener(sync_entities))
    sync_entities()
