"""Spools from the Materials extension, when the server has it installed.

The extension keeps every spool with what is left on it and the material each
tool is set to. It is read over REST and read again when the extension
announces a change on the event stream (one event per gram used while a
printer prints), with a full read every few minutes as a safety net.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, STREAM_RESTART_DELAY
from .library import (
    EVENT_MATERIALS_SPOOL,
    EVENT_MATERIALS_TOOL,
    AuthenticationError,
    Event,
    ForbiddenError,
    MaterialAssignment,
    NotFoundError,
    PrusaConnect,
    PrusaConnectError,
    Spool,
    StreamConnected,
)

if TYPE_CHECKING:
    from .coordinator import PrusaConnectConfigEntry

_LOGGER = logging.getLogger(__name__)

MATERIALS_REFRESH = timedelta(minutes=5)
# Events arrive in bursts (a gram at a time on every printing tool): they are
# gathered into one read.
MATERIALS_COOLDOWN = 2.0


@dataclass(frozen=True, slots=True)
class ToolSpool:
    """What one tool is loaded with: a spool the extension weighs, or only a material."""

    assignment: MaterialAssignment
    spool: Spool | None


type MaterialsData = dict[tuple[str, str], ToolSpool]


async def async_has_materials(client: PrusaConnect) -> bool:
    """Whether the server runs the Materials extension and the token may read it."""
    try:
        await client.materials_spools()
    except (NotFoundError, ForbiddenError):
        return False
    except PrusaConnectError as err:
        _LOGGER.debug("Cannot tell whether the Materials extension is installed: %s", err)
        return False
    return True


class MaterialsCoordinator(DataUpdateCoordinator[MaterialsData]):
    """The spool each tool of each printer is loaded with, keyed by (device id, tool id)."""

    config_entry: PrusaConnectConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: PrusaConnectConfigEntry,
        client: PrusaConnect,
        printers: Callable[[], Iterable[str]],
    ) -> None:
        """``printers`` names the printers whose tools are read."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} materials",
            update_interval=MATERIALS_REFRESH,
            request_refresh_debouncer=Debouncer(hass, _LOGGER, cooldown=MATERIALS_COOLDOWN, immediate=False),
        )
        self.client = client
        self._printers = printers
        self._restart_unsub: CALLBACK_TYPE | None = None

    async def _async_update_data(self) -> MaterialsData:
        try:
            spools = await self.client.materials_spools()
            device_ids = list(self._printers())
            results = await asyncio.gather(
                *(self.client.materials_assignments(i) for i in device_ids), return_exceptions=True
            )
        except PrusaConnectError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="cannot_connect",
                translation_placeholders={"error": str(err)},
            ) from err
        loaded = {
            (s.device_id, s.tool_id): s
            for s in spools
            if s.device_id is not None and s.tool_id is not None and not s.archived
        }
        by_id = {s.id: s for s in spools}
        data: MaterialsData = {}
        for device_id, result in zip(device_ids, results, strict=True):
            if isinstance(result, NotFoundError):
                # A printer the extension does not know (yet).
                continue
            if isinstance(result, BaseException):
                raise UpdateFailed(
                    translation_domain=DOMAIN,
                    translation_key="cannot_connect",
                    translation_placeholders={"error": str(result)},
                )
            for assignment in result:
                spool = loaded.get((device_id, assignment.tool_id))
                if spool is None and assignment.spool_id is not None:
                    candidate = by_id.get(assignment.spool_id)
                    spool = candidate if candidate is not None and not candidate.archived else None
                data[(device_id, assignment.tool_id)] = ToolSpool(assignment, spool)
        return data

    @callback
    def async_start_stream(self) -> None:
        """Read the extension's events for as long as the entry is loaded."""
        self.config_entry.async_create_background_task(
            self.hass, self._async_run_stream(), f"{DOMAIN} materials events"
        )

    async def _async_run_stream(self) -> None:
        try:
            async for item in self.client.events(types=(EVENT_MATERIALS_SPOOL, EVENT_MATERIALS_TOOL)):
                if isinstance(item, (Event, StreamConnected)):
                    await self.async_request_refresh()
        except AuthenticationError:
            # The main stream asks for a new token; this one simply stops.
            return
        except PrusaConnectError as err:
            _LOGGER.debug("The Materials event stream stopped, retrying: %s", err)
            self._restart_unsub = async_call_later(self.hass, STREAM_RESTART_DELAY, self._restart_stream)

    @callback
    def _restart_stream(self, _now: object) -> None:
        self._restart_unsub = None
        self.async_start_stream()

    async def async_shutdown(self) -> None:
        if self._restart_unsub is not None:
            self._restart_unsub()
            self._restart_unsub = None
        await super().async_shutdown()
