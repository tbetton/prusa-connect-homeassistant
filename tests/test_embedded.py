"""The server inside Home Assistant: the flow that installs it, its entry, its updates."""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from unittest.mock import patch

import pytest
from homeassistant.components.http import ApiConfig
from homeassistant.config_entries import SOURCE_USER, ConfigEntry, ConfigEntryState, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_TOKEN, CONF_URL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, UnknownFlow
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.prusa_connect_ce.config_flow import normalize_host
from custom_components.prusa_connect_ce.const import (
    CONF_INSTANCE_ID,
    CONF_MODE,
    CONF_PORT,
    CONF_PRERELEASE,
    DOMAIN,
    MODE_EMBEDDED,
)
from custom_components.prusa_connect_ce.embedded import browser_url, default_host, host_options
from custom_components.prusa_connect_ce.library import CannotConnectError
from custom_components.prusa_connect_ce.releases import ReleaseError
from custom_components.prusa_connect_ce.server import ServerError

from .conftest import INSTANCE, PAIRING, RELEASE, TOKEN, FakeClient, FakeEmbedded

FLOW = "custom_components.prusa_connect_ce.config_flow"


@pytest.fixture(autouse=True)
def fast_first_run() -> Generator[None]:
    with patch(f"{FLOW}.FIRST_RUN_POLL", 0):
        yield


async def _wait_for(hass: HomeAssistant, flow_id: str, step: str | None) -> None:
    """Let the flow's background steps run until it shows ``step`` (``None``: it has ended)."""
    for _ in range(200):
        try:
            current = hass.config_entries.flow.async_get(flow_id)["step_id"]
        except UnknownFlow:
            current = None
        if current == step:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"the flow stays at {current}, not {step}")


async def _finish(hass: HomeAssistant, flow_id: str) -> ConfigFlowResult | None:
    """Go on through the flow, as the frontend does, until it creates an entry or aborts.

    ``None`` when Home Assistant carried a finished progress step to the end on its own.
    """
    for _ in range(200):
        try:
            result = await hass.config_entries.flow.async_configure(flow_id)
        except UnknownFlow:
            return None
        if result["type"] in (FlowResultType.CREATE_ENTRY, FlowResultType.ABORT):
            return result
        await asyncio.sleep(0.01)
    raise AssertionError(f"the flow did not end: {result}")


