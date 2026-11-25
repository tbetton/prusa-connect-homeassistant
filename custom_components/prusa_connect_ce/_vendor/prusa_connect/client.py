"""Async client for the Prusa Connect HTTP API and event stream."""

from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import AsyncIterator, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import aiohttp

from .exceptions import (
    ApiError,
    AuthenticationError,
    CannotConnectError,
    CertificateError,
    CommandRefusedError,
    ForbiddenError,
    NotFoundError,
    PairingDeniedError,
    PairingExpiredError,
    UnsupportedError,
)
from .models import (
    ARG_BUTTON,
    ARG_DIALOG_ID,
    ARG_TOOL,
    CHAMBER_FAN_AUTOMATIC,
    EVENT_AUTH_EXPIRED,
    MATERIALS_MODULE,
    VERB_DIALOG_ACTION,
    VERB_PAUSE_PRINT,
    VERB_RESUME_PRINT,
    VERB_SET_BED_TEMPERATURE,
    VERB_SET_CHAMBER_FAN,
    VERB_SET_CHAMBER_LIGHT,
    VERB_SET_CHAMBER_TEMPERATURE,
    VERB_SET_FLOW,
    VERB_SET_NOZZLE_TEMPERATURE,
    VERB_SET_SPEED,
    VERB_STOP_PRINT,
    AppPairing,
    AppSettings,
    Camera,
    CommandResult,
    Device,
    DeviceStates,
    Event,
    Health,
    MaterialAssignment,
    SetupStatus,
    Snapshot,
    Spool,
    StreamConnected,
    Thumbnail,
    parse_tool_id,
)
from .sse import SSEParser
from .tls import fingerprint_to_bytes

_LOGGER = logging.getLogger(__name__)

API = "/api/v1"
DEFAULT_TIMEOUT = 10.0
# The stream writes a keep-alive frame while idle. A read that waits longer
# than this treats the connection as dead and reconnects.
STREAM_READ_TIMEOUT = 90.0

# The scope an app asks for when it pairs: reading, and the commands of the
# operate class (start, pause, stop, temperatures, speed).
SCOPE_READ = "read"
SCOPE_OPERATE = "operate"
SCOPE_ADMINISTER = "administer"

# The answers a pairing poll gives before it succeeds (RFC 8628 section 3.5).
_PAIRING_PENDING = "core.auth/APP_PAIRING_PENDING"
_PAIRING_SLOW_DOWN = "core.auth/APP_PAIRING_SLOW_DOWN"
_PAIRING_DENIED = "core.auth/APP_PAIRING_DENIED"
_PAIRING_GONE = ("core.auth/APP_PAIRING_EXPIRED", "core.auth/APP_PAIRING_UNKNOWN")
# RFC 8628: a slow_down answer adds five seconds to the interval for good.
_SLOW_DOWN_STEP = 5
DEVICE_PAGE_SIZE = 100
SPOOL_PAGE_SIZE = 100


@dataclass(frozen=True, slots=True)
class StreamDisconnected:
    """Yielded when the event stream drops and a reconnect is scheduled."""

    error: Exception | None
    retry_in: float


StreamItem = Event | StreamConnected | StreamDisconnected


