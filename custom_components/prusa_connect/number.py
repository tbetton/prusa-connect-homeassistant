"""Numbers for temperature targets and the print speed."""

from __future__ import annotations

from collections.abc import Callable, Coroutine, Iterator
from dataclasses import dataclass
from typing import Any

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.const import PERCENTAGE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import PrusaConnectConfigEntry, PrusaConnectCoordinator
from .entity import (
    PrusaConnectEntity,
    PrusaConnectToolEntity,
    async_add_entities_as_found,
    enabled_heads,
)
from .helpers import command
from .library import (
    ARG_TOOL,
    VERB_SET_BED_TEMPERATURE,
    VERB_SET_CHAMBER_TEMPERATURE,
    VERB_SET_FLOW,
    VERB_SET_NOZZLE_TEMPERATURE,
    VERB_SET_SPEED,
    Device,
    PrusaConnect,
    Tool,
)

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class PrinterNumberDescription(NumberEntityDescription):
    verb: str
    value_fn: Callable[[Device], float | None]
    set_fn: Callable[[PrusaConnect, Device, float], Coroutine[Any, Any, Any]]
    # The model's own ceiling, where it differs from native_max_value.
    max_fn: Callable[[Device], float] | None = None


BED_TARGET = PrinterNumberDescription(
    key="bed_target_temperature",
    translation_key="bed_target_temperature",
    device_class=NumberDeviceClass.TEMPERATURE,
    native_unit_of_measurement=UnitOfTemperature.CELSIUS,
    native_min_value=0,
    native_max_value=120,
    native_step=1,
    mode=NumberMode.BOX,
    verb=VERB_SET_BED_TEMPERATURE,
    value_fn=lambda d: d.bed_target_temp,
    set_fn=lambda client, device, value: client.set_bed_temperature(device, value),
)
SPEED = PrinterNumberDescription(
    key="speed",
    translation_key="speed",
    native_unit_of_measurement=PERCENTAGE,
    native_min_value=20,
    native_max_value=999,
    native_step=1,
    mode=NumberMode.BOX,
    verb=VERB_SET_SPEED,
    value_fn=lambda d: d.speed,
    set_fn=lambda client, device, value: client.set_speed(device, value),
)
FLOW = PrinterNumberDescription(
    key="flow",
    translation_key="flow",
    native_unit_of_measurement=PERCENTAGE,
    native_min_value=20,
    native_max_value=999,
    native_step=1,
    mode=NumberMode.BOX,
    verb=VERB_SET_FLOW,
    value_fn=lambda d: d.flow,
    set_fn=lambda client, device, value: client.set_flow(device, value),
)
# The chamber has fans and no heater, so its target is the temperature the
# fans hold the air under, and only while they run on their own. The printer
# takes up to 55 C on a CORE One and up to 60 C on a CORE One L.
CHAMBER_TARGET = PrinterNumberDescription(
    key="chamber_target_temperature",
    translation_key="chamber_target_temperature",
    device_class=NumberDeviceClass.TEMPERATURE,
    native_unit_of_measurement=UnitOfTemperature.CELSIUS,
    native_min_value=0,
    native_max_value=60,
    native_step=1,
    mode=NumberMode.BOX,
    verb=VERB_SET_CHAMBER_TEMPERATURE,
    value_fn=lambda d: d.chamber_target_temp,
    set_fn=lambda client, device, value: client.set_chamber_temperature(device, value),
    max_fn=lambda d: d.chamber_max_temp,
)
NOZZLE_TARGET = NumberEntityDescription(
    key="nozzle_target_temperature",
    translation_key="nozzle_target_temperature",
    device_class=NumberDeviceClass.TEMPERATURE,
    native_unit_of_measurement=UnitOfTemperature.CELSIUS,
    native_min_value=0,
    native_max_value=300,
    native_step=1,
    mode=NumberMode.BOX,
)


def _addressable(device: Device, head: Tool) -> bool:
    """Whether a nozzle target can be sent to this head.

    A verb without a tool argument heats the one head a printer has.
    """
    return ARG_TOOL in device.arguments(VERB_SET_NOZZLE_TEMPERATURE) or head.head == 0


def _entities(coordinator: PrusaConnectCoordinator, device: Device) -> Iterator[Entity]:
    if not device.may("operate"):
        return
    for description in (BED_TARGET, SPEED, FLOW, CHAMBER_TARGET):
        if device.supports(description.verb) and description.value_fn(device) is not None:
            yield PrinterNumber(coordinator, device, description)
    if device.supports(VERB_SET_NOZZLE_TEMPERATURE):
        for head in enabled_heads(device):
            if _addressable(device, head):
                yield NozzleTargetNumber(coordinator, device, head)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PrusaConnectConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the numbers."""
    async_add_entities_as_found(entry, async_add_entities, _entities)


class PrinterNumber(PrusaConnectEntity, NumberEntity):
    entity_description: PrinterNumberDescription

    def __init__(
        self,
        coordinator: PrusaConnectCoordinator,
        device: Device,
        description: PrinterNumberDescription,
    ) -> None:
        super().__init__(coordinator, device, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> float | None:
        device = self.device
        return self.entity_description.value_fn(device) if device else None

    @property
    def native_max_value(self) -> float:
        device = self.device
        max_fn = self.entity_description.max_fn
        if device is not None and max_fn is not None:
            return max_fn(device)
        return super().native_max_value

    @command
    async def async_set_native_value(self, value: float) -> None:
        device = self.device
        if device is not None:
            await self.entity_description.set_fn(self.coordinator.client, device, value)


class NozzleTargetNumber(PrusaConnectToolEntity, NumberEntity):
    """A head's nozzle target. ``0`` is a heater that is off."""

    entity_description = NOZZLE_TARGET

    def __init__(self, coordinator: PrusaConnectCoordinator, device: Device, head: Tool) -> None:
        super().__init__(coordinator, device, head, NOZZLE_TARGET.key)

    @property
    def native_value(self) -> float | None:
        tool = self.tool
        return tool.nozzle_target_temp if tool else None

    @command
    async def async_set_native_value(self, value: float) -> None:
        device = self.device
        if device is not None:
            await self.coordinator.client.set_nozzle_temperature(device, value, tool=self.tool_id)