async def _install(hass: HomeAssistant) -> str:
    """Pick the server inside Home Assistant on port 8080. Returns the flow id."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "embedded"})
    assert result["step_id"] == "embedded"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PORT: 8080})
    return str(result["flow_id"])


def _entry(hass: HomeAssistant) -> ConfigEntry:
    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 1
    return entries[0]


async def test_embedded_flow(hass: HomeAssistant, mock_client: FakeClient, embedded: FakeEmbedded) -> None:
    hass.config.internal_url = "http://192.168.1.5:8123"
    mock_client.first_run.clear()
    mock_client.server_id = "server-1"
    with patch("custom_components.prusa_connect_ce.async_setup_entry", return_value=True):
        flow_id = await _install(hass)
        await _wait_for(hass, flow_id, "first_run")
        assert embedded.installs == [RELEASE]
        result = await hass.config_entries.flow.async_configure(flow_id)
        assert result["type"] is FlowResultType.SHOW_PROGRESS
        assert result["description_placeholders"] == {
            "url": "http://192.168.1.5:8080",
            "code": "ABCD-EFGH-JKMN",
        }
        mock_client.first_run.set()
        await _wait_for(hass, flow_id, "first_run_done")
        result = await hass.config_entries.flow.async_configure(flow_id)
        assert result["step_id"] == "pair"
        # The server built the link for 127.0.0.1; the browser reaches Home Assistant's host.
        assert result["description_placeholders"]["url"] == "http://192.168.1.5:8080/pair?code=BCDF-GHJK"
        mock_client.approve.set()
        await _finish(hass, flow_id)
    entry = _entry(hass)
    assert entry.data[CONF_MODE] == MODE_EMBEDDED
    assert entry.data[CONF_PORT] == 8080
    assert entry.data[CONF_HOST] == "192.168.1.5"
    assert entry.data[CONF_URL] == embedded.url
    assert entry.data[CONF_TOKEN] == "pcl_paired"
    assert entry.unique_id == "server-1"
    assert embedded.stops == 0
    assert PAIRING.user_code == "BCDF-GHJK"


async def test_embedded_open_mode_needs_no_token(
    hass: HomeAssistant, mock_client: FakeClient, embedded: FakeEmbedded
) -> None:
    mock_client.setup_mode = "open"
    with patch("custom_components.prusa_connect_ce.async_setup_entry", return_value=True):
        flow_id = await _install(hass)
        await _finish(hass, flow_id)
    assert _entry(hass).data[CONF_TOKEN] == ""


async def test_embedded_reuses_an_installed_server(
    hass: HomeAssistant, mock_client: FakeClient, embedded: FakeEmbedded
) -> None:
    embedded.version = "0.0.1-alpha.1"
    mock_client.first_run.clear()
    flow_id = await _install(hass)
    await _wait_for(hass, flow_id, "first_run")
    assert embedded.installs == []
    assert embedded.running
    hass.config_entries.flow.async_abort(flow_id)


@pytest.mark.parametrize("error", [ServerError("no space"), ReleaseError("offline")])
async def test_embedded_install_fails(
    hass: HomeAssistant, mock_client: FakeClient, embedded: FakeEmbedded, error: Exception
) -> None:
    if isinstance(error, ReleaseError):
        patcher = patch(f"{FLOW}.fetch_releases", side_effect=error)
    else:
        embedded.error = error
        patcher = patch(f"{FLOW}.fetch_releases", return_value=[RELEASE])
    with patcher:
        flow_id = await _install(hass)
        result = await _finish(hass, flow_id)
        assert result is None or result["reason"] == "install_failed"
    assert hass.config_entries.async_entries(DOMAIN) == []
    assert embedded.stops >= 1


async def test_embedded_server_does_not_answer(
    hass: HomeAssistant, mock_client: FakeClient, embedded: FakeEmbedded
) -> None:
    with patch.object(mock_client, "health", side_effect=CannotConnectError("down")):
        flow_id = await _install(hass)
        result = await _finish(hass, flow_id)
        assert result is None or result["reason"] == "cannot_connect"
    assert hass.config_entries.async_entries(DOMAIN) == []


async def test_closing_the_flow_stops_the_server(
    hass: HomeAssistant, mock_client: FakeClient, embedded: FakeEmbedded
) -> None:
    mock_client.first_run.clear()
    flow_id = await _install(hass)
    await _wait_for(hass, flow_id, "first_run")
    hass.config_entries.flow.async_abort(flow_id)
    await hass.async_block_till_done()
    assert embedded.stops == 1


async def test_only_one_embedded_server(
    hass: HomeAssistant, mock_client: FakeClient, embedded: FakeEmbedded
) -> None:
    MockConfigEntry(domain=DOMAIN, data={CONF_MODE: MODE_EMBEDDED, CONF_PORT: 8080}).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "embedded"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


def _embedded_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="Prusa Connect",
        data={
            CONF_URL: "http://127.0.0.1:8080",
            CONF_TOKEN: TOKEN,
            CONF_INSTANCE_ID: INSTANCE,
            CONF_MODE: MODE_EMBEDDED,
            CONF_PORT: 8080,
        },
    )


async def test_entry_starts_and_stops_the_server(
    hass: HomeAssistant, mock_client: FakeClient, embedded: FakeEmbedded
) -> None:
    embedded.version = "0.0.1-alpha.1"
    entry = _embedded_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert embedded.running
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert embedded.stops == 1


async def test_entry_retries_when_the_server_does_not_start(
    hass: HomeAssistant, mock_client: FakeClient, embedded: FakeEmbedded
) -> None:
    embedded.error = ServerError("port in use")
    entry = _embedded_entry()
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_reconfigure_has_no_address(
    hass: HomeAssistant, mock_client: FakeClient, embedded: FakeEmbedded
) -> None:
    embedded.version = "0.0.1-alpha.1"
    entry = _embedded_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "embedded_has_no_address"


async def test_reauth_pairs_through_the_browser_address(
    hass: HomeAssistant, mock_client: FakeClient, embedded: FakeEmbedded
) -> None:
    # Home Assistant behind a reverse proxy: the link goes to the machine, not the proxy.
    hass.config.internal_url = "https://ha.example.com"
    embedded.version = "0.0.1-alpha.1"
    entry = _embedded_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    assert hass.config.api is not None
    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "pair"})
    assert result["description_placeholders"]["url"].startswith(
        f"http://{hass.config.api.local_ip}:8080/pair"
    )
    mock_client.approve.set()
    await hass.async_block_till_done()
    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["reason"] == "reauth_successful"
    # The entry reloads with the new token, and the server runs again.
    await hass.async_block_till_done()
    assert embedded.running


async def test_update_entity(hass: HomeAssistant, mock_client: FakeClient, embedded: FakeEmbedded) -> None:
    embedded.version = "0.0.1-alpha.1"
    entry = _embedded_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    entity_id = er.async_get(hass).async_get_entity_id("update", DOMAIN, f"{entry.entry_id}:server_update")
    assert entity_id is not None
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == "on"
    assert state.attributes["installed_version"] == "0.0.1-alpha.1"
    assert state.attributes["latest_version"] == "0.0.1-alpha.2"
    assert state.attributes["release_url"] == RELEASE.url
    await hass.services.async_call("update", "install", {"entity_id": entity_id}, blocking=True)
    assert embedded.updates == [RELEASE]
    assert hass.states.get(entity_id).state == "off"  # type: ignore[union-attr]

    embedded.version = "0.0.1-alpha.1"
    embedded.error = ServerError("did not start")
    with pytest.raises(HomeAssistantError, match="did not start"):
        await hass.services.async_call("update", "install", {"entity_id": entity_id}, blocking=True)


async def test_update_entity_without_releases(
    hass: HomeAssistant, mock_client: FakeClient, embedded: FakeEmbedded
) -> None:
    embedded.version = "0.0.1-alpha.1"
    entry = _embedded_entry()
    entry.add_to_hass(hass)
    with patch(
        "custom_components.prusa_connect_ce.update.fetch_releases", side_effect=ReleaseError("offline")
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    entity_id = er.async_get(hass).async_get_entity_id("update", DOMAIN, f"{entry.entry_id}:server_update")
    state = hass.states.get(entity_id)  # type: ignore[arg-type]
    assert state is not None
    assert state.state == "off"
    assert state.attributes["latest_version"] == "0.0.1-alpha.1"
    assert state.attributes["release_url"] is None


async def test_external_server_has_no_update_entity(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    assert hass.states.async_entity_ids("update") == []


async def test_options_offer_pre_releases_for_embedded(
    hass: HomeAssistant, mock_client: FakeClient, embedded: FakeEmbedded
) -> None:
    embedded.version = "0.0.1-alpha.1"
    entry = _embedded_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert CONF_PRERELEASE in {str(key) for key in result["data_schema"].schema}


def test_browser_url() -> None:
    assert browser_url("homeassistant.local", 8080) == "http://homeassistant.local:8080"
    assert browser_url("fd00::5", 8099) == "http://[fd00::5]:8099"


async def test_host_guess_and_options(hass: HomeAssistant) -> None:
    assert default_host(hass) == "homeassistant.local"
    assert host_options(hass) == ["homeassistant.local"]
    # Behind a reverse proxy the internal URL names the proxy: the machine's own address wins.
    hass.config.internal_url = "https://ha.example.com"
    hass.config.api = ApiConfig("192.168.1.5", "192.168.1.5", 8123, False)
    assert default_host(hass) == "192.168.1.5"
    assert host_options(hass) == ["192.168.1.5", "ha.example.com", "homeassistant.local"]
    # A loopback address reaches no browser: the internal URL is the guess after all.
    hass.config.api = ApiConfig("127.0.0.1", "127.0.0.1", 8123, False)
    assert default_host(hass) == "ha.example.com"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("192.168.1.5", "192.168.1.5"),
        (" printer-box.lan ", "printer-box.lan"),
        ("http://192.168.1.5:8080/", "192.168.1.5"),
        ("fd00::5", "fd00::5"),
        ("[fd00::5]", "fd00::5"),
    ],
)
def test_normalize_host(raw: str, expected: str) -> None:
    assert normalize_host(raw) == expected


async def test_embedded_flow_at_a_picked_host(
    hass: HomeAssistant, mock_client: FakeClient, embedded: FakeEmbedded
) -> None:
    hass.config.internal_url = "https://ha.example.com"
    mock_client.first_run.clear()
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "embedded"})
    flow_id = result["flow_id"]
    result = await hass.config_entries.flow.async_configure(flow_id, {CONF_PORT: 8080, CONF_HOST: "ftp://"})
    assert result["errors"] == {CONF_HOST: "invalid_host"}
    await hass.config_entries.flow.async_configure(flow_id, {CONF_PORT: 8080, CONF_HOST: "10.0.0.7"})
    await _wait_for(hass, flow_id, "first_run")
    result = await hass.config_entries.flow.async_configure(flow_id)
    assert result["description_placeholders"]["url"] == "http://10.0.0.7:8080"
    mock_client.first_run.set()
    await _wait_for(hass, flow_id, "first_run_done")
    result = await hass.config_entries.flow.async_configure(flow_id)
    assert result["description_placeholders"]["url"] == "http://10.0.0.7:8080/pair?code=BCDF-GHJK"
    hass.config_entries.flow.async_abort(flow_id)


async def test_first_run_survives_the_server_restart(
    hass: HomeAssistant, mock_client: FakeClient, embedded: FakeEmbedded
) -> None:
    """A wizard that changes the configuration restarts the server, so a poll finds no port."""
    mock_client.first_run.clear()
    mock_client.server_id = "server-1"
    real = mock_client.setup_status
    calls = 0

    async def restarting() -> object:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise CannotConnectError("connection refused")
        if calls == 3:
            mock_client.first_run.set()
        return await real()

    with patch.object(mock_client, "setup_status", side_effect=restarting):
        flow_id = await _install(hass)
        await _wait_for(hass, flow_id, "first_run")
        mock_client.first_run.set()
        await _wait_for(hass, flow_id, "first_run_done")
    result = await hass.config_entries.flow.async_configure(flow_id)
    assert result["step_id"] == "pair"
    hass.config_entries.flow.async_abort(flow_id)


async def test_first_run_of_a_server_already_added(
    hass: HomeAssistant, mock_client: FakeClient, embedded: FakeEmbedded
) -> None:
    """The finished wizard names a server Home Assistant already has: the window says so."""
    MockConfigEntry(domain=DOMAIN, unique_id="server-1", data={CONF_URL: "http://other:8080"}).add_to_hass(
        hass
    )
    mock_client.first_run.clear()
    mock_client.server_id = "server-1"
    flow_id = await _install(hass)
    await _wait_for(hass, flow_id, "first_run")
    mock_client.first_run.set()
    result = await _finish(hass, flow_id)
    assert result is not None
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
