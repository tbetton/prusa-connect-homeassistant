"""Sensors of Prusa Connect printers and their tools."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    REVOLUTIONS_PER_MINUTE,
    EntityCategory,
    UnitOfLength,
    UnitOfMass,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import PRUSA_STATES
from .coordinator import PrusaConnectConfigEntry, PrusaConnectCoordinator
from .entity import (
    PrusaConnectEntity,
    PrusaConnectToolEntity,
    async_add_entities_as_found,
    enabled_heads,
    enabled_slots,
    printer_device_info,
    tool_placeholders,
)
from .library import Device, Tool, parse_tool_id
from .materials import MaterialsCoordinator, ToolSpool

PARALLEL_UPDATES = 0


def _job_value[T](getter: Callable[[Device], T | None]) -> Callable[[Device], T | None]:
    """A job reading, or nothing while the printer carries no print."""
    return lambda device: getter(device) if device.has_job else None


def _finish_time(device: Device) -> datetime | None:
    remaining = device.job_time_remaining
    if not device.has_job or remaining is None:
        return None
    # Rounded to the minute, so the estimate does not move every second.
    finish = dt_util.utcnow() + timedelta(seconds=remaining)
    return finish.replace(second=0, microsecond=0)


@dataclass(frozen=True, kw_only=True)
class PrinterSensorDescription(SensorEntityDescription):
    value_fn: Callable[[Device], Any]
    exists_fn: Callable[[Device], bool] = lambda _: True


@dataclass(frozen=True, kw_only=True)
class ToolSensorDescription(SensorEntityDescription):
    value_fn: Callable[[Tool], Any]
    exists_fn: Callable[[Device, Tool], bool] = lambda _d, _t: True


PRINTER_SENSORS: tuple[PrinterSensorDescription, ...] = (
    PrinterSensorDescription(
        key="progress",
        translation_key="progress",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=_job_value(lambda d: d.job_progress),
    ),
    PrinterSensorDescription(
        key="time_remaining",
        translation_key="time_remaining",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_unit_of_measurement=UnitOfTime.MINUTES,
        suggested_display_precision=0,
        value_fn=_job_value(lambda d: d.job_time_remaining),
    ),
    PrinterSensorDescription(
        key="time_printing",
        translation_key="time_printing",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_unit_of_measurement=UnitOfTime.MINUTES,
        suggested_display_precision=0,
        entity_registry_enabled_default=False,
        value_fn=_job_value(lambda d: d.job_time_printing),
    ),
    PrinterSensorDescription(
        key="finish_time",
        translation_key="finish_time",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=_finish_time,
    ),
    PrinterSensorDescription(
        key="job_name",
        translation_key="job_name",
        value_fn=_job_value(lambda d: d.job_name),
    ),
    PrinterSensorDescription(
        key="bed_temperature",
        translation_key="bed_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda d: d.bed_temp,
        exists_fn=lambda d: d.bed_temp is not None,
    ),
    PrinterSensorDescription(
        key="chamber_temperature",
        translation_key="chamber_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda d: d.chamber_temp,
        exists_fn=lambda d: d.chamber_temp is not None,
    ),
    *(
        PrinterSensorDescription(
            key=f"chamber_fan_{fan}_speed",
            translation_key=f"chamber_fan_{fan}_speed",
            native_unit_of_measurement=REVOLUTIONS_PER_MINUTE,
            state_class=SensorStateClass.MEASUREMENT,
            entity_category=EntityCategory.DIAGNOSTIC,
            value_fn=lambda d, fan=fan: d.chamber_fan_rpm(fan),  # type: ignore[misc]
            exists_fn=lambda d, fan=fan: d.chamber_fan_rpm(fan) is not None,  # type: ignore[misc]
        )
        for fan in (1, 2)
    ),
    *(
        PrinterSensorDescription(
            key=f"axis_{axis}",
            translation_key=f"axis_{axis}",
            device_class=SensorDeviceClass.DISTANCE,
            native_unit_of_measurement=UnitOfLength.MILLIMETERS,
            state_class=SensorStateClass.MEASUREMENT,
            suggested_display_precision=1,
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
            value_fn=lambda d, axis=axis: d.axis(axis),  # type: ignore[misc]
            exists_fn=lambda d, axis=axis: d.axis(axis) is not None,  # type: ignore[misc]
        )
        for axis in ("x", "y", "z")
    ),
)


def _material(tool: Tool) -> str | None:
    # An empty abbreviation is a tool with nothing loaded.
    return tool.material or None


HEAD_SENSORS: tuple[ToolSensorDescription, ...] = (
    ToolSensorDescription(
        key="nozzle_temperature",
        translation_key="nozzle_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda t: t.nozzle_temp,
    ),
    ToolSensorDescription(
        key="material",
        translation_key="material",
        value_fn=_material,
        # A head with a feeder prints the material of the loaded slot, which
        # the slot's own sensor reports.
        exists_fn=lambda _d, t: t.feeder is None and t.material is not None,
    ),
    ToolSensorDescription(
        key="nozzle_diameter",
        translation_key="nozzle_diameter",
        device_class=SensorDeviceClass.DISTANCE,
        native_unit_of_measurement=UnitOfLength.MILLIMETERS,
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda t: t.nozzle_diameter,
        exists_fn=lambda _d, t: t.nozzle_diameter is not None,
    ),
    ToolSensorDescription(
        key="print_fan_speed",
        translation_key="print_fan_speed",
        native_unit_of_measurement=REVOLUTIONS_PER_MINUTE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda t: t.print_fan_rpm,
        exists_fn=lambda _d, t: t.print_fan_rpm is not None,
    ),
    ToolSensorDescription(
        key="heatbreak_fan_speed",
        translation_key="heatbreak_fan_speed",
        native_unit_of_measurement=REVOLUTIONS_PER_MINUTE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda t: t.heatbreak_fan_rpm,
        exists_fn=lambda _d, t: t.heatbreak_fan_rpm is not None,
    ),
    ToolSensorDescription(
        key="feeder_progress",
        translation_key="feeder_progress",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda t: t.feeder.mmu_progress_code if t.feeder else None,
        exists_fn=lambda _d, t: t.feeder is not None,
    ),
    ToolSensorDescription(
        key="feeder_command",
        translation_key="feeder_command",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda t: t.feeder.mmu_command_in_progress if t.feeder else None,
        exists_fn=lambda _d, t: t.feeder is not None,
    ),
)

SLOT_SENSORS: tuple[ToolSensorDescription, ...] = (
    ToolSensorDescription(
        key="material",
        translation_key="slot_material",
        value_fn=_material,
    ),
)


def _entities(coordinator: PrusaConnectCoordinator, device: Device) -> Iterator[Entity]:
    yield PrinterStateSensor(coordinator, device)
    yield StartTimeSensor(coordinator, device)
    for description in PRINTER_SENSORS:
        if description.exists_fn(device):
            yield PrinterSensor(coordinator, device, description)
    for head in enabled_heads(device):
        for tool_description in HEAD_SENSORS:
            if tool_description.exists_fn(device, head):
                yield ToolSensor(coordinator, device, head, tool_description)
        for slot in enabled_slots(device, head):
            for tool_description in SLOT_SENSORS:
                if tool_description.exists_fn(device, slot):
                    yield ToolSensor(coordinator, device, slot, tool_description)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PrusaConnectConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the sensors."""
    async_add_entities_as_found(entry, async_add_entities, _entities)
    coordinator = entry.runtime_data
    materials = coordinator.materials
    if materials is None:
        return
    known: set[tuple[str, str]] = set()

    @callback
    def _add_spools() -> None:
        new: list[SpoolSensor] = []
        for key in materials.data or {}:
            device = coordinator.data.devices.get(key[0])
            parsed = parse_tool_id(key[1])
            if key in known or device is None or parsed is None:
                continue
            known.add(key)
            new.append(SpoolSensor(coordinator, materials, device, key[1]))
        if new:
            async_add_entities(new)

    _add_spools()
    entry.async_on_unload(materials.async_add_listener(_add_spools))


