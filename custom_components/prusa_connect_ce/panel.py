"""The Prusa Connect web app in the Home Assistant sidebar."""

from __future__ import annotations

from homeassistant.components import frontend
from homeassistant.const import CONF_HOST, CONF_URL
from homeassistant.core import HomeAssistant, callback

from .const import (
    CONF_MODE,
    CONF_PANEL_URL,
    CONF_PORT,
    CONF_SIDEBAR,
    DEFAULT_PORT,
    DEFAULT_SIDEBAR,
    MODE_EMBEDDED,
    PANEL_ICON,
    PRODUCT_NAME,
)
from .coordinator import PrusaConnectConfigEntry
from .embedded import browser_url, default_host


def panel_path(entry: PrusaConnectConfigEntry) -> str:
    """The sidebar path of one server's panel, unique per config entry."""
    return f"prusa-connect-ce-{entry.entry_id[-6:].lower()}"


def default_panel_url(hass: HomeAssistant, entry: PrusaConnectConfigEntry) -> str:
    """The server as the browser reaches it, unless an option says otherwise.

    An external server is where Home Assistant reaches it. The embedded one
    listens on Home Assistant's own host, which Home Assistant itself reaches
    as 127.0.0.1: the browser goes to the host picked at setup.
    """
    if entry.data.get(CONF_MODE) == MODE_EMBEDDED:
        host = entry.data.get(CONF_HOST) or default_host(hass)
        return browser_url(host, entry.data.get(CONF_PORT, DEFAULT_PORT))
    return str(entry.data[CONF_URL])


def panel_url(hass: HomeAssistant, entry: PrusaConnectConfigEntry) -> str:
    """The address the browser opens the web app at: the option, or the default."""
    return str(entry.options.get(CONF_PANEL_URL) or default_panel_url(hass, entry))


@callback
def async_register_panel(hass: HomeAssistant, entry: PrusaConnectConfigEntry) -> None:
    """Show the web app in the sidebar, in an iframe, until the entry unloads.

    The browser loads the web app itself, so it must reach the address, and a
    Home Assistant served over https can only frame an https server. The person
    signs in to Prusa Connect inside the frame.
    """
    if not entry.options.get(CONF_SIDEBAR, DEFAULT_SIDEBAR) or "frontend" not in hass.config.components:
        return
    path = panel_path(entry)
    frontend.async_register_built_in_panel(
        hass,
        "iframe",
        sidebar_title=PRODUCT_NAME,
        sidebar_icon=PANEL_ICON,
        frontend_url_path=path,
        config={"url": panel_url(hass, entry)},
        require_admin=False,
    )
    entry.async_on_unload(lambda: frontend.async_remove_panel(hass, path, warn_if_unknown=False))
