"""The config flow: manual entry, discovery, certificate pinning, reauth, reconfigure, options."""

from __future__ import annotations

from ipaddress import ip_address
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import SOURCE_USER, SOURCE_ZEROCONF, ConfigFlowResult
from homeassistant.const import CONF_TOKEN, CONF_URL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.prusa_connect_ce.config_flow import normalize_url
from custom_components.prusa_connect_ce.const import (
    CONF_CAMERAS,
    CONF_FINGERPRINT,
    CONF_INSTANCE_ID,
    CONF_PANEL_URL,
    CONF_SIDEBAR,
    CONF_UPDATE_THROTTLE,
    DOMAIN,
)
from custom_components.prusa_connect_ce.library import (
    ApiError,
    AuthenticationError,
    CannotConnectError,
    CertificateError,
    ForbiddenError,
    PairingDeniedError,
    PairingExpiredError,
    PrusaConnectError,
)

from .conftest import PAIRING, TOKEN, URL, FakeClient

FINGERPRINT = ":".join(["AB"] * 32)
FLOW = "custom_components.prusa_connect_ce.config_flow"


def _zeroconf(
    tls: bool = True, api: str = "v1", server_id: str | None = None, address: str = "192.168.1.10"
) -> ZeroconfServiceInfo:
    properties = {"path": "/", "version": "1.2.3", "api": api}
    if server_id:
        properties["id"] = server_id
    if tls:
        properties["tls"] = "1"
    return ZeroconfServiceInfo(
        ip_address=ip_address(address),
        ip_addresses=[ip_address(address)],
        hostname="connect.local.",
        name="Workshop._prusa-connect._tcp.local.",
        port=8080,
        type="_prusa-connect._tcp.local.",
        properties=properties,
    )


