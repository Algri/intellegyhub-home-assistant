from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from homeassistant.const import Platform
from homeassistant.core import callback
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    XPORT_MODE_AI,
    XPORT_MODE_COUNTER,
    XPORT_MODE_DI,
    XPORT_MODE_DO,
    XPORT_MODE_PWM,
)

T = TypeVar("T", bound=Entity)

XPORT_ENTITY_KINDS: tuple[tuple[Platform, str], ...] = (
    (Platform.SWITCH, "do"),
    (Platform.BINARY_SENSOR, "di"),
    (Platform.SENSOR, "ai"),
    (Platform.SENSOR, "counter"),
    (Platform.NUMBER, "pwm"),
    (Platform.BUTTON, "counter_reset"),
)


def xport_entity_unique_id(channel: int, suffix: str) -> str:
    return f"intellegyhub_xport_x{channel}_{suffix}"


def xport_platform_value(platform: Platform) -> str:
    return str(getattr(platform, "value", platform))


def xport_desired_entity_kinds(mode: str | None) -> set[tuple[str, str]]:
    if mode == XPORT_MODE_DO:
        return {(xport_platform_value(Platform.SWITCH), "do")}
    if mode == XPORT_MODE_PWM:
        return {(xport_platform_value(Platform.NUMBER), "pwm")}
    if mode == XPORT_MODE_AI:
        return {(xport_platform_value(Platform.SENSOR), "ai")}
    if mode in XPORT_MODE_DI:
        return {(xport_platform_value(Platform.BINARY_SENSOR), "di")}
    if mode in XPORT_MODE_COUNTER:
        return {
            (xport_platform_value(Platform.SENSOR), "counter"),
            (xport_platform_value(Platform.BUTTON), "counter_reset"),
        }
    return set()


def xport_desired_unique_ids(channel: int, mode: str | None) -> set[tuple[str, str]]:
    return {
        (platform, xport_entity_unique_id(channel, suffix))
        for platform, suffix in xport_desired_entity_kinds(mode)
    }


def xport_desired_channels_for_suffix(manager, platform: Platform, suffix: str) -> set[int]:
    if not hasattr(manager, "xport_channel"):
        return set()
    platform_value = xport_platform_value(platform)
    channels: set[int] = set()
    for channel in range(1, 5):
        mode = manager.xport_channel(channel).get("confirmed_mode")
        if (platform_value, suffix) in xport_desired_entity_kinds(mode):
            channels.add(channel)
    return channels


def setup_xport_dynamic_platform(
    entry,
    manager,
    async_add_entities: AddEntitiesCallback,
    platform: Platform,
    suffix: str,
    entity_factory: Callable[[int], T],
) -> None:
    known: dict[int, T] = {}

    @callback
    def sync_entities() -> None:
        desired = xport_desired_channels_for_suffix(manager, platform, suffix)

        for channel in sorted(desired - set(known)):
            entity = entity_factory(channel)
            known[channel] = entity
            async_add_entities([entity])

        stale_channels = set(known) - desired
        for channel in sorted(stale_channels):
            entity = known.pop(channel)
            manager.hass.async_create_task(
                entity.async_remove(force_remove=True),
                f"intellegyhub_remove_xport_x{channel}_{suffix}",
            )

    entry.async_on_unload(manager.async_add_listener(sync_entities))
    sync_entities()
