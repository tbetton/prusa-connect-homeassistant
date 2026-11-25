"""Shared access to the embedded server and the release settings."""

from __future__ import annotations

from dataclasses import dataclass
from ipaddress import ip_address
from urllib.parse import urlsplit

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.network import NoURLAvailableError, get_url
from homeassistant.util.hass_dict import HassKey
from homeassistant.util.network import is_loopback

from .const import DOMAIN, RELEASES_URL, TRUSTED_KEYS
from .server import EmbeddedServer


@dataclass(frozen=True, slots=True)
class ReleaseSettings:
    """Where releases come from and which keys sign them (configuration.yaml may add a source)."""

    url: str = RELEASES_URL
    trusted_keys: tuple[str, ...] = TRUSTED_KEYS


DATA_SETTINGS: HassKey[ReleaseSettings] = HassKey(f"{DOMAIN}_release_settings")
DATA_SERVER: HassKey[EmbeddedServer] = HassKey(f"{DOMAIN}_server")


def release_settings(hass: HomeAssistant) -> ReleaseSettings:
    return hass.data.get(DATA_SETTINGS) or ReleaseSettings()


def embedded_server(hass: HomeAssistant, port: int) -> EmbeddedServer:
    """The one embedded server of this Home Assistant, made on first use."""
    server = hass.data.get(DATA_SERVER)
    if server is None:
        server = EmbeddedServer(
            hass, async_get_clientsession(hass), port, release_settings(hass).trusted_keys
        )
        hass.data[DATA_SERVER] = server
    server.port = port
    return server


FALLBACK_HOST = "homeassistant.local"


def _own_ip(hass: HomeAssistant) -> str | None:
    host = hass.config.api.local_ip if hass.config.api else None
    return None if not host or is_loopback(ip_address(host)) else host


def _internal_host(hass: HomeAssistant) -> str | None:
    try:
        return urlsplit(get_url(hass, allow_external=False, allow_cloud=False)).hostname
    except NoURLAvailableError:
        return None


def default_host(hass: HomeAssistant) -> str:
    """Where a browser on the network best reaches this machine.

    The machine's own address comes first. The internal URL may name a reverse
    proxy in front of Home Assistant, which does not forward the server's port.
    """
    return _own_ip(hass) or _internal_host(hass) or FALLBACK_HOST


def host_options(hass: HomeAssistant) -> list[str]:
    """The addresses offered for this machine, the best guess first."""
    hosts = (_own_ip(hass), _internal_host(hass), FALLBACK_HOST)
    return list(dict.fromkeys(host for host in hosts if host))


def browser_url(host: str, port: int) -> str:
    """The embedded server as a browser on the network opens it: this machine's host, the server's port."""
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{port}"
