"""Base entities and the helper that adds entities as printers and tools appear."""

from __future__ import annotations

from collections.abc import Callable, Iterable

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import PrusaConnectConfigEntry, PrusaConnectCoordinator
from .library import Device, Tool


def tool_placeholders(tool: Tool) -> dict[str, str]:
    """Name placeholders for a tool: 1-based, as a printer's screen counts.

    The unique id keeps the 0-based id the server uses.
    """
    placeholders = {"tool": str(tool.head + 1)}
    if tool.slot is not None:
        placeholders["slot"] = str(tool.slot + 1)
    return placeholders


def printer_device_info(coordinator: PrusaConnectCoordinator, device: Device) -> DeviceInfo:
    return DeviceInfo(
        identifiers={coordinator.device_identifier(device.id)},
        name=device.name,
        manufacturer=device.vendor or None,
        model=device.model or None,
        serial_number=device.serial or None,
        sw_version=device.firmware or None,
        via_device=coordinator.hub_identifier,
    )


class PrusaConnectEntity(CoordinatorEntity[PrusaConnectCoordinator]):
    """An entity of one printer."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: PrusaConnectCoordinator, device: Device, key: str) -> None:
        super().__init__(coordinator)
        self.device_id = device.id
        self._attr_unique_id = f"{coordinator.instance_id}:{device.id}:{key}"
        self._attr_device_info = printer_device_info(coordinator, device)

    @property
    def device(self) -> Device | None:
        return self.coordinator.data.devices.get(self.device_id)

    @property
    def available(self) -> bool:
        device = self.device
        return super().available and device is not None and device.connected


class PrusaConnectToolEntity(PrusaConnectEntity):
    """An entity of one head or feeder slot, keyed by its 0-based tool id."""

    def __init__(self, coordinator: PrusaConnectCoordinator, device: Device, tool: Tool, key: str) -> None:
        super().__init__(coordinator, device, f"tool:{tool.id}:{key}")
        self.tool_id = tool.id
        self._attr_translation_placeholders = tool_placeholders(tool)

    @property
    def tool(self) -> Tool | None:
        device = self.device
        if device is None:
            return None
        tool = device.tools.tools.get(self.tool_id)
        return tool if tool is not None and tool.enabled else None

    @property
    def available(self) -> bool:
        return super().available and self.tool is not None


EntityFactory = Callable[[PrusaConnectCoordinator, Device], Iterable[Entity]]


def async_add_entities_as_found(
    entry: PrusaConnectConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
    factory: EntityFactory,
) -> None:
    """Add the entities of every printer and tool, now and whenever new ones appear.

    An entity whose printer or tool goes away stays registered and becomes
    unavailable; a printer that leaves the server is removed with its device.
    """
    coordinator = entry.runtime_data
    known: set[str] = set()

    def _add() -> None:
        new: list[Entity] = []
        for device in coordinator.data.devices.values():
            for entity in factory(coordinator, device):
                if entity.unique_id is not None and entity.unique_id not in known:
                    known.add(entity.unique_id)
                    new.append(entity)
        if new:
            async_add_entities(new)

    _add()
    entry.async_on_unload(coordinator.async_add_listener(_add))


def enabled_heads(device: Device) -> list[Tool]:
    return [t for t in device.tools.heads if t.enabled]


def enabled_slots(device: Device, head: Tool) -> list[Tool]:
    return [t for t in device.tools.slots(head.head) if t.enabled]
