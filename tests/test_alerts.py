"""Printer dialogs and print events."""

from __future__ import annotations

import pytest
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.prusa_connect_ce.const import DOMAIN

from .conftest import CORE_ONE, INSTANCE, MK4S_MMU, FakeClient
from .helpers import event, flush, telemetry


def _entity_id(hass: HomeAssistant, platform: str, device_id: str, key: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{INSTANCE}:{device_id}:{key}")
    assert entity_id is not None
    return entity_id


async def test_dialog_sensor(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    state = hass.states.get(_entity_id(hass, "sensor", MK4S_MMU, "dialog_message"))
    assert state is not None
    assert state.state == "Filament runout"
    assert state.attributes["text"] == "Load filament and continue."
    assert state.attributes["buttons"] == ["Continue", "Cancel"]
    assert state.attributes["code"] == "10101"
    quiet = hass.states.get(_entity_id(hass, "sensor", CORE_ONE, "dialog_message"))
    assert quiet is not None
    assert quiet.state == STATE_UNKNOWN
    assert quiet.attributes["buttons"] == []


async def test_press_dialog_button(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_client: FakeClient
) -> None:
    entity_id = _entity_id(hass, "sensor", MK4S_MMU, "dialog_message")
    await hass.services.async_call(
        DOMAIN, "press_dialog_button", {ATTR_ENTITY_ID: entity_id, "button": "continue"}, blocking=True
    )
    assert mock_client.commands == [
        (MK4S_MMU, "core.command.dialog_action", {"dialog_id": "1", "button": "Continue"})
    ]
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN, "press_dialog_button", {ATTR_ENTITY_ID: entity_id, "button": "Retry"}, blocking=True
        )
    assert err.value.translation_key == "no_such_button"
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN,
            "press_dialog_button",
            {ATTR_ENTITY_ID: _entity_id(hass, "sensor", CORE_ONE, "dialog_message"), "button": "OK"},
            blocking=True,
        )
    assert err.value.translation_key == "no_dialog"


async def test_dialog_events(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_client: FakeClient
) -> None:
    entity_id = _entity_id(hass, "event", CORE_ONE, "dialog")
    before = hass.states.get(entity_id)
    assert before is not None
    assert before.state == STATE_UNKNOWN
    for path, value in (
        ("/dialog/id", 7),
        ("/dialog/title", "Tool change failed"),
        ("/dialog/text", "Check the head and retry."),
        ("/dialog/buttons", ["Retry", "Abort"]),
    ):
        mock_client.queue.put_nowait(telemetry(CORE_ONE, path, value))
    await flush(hass)
    opened = hass.states.get(entity_id)
    assert opened is not None
    assert opened.attributes["event_type"] == "opened"
    assert opened.attributes["title"] == "Tool change failed"
    assert opened.attributes["buttons"] == ["Retry", "Abort"]
    assert opened.attributes["dialog_id"] == "7"
    mock_client.queue.put_nowait(telemetry(CORE_ONE, "/dialog/id", None))
    await flush(hass)
    closed = hass.states.get(entity_id)
    assert closed is not None
    assert closed.attributes["event_type"] == "closed"
    assert closed.attributes["dialog_id"] == "7"


async def test_print_events(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_client: FakeClient
) -> None:
    entity_id = _entity_id(hass, "event", CORE_ONE, "print")
    mock_client.queue.put_nowait(
        event(
            "com.prusa.connect.device.job.started",
            CORE_ONE,
            {"job_id": "j1", "path": "/usb/tangle.bgcode", "display_name": "tangle.bgcode"},
        )
    )
    await flush(hass)
    started = hass.states.get(entity_id)
    assert started is not None
    assert started.attributes["event_type"] == "started"
    assert started.attributes["file"] == "tangle.bgcode"
    mock_client.queue.put_nowait(
        event(
            "com.prusa.connect.device.job.settled",
            CORE_ONE,
            {
                "job_id": "j1",
                "display_name": "tangle.bgcode",
                "outcome": "finished",
                "progress": 100,
                "time_printing_s": 10800,
                "time_printing_known": True,
                "filament_used_mm": 4128.6,
                "filament_used_known": False,
            },
        )
    )
    await flush(hass)
    settled = hass.states.get(entity_id)
    assert settled is not None
    assert settled.attributes["event_type"] == "finished"
    assert settled.attributes["print_time"] == 10800
    assert "filament_used_mm" not in settled.attributes
    mock_client.queue.put_nowait(
        event("com.prusa.connect.device.job.settled", CORE_ONE, {"outcome": "exploded"})
    )
    await flush(hass)
    odd = hass.states.get(entity_id)
    assert odd is not None
    assert odd.attributes["event_type"] == "unknown"