async def _external(hass: HomeAssistant) -> ConfigFlowResult:
    """Start a manual flow and pick the server that runs elsewhere."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == ["embedded", "external"]
    return await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "external"})


async def _choose(
    hass: HomeAssistant, result: ConfigFlowResult, step: str, menu: str = "auth"
) -> ConfigFlowResult:
    """Pick ``step`` in the menu the flow shows."""
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == menu
    return await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": step})


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("connect.local:8080", "http://connect.local:8080"),
        ("https://10.0.0.2:8443/settings/", "https://10.0.0.2:8443"),
        (" http://[fe80::1]:8080 ", "http://[fe80::1]:8080"),
    ],
)
def test_normalize_url(raw: str, expected: str) -> None:
    assert normalize_url(raw) == expected


@pytest.mark.parametrize("raw", ["ftp://x", "http://", "https:///path"])
def test_normalize_url_refuses(raw: str) -> None:
    with pytest.raises(ValueError, match=r".*"):
        normalize_url(raw)


async def test_user_flow(hass: HomeAssistant, mock_client: FakeClient) -> None:
    result = await _external(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "external"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: "connect.local:8080"}
    )
    result = await _choose(hass, result, "token")
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "token"
    with patch("custom_components.prusa_connect_ce.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_TOKEN: f" {TOKEN} "})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Workshop"
    assert result["data"][CONF_URL] == URL
    assert result["data"][CONF_TOKEN] == TOKEN
    assert len(result["data"][CONF_INSTANCE_ID]) == 32
    assert CONF_FINGERPRINT not in result["data"]


async def test_user_flow_invalid_url(hass: HomeAssistant, mock_client: FakeClient) -> None:
    result = await _external(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_URL: "ftp://x"})
    assert result["errors"] == {CONF_URL: "invalid_url"}


@pytest.mark.parametrize(
    ("error", "key"),
    [(CannotConnectError("down"), "cannot_connect"), (ApiError(404), "not_prusa_connect")],
)
async def test_user_flow_probe_errors(
    hass: HomeAssistant, mock_client: FakeClient, error: Exception, key: str
) -> None:
    mock_client.error = error
    result = await _external(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_URL: URL})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": key}
    mock_client.error = None
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_URL: URL})
    assert result["step_id"] == "auth"


@pytest.mark.parametrize(
    ("error", "key"),
    [
        (AuthenticationError(401), "invalid_auth"),
        (ForbiddenError(403), "insufficient_permissions"),
        (CannotConnectError("down"), "cannot_connect"),
        (PrusaConnectError("odd"), "unknown"),
    ],
)
async def test_token_errors(hass: HomeAssistant, mock_client: FakeClient, error: Exception, key: str) -> None:
    result = await _external(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_URL: URL})
    result = await _choose(hass, result, "token")
    mock_client.devices = AsyncMock(side_effect=error)  # type: ignore[method-assign]
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_TOKEN: "bad"})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": key}
    mock_client.devices = AsyncMock(return_value=[])  # type: ignore[method-assign]
    with patch("custom_components.prusa_connect_ce.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_TOKEN: TOKEN})
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_untrusted_certificate_is_pinned(hass: HomeAssistant, mock_client: FakeClient) -> None:
    mock_client.error = CertificateError("self signed")
    with (
        patch(f"{FLOW}.fetch_certificate_fingerprint", return_value=FINGERPRINT) as fetch,
        patch(f"{FLOW}.PrusaConnect", return_value=mock_client) as factory,
    ):
        result = await _external(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_URL: "https://10.0.0.2:8443"}
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "certificate"
        assert result["description_placeholders"]["fingerprint"] == FINGERPRINT
        fetch.assert_called_once_with("10.0.0.2", 8443)
        mock_client.error = None
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        result = await _choose(hass, result, "token")
        assert factory.call_args.kwargs["fingerprint"] == FINGERPRINT
        with patch("custom_components.prusa_connect_ce.async_setup_entry", return_value=True):
            result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_TOKEN: TOKEN})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_FINGERPRINT] == FINGERPRINT


async def test_pinned_certificate_that_changes(hass: HomeAssistant, mock_client: FakeClient) -> None:
    mock_client.error = CertificateError("self signed")
    with patch(f"{FLOW}.fetch_certificate_fingerprint", return_value=FINGERPRINT):
        result = await _external(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_URL: "https://h:1"})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["errors"] == {"base": "certificate_changed"}


async def test_certificate_unreadable(hass: HomeAssistant, mock_client: FakeClient) -> None:
    mock_client.error = CertificateError("self signed")
    with patch(f"{FLOW}.fetch_certificate_fingerprint", side_effect=CannotConnectError("x")):
        result = await _external(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_URL: "https://h:1"})
    assert result["errors"] == {"base": "cannot_connect"}


async def test_already_configured(
    hass: HomeAssistant, mock_client: FakeClient, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    result = await _external(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_URL: URL})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_zeroconf_flow(hass: HomeAssistant, mock_client: FakeClient) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=_zeroconf()
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "zeroconf_confirm"
    assert result["description_placeholders"] == {"name": "Workshop", "url": "https://192.168.1.10:8080"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    result = await _choose(hass, result, "token")
    with patch("custom_components.prusa_connect_ce.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_TOKEN: TOKEN})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_URL] == "https://192.168.1.10:8080"


async def test_zeroconf_unreachable_aborts(hass: HomeAssistant, mock_client: FakeClient) -> None:
    mock_client.error = CannotConnectError("down")
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=_zeroconf(tls=False)
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "cannot_connect"


async def test_zeroconf_other_api(hass: HomeAssistant, mock_client: FakeClient) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=_zeroconf(api="v2")
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_supported"


async def test_zeroconf_already_configured(hass: HomeAssistant, mock_client: FakeClient) -> None:
    MockConfigEntry(
        domain=DOMAIN, data={CONF_URL: "http://192.168.1.10:8080", CONF_TOKEN: "t", CONF_INSTANCE_ID: "i"}
    ).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=_zeroconf(tls=False)
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_client: FakeClient
) -> None:
    result = await setup_integration.start_reauth_flow(hass)
    result = await _choose(hass, result, "reauth_token", menu="reauth_confirm")
    mock_client.devices = AsyncMock(side_effect=AuthenticationError(401))  # type: ignore[method-assign]
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_TOKEN: "bad"})
    assert result["errors"] == {"base": "invalid_auth"}
    mock_client.devices = AsyncMock(return_value=[])  # type: ignore[method-assign]
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_TOKEN: "pcl_new"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert setup_integration.data[CONF_TOKEN] == "pcl_new"


async def _start_pairing(hass: HomeAssistant) -> ConfigFlowResult:
    result = await _external(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_URL: URL})
    return await _choose(hass, result, "pair")


async def test_pair_flow(hass: HomeAssistant, mock_client: FakeClient) -> None:
    hass.config.location_name = "Home"
    result = await _start_pairing(hass)
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    assert result["step_id"] == "pair"
    assert result["description_placeholders"] == {
        "code": PAIRING.user_code,
        "url": PAIRING.verification_uri_complete,
    }
    assert mock_client.pairing_requests == [("Home Assistant (Home)", ("operate",))]
    mock_client.approve.set()
    await hass.async_block_till_done()
    with patch("custom_components.prusa_connect_ce.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Workshop"
    assert result["data"][CONF_TOKEN] == "pcl_paired"
    assert result["data"][CONF_URL] == URL


@pytest.mark.parametrize(
    ("outcome", "reason"),
    [
        (PairingDeniedError("no"), "pairing_denied"),
        (PairingExpiredError("late"), "pairing_expired"),
        (CannotConnectError("down"), "cannot_connect"),
    ],
)
async def test_pair_flow_ends_without_a_token(
    hass: HomeAssistant, mock_client: FakeClient, outcome: Exception, reason: str
) -> None:
    mock_client.pairing_outcome = outcome
    result = await _start_pairing(hass)
    mock_client.approve.set()
    await hass.async_block_till_done()
    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == reason


@pytest.mark.parametrize(
    ("error", "reason"),
    [(CannotConnectError("down"), "cannot_connect"), (ApiError(404), "pairing_unavailable")],
)
async def test_pair_request_refused(
    hass: HomeAssistant, mock_client: FakeClient, error: Exception, reason: str
) -> None:
    mock_client.pairing_error = error
    result = await _start_pairing(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == reason


async def test_pair_approved_with_too_little_access(hass: HomeAssistant, mock_client: FakeClient) -> None:
    result = await _start_pairing(hass)
    mock_client.devices = AsyncMock(side_effect=ForbiddenError(403))  # type: ignore[method-assign]
    mock_client.approve.set()
    await hass.async_block_till_done()
    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "insufficient_permissions"


async def test_pair_flow_closed_stops_polling(hass: HomeAssistant, mock_client: FakeClient) -> None:
    result = await _start_pairing(hass)
    hass.config_entries.flow.async_abort(result["flow_id"])
    await hass.async_block_till_done()
    assert mock_client.pairing_cancelled


async def test_reauth_by_pairing(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_client: FakeClient
) -> None:
    result = await setup_integration.start_reauth_flow(hass)
    result = await _choose(hass, result, "pair", menu="reauth_confirm")
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    mock_client.approve.set()
    await hass.async_block_till_done()
    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert setup_integration.data[CONF_TOKEN] == "pcl_paired"


async def test_reconfigure(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_client: FakeClient
) -> None:
    result = await setup_integration.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_URL: "ftp://x"})
    assert result["errors"] == {CONF_URL: "invalid_url"}
    mock_client.devices = AsyncMock(side_effect=CannotConnectError("down"))  # type: ignore[method-assign]
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_URL: "http://new:8080"})
    assert result["errors"] == {"base": "cannot_connect"}
    mock_client.devices = AsyncMock(return_value=[])  # type: ignore[method-assign]
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_URL: "http://new:8080"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert setup_integration.data[CONF_URL] == "http://new:8080"
    assert setup_integration.data[CONF_TOKEN] == TOKEN


async def test_reconfigure_signs_in_again_when_the_token_ended(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_client: FakeClient
) -> None:
    result = await setup_integration.start_reconfigure_flow(hass)
    # The old token fails at the new address, the new one works.
    mock_client.devices = AsyncMock(side_effect=[AuthenticationError(401), []])  # type: ignore[method-assign]
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_URL: "http://new:8080"})
    assert result["step_id"] == "auth"
    result = await _choose(hass, result, "token")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_TOKEN: "pcl_new"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert setup_integration.data[CONF_URL] == "http://new:8080"
    assert setup_integration.data[CONF_TOKEN] == "pcl_new"
    assert setup_integration.data[CONF_INSTANCE_ID] == "0123456789abcdef0123456789abcdef"


async def test_reconfigure_refuses_another_server(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_client: FakeClient
) -> None:
    hass.config_entries.async_update_entry(setup_integration, unique_id=SERVER_ID)
    mock_client.server_id = "another-server"
    result = await setup_integration.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_URL: "http://new:8080"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_server"
    assert setup_integration.data[CONF_URL] == URL


async def test_reconfigure_pins_new_certificate(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_client: FakeClient
) -> None:
    result = await setup_integration.start_reconfigure_flow(hass)
    mock_client.error = CertificateError("self signed")
    with patch(f"{FLOW}.fetch_certificate_fingerprint", return_value=FINGERPRINT):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_URL: "https://new:1"}
        )
    assert result["step_id"] == "certificate"
    mock_client.error = None
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["reason"] == "reconfigure_successful"
    assert setup_integration.data[CONF_FINGERPRINT] == FINGERPRINT


async def test_options(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    result = await hass.config_entries.options.async_init(setup_integration.entry_id)
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_CAMERAS: False, CONF_UPDATE_THROTTLE: 5}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert setup_integration.options == {CONF_CAMERAS: False, CONF_UPDATE_THROTTLE: 5, CONF_SIDEBAR: True}
    assert hass.states.async_entity_ids("camera") == []


@pytest.mark.parametrize(
    ("panel", "stored"),
    [
        ("http://printers.example:8080", "http://printers.example:8080"),
        (f" {URL} ", None),
        ("", None),
    ],
)
async def test_options_panel_url(
    hass: HomeAssistant, setup_integration: MockConfigEntry, panel: str, stored: str | None
) -> None:
    result = await hass.config_entries.options.async_init(setup_integration.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_CAMERAS: True, CONF_UPDATE_THROTTLE: 1, CONF_SIDEBAR: True, CONF_PANEL_URL: panel},
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert setup_integration.options.get(CONF_PANEL_URL) == stored


async def test_options_panel_url_invalid(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    result = await hass.config_entries.options.async_init(setup_integration.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_CAMERAS: True, CONF_UPDATE_THROTTLE: 1, CONF_SIDEBAR: True, CONF_PANEL_URL: "ftp://x"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_PANEL_URL: "invalid_url"}


SERVER_ID = "13fa0324-ba5e-4dff-8ac3-f3eb99a21852"


async def test_user_flow_keys_the_entry_by_server_id(hass: HomeAssistant, mock_client: FakeClient) -> None:
    mock_client.server_id = SERVER_ID
    result = await _external(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_URL: URL})
    result = await _choose(hass, result, "token")
    with patch("custom_components.prusa_connect_ce.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_TOKEN: TOKEN})
    assert result["result"].unique_id == SERVER_ID


async def test_same_server_at_another_address(hass: HomeAssistant, mock_client: FakeClient) -> None:
    MockConfigEntry(domain=DOMAIN, unique_id=SERVER_ID, data={CONF_URL: "http://other:8080"}).add_to_hass(
        hass
    )
    mock_client.server_id = SERVER_ID
    result = await _external(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_URL: URL})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_zeroconf_one_flow_per_server(hass: HomeAssistant, mock_client: FakeClient) -> None:
    first = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=_zeroconf(server_id=SERVER_ID)
    )
    assert first["step_id"] == "zeroconf_confirm"
    second = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=_zeroconf(server_id=SERVER_ID, address="10.0.0.7")
    )
    assert second["type"] is FlowResultType.ABORT
    assert second["reason"] == "already_in_progress"


async def test_zeroconf_server_already_set_up(hass: HomeAssistant, mock_client: FakeClient) -> None:
    MockConfigEntry(domain=DOMAIN, unique_id=SERVER_ID, data={CONF_URL: "http://localhost:8080"}).add_to_hass(
        hass
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=_zeroconf(server_id=SERVER_ID)
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_user_flow_takes_over_a_discovered_one(hass: HomeAssistant, mock_client: FakeClient) -> None:
    mock_client.server_id = SERVER_ID
    discovered = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=_zeroconf(server_id=SERVER_ID)
    )
    result = await _external(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_URL: URL})
    result = await _choose(hass, result, "token")
    with patch("custom_components.prusa_connect_ce.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_TOKEN: TOKEN})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert discovered["flow_id"] not in {f["flow_id"] for f in hass.config_entries.flow.async_progress()}
