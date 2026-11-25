"""Push coordinator: a REST snapshot kept current by the event stream."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_INSTANCE_ID,
    CONF_UPDATE_THROTTLE,
    DEFAULT_UPDATE_THROTTLE,
    DEVICE_REFETCH_DELAY,
    DOMAIN,
    FALLBACK_REFRESH,
    STREAM_RESTART_DELAY,
)
from .library import (
    EVENT_ARRIVED,
    EVENT_CONTINUITY_LOST,
    EVENT_DEPARTED,
    EVENT_JOB_SETTLED,
    EVENT_JOB_STARTED,
    EVENT_PRESENCE,
    EVENT_RECORD,
    EVENT_TELEMETRY,
    AuthenticationError,
    Camera,
    Device,
    DeviceStates,
    Event,
    ForbiddenError,
    PrusaConnect,
    PrusaConnectError,
    StreamConnected,
    StreamDisconnected,
)

if TYPE_CHECKING:
    from .materials import MaterialsCoordinator

_LOGGER = logging.getLogger(__name__)

type PrusaConnectConfigEntry = ConfigEntry[PrusaConnectCoordinator]

# Events after which the device record is read again rather than patched:
# they change members that telemetry does not carry, such as the job's name.
_REFETCH_EVENTS = frozenset({EVENT_ARRIVED, EVENT_RECORD, EVENT_JOB_STARTED, EVENT_JOB_SETTLED})


@dataclass
class FleetData:
    """What the coordinator holds: every device and camera the token sees."""

    devices: dict[str, Device] = field(default_factory=dict)
    cameras: dict[str, Camera] = field(default_factory=dict)


class PrusaConnectCoordinator(DataUpdateCoordinator[FleetData]):
    """Holds the fleet, refreshed over REST and patched from the event stream."""

    config_entry: PrusaConnectConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: PrusaConnectConfigEntry,
        client: PrusaConnect,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=FALLBACK_REFRESH,
            always_update=True,
        )
        self.client = client
        self.instance_id: str = entry.data[CONF_INSTANCE_ID]
        self.server_version: str | None = None
        self.vocabulary = DeviceStates()
        self.stream_connected = False
        # Spools, when the server runs the Materials extension.
        self.materials: MaterialsCoordinator | None = None
        self._throttle = float(entry.options.get(CONF_UPDATE_THROTTLE, DEFAULT_UPDATE_THROTTLE))
        self._push_unsub: CALLBACK_TYPE | None = None
        self._refetch_unsub: CALLBACK_TYPE | None = None
        self._refetch_ids: set[str] = set()
        self._full_refresh_wanted = False
        self._restart_unsub: CALLBACK_TYPE | None = None
        # Devices the stream mentions that the listing does not carry, such
        # as a device of another kind. They are not fetched again and again.
        self._ignored_ids: set[str] = set()

    async def _async_setup(self) -> None:
        """Read what does not change while the server runs."""
        try:
            self.server_version = (await self.client.health()).version
            self.vocabulary = await self.client.device_states()
        except AuthenticationError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except ForbiddenError:
            # A token without access to the vocabulary still reads devices.
            self.vocabulary = DeviceStates()
        except PrusaConnectError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="cannot_connect",
                translation_placeholders={"error": str(err)},
            ) from err

    async def _async_update_data(self) -> FleetData:
        try:
            devices = await self.client.devices()
            cameras = await self.client.cameras()
        except AuthenticationError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except PrusaConnectError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="cannot_connect",
                translation_placeholders={"error": str(err)},
            ) from err
        data = FleetData(
            devices={d.id: d for d in devices if d.kind == "printer"},
            cameras={c.name: c for c in cameras},
        )
        self._sync_device_registry(data)
        return data

    # -- device registry -------------------------------------------------

    def device_identifier(self, device_id: str) -> tuple[str, str]:
        return (DOMAIN, f"{self.instance_id}:{device_id}")

    @property
    def hub_identifier(self) -> tuple[str, str]:
        return (DOMAIN, self.instance_id)

    def _sync_device_registry(self, data: FleetData) -> None:
        """Keep firmware versions current and drop printers that are gone."""
        registry = dr.async_get(self.hass)
        known = {self.device_identifier(device_id): d for device_id, d in data.devices.items()}
        for entry in dr.async_entries_for_config_entry(registry, self.config_entry.entry_id):
            identifier = next((i for i in entry.identifiers if i[0] == DOMAIN), None)
            if identifier is None or identifier == self.hub_identifier:
                continue
            device = known.get(identifier)
            if device is None:
                registry.async_update_device(entry.id, remove_config_entry_id=self.config_entry.entry_id)
            elif entry.sw_version != device.firmware or entry.name != device.name:
                registry.async_update_device(entry.id, sw_version=device.firmware, name=device.name)

    # -- event stream ----------------------------------------------------

    @callback
    def async_start_stream(self) -> None:
        """Read the event stream for as long as the entry is loaded."""
        self.config_entry.async_create_background_task(
            self.hass, self._async_run_stream(), f"{DOMAIN} event stream"
        )

    async def _async_run_stream(self) -> None:
        try:
            async for item in self.client.events():
                if isinstance(item, Event):
                    self._handle_event(item)
                elif isinstance(item, StreamConnected):
                    self.stream_connected = True
                    # Telemetry is never replayed, so whatever changed before
                    # this connection is read again.
                    await self.async_refresh()
                elif isinstance(item, StreamDisconnected):
                    self.stream_connected = False
                    self._cancel_pending()
                    _LOGGER.debug("Event stream dropped: %s", item.error)
                    # A refresh tells a dropped stream from a server that is gone,
                    # and marks the entities unavailable in the second case.
                    await self.async_refresh()
        except AuthenticationError as err:
            # The token was revoked or expired: what the entities show is no
            # longer current, so they go unavailable until it is replaced.
            self.stream_connected = False
            self._cancel_pending()
            self.async_set_update_error(err)
            self.config_entry.async_start_reauth(self.hass)
        except PrusaConnectError as err:
            self.stream_connected = False
            self._cancel_pending()
            _LOGGER.warning("The event stream stopped, retrying in %s s: %s", STREAM_RESTART_DELAY, err)
            self.async_set_update_error(err)
            self._restart_unsub = async_call_later(self.hass, STREAM_RESTART_DELAY, self._restart_stream)

    @callback
    def _restart_stream(self, _now: object) -> None:
        self._restart_unsub = None
        self.async_start_stream()

    @callback
    def _handle_event(self, event: Event) -> None:
        if event.type == EVENT_CONTINUITY_LOST:
            self._full_refresh_wanted = True
            self._schedule_refetch()
            return
        device_id = event.device_id
        if device_id is None:
            return
        if event.type == EVENT_ARRIVED:
            self._ignored_ids.discard(device_id)
        elif device_id in self._ignored_ids:
            return
        device = self.data.devices.get(device_id)
        if event.type == EVENT_TELEMETRY:
            path = event.data.get("path")
            if device is None:
                self._refetch_ids.add(device_id)
                self._schedule_refetch()
            elif isinstance(path, str):
                device.apply(path, event.data.get("value"))
                self._schedule_push()
        elif event.type == EVENT_PRESENCE and device is not None:
            device.connected = bool(event.data.get("connected"))
            self._schedule_push()
        elif event.type == EVENT_DEPARTED and device is not None:
            device.connected = False
            self._schedule_push()
        elif event.type in _REFETCH_EVENTS:
            self._refetch_ids.add(device_id)
            self._schedule_refetch()

    @callback
    def _schedule_push(self) -> None:
        """Write entity states at most once per throttle window."""
        if self._push_unsub is None:
            self._push_unsub = async_call_later(self.hass, self._throttle, self._push)

    @callback
    def _push(self, _now: object) -> None:
        self._push_unsub = None
        # A write queued before the stream or the server failed must not mark
        # the data current again: only a refresh does that.
        if self.last_update_success:
            self.async_set_updated_data(self.data)

    @callback
    def _cancel_pending(self) -> None:
        """Drop queued writes and refetches; a refresh decides what is current."""
        for unsub in (self._push_unsub, self._refetch_unsub):
            if unsub is not None:
                unsub()
        self._push_unsub = self._refetch_unsub = None

    @callback
    def _schedule_refetch(self) -> None:
        if self._refetch_unsub is None:
            self._refetch_unsub = async_call_later(self.hass, DEVICE_REFETCH_DELAY, self._start_refetch)

    @callback
    def _start_refetch(self, _now: object) -> None:
        self._refetch_unsub = None
        self.config_entry.async_create_task(self.hass, self._async_refetch(), f"{DOMAIN} refetch")

    async def _async_refetch(self) -> None:
        ids, self._refetch_ids = self._refetch_ids, set()
        if self._full_refresh_wanted:
            self._full_refresh_wanted = False
            await self.async_refresh()
            return
        unknown = [i for i in ids if i not in self.data.devices]
        if unknown:
            # A device this coordinator has not seen: entities and registry
            # entries are created from a full refresh.
            await self.async_refresh()
            self._ignored_ids.update(i for i in unknown if i not in self.data.devices)
            return
        results = await asyncio.gather(*(self.client.device(i) for i in ids), return_exceptions=True)
        for result in results:
            if isinstance(result, AuthenticationError):
                self.config_entry.async_start_reauth(self.hass)
                return
            if isinstance(result, BaseException):
                # A device that is gone, or a server that is: a full refresh
                # drops the first and marks the entities unavailable for the
                # second.
                _LOGGER.debug("Refetching a device failed: %s", result)
                await self.async_refresh()
                return
            self.data.devices[result.id] = result
        self.async_set_updated_data(self.data)

    async def async_shutdown(self) -> None:
        for unsub in (self._push_unsub, self._refetch_unsub, self._restart_unsub):
            if unsub is not None:
                unsub()
        self._push_unsub = self._refetch_unsub = self._restart_unsub = None
        await super().async_shutdown()
