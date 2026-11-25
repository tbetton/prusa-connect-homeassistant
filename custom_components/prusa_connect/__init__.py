"""The Prusa Connect integration."""

from __future__ import annotations

import voluptuous as vol
from homeassistant.const import CONF_TOKEN, CONF_URL, EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_FINGERPRINT,
    CONF_MODE,
    CONF_PORT,
    CONF_RELEASES_URL,
    CONF_TRUSTED_KEYS,
    DEFAULT_PORT,
    DOMAIN,
    HUB_MODEL,
    MANUFACTURER,
    MODE_EMBEDDED,
    RELEASES_URL,
    TRUSTED_KEYS,
)
from .coordinator import PrusaConnectConfigEntry, PrusaConnectCoordinator
from .embedded import DATA_SETTINGS, ReleaseSettings, embedded_server
from .library import PrusaConnect
from .materials import MaterialsCoordinator, async_has_materials
from .panel import async_register_panel
from .server import ServerError

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CAMERA,
    Platform.EVENT,
    Platform.FAN,
    Platform.IMAGE,
    Platform.LIGHT,
    Platform.NUMBER,
    Platform.SENSOR,
    Platform.UPDATE,
]

# Only for development: another release source for the embedded server, and
# the keys it signs with. The official keys stay trusted.
CONFIG_SCHEMA = vol.Schema(
    {
        vol.Optional(DOMAIN): vol.Schema(
            {
                vol.Optional(CONF_RELEASES_URL): cv.url,
                vol.Optional(CONF_TRUSTED_KEYS): vol.All(cv.ensure_list, [cv.string]),
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Read the development settings for the embedded server's releases."""
    conf = config.get(DOMAIN, {})
    hass.data[DATA_SETTINGS] = ReleaseSettings(
        url=conf.get(CONF_RELEASES_URL, RELEASES_URL),
        trusted_keys=TRUSTED_KEYS + tuple(conf.get(CONF_TRUSTED_KEYS, [])),
    )
    return True


async def _async_start_embedded(hass: HomeAssistant, entry: PrusaConnectConfigEntry) -> None:
    """Start the server this entry runs inside Home Assistant, and stop it with the entry."""
    server = embedded_server(hass, entry.data.get(CONF_PORT, DEFAULT_PORT))
    if not server.running:
        try:
            await server.start()
        except ServerError as err:
            await server.stop()
            raise ConfigEntryNotReady(str(err)) from err

    async def _stop(_: Event | None = None) -> None:
        await server.stop()

    entry.async_on_unload(_stop)
    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _stop))


async def async_setup_entry(hass: HomeAssistant, entry: PrusaConnectConfigEntry) -> bool:
    """Set up Prusa Connect from a config entry."""
    if entry.data.get(CONF_MODE) == MODE_EMBEDDED:
        await _async_start_embedded(hass, entry)
    client = PrusaConnect(
        async_get_clientsession(hass),
        entry.data[CONF_URL],
        entry.data[CONF_TOKEN],
        fingerprint=entry.data.get(CONF_FINGERPRINT),
    )
    coordinator = PrusaConnectCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    if await async_has_materials(client):
        materials = MaterialsCoordinator(hass, entry, client, lambda: list(coordinator.data.devices))
        await materials.async_refresh()
        coordinator.materials = materials

    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={coordinator.hub_identifier},
        name=entry.title,
        manufacturer=MANUFACTURER,
        model=HUB_MODEL,
        sw_version=coordinator.server_version,
        configuration_url=entry.data[CONF_URL],
        entry_type=dr.DeviceEntryType.SERVICE,
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    async_register_panel(hass, entry)
    coordinator.async_start_stream()
    if coordinator.materials is not None:
        coordinator.materials.async_start_stream()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: PrusaConnectConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: PrusaConnectConfigEntry, device: dr.DeviceEntry
) -> bool:
    """Allow removing a printer the server no longer knows, and never the hub."""
    coordinator = entry.runtime_data
    if coordinator.hub_identifier in device.identifiers:
        return False
    known = {coordinator.device_identifier(device_id) for device_id in coordinator.data.devices}
    return not device.identifiers & known