def _format_arg(value: str | float | bool) -> str:
    """Arguments travel as text and are checked by the server."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


class PrusaConnect:
    """A client for one Prusa Connect server.

    ``url`` is the server origin, such as ``https://connect.local:8080``.
    ``fingerprint`` pins the server's TLS certificate by its SHA-256
    fingerprint, which is how a deployment with its own certificate authority
    is trusted without switching verification off.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        url: str,
        token: str | None = None,
        *,
        fingerprint: str | None = None,
        verify_ssl: bool = True,
        request_timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._session = session
        self.url = url.rstrip("/")
        self._token = token
        self._ssl: aiohttp.Fingerprint | bool = True
        if fingerprint:
            self._ssl = aiohttp.Fingerprint(fingerprint_to_bytes(fingerprint))
        elif not verify_ssl:
            self._ssl = False
        self._timeout = aiohttp.ClientTimeout(total=request_timeout)

    # -- transport ---------------------------------------------------------

    def _headers(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        if extra:
            headers.update(extra)
        return headers

    async def _send(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        body: Any = None,
        headers: Mapping[str, str] | None = None,
        client_timeout: aiohttp.ClientTimeout | None = None,
    ) -> aiohttp.ClientResponse:
        try:
            response = await self._session.request(
                method,
                self.url + path,
                params=params,
                json=body,
                headers=self._headers(headers),
                ssl=self._ssl,
                timeout=client_timeout or self._timeout,
                allow_redirects=False,
            )
        except aiohttp.ServerFingerprintMismatch as err:
            raise CertificateError(
                "the server's certificate does not match the pinned fingerprint",
                fingerprint=err.got.hex(":").upper(),
            ) from err
        except (aiohttp.ClientConnectorCertificateError, aiohttp.ClientSSLError) as err:
            raise CertificateError(f"the server's certificate was not accepted: {err}") from err
        except (aiohttp.ClientError, TimeoutError) as err:
            raise CannotConnectError(f"cannot reach {self.url}: {err!r}") from err
        if response.status >= 400:
            try:
                await _raise_for_problem(response)
            finally:
                response.release()
        return response

    async def _json(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        body: Any = None,
    ) -> Any:
        response = await self._send(method, path, params=params, body=body)
        try:
            return await response.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise CannotConnectError(f"reading {path} failed: {err!r}") from err
        except ValueError as err:
            raise ApiError(response.status, detail=f"{path} did not answer JSON") from err
        finally:
            response.release()

    # -- server ------------------------------------------------------------

    async def health(self) -> Health:
        """Liveness and build version. Takes no credential."""
        return Health.from_dict(await self._json("GET", f"{API}/health"))

    async def request_app_pairing(
        self, app_name: str, scopes: Iterable[str] = (SCOPE_OPERATE,)
    ) -> AppPairing:
        """Ask the server for a token a person approves (RFC 8628). Takes no credential.

        Show :attr:`AppPairing.user_code` and
        :attr:`AppPairing.verification_uri_complete` to the person, then call
        :meth:`wait_for_app_pairing`. The person may approve fewer scopes.
        """
        body = {"app_name": app_name[:64], "scopes": list(scopes)}
        return AppPairing.from_dict(await self._json("POST", f"{API}/auth/app-pairings", body=body))

    async def collect_app_pairing(self, device_code: str) -> str | None:
        """Poll a pairing once. Returns the token, or ``None`` while nobody has decided.

        Raises :class:`PairingDeniedError` or :class:`PairingExpiredError` when
        it is over, and ``ApiError`` with ``APP_PAIRING_SLOW_DOWN`` when the
        app polls too often.
        """
        try:
            answer = await self._json(
                "POST", f"{API}/auth/app-pairings/token", body={"device_code": device_code}
            )
        except ApiError as err:
            if err.code == _PAIRING_PENDING:
                return None
            if err.code == _PAIRING_DENIED:
                raise PairingDeniedError(str(err)) from err
            if err.code in _PAIRING_GONE:
                raise PairingExpiredError(str(err)) from err
            raise
        return str(answer["token"])

    async def wait_for_app_pairing(self, pairing: AppPairing) -> str:
        """Poll a pairing at its interval until the person decides. Returns the token.

        Raises :class:`PairingDeniedError`, or :class:`PairingExpiredError` once
        the pairing has expired.
        """
        interval = pairing.interval
        deadline = asyncio.get_running_loop().time() + pairing.expires_in
        while True:
            await asyncio.sleep(interval)
            if asyncio.get_running_loop().time() > deadline:
                raise PairingExpiredError("the pairing expired before anybody approved it")
            try:
                token = await self.collect_app_pairing(pairing.device_code)
            except ApiError as err:
                if err.code != _PAIRING_SLOW_DOWN:
                    raise
                interval += _SLOW_DOWN_STEP
                continue
            if token is not None:
                return token

    async def setup_status(self) -> SetupStatus:
        """Whether the server's first-run wizard has finished. Takes no credential."""
        return SetupStatus.from_dict(await self._json("GET", f"{API}/setup"))

    async def app_settings(self) -> AppSettings:
        """The name and look of the web app. Takes no credential."""
        return AppSettings.from_dict(await self._json("GET", f"{API}/ui"))

    async def device_states(self) -> DeviceStates:
        """The states and transitions each device kind declared."""
        return DeviceStates.from_dict(await self._json("GET", f"{API}/vocabularies/device-states"))

    # -- devices -----------------------------------------------------------

    async def devices(self) -> list[Device]:
        """Every device the token may observe, walking every page."""
        devices: list[Device] = []
        offset = 0
        while True:
            page = await self._json(
                "GET",
                f"{API}/devices",
                params={"limit": str(DEVICE_PAGE_SIZE), "offset": str(offset), "sort": "id"},
            )
            items = page.get("items") or []
            devices.extend(Device.from_dict(item) for item in items)
            offset += len(items)
            if not items or offset >= int(page.get("total", 0)):
                return devices

    async def device(self, device_id: str) -> Device:
        """One device record."""
        return Device.from_dict(await self._json("GET", f"{API}/devices/{device_id}"))

    # -- commands ----------------------------------------------------------

    async def command(
        self,
        device_id: str,
        verb: str,
        args: Mapping[str, str | int | float | bool] | None = None,
        *,
        key: str | None = None,
    ) -> CommandResult:
        """Send one command and raise :class:`CommandRefusedError` on refusal.

        ``key`` is an idempotency key: while a command under the same key is
        running, the server does not send it a second time.
        """
        body: dict[str, Any] = {"verb": verb}
        if args:
            body["args"] = {name: _format_arg(value) for name, value in args.items()}
        if key:
            body["key"] = key
        result = CommandResult.from_dict(
            await self._json("POST", f"{API}/devices/{device_id}/commands", body=body)
        )
        if not result.ok:
            raise CommandRefusedError(verb, result.fault_code, result.machine_reason)
        return result

    async def pause_print(self, device_id: str) -> CommandResult:
        return await self.command(device_id, VERB_PAUSE_PRINT)

    async def resume_print(self, device_id: str) -> CommandResult:
        return await self.command(device_id, VERB_RESUME_PRINT)

    async def stop_print(self, device_id: str) -> CommandResult:
        return await self.command(device_id, VERB_STOP_PRINT)

    async def set_bed_temperature(self, device: Device, celsius: float) -> CommandResult:
        """Set the bed target. ``0`` switches the heater off."""
        name = _value_argument(device, VERB_SET_BED_TEMPERATURE)
        return await self.command(device.id, VERB_SET_BED_TEMPERATURE, {name: round(celsius)})

    async def set_nozzle_temperature(
        self, device: Device, celsius: float, tool: str | None = None
    ) -> CommandResult:
        """Set a nozzle target. ``0`` switches the heater off.

        ``tool`` is the head's 0-based tool id, such as ``"1"``. Without it
        the printer heats the head it has picked. A printer whose verb takes
        no tool argument has one head, so ``"0"`` is sent without one.
        """
        verb = VERB_SET_NOZZLE_TEMPERATURE
        name = _value_argument(device, verb)
        args: dict[str, str | int | float | bool] = {name: round(celsius)}
        if tool is not None:
            parsed = parse_tool_id(tool)
            if parsed is None or parsed[1] is not None:
                raise UnsupportedError(f"{tool!r} is not a head")
            if ARG_TOOL in device.arguments(verb):
                args[ARG_TOOL] = parsed[0]
            elif parsed[0] != 0:
                raise UnsupportedError(f"{device.id} takes no tool argument for {verb}")
        return await self.command(device.id, verb, args)

    async def set_speed(self, device: Device, percent: float) -> CommandResult:
        name = _value_argument(device, VERB_SET_SPEED)
        return await self.command(device.id, VERB_SET_SPEED, {name: round(percent)})

    async def set_flow(self, device: Device, percent: float) -> CommandResult:
        name = _value_argument(device, VERB_SET_FLOW)
        return await self.command(device.id, VERB_SET_FLOW, {name: round(percent)})

    async def set_chamber_temperature(self, device: Device, celsius: float) -> CommandResult:
        """Set the chamber target."""
        name = _value_argument(device, VERB_SET_CHAMBER_TEMPERATURE)
        return await self.command(device.id, VERB_SET_CHAMBER_TEMPERATURE, {name: round(celsius)})

    async def set_chamber_fan(self, device: Device, percent: float | None) -> CommandResult:
        """Set the chamber fan target. ``None`` hands the fans back to the printer."""
        name = _value_argument(device, VERB_SET_CHAMBER_FAN)
        value = CHAMBER_FAN_AUTOMATIC if percent is None else min(max(round(percent), 0), 100)
        return await self.command(device.id, VERB_SET_CHAMBER_FAN, {name: value})

    async def set_chamber_light(self, device: Device, percent: float) -> CommandResult:
        """Set the chamber light intensity. ``0`` switches it off."""
        name = _value_argument(device, VERB_SET_CHAMBER_LIGHT)
        return await self.command(device.id, VERB_SET_CHAMBER_LIGHT, {name: min(max(round(percent), 0), 100)})

    async def dialog_action(self, device: Device, button: str, dialog_id: str | None = None) -> CommandResult:
        """Press a button of the dialog the printer shows.

        ``button`` is matched without regard to case against the dialog's
        buttons and sent as the printer spells it. ``dialog_id`` guards against
        answering a dialog that has since been replaced.
        """
        if not device.supports(VERB_DIALOG_ACTION):
            raise UnsupportedError(f"{device.id} does not accept {VERB_DIALOG_ACTION}")
        dialog = device.dialog
        if dialog is None:
            raise UnsupportedError(f"{device.id} shows no dialog")
        if dialog_id is not None and dialog_id != dialog.id:
            raise UnsupportedError(f"dialog {dialog_id} is no longer shown")
        match = next((b for b in dialog.buttons if b.casefold() == button.casefold()), None)
        if match is None:
            raise UnsupportedError(f"the dialog has no {button!r} button")
        return await self.command(
            device.id, VERB_DIALOG_ACTION, {ARG_DIALOG_ID: dialog.id, ARG_BUTTON: match}
        )

    # -- files ---------------------------------------------------------------

    async def thumbnail(self, device_id: str, path: str, etag: str | None = None) -> Thumbnail | None:
        """The picture inside a print file on a printer's storage.

        ``None`` is a file that carries no picture, or, with ``etag``, a
        picture that has not changed. Reading it takes *operate*.
        """
        headers = {"Accept": "image/png, image/jpeg"}
        if etag:
            headers["If-None-Match"] = etag
        try:
            response = await self._send(
                "GET", f"{API}/devices/{device_id}/files/thumbnail", params={"path": path}, headers=headers
            )
        except NotFoundError:
            return None
        try:
            if response.status in (204, 304):
                return None
            content = await response.read()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise CannotConnectError(f"reading the thumbnail of {path} failed: {err!r}") from err
        finally:
            response.release()
        return Thumbnail(
            content=content,
            content_type=response.headers.get("Content-Type", "image/png"),
            etag=response.headers.get("ETag"),
        )

    # -- Materials extension -------------------------------------------------

    async def materials_spools(self) -> list[Spool]:
        """Every spool the Materials extension keeps, walking every page.

        A server without the extension answers :class:`NotFoundError`.
        """
        spools: list[Spool] = []
        offset = 0
        while True:
            page = await self._json(
                "GET",
                f"{API}/modules/{MATERIALS_MODULE}/spools",
                params={"limit": str(SPOOL_PAGE_SIZE), "offset": str(offset)},
            )
            items = page.get("spools") or []
            spools.extend(Spool.from_dict(item) for item in items)
            offset += len(items)
            if not items or offset >= int(page.get("total", 0)):
                return spools

    async def materials_assignments(self, device_id: str) -> list[MaterialAssignment]:
        """The material each tool of a printer is set to in the Materials extension."""
        data = await self._json("GET", f"{API}/modules/{MATERIALS_MODULE}/printers/{device_id}/tools")
        return [MaterialAssignment.from_dict(item) for item in data.get("tools") or []]

    # -- cameras -----------------------------------------------------------

    async def cameras(self, device_id: str | None = None) -> list[Camera]:
        """The cameras the token may see, optionally of one device."""
        params = {"device": device_id} if device_id else None
        data = await self._json("GET", f"{API}/cameras", params=params)
        return [Camera.from_dict(item) for item in data.get("items") or []]

    async def snapshot(self, camera: str, if_modified_since: str | None = None) -> Snapshot | None:
        """The most recent still image, or ``None`` when there is no new one.

        A camera that has produced no frame yet answers 204, and a copy that
        is still current answers 304; both come back as ``None``.
        """
        headers = {"Accept": "image/jpeg"}
        if if_modified_since:
            headers["If-Modified-Since"] = if_modified_since
        response = await self._send("GET", f"{API}/cameras/{camera}/snapshot", headers=headers)
        try:
            if response.status in (204, 304):
                return None
            content = await response.read()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise CannotConnectError(f"reading a snapshot of {camera} failed: {err!r}") from err
        finally:
            response.release()
        return Snapshot(
            content=content,
            content_type=response.headers.get("Content-Type", "image/jpeg"),
            last_modified=response.headers.get("Last-Modified"),
        )

    async def camera_stream(self, camera: str) -> aiohttp.ClientResponse:
        """Open a camera's multipart live view. The caller releases it."""
        return await self._send(
            "GET",
            f"{API}/cameras/{camera}/stream",
            headers={"Accept": "multipart/x-mixed-replace"},
            client_timeout=aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=30),
        )

    # -- events ------------------------------------------------------------

    async def events(
        self,
        *,
        types: Iterable[str] | None = None,
        last_event_id: str | None = None,
        min_backoff: float = 1.0,
        max_backoff: float = 60.0,
        read_timeout: float = STREAM_READ_TIMEOUT,
    ) -> AsyncIterator[StreamItem]:
        """Read the event stream for as long as the caller iterates.

        The stream reconnects on its own with exponential backoff and resumes
        from the last frame id it saw. It yields :class:`StreamConnected` on
        every connection and :class:`StreamDisconnected` on every drop, so a
        caller knows when telemetry has a gap and must be refetched. A token
        that is refused, or expires while the stream is open, raises
        :class:`AuthenticationError`; a refused request raises its
        :class:`ApiError`.
        """
        params = {"types": ",".join(types)} if types else None
        parser = SSEParser(last_event_id)
        connected_before = False
        backoff = min_backoff
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=read_timeout)
        while True:
            headers = {"Accept": "text/event-stream", "Cache-Control": "no-cache"}
            if parser.last_event_id:
                headers["Last-Event-ID"] = parser.last_event_id
            error: Exception | None = None
            try:
                response = await self._send(
                    "GET", f"{API}/events", params=params, headers=headers, client_timeout=timeout
                )
                try:
                    yield StreamConnected(reconnect=connected_before)
                    connected_before = True
                    backoff = min_backoff
                    async for item in _read_frames(response, parser):
                        if item.type == EVENT_AUTH_EXPIRED:
                            raise AuthenticationError(401, detail="the credential expired")
                        yield item
                finally:
                    response.release()
            except (AuthenticationError, ForbiddenError, NotFoundError, CertificateError):
                raise
            except ApiError as err:
                if err.status < 500 and err.status != 429:
                    raise
                error = err
            except CannotConnectError as err:
                error = err
            except (aiohttp.ClientError, TimeoutError) as err:
                error = CannotConnectError(f"the event stream dropped: {err!r}")
            if parser.retry is not None:
                backoff = max(backoff, parser.retry / 1000)
            delay = min(backoff, max_backoff) * random.uniform(0.8, 1.2)  # noqa: S311 - jitter, not a secret
            _LOGGER.debug("event stream ended (%s), reconnecting in %.1fs", error, delay)
            yield StreamDisconnected(error=error, retry_in=delay)
            await asyncio.sleep(delay)
            backoff = min(backoff * 2, max_backoff)