class PrinterSensor(PrusaConnectEntity, SensorEntity):
    entity_description: PrinterSensorDescription

    def __init__(
        self,
        coordinator: PrusaConnectCoordinator,
        device: Device,
        description: PrinterSensorDescription,
    ) -> None:
        super().__init__(coordinator, device, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        device = self.device
        return self.entity_description.value_fn(device) if device else None


class ToolSensor(PrusaConnectToolEntity, SensorEntity):
    entity_description: ToolSensorDescription

    def __init__(
        self,
        coordinator: PrusaConnectCoordinator,
        device: Device,
        tool: Tool,
        description: ToolSensorDescription,
    ) -> None:
        super().__init__(coordinator, device, tool, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        tool = self.tool
        return self.entity_description.value_fn(tool) if tool else None


class PrinterStateSensor(PrusaConnectEntity, SensorEntity):
    """The machine state, translated for the states Prusa printers report.

    Another vendor's driver may report states outside that set. They are kept
    as the machine spells them, lower-cased, rather than dropped.
    """

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_translation_key = "state"

    def __init__(self, coordinator: PrusaConnectCoordinator, device: Device) -> None:
        super().__init__(coordinator, device, "state")

    @property
    def native_value(self) -> str | None:
        device = self.device
        state = device.printer_state if device else None
        return state.lower() if state else None

    @property
    def options(self) -> list[str]:
        options = set(PRUSA_STATES)
        options.update(s.lower() for s in self.coordinator.vocabulary.states)
        if (current := self.native_value) is not None:
            options.add(current)
        return sorted(options)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        device = self.device
        volume = device.build_volume if device else None
        if volume is None:
            return None
        return {
            "build_volume_x": volume.x_mm,
            "build_volume_y": volume.y_mm,
            "build_volume_z": volume.z_mm,
        }


class StartTimeSensor(PrusaConnectEntity, SensorEntity):
    """When the current print started.

    The printer reports how long the print has run; the start is worked out
    once per print and kept, so it does not move with every reading.
    """

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_translation_key = "start_time"

    def __init__(self, coordinator: PrusaConnectCoordinator, device: Device) -> None:
        super().__init__(coordinator, device, "start_time")
        self._job: tuple[int | None, str | None] | None = None
        self._start: datetime | None = None

    @property
    def native_value(self) -> datetime | None:
        device = self.device
        if device is None or not device.has_job:
            return None
        job = (device.job_id, device.job_path)
        if job != self._job or self._start is None:
            elapsed = device.job_time_printing
            if elapsed is None:
                return None
            start = dt_util.utcnow() - timedelta(seconds=elapsed)
            self._job, self._start = job, start.replace(second=0, microsecond=0)
        return self._start


def _color_hex(rgba: str | None) -> str | None:
    return rgba[:7] if rgba and rgba.startswith("#") and len(rgba) >= 7 else None


class SpoolSensor(CoordinatorEntity[MaterialsCoordinator], SensorEntity):
    """The filament left on the spool a tool is loaded with, from the Materials extension.

    Without a spool the extension weighs, the state is unknown and the
    attributes describe the material the tool is set to.
    """

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.WEIGHT
    _attr_native_unit_of_measurement = UnitOfMass.GRAMS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0

    def __init__(
        self,
        printers: PrusaConnectCoordinator,
        materials: MaterialsCoordinator,
        device: Device,
        tool_id: str,
    ) -> None:
        super().__init__(materials)
        self._printers = printers
        self._key = (device.id, tool_id)
        self._attr_unique_id = f"{printers.instance_id}:{device.id}:tool:{tool_id}:spool"
        self._attr_device_info = printer_device_info(printers, device)
        head, slot = parse_tool_id(tool_id) or (0, None)
        self._attr_translation_key = "spool" if slot is None else "slot_spool"
        self._attr_translation_placeholders = tool_placeholders(Tool(id=tool_id, head=head, slot=slot))

    @property
    def _loaded(self) -> ToolSpool | None:
        return (self.coordinator.data or {}).get(self._key)

    @property
    def available(self) -> bool:
        return super().available and self._loaded is not None

    @property
    def native_value(self) -> float | None:
        loaded = self._loaded
        spool = loaded.spool if loaded else None
        if spool is None or spool.remaining_weight_g is None:
            return None
        return max(0.0, spool.remaining_weight_g)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        loaded = self._loaded
        if loaded is None:
            return None
        spool, assignment = loaded.spool, loaded.assignment
        if spool is None:
            return {
                "spool_id": None,
                "brand": assignment.brand,
                "name": assignment.material_name,
                "type": assignment.type,
                "color_hex": _color_hex(assignment.color_rgba),
                "color_rgba": assignment.color_rgba,
            }
        net = spool.net_weight_g
        remaining = spool.remaining_weight_g
        return {
            "spool_id": spool.id,
            "brand": spool.brand,
            "name": spool.material_name,
            "type": spool.type,
            "color_hex": _color_hex(spool.color_rgba),
            "color_rgba": spool.color_rgba,
            "initial_weight": net,
            "percent": round(100 * max(0.0, remaining) / net) if net and remaining is not None else None,
            "full_weight": spool.full_weight_g,
            "empty_weight": spool.empty_weight_g,
            "location": spool.location,
            "notes": spool.notes,
            "material_uuid": spool.material_uuid,
        }
