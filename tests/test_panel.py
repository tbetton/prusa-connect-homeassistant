"""The web app in the sidebar."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.prusa_connect_ce.const import CONF_PANEL_URL, CONF_SIDEBAR
from custom_components.prusa_connect_ce.panel import panel_path

from .conftest import URL, FakeClient

PANEL = "custom_components.prusa_connect_ce.panel.frontend"


@pytest.fixture
def frontend(hass: HomeAssistant) -> MagicMock:
    hass.config.components.add("frontend")
    with patch(PANEL) as mock:
        yield mock


def _with_options(entry: MockConfigEntry, options: dict[str, object]) -> MockConfigEntry:
    return MockConfigEntry(domain=entry.domain, title=entry.title, data=dict(entry.data), options=options)


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_panel_opens_the_server(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: FakeClient, frontend: MagicMock
) -> None:
    await _setup(hass, config_entry)
    frontend.async_register_built_in_panel.assert_called_once()
    args, kwargs = frontend.async_register_built_in_panel.call_args
    assert args[1] == "iframe"
    assert kwargs["frontend_url_path"] == panel_path(config_entry)
    assert kwargs["config"] == {"url": URL}
    assert kwargs["sidebar_title"] == "Prusa Connect"
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    frontend.async_remove_panel.assert_called_once_with(hass, panel_path(config_entry), warn_if_unknown=False)


async def test_panel_at_another_address(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: FakeClient, frontend: MagicMock
) -> None:
    await _setup(hass, _with_options(config_entry, {CONF_PANEL_URL: "https://printers.example"}))
    assert frontend.async_register_built_in_panel.call_args.kwargs["config"] == {
        "url": "https://printers.example"
    }


async def test_panel_switched_off(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: FakeClient, frontend: MagicMock
) -> None:
    await _setup(hass, _with_options(config_entry, {CONF_SIDEBAR: False}))
    frontend.async_register_built_in_panel.assert_not_called()