async def _read_frames(response: aiohttp.ClientResponse, parser: SSEParser) -> AsyncIterator[Event]:
    while True:
        raw = await response.content.readline()
        if not raw:
            return
        frame = parser.feed(raw.decode("utf-8", errors="replace").rstrip("\r\n"))
        if frame is None:
            continue
        try:
            envelope = json.loads(frame.data)
        except ValueError:
            _LOGGER.debug("skipping a frame that is not JSON: %s", frame.event)
            continue
        if not isinstance(envelope, dict):
            continue
        yield Event(type=str(envelope.get("type") or frame.event), envelope=envelope, id=frame.id)


def _value_argument(device: Device, verb: str) -> str:
    """The one argument of a setter verb that carries the value.

    Drivers name it differently, so it is read from the device's capability
    set rather than assumed.
    """
    if not device.supports(verb):
        raise UnsupportedError(f"{device.id} does not accept {verb}")
    names = [n for n in device.arguments(verb) if n != ARG_TOOL]
    if len(names) != 1:
        raise UnsupportedError(f"{device.id} declares no single value argument for {verb}")
    return names[0]


async def _raise_for_problem(response: aiohttp.ClientResponse) -> None:
    problem: dict[str, Any] = {}
    try:
        decoded = await response.json(content_type=None)
        if isinstance(decoded, dict):
            problem = decoded
    except (aiohttp.ClientError, TimeoutError, ValueError):
        pass
    status = response.status
    kwargs: dict[str, Any] = {
        "code": problem.get("code"),
        "title": problem.get("title"),
        "detail": problem.get("detail"),
        "params": problem.get("params") if isinstance(problem.get("params"), dict) else None,
    }
    if status == 401:
        raise AuthenticationError(status, **kwargs)
    if status == 403:
        raise ForbiddenError(status, **kwargs)
    if status == 404:
        raise NotFoundError(status, **kwargs)
    raise ApiError(status, **kwargs)
