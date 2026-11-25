"""Buttons that pause, resume and stop a print."""

from __future__ import annotations

from collections.abc import Callable, Coroutine, Iterator
from dataclasses import dataclass
from typing import Any

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import PRUSA_STATES
from .coordinator import PrusaConnectConfigEntry, PrusaConnectCoordinator
from .entity import PrusaConnectEntity, async_add_entities_as_found
from .helpers import command
from .library import VERB_PAUSE_PRINT, VERB_RESUME_PRINT, VERB_STOP_PRINT, Device, PrusaConnect

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class PrintButtonDescription(ButtonEntityDescription):
    verb: str
    press_fn: Callable[[PrusaConnect, str], Coroutine[Any, Any, Any]]
    # The Prusa states the verb makes sense in. A state outside the Prusa set
    # is looked up in the server's vocabulary, and left to the server to judge
    # when the vocabulary does not know it either.
    prusa_states: frozenset[str]


BUTTONS: tuple[PrintButtonDescription, ...] = (
    PrintButtonDescription(
        key="pause",
        translation_key="pause",
        verb=VERB_PAUSE_PRINT,
        press_fn=lambda client, device_id: client.pause_print(device_id),
        prusa_states=frozenset({"printing"}),
    ),
    PrintButtonDescription(
        key="resume",
        translation_key="resume",
        verb=VERB_RESUME_PRINT,
        press_fn=lambda client, device_id: client.resume_print(device_id),
        prusa_states=frozenset({"paused"}),
    ),
    PrintButtonDescription(
        key="stop",
        translation_key="stop",
        verb=VERB_STOP_PRINT,
        press_fn=lambda client, device_id: client.stop_print(device_id),
        prusa_states=frozenset({"printing", "paused", "attention"}),
    ),
)


def _entities(coordinator: PrusaConnectCoordinator, device: Device) -> Iterator[Entity]:
    if not device.may("operate"):
        return
    for description in BUTTONS:
        if device.supports(description.verb):
            yield PrintButton(coordinator, device, description)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PrusaConnectConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the print control buttons."""
    async_add_entities_as_found(entry, async_add_entities, _entities)


class PrintButton(PrusaConnectEntity, ButtonEntity):
    entity_description: PrintButtonDescription

    def __init__(
        self,
        coordinator: PrusaConnectCoordinator,
        device: Device,
        description: PrintButtonDescription,
    ) -> None:
        super().__init__(coordinator, device, description.key)
        self.entity_description = description

    @property
    def available(self) -> bool:
        device = self.device
        if not super().available or device is None:
            return False
        state = device.printer_state
        if state is None:
            return False
        if state.lower() in PRUSA_STATES:
            return state.lower() in self.entity_description.prusa_states
        verbs = self.coordinator.vocabulary.verbs_from(state)
        return verbs is None or self.entity_description.verb in verbs

    @command
    async def async_press(self) -> None:
        await self.entity_description.press_fn(self.coordinator.client, self.device_id)
