"""Updates of the Prusa Connect server that runs inside Home Assistant."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.update import UpdateDeviceClass, UpdateEntity, UpdateEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_MODE,
    CONF_PORT,
    CONF_PRERELEASE,
    DEFAULT_PORT,
    DEFAULT_PRERELEASE,
    MODE_EMBEDDED,
    RELEASE_CHECK_INTERVAL,
)
from .coordinator import PrusaConnectConfigEntry
from .embedded import embedded_server, release_settings
from .releases import Release, ReleaseError, fetch_releases, machine_asset, pick_release
from .server import EmbeddedServer, ServerError

_LOGGER = logging.getLogger(__name__)


class ReleaseCoordinator(DataUpdateCoordinator[Release | None]):
    """The newest release the embedded server could be updated to."""

    def __init__(self, hass: HomeAssistant, entry: PrusaConnectConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name="Prusa Connect releases",
            update_interval=RELEASE_CHECK_INTERVAL,
        )
        self.prerelease = bool(entry.options.get(CONF_PRERELEASE, DEFAULT_PRERELEASE))

    async def _async_update_data(self) -> Release | None:
        try:
            releases = await fetch_releases(
                async_get_clientsession(self.hass), release_settings(self.hass).url
            )
            return pick_release(releases, self.prerelease, machine_asset())
        except ReleaseError as err:
            raise UpdateFailed(str(err)) from err


async def async_setup_entry(
    hass: HomeAssistant, entry: PrusaConnectConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """An update entity for a server this integration runs; none for an external one."""
    if entry.data.get(CONF_MODE) != MODE_EMBEDDED:
        return
    releases = ReleaseCoordinator(hass, entry)
    await releases.async_refresh()
    server = embedded_server(hass, entry.data.get(CONF_PORT, DEFAULT_PORT))
    await server.async_load()
    async_add_entities([ServerUpdateEntity(releases, server, entry)])


class ServerUpdateEntity(CoordinatorEntity[ReleaseCoordinator], UpdateEntity):
    """The installed server against the newest release; installing replaces the binary."""

    _attr_has_entity_name = True
    _attr_translation_key = "server"
    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_title = "Prusa Connect"
    _attr_supported_features = (
        UpdateEntityFeature.INSTALL | UpdateEntityFeature.PROGRESS | UpdateEntityFeature.RELEASE_NOTES
    )

    def __init__(
        self, releases: ReleaseCoordinator, server: EmbeddedServer, entry: PrusaConnectConfigEntry
    ) -> None:
        super().__init__(releases)
        self._server = server
        self._attr_unique_id = f"{entry.entry_id}:server_update"
        self._attr_device_info = DeviceInfo(identifiers={entry.runtime_data.hub_identifier})

    @property
    def available(self) -> bool:
        # The installed version is known even when the releases cannot be read.
        return self._server.installed() is not None

    @property
    def installed_version(self) -> str | None:
        installed = self._server.installed()
        return installed.version if installed else None

    @property
    def latest_version(self) -> str | None:
        if self.coordinator.data is None:
            return self.installed_version
        return self.coordinator.data.version

    @property
    def release_url(self) -> str | None:
        return self.coordinator.data.url if self.coordinator.data else None

    async def async_release_notes(self) -> str | None:
        return self.coordinator.data.notes if self.coordinator.data else None

    def _progress(self, percent: int) -> None:
        self._attr_update_percentage = percent
        self.async_write_ha_state()

    async def async_install(self, version: str | None, backup: bool, **kwargs: Any) -> None:
        release = self.coordinator.data
        if release is None:
            raise HomeAssistantError("No Prusa Connect release is known; check for updates first")
        self._attr_in_progress = True
        self._progress(0)
        try:
            await self._server.update(release, self._progress)
        except ServerError as err:
            raise HomeAssistantError(str(err)) from err
        finally:
            self._attr_in_progress = False
            self._attr_update_percentage = None
            self.async_write_ha_state()
