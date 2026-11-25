"""Spools from the Materials extension."""

from __future__ import annotations

from typing import Any

from homeassistant.const import STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.prusa_connect_ce.const import DOMAIN

from .conftest import CORE_ONE, INSTANCE, XL, FakeClient
from .helpers import event, flush


def _spool(spool_id: int, remaining: float, tool: dict[str, Any] | None, **extra: Any) -> dict[str, Any]:
    return {
        "id": spool_id,
        "material_name": "PolyLite PLA Black",
        "brand_name": "Polymaker",
        "type": "PLA",
        "color_rgba": "#000000ff",
        "full_weight_g": 1145,
        "empty_weight_g": 145,
        "remaining_weight_g": remaining,
        "location": "",
        "notes": "",
        "archived_at": None,
        "tool": tool,
        **extra,
    }


def _tool(tool_id: str, spool_id: int | None = None) -> dict[str, Any]:
    assignment = {"material_name": "Generic PETG", "brand_name": "Prusament", "type": "PETG"}
    if spool_id is not None:
        assignment["spool_id"] = spool_id
    return {"id": tool_id, "assignment": assignment}


def _entity_id(hass: HomeAssistant, device_id: str, tool_id: str) -> str | None:
    return er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{INSTANCE}:{device_id}:tool:{tool_id}:spool"
    )


async def _setup(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()


async def test_no_extension_no_spools(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    assert _entity_id(hass, CORE_ONE, "0") is None


async def test_spool_sensors(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: FakeClient
) -> None:
    mock_client.spools = [
        _spool(16, 523.6, {"device_id": CORE_ONE, "tool_id": "0", "gone": False}),
        _spool(3, 50, {"device_id": XL, "tool_id": "1", "gone": True}),
    ]
    mock_client.material_tools = {CORE_ONE: [_tool("0", 16)], XL: [_tool("0"), _tool("1")]}
    await _setup(hass, config_entry)

    entity_id = _entity_id(hass, CORE_ONE, "0")
    assert entity_id is not None
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == "523.6"
    assert state.attributes["unit_of_measurement"] == "g"
    assert state.attributes["friendly_name"] == "CoreOne DEMO-01 Tool 1 spool"
    assert state.attributes["spool_id"] == 16
    assert state.attributes["initial_weight"] == 1000
    assert state.attributes["percent"] == 52
    assert state.attributes["color_hex"] == "#000000"
    assert state.attributes["brand"] == "Polymaker"

    # A tool with a material but no spool the extension weighs.
    empty_id = _entity_id(hass, XL, "1")
    assert empty_id is not None
    empty = hass.states.get(empty_id)
    assert empty is not None
    assert empty.state == STATE_UNKNOWN
    assert empty.attributes["spool_id"] is None
    assert empty.attributes["type"] == "PETG"


async def test_spools_follow_extension_events(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: FakeClient
) -> None:
    mock_client.spools = [_spool(16, 523.6, {"device_id": CORE_ONE, "tool_id": "0", "gone": False})]
    mock_client.material_tools = {CORE_ONE: [_tool("0", 16)]}
    await _setup(hass, config_entry)
    entity_id = _entity_id(hass, CORE_ONE, "0")
    assert entity_id is not None

    mock_client.spools[0]["remaining_weight_g"] = 522.6
    mock_client.typed_queue.put_nowait(event("com.github.aux.prusa3d.materials.spool", None, {"id": 16}))
    await flush(hass, seconds=5)
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == "522.6"
