"""The push coordinator: telemetry, presence, resync on gaps, dynamic devices."""

from __future__ import annotations

import copy

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.prusa_connect_ce.const import DOMAIN
from custom_components.prusa_connect_ce.library import (
    AuthenticationError,
    CannotConnectError,
    ForbiddenError,
    StreamConnected,
    StreamDisconnected,
)

from .conftest import CORE_ONE, INSTANCE, MK4S_MMU, XL, FakeClient
from .helpers import event, flush, telemetry


def _state(hass: HomeAssistant, platform: str, key: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{INSTANCE}:{key}")
    assert entity_id is not None, key
    state = hass.states.get(entity_id)
    assert state is not None
    return state.state


async def test_telemetry_patches_state(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_client: FakeClient
) -> None:
    mock_client.queue.put_nowait(telemetry(CORE_ONE, "/tools/0/nozzle_temp", 180.5))
    mock_client.queue.put_nowait(telemetry(CORE_ONE, "/job/progress", 42))
    mock_client.queue.put_nowait(telemetry(XL, "/tools/3/nozzle_temp", 99))
    mock_client.queue.put_nowait(
        telemetry(MK4S_MMU, "/tools/0.4/material_package_instance/package/material/abbreviation", "PETG")
    )
    await flush(hass)
    assert _state(hass, "sensor", f"{CORE_ONE}:tool:0:nozzle_temperature") == "180.5"
    assert _state(hass, "sensor", f"{CORE_ONE}:progress") == "42.0"
    assert _state(hass, "sensor", f"{XL}:tool:3:nozzle_temperature") == "99.0"
    assert _state(hass, "sensor", f"{MK4S_MMU}:tool:0.4:material") == "PETG"
    # A patch without a value removes the path: the reading becomes unknown.
    mock_client.queue.put_nowait(telemetry(CORE_ONE, "/tools/0/nozzle_temp", None))
    await flush(hass)
    assert _state(hass, "sensor", f"{CORE_ONE}:tool:0:nozzle_temperature") == "unknown"


async def test_presence_and_departure(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_client: FakeClient
) -> None:
    presence = "com.prusa.connect.device.presence.changed"
    mock_client.queue.put_nowait(event(presence, CORE_ONE, {"device_id": CORE_ONE, "connected": False}))
    await flush(hass)
    assert _state(hass, "sensor", f"{CORE_ONE}:tool:0:nozzle_temperature") == STATE_UNAVAILABLE
    assert _state(hass, "binary_sensor", f"{CORE_ONE}:connected") == "off"
    mock_client.queue.put_nowait(event(presence, CORE_ONE, {"device_id": CORE_ONE, "connected": True}))
    await flush(hass)
    assert _state(hass, "sensor", f"{CORE_ONE}:tool:0:nozzle_temperature") == "215.0"
    mock_client.queue.put_nowait(event("com.prusa.connect.device.departed", XL, {"device_id": XL}))
    await flush(hass)
    assert _state(hass, "sensor", f"{XL}:state") == STATE_UNAVAILABLE


async def test_disabled_head_becomes_unavailable(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_client: FakeClient
) -> None:
    mock_client.queue.put_nowait(telemetry(XL, "/tools/3/enabled", False))
    await flush(hass)
    assert _state(hass, "sensor", f"{XL}:tool:3:nozzle_temperature") == STATE_UNAVAILABLE
    assert _state(hass, "sensor", f"{XL}:tool:2:nozzle_temperature") != STATE_UNAVAILABLE


async def test_reconnect_and_gap_refetch(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_client: FakeClient
) -> None:
    calls = mock_client.devices_calls
    mock_client.queue.put_nowait(StreamConnected(reconnect=True))
    await flush(hass)
    assert mock_client.devices_calls == calls + 1
    mock_client.queue.put_nowait(event("com.prusa.connect.stream.continuity.lost", None, {}))
    await flush(hass)
    assert mock_client.devices_calls == calls + 2


async def test_job_event_refetches_device(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_client: FakeClient
) -> None:
    record = mock_client.record(XL)
    record["state"].update(
        {"/job/id": 9, "/job/path": "/usb/benchy.bgcode", "/job/progress": 1, "/state": "PRINTING"}
    )
    record["job"] = {
        "id": "j",
        "device_job_id": "9",
        "path": "/usb/benchy.bgcode",
        "display_name": "Benchy",
        "state": "PRINTING",
        "progress": 1,
        "time_remaining_ms": 60000,
        "running": True,
    }
    mock_client.queue.put_nowait(event("com.prusa.connect.device.job.started", XL, {"job_id": "j"}))
    await flush(hass)
    assert mock_client.device_calls == [XL]
    assert _state(hass, "sensor", f"{XL}:job_name") == "Benchy"
    assert _state(hass, "sensor", f"{XL}:state") == "printing"
    assert _state(hass, "button", f"{XL}:pause") != STATE_UNAVAILABLE


async def test_failed_refetch_falls_back_to_refresh(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_client: FakeClient
) -> None:
    calls = mock_client.devices_calls
    mock_client.queue.put_nowait(event("com.prusa.connect.device.record.changed", "gone", {}))
    await flush(hass)
    # An unknown device is read with a full refresh.
    assert mock_client.devices_calls == calls + 1
    del mock_client.records[XL]
    mock_client.queue.put_nowait(event("com.prusa.connect.device.job.settled", XL, {}))
    original = mock_client.device

    async def missing(device_id: str) -> object:
        raise CannotConnectError("gone")

    mock_client.device = missing  # type: ignore[assignment,method-assign]
    await flush(hass)
    mock_client.device = original  # type: ignore[method-assign]
    assert mock_client.devices_calls == calls + 2
    # The printer left the server, so its device is removed.
    assert dr.async_get(hass).async_get_device(identifiers={(DOMAIN, f"{INSTANCE}:{XL}")}) is None


async def test_new_printer_adds_entities(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_client: FakeClient
) -> None:
    new = copy.deepcopy(mock_client.record(CORE_ONE))
    new["id"] = "11111111-2222-3333-4444-555555555555"
    new["serial"] = "DEMO-09"
    mock_client.records[new["id"]] = new
    mock_client.queue.put_nowait(telemetry(new["id"], "/tools/0/nozzle_temp", 30))
    await flush(hass)
    assert _state(hass, "sensor", f"{new['id']}:tool:0:nozzle_temperature") == "215.0"
    assert dr.async_get(hass).async_get_device(identifiers={(DOMAIN, f"{INSTANCE}:{new['id']}")}) is not None


async def test_new_head_adds_entities(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_client: FakeClient
) -> None:
    mock_client.queue.put_nowait(telemetry(CORE_ONE, "/tools/1/nozzle_temp", 22))
    await flush(hass)
    assert _state(hass, "sensor", f"{CORE_ONE}:tool:1:nozzle_temperature") == "22.0"


async def test_firmware_update_reaches_registry(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_client: FakeClient
) -> None:
    mock_client.record(XL)["firmware"] = "6.5.0"
    mock_client.queue.put_nowait(StreamConnected(reconnect=True))
    await flush(hass)
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, f"{INSTANCE}:{XL}")})
    assert device is not None
    assert device.sw_version == "6.5.0"


