"""Events of a printer: a print starting or ending, and a dialog opening or closing."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import PrusaConnectConfigEntry, PrusaConnectCoordinator
from .entity import PrusaConnectEntity, async_add_entities_as_found
from .library import EVENT_JOB_STARTED, Device, Dialog, Event

PARALLEL_UPDATES = 0

# How a print ended, as the server reports it.
OUTCOMES = ("finished", "failed", "cancelled", "unknown")
PRINT_EVENTS = ["started", *OUTCOMES]
DIALOG_EVENTS = ["opened", "closed"]


def dialog_attributes(dialog: Dialog) -> dict[str, Any]:
    return {
        "dialog_id": dialog.id,
        "code": dialog.code,
        "title": dialog.title,
        "text": dialog.text,
        "buttons": list(dialog.buttons),
    }


def _entities(coordinator: PrusaConnectCoordinator, device: Device) -> Iterator[Entity]:
    yield PrintEvent(coordinator, device)
    yield DialogEvent(coordinator, device)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PrusaConnectConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the event entities."""
    async_add_entities_as_found(entry, async_add_entities, _entities)


class PrintEvent(PrusaConnectEntity, EventEntity):
    """Fires when a print starts and when it ends, with how it ended."""

    _attr_translation_key = "print"

    def __init__(self, coordinator: PrusaConnectCoordinator, device: Device) -> None:
        super().__init__(coordinator, device, "print")
        self._attr_event_types = PRINT_EVENTS

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.coordinator.async_add_job_listener(self._on_job))

    @callback
    def _on_job(self, device_id: str, event: Event) -> None:
        if device_id != self.device_id:
            return
        data = event.data
        if event.type == EVENT_JOB_STARTED:
            event_type = "started"
        else:
            outcome = str(data.get("outcome") or "unknown").lower()
            event_type = outcome if outcome in OUTCOMES else "unknown"
        attributes = {
            "file": data.get("display_name"),
            "path": data.get("path"),
            "job_id": data.get("job_id"),
            "progress": data.get("progress"),
            "print_time": data.get("time_printing_s") if data.get("time_printing_known", True) else None,
            "filament_used_mm": data.get("filament_used_mm")
            if data.get("filament_used_known", True)
            else None,
        }
        self._trigger_event(event_type, {k: v for k, v in attributes.items() if v is not None})
        self.async_write_ha_state()


class DialogEvent(PrusaConnectEntity, EventEntity):
    """Fires when the printer opens a dialog and waits on it, and when the dialog closes."""

    _attr_translation_key = "dialog"

    def __init__(self, coordinator: PrusaConnectCoordinator, device: Device) -> None:
        super().__init__(coordinator, device, "dialog")
        self._attr_event_types = DIALOG_EVENTS
        # A dialog already shown at start is not news; only changes fire.
        self._dialog = device.dialog

    @callback
    def _handle_coordinator_update(self) -> None:
        device = self.device
        dialog = device.dialog if device else None
        previous, self._dialog = self._dialog, dialog
        if (previous.id if previous else None) != (dialog.id if dialog else None):
            if previous is not None:
                self._trigger_event("closed", dialog_attributes(previous))
            if dialog is not None:
                self._trigger_event("opened", dialog_attributes(dialog))
        super()._handle_coordinator_update()
