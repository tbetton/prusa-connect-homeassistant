"""Setting up, failing to set up, and removing devices."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.typing import WebSocketGenerator

from custom_components.prusa_connect_ce.const import DOMAIN
from custom_components.prusa_connect_ce.library import AuthenticationError, CannotConnectError, ForbiddenError

from .conftest import CORE_ONE, INSTANCE, FakeClient


async def test_setup_retry_when_unreachable(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: FakeClient
) -> None:
    mock_client.error = CannotConnectError("down")
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_setup_auth_failure_starts_reauth(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: FakeClient
) -> None:
    mock_client.error = AuthenticationError(401)
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    assert [f["context"]["source"] for f in hass.config_entries.flow.async_progress()] == ["reauth"]


async def test_refresh_auth_failure(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: FakeClient
) -> None:
    async def refuse() -> list[object]:
        raise AuthenticationError(401)

    mock_client.devices = refuse  # type: ignore[assignment,method-assign]
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.SETUP_ERROR


async def test_vocabulary_forbidden_still_sets_up(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: FakeClient
) -> None:
    async def forbidden() -> object:
        raise ForbiddenError(403)

    mock_client.device_states = forbidden  # type: ignore[assignment,method-assign]
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.LOADED


async def test_remove_device(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    setup_integration: MockConfigEntry,
    mock_client: FakeClient,
) -> None:
    assert await async_setup_component(hass, "config", {})
    registry = dr.async_get(hass)
    hub = registry.async_get_device(identifiers={(DOMAIN, INSTANCE)})
    printer = registry.async_get_device(identifiers={(DOMAIN, f"{INSTANCE}:{CORE_ONE}")})
    stale = registry.async_get_or_create(
        config_entry_id=setup_integration.entry_id, identifiers={(DOMAIN, f"{INSTANCE}:gone")}
    )
    assert hub is not None
    assert printer is not None
    client = await hass_ws_client(hass)
    for device, allowed in ((hub, False), (printer, False), (stale, True)):
        response = await client.remove_device(device.id, setup_integration.entry_id)
        assert response["success"] is allowed