async def test_server_gone_marks_unavailable(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_client: FakeClient
) -> None:
    mock_client.error = CannotConnectError("down")
    mock_client.queue.put_nowait(StreamDisconnected(error=CannotConnectError("down"), retry_in=1))
    await flush(hass)
    assert _state(hass, "sensor", f"{CORE_ONE}:state") == STATE_UNAVAILABLE
    assert _state(hass, "binary_sensor", f"{CORE_ONE}:connected") == STATE_UNAVAILABLE
    mock_client.error = None
    mock_client.queue.put_nowait(StreamConnected(reconnect=True))
    await flush(hass)
    assert _state(hass, "sensor", f"{CORE_ONE}:state") == "printing"


async def test_stream_auth_failure_starts_reauth(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_client: FakeClient
) -> None:
    mock_client.queue.put_nowait(AuthenticationError(401))
    await flush(hass)
    flows = hass.config_entries.flow.async_progress()
    assert [f["context"]["source"] for f in flows] == ["reauth"]


async def test_revoked_token_does_not_leave_stale_entities(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_client: FakeClient
) -> None:
    """Telemetry queues a throttled write; a revoked token must still win over it."""
    mock_client.queue.put_nowait(telemetry(CORE_ONE, "/tools/0/nozzle_temp", 150))
    mock_client.queue.put_nowait(AuthenticationError(401))
    mock_client.error = AuthenticationError(401)
    await flush(hass)
    assert _state(hass, "sensor", f"{CORE_ONE}:tool:0:nozzle_temperature") == STATE_UNAVAILABLE
    assert _state(hass, "binary_sensor", f"{CORE_ONE}:connected") == STATE_UNAVAILABLE


async def test_dropped_stream_on_a_revoked_token(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_client: FakeClient
) -> None:
    """The stream drops, the refresh gets 401, and a queued write lands after it."""
    mock_client.queue.put_nowait(telemetry(CORE_ONE, "/tools/0/nozzle_temp", 150))
    await hass.async_block_till_done()
    mock_client.error = AuthenticationError(401)
    mock_client.queue.put_nowait(StreamDisconnected(error=None, retry_in=1))
    await flush(hass)
    assert _state(hass, "sensor", f"{CORE_ONE}:tool:0:nozzle_temperature") == STATE_UNAVAILABLE
    assert [f["context"]["source"] for f in hass.config_entries.flow.async_progress()] == ["reauth"]


async def test_stream_refused_marks_unavailable_and_restarts(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_client: FakeClient
) -> None:
    mock_client.queue.put_nowait(ForbiddenError(403))
    await flush(hass)
    assert _state(hass, "sensor", f"{CORE_ONE}:state") == STATE_UNAVAILABLE
    assert setup_integration.state is ConfigEntryState.LOADED
    calls = mock_client.devices_calls
    # The stream is opened again later, and its first connection refreshes.
    mock_client.queue.put_nowait(StreamConnected(reconnect=False))
    await flush(hass, seconds=61)
    assert mock_client.devices_calls == calls + 1
    assert _state(hass, "sensor", f"{CORE_ONE}:state") == "printing"


async def test_unknown_device_is_fetched_once(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_client: FakeClient
) -> None:
    calls = mock_client.devices_calls
    for _ in range(3):
        mock_client.queue.put_nowait(telemetry("not-listed", "/state", "IDLE"))
        await flush(hass)
    assert mock_client.devices_calls == calls + 1
    mock_client.queue.put_nowait(event("com.prusa.connect.device.arrived", "not-listed", {}))
    await flush(hass)
    assert mock_client.devices_calls == calls + 2


async def test_unload(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    assert await hass.config_entries.async_unload(setup_integration.entry_id)
    await hass.async_block_till_done()
    assert setup_integration.state is ConfigEntryState.NOT_LOADED
