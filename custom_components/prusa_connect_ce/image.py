"""The picture inside the file a printer is printing."""

from __future__ import annotations

from collections.abc import Iterator

from homeassistant.components.image import ImageEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .coordinator import PrusaConnectConfigEntry, PrusaConnectCoordinator
from .entity import PrusaConnectEntity, async_add_entities_as_found
from .library import Device, PrusaConnectError

PARALLEL_UPDATES = 0


def _entities(coordinator: PrusaConnectCoordinator, device: Device) -> Iterator[Entity]:
    # The server answers the picture only to a caller that may operate the printer.
    if device.may("operate"):
        yield JobThumbnail(coordinator, device)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PrusaConnectConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the print thumbnails."""
    async_add_entities_as_found(entry, async_add_entities, _entities)


class JobThumbnail(PrusaConnectEntity, ImageEntity):
    """The thumbnail of the current print, read when the file changes.

    It keeps the last print's picture once the print ends, as the printer's
    screen does, until another file is printed.
    """

    _attr_translation_key = "thumbnail"

    def __init__(self, coordinator: PrusaConnectCoordinator, device: Device) -> None:
        PrusaConnectEntity.__init__(self, coordinator, device, "thumbnail")
        ImageEntity.__init__(self, coordinator.hass)
        self._path: str | None = None
        self._image: bytes | None = None
        self._etag: str | None = None
        self._follow(device)

    @callback
    def _follow(self, device: Device | None) -> None:
        path = device.job_path if device else None
        if path and path != self._path:
            self._path = path
            self._image = self._etag = None
            self._cached_image = None
            self._attr_image_last_updated = dt_util.utcnow()

    @callback
    def _handle_coordinator_update(self) -> None:
        self._follow(self.device)
        super()._handle_coordinator_update()

    async def async_image(self) -> bytes | None:
        path = self._path
        if path is None:
            return None
        try:
            thumbnail = await self.coordinator.client.thumbnail(self.device_id, path, etag=self._etag)
        except PrusaConnectError:
            return self._image
        if thumbnail is not None:
            self._image = thumbnail.content
            self._etag = thumbnail.etag
            self._attr_content_type = thumbnail.content_type
        return self._image
