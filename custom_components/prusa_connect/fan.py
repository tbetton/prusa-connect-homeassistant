"""The chamber fans of printers that have them."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import PrusaConnectConfigEntry, PrusaConnectCoordinator
from .entity import PrusaConnectEntity, async_add_entities_as_found
from .helpers import command
from .library import CHAMBER_FAN_AUTO, VERB_SET_CHAMBER_FAN_TARGET, Device

PARALLEL_UPDATES = 1

PRESET_AUTO = "auto"


def _entities(coordinator: PrusaConnectCoordinator, device: Device) -> Iterator[Entity]:
    if (
        device.may("operate")
        and device.supports(VERB_SET_CHAMBER_FAN_TARGET)
        and device.chamber_fan_target is not None
    ):
        yield ChamberFan(coordinator, device)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PrusaConnectConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the chamber fans."""
    async_add_entities_as_found(entry, async_add_entities, _entities)


class ChamberFan(PrusaConnectEntity, FanEntity):
    """The chamber fans' target: a fixed speed, or the printer's own control (*auto*)."""

    _attr_translation_key = "chamber_fan"
    _attr_supported_features = (
        FanEntityFeature.SET_SPEED
        | FanEntityFeature.PRESET_MODE
        | FanEntityFeature.TURN_ON
        | FanEntityFeature.TURN_OFF
    )
    _attr_speed_count = 100

    def __init__(self, coordinator: PrusaConnectCoordinator, device: Device) -> None:
        super().__init__(coordinator, device, "chamber_fan")
        self._attr_preset_modes = [PRESET_AUTO]

    @property
    def _target(self) -> int | None:
        device = self.device
        return device.chamber_fan_target if device else None

    @property
    def is_on(self) -> bool | None:
        target = self._target
        return None if target is None else target != 0

    @property
    def percentage(self) -> int | None:
        target = self._target
        return target if target is not None and target >= 0 else None

    @property
    def preset_mode(self) -> str | None:
        return PRESET_AUTO if self._target == CHAMBER_FAN_AUTO else None

    async def _set(self, percent: float | None) -> None:
        device = self.device
        if device is not None:
            await self.coordinator.client.set_chamber_fan(device, percent)

    @command
    async def async_turn_on(
        self, /, percentage: int | None = None, preset_mode: str | None = None, **kwargs: Any
    ) -> None:
        # A plain "turn on" hands the fans back to the printer.
        await self._set(percentage if percentage is not None and preset_mode is None else None)

    @command
    async def async_turn_off(self, /, **kwargs: Any) -> None:
        await self._set(0)

    @command
    async def async_set_percentage(self, percentage: int) -> None:
        await self._set(percentage)

    @command
    async def async_set_preset_mode(self, preset_mode: str) -> None:
        await self._set(None)
