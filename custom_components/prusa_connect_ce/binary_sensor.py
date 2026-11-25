"""Binary sensors of Prusa Connect printers and their tools."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import PrusaConnectConfigEntry, PrusaConnectCoordinator
from .entity import (
    PrusaConnectEntity,
    PrusaConnectToolEntity,
    async_add_entities_as_found,
    enabled_heads,
    enabled_slots,
)
from .library import Device, Tool

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class ToolBinarySensorDescription(BinarySensorEntityDescription):
    value_fn: Callable[[Tool], bool | None]


ACTIVE = ToolBinarySensorDescription(
    key="active",
    translation_key="active",
    value_fn=lambda t: t.active,
)
SLOT_ACTIVE = ToolBinarySensorDescription(
    key="active",
    translation_key="slot_active",
    value_fn=lambda t: t.active,
)
HEAD_FLAGS: tuple[ToolBinarySensorDescription, ...] = (
    ToolBinarySensorDescription(
        key="nozzle_high_flow",
        translation_key="nozzle_high_flow",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda t: t.nozzle_high_flow,
    ),
    ToolBinarySensorDescription(
        key="nozzle_hardened",
        translation_key="nozzle_hardened",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda t: t.nozzle_hardened,
    ),
)


def _entities(coordinator: PrusaConnectCoordinator, device: Device) -> Iterator[Entity]:
    yield ConnectedBinarySensor(coordinator, device)
    for head in enabled_heads(device):
        if head.active is not None:
            yield ToolBinarySensor(coordinator, device, head, ACTIVE)
        for description in HEAD_FLAGS:
            if description.value_fn(head) is not None:
                yield ToolBinarySensor(coordinator, device, head, description)
        for slot in enabled_slots(device, head):
            yield ToolBinarySensor(coordinator, device, slot, SLOT_ACTIVE)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PrusaConnectConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the binary sensors."""
    async_add_entities_as_found(entry, async_add_entities, _entities)


class ConnectedBinarySensor(PrusaConnectEntity, BinarySensorEntity):
    """Whether the printer holds a session with the server.

    It stays available while the printer is away, which is what it reports.
    """

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: PrusaConnectCoordinator, device: Device) -> None:
        super().__init__(coordinator, device, "connected")

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success and self.device is not None

    @property
    def is_on(self) -> bool | None:
        device = self.device
        return device.connected if device else None


class ToolBinarySensor(PrusaConnectToolEntity, BinarySensorEntity):
    entity_description: ToolBinarySensorDescription

    def __init__(
        self,
        coordinator: PrusaConnectCoordinator,
        device: Device,
        tool: Tool,
        description: ToolBinarySensorDescription,
    ) -> None:
        super().__init__(coordinator, device, tool, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        tool = self.tool
        return self.entity_description.value_fn(tool) if tool else None
