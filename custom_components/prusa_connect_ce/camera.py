"""Cameras declared on the Prusa Connect server."""

from __future__ import annotations

from aiohttp import web
from homeassistant.components.camera import Camera
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_aiohttp_proxy_stream
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_CAMERAS, DEFAULT_CAMERAS
from .coordinator import PrusaConnectConfigEntry, PrusaConnectCoordinator
from .entity import printer_device_info
from .library import Camera as ServerCamera
from .library import PrusaConnectError

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PrusaConnectConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up a camera entity for every camera that answers still images."""
    if not entry.options.get(CONF_CAMERAS, DEFAULT_CAMERAS):
        # Switched off in the options: drop the cameras created before.
        registry = er.async_get(hass)
        for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
            if entity.domain == Platform.CAMERA:
                registry.async_remove(entity.entity_id)
        return
    coordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def _add() -> None:
        new = []
        for camera in coordinator.data.cameras.values():
            if camera.name in known or not camera.snapshot:
                continue
            known.add(camera.name)
            new.append(PrusaConnectCamera(coordinator, camera))
        if new:
            async_add_entities(new)

    _add()
    entry.async_on_unload(coordinator.async_add_listener(_add))


class PrusaConnectCamera(CoordinatorEntity[PrusaConnectCoordinator], Camera):
    """A camera watching a printer, or the server's own camera when it watches none."""

    _attr_has_entity_name = True
    _attr_translation_key = "camera"

    def __init__(self, coordinator: PrusaConnectCoordinator, camera: ServerCamera) -> None:
        CoordinatorEntity.__init__(self, coordinator)
        Camera.__init__(self)
        self.camera_name = camera.name
        self._attr_unique_id = f"{coordinator.instance_id}:camera:{camera.name}"
        self._attr_translation_placeholders = {"camera": camera.name}
        device = coordinator.data.devices.get(camera.device_id)
        if device is not None:
            self._attr_device_info = printer_device_info(coordinator, device)
        else:
            self._attr_device_info = DeviceInfo(identifiers={coordinator.hub_identifier})
        if camera.min_interval_ms:
            self._attr_frame_interval = max(camera.min_interval_ms / 1000, 0.1)
        self._image: bytes | None = None
        self._last_modified: str | None = None

    @property
    def camera(self) -> ServerCamera | None:
        return self.coordinator.data.cameras.get(self.camera_name)

    @property
    def available(self) -> bool:
        camera = self.camera
        return super().available and camera is not None and camera.serving

    @property
    def is_streaming(self) -> bool:
        camera = self.camera
        return camera is not None and camera.stream

    async def async_camera_image(self, width: int | None = None, height: int | None = None) -> bytes | None:
        try:
            snapshot = await self.coordinator.client.snapshot(
                self.camera_name, if_modified_since=self._last_modified if self._image else None
            )
        except PrusaConnectError:
            return self._image
        if snapshot is not None:
            self._image = snapshot.content
            self._last_modified = snapshot.last_modified
        return self._image

    async def handle_async_mjpeg_stream(self, request: web.Request) -> web.StreamResponse | None:
        if not self.is_streaming:
            return await super().handle_async_mjpeg_stream(request)
        try:
            response = await self.coordinator.client.camera_stream(self.camera_name)
        except PrusaConnectError:
            return None
        try:
            return await async_aiohttp_proxy_stream(
                self.hass,
                request,
                response.content,
                response.headers.get("Content-Type", "multipart/x-mixed-replace"),
            )
        finally:
            response.release()
