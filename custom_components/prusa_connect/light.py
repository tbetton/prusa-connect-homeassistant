"""The chamber light of printers that have one."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from homeassistant.components.light import ATTR_BRIGHTNESS, LightEntity
from homeassistant.components.light.const import ColorMode
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util.color import brightness_to_value, value_to_brightness

from .coordinator import PrusaConnectConfigEntry, PrusaConnectCoordinator
from .entity import PrusaConnectEntity, async_add_entities_as_found
from .helpers import command
from .library import VERB_SET_CHAMBER_LED_INTENSITY, Device

PARALLEL_UPDATES = 1

# The printer takes the intensity in percent.
_SCALE = (1, 100)


def _entities(coordinator: PrusaConnectCoordinator, device: Device) -> Iterator[Entity]:
    if (
        device.may("operate")
        and device.supports(VERB_SET_CHAMBER_LED_INTENSITY)
        and device.chamber_led_intensity is not None
    ):
        yield ChamberLight(coordinator, device)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PrusaConnectConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the chamber lights."""
    async_add_entities_as_found(entry, async_add_entities, _entities)


class ChamberLight(PrusaConnectEntity, LightEntity):
    """The chamber light, dimmable in percent."""

    _attr_translation_key = "chamber_light"
    _attr_color_mode = ColorMode.BRIGHTNESS

    def __init__(self, coordinator: PrusaConnectCoordinator, device: Device) -> None:
        super().__init__(coordinator, device, "chamber_light")
        self._attr_supported_color_modes = {ColorMode.BRIGHTNESS}
        # The intensity a plain "turn on" brings back.
        self._last_on = 100

    @property
    def _intensity(self) -> int | None:
        device = self.device
        intensity = device.chamber_led_intensity if device else None
        if intensity:
            self._last_on = intensity
        return intensity

    @property
    def is_on(self) -> bool | None:
        intensity = self._intensity
        return None if intensity is None else intensity > 0

    @property
    def brightness(self) -> int | None:
        intensity = self._intensity
        return value_to_brightness(_SCALE, intensity) if intensity else None

    @command
    async def async_turn_on(self, /, **kwargs: Any) -> None:
        device = self.device
        if device is None:
            return
        percent = self._last_on
        if ATTR_BRIGHTNESS in kwargs:
            percent = max(1, round(brightness_to_value(_SCALE, kwargs[ATTR_BRIGHTNESS])))
        await self.coordinator.client.set_chamber_light(device, percent)

    @command
    async def async_turn_off(self, /, **kwargs: Any) -> None:
        device = self.device
        if device is not None:
            await self.coordinator.client.set_chamber_light(device, 0)
