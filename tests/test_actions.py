"""Buttons and numbers send commands with the arguments the server expects."""

from __future__ import annotations

import pytest
from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN
from homeassistant.components.button import SERVICE_PRESS
from homeassistant.components.number import ATTR_VALUE, SERVICE_SET_VALUE
from homeassistant.components.number import DOMAIN as NUMBER_DOMAIN
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.prusa_connect_ce.const import DOMAIN
from custom_components.prusa_connect_ce.library import (
    CannotConnectError,
    CommandRefusedError,
    ForbiddenError,
    UnsupportedError,
)

from .conftest import CORE_ONE, INSTANCE, KLIPPER, XL, FakeClient


def _entity_id(hass: HomeAssistant, platform: str, key: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{INSTANCE}:{key}")
    assert entity_id is not None
    return entity_id


async def test_pause_button(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_client: FakeClient
) -> None:
    await hass.services.async_call(
        BUTTON_DOMAIN,
        SERVICE_PRESS,
        {ATTR_ENTITY_ID: _entity_id(hass, "button", f"{CORE_ONE}:pause")},
        blocking=True,
    )
    assert mock_client.commands == [(CORE_ONE, "core.command.pause_print", {})]


async def test_xl_nozzle_target_uses_zero_based_tool(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_client: FakeClient
) -> None:
    await hass.services.async_call(
        NUMBER_DOMAIN,
        SERVICE_SET_VALUE,
        {
            ATTR_ENTITY_ID: _entity_id(hass, "number", f"{XL}:tool:1:nozzle_target_temperature"),
            ATTR_VALUE: 215,
        },
        blocking=True,
    )
    assert mock_client.commands == [
        (XL, "core.command.set_nozzle_temperature", {"nozzle_temperature": 215, "tool": 1})
    ]


async def test_single_head_nozzle_target_sends_no_tool(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_client: FakeClient
) -> None:
    await hass.services.async_call(
        NUMBER_DOMAIN,
        SERVICE_SET_VALUE,
        {
            ATTR_ENTITY_ID: _entity_id(hass, "number", f"{CORE_ONE}:tool:0:nozzle_target_temperature"),
            ATTR_VALUE: 0,
        },
        blocking=True,
    )
    await hass.services.async_call(
        NUMBER_DOMAIN,
        SERVICE_SET_VALUE,
        {
            ATTR_ENTITY_ID: _entity_id(hass, "number", f"{KLIPPER}:tool:0:nozzle_target_temperature"),
            ATTR_VALUE: 200,
        },
        blocking=True,
    )
    assert mock_client.commands == [
        (CORE_ONE, "core.command.set_nozzle_temperature", {"nozzle_temperature": 0}),
        (KLIPPER, "core.command.set_nozzle_temperature", {"celsius": 200}),
    ]


async def test_bed_and_speed(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_client: FakeClient
) -> None:
    for key, value in (("bed_target_temperature", 60), ("speed", 150)):
        await hass.services.async_call(
            NUMBER_DOMAIN,
            SERVICE_SET_VALUE,
            {ATTR_ENTITY_ID: _entity_id(hass, "number", f"{CORE_ONE}:{key}"), ATTR_VALUE: value},
            blocking=True,
        )
    assert mock_client.commands == [
        (CORE_ONE, "core.command.set_bed_temperature", {"bed_temperature": 60}),
        (CORE_ONE, "core.command.set_speed", {"speed": 150}),
    ]


@pytest.mark.parametrize(
    ("error", "key"),
    [
        (CommandRefusedError("v", machine_reason="busy"), "command_refused"),
        (ForbiddenError(403), "command_forbidden"),
        (UnsupportedError("no"), "command_unsupported"),
        (CannotConnectError("down"), "command_failed"),
    ],
)
async def test_command_errors(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_client: FakeClient,
    error: Exception,
    key: str,
) -> None:
    mock_client.command_error = error
    with pytest.raises(HomeAssistantError) as raised:
        await hass.services.async_call(
            BUTTON_DOMAIN,
            SERVICE_PRESS,
            {ATTR_ENTITY_ID: _entity_id(hass, "button", f"{CORE_ONE}:stop")},
            blocking=True,
        )
    assert raised.value.translation_key == key


async def test_observe_only_token_gets_no_controls(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: FakeClient
) -> None:
    for record in mock_client.records.values():
        record["actions"] = ["observe"]
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.async_entity_ids("button") == []
    assert hass.states.async_entity_ids("number") == []
    assert hass.states.async_entity_ids("sensor")
