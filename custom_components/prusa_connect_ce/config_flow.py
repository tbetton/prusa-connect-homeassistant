"""Config flow for Prusa Connect."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Mapping
from contextlib import suppress
from ipaddress import ip_address
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import voluptuous as vol
from homeassistant.config_entries import (
    SOURCE_REAUTH,
    SOURCE_RECONFIGURE,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_HOST, CONF_TOKEN, CONF_URL
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .const import (
    CONF_CAMERAS,
    CONF_FINGERPRINT,
    CONF_INSTANCE_ID,
    CONF_MODE,
    CONF_PANEL_URL,
    CONF_PORT,
    CONF_PRERELEASE,
    CONF_SIDEBAR,
    CONF_UPDATE_THROTTLE,
    DEFAULT_CAMERAS,
    DEFAULT_PORT,
    DEFAULT_PRERELEASE,
    DEFAULT_SIDEBAR,
    DEFAULT_UPDATE_THROTTLE,
    DOMAIN,
    MODE_EMBEDDED,
    MODE_EXTERNAL,
    PRODUCT_NAME,
)
from .coordinator import PrusaConnectConfigEntry
from .embedded import browser_url, default_host, embedded_server, host_options, release_settings
from .library import (
    SCOPE_OPERATE,
    ApiError,
    AppPairing,
    AuthenticationError,
    CannotConnectError,
    CertificateError,
    ForbiddenError,
    PairingDeniedError,
    PairingExpiredError,
    PrusaConnect,
    PrusaConnectError,
    fetch_certificate_fingerprint,
)
from .panel import default_panel_url
from .releases import ReleaseError, fetch_releases, machine_asset, pick_release
from .server import EmbeddedServer, ServerError

_LOGGER = logging.getLogger(__name__)

URL_SCHEMA = vol.Schema({vol.Required(CONF_URL): TextSelector(TextSelectorConfig(type=TextSelectorType.URL))})
TOKEN_SCHEMA = vol.Schema(
    {vol.Required(CONF_TOKEN): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))}
)
# How often the flow asks whether the first-run wizard has finished.
FIRST_RUN_POLL = 3
# How long the server may not answer while the flow waits for the wizard. A
# wizard that changes the configuration restarts the server, and its port is
# closed until the new process listens.
FIRST_RUN_RESTART_GRACE = 120


def normalize_url(url: str) -> str:
    """The server origin: scheme, host and port, without a path."""
    url = url.strip()
    if "://" not in url:
        url = f"http://{url}"
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(url)
    return f"{parts.scheme}://{parts.netloc}".rstrip("/")


def normalize_host(raw: str) -> str:
    """A host name or IP address, also taken out of a pasted address."""
    raw = raw.strip()
    with suppress(ValueError):
        return str(ip_address(raw.strip("[]")))
    return str(urlsplit(normalize_url(raw)).hostname)


def _host_port(url: str) -> tuple[str, int]:
    parts = urlsplit(url)
    assert parts.hostname is not None
    return parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)


class PrusaConnectConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up a connection to one Prusa Connect server."""

    VERSION = 1
    MINOR_VERSION = 1

    def __init__(self) -> None:
        self._url: str | None = None
        self._fingerprint: str | None = None
        self._offered_fingerprint: str | None = None
        self._title: str | None = None
        self._pairing: AppPairing | None = None
        self._pairing_task: asyncio.Task[str] | None = None
        self._token: str | None = None
        self._server_id: str | None = None
        self._mode = MODE_EXTERNAL
        self._port = DEFAULT_PORT
        self._host: str | None = None
        self._server: EmbeddedServer | None = None
        self._task: asyncio.Task[Any] | None = None
        self._open = False
        self._finished = False

    def _client(self, token: str | None = None) -> PrusaConnect:
        assert self._url is not None
        return PrusaConnect(
            async_get_clientsession(self.hass), self._url, token, fingerprint=self._fingerprint
        )

    async def _async_probe(self) -> str | None:
        """Reach the server without a token. Returns an error key, or None.

        A certificate the system does not trust leads to the step that offers
        to pin it.
        """
        try:
            health = await self._client().health()
            settings = await self._client().app_settings()
        except CertificateError:
            if self._fingerprint is not None:
                return "certificate_changed"
            assert self._url is not None
            host, port = _host_port(self._url)
            try:
                self._offered_fingerprint = await fetch_certificate_fingerprint(host, port)
            except CannotConnectError:
                return "cannot_connect"
            return "untrusted_certificate"
        except CannotConnectError:
            return "cannot_connect"
        except ApiError:
            return "not_prusa_connect"
        self._title = settings.title or None
        self._server_id = health.instance_id
        return None

    async def _async_check_token(self, token: str) -> str | None:
        try:
            await self._client(token).devices()
        except AuthenticationError:
            return "invalid_auth"
        except ForbiddenError:
            return "insufficient_permissions"
        except CannotConnectError:
            return "cannot_connect"
        except PrusaConnectError:
            _LOGGER.exception("Unexpected answer while checking the token")
            return "unknown"
        return None

    async def _async_after_url(self) -> ConfigFlowResult:
        """Continue once the URL is known: certificate, then the token."""
        error = await self._async_probe()
        if error == "untrusted_certificate":
            return await self.async_step_certificate()
        if error is not None:
            return self.async_abort(reason=error) if self.source == "zeroconf" else self._url_form(error)
        if self.source == SOURCE_RECONFIGURE:
            return await self._async_finish_reconfigure()
        if self._server_id:
            # One server is one entry, whichever address it was found or typed at. A
            # person who types the address takes over a discovered flow of the server.
            await self.async_set_unique_id(self._server_id, raise_on_progress=False)
            self._abort_if_unique_id_configured()
        return await self.async_step_auth()

    def _url_form(self, error: str) -> ConfigFlowResult:
        step = "reconfigure" if self.source == SOURCE_RECONFIGURE else "external"
        return self.async_show_form(
            step_id=step,
            data_schema=self.add_suggested_values_to_schema(URL_SCHEMA, {CONF_URL: self._url}),
            errors={"base": error},
        )

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Run the server inside Home Assistant, or connect to one that runs elsewhere."""
        return self.async_show_menu(step_id="user", menu_options=["embedded", "external"])

    async def async_step_external(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for the server's address."""
        if user_input is None:
            return self.async_show_form(step_id="external", data_schema=URL_SCHEMA)
        try:
            self._url = normalize_url(user_input[CONF_URL])
        except ValueError:
            return self.async_show_form(
                step_id="external", data_schema=URL_SCHEMA, errors={CONF_URL: "invalid_url"}
            )
        self._async_abort_entries_match({CONF_URL: self._url})
        return await self._async_after_url()

    async def async_step_zeroconf(self, discovery_info: ZeroconfServiceInfo) -> ConfigFlowResult:
        """A server announced itself as _prusa-connect._tcp."""
        properties = discovery_info.properties
        if properties.get("api", "v1") != "v1":
            return self.async_abort(reason="not_supported")
        scheme = "https" if properties.get("tls") == "1" else "http"
        host = discovery_info.host
        if ":" in host:
            host = f"[{host}]"
        self._url = f"{scheme}://{host}:{discovery_info.port}"
        self._async_abort_entries_match({CONF_URL: self._url})
        # A server announces itself on every address it has: its id keeps that
        # to one discovered flow, and to none once the server is set up.
        if server_id := properties.get("id"):
            await self.async_set_unique_id(server_id)
            self._abort_if_unique_id_configured()
        name = discovery_info.name.removesuffix(f".{discovery_info.type}") or PRODUCT_NAME
        self.context["title_placeholders"] = {"name": name}
        self._title = name
        return await self.async_step_zeroconf_confirm()

    async def async_step_zeroconf_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask before connecting to a discovered server."""
        if user_input is None:
            self._set_confirm_only()
            return self.async_show_form(
                step_id="zeroconf_confirm",
                description_placeholders={"name": self._title or PRODUCT_NAME, "url": self._url or ""},
            )
        return await self._async_after_url()

    async def async_step_certificate(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Offer to trust the server's certificate by its fingerprint."""
        if user_input is None:
            return self.async_show_form(
                step_id="certificate",
                description_placeholders={
                    "url": self._url or "",
                    "fingerprint": self._offered_fingerprint or "",
                },
            )
        self._fingerprint = self._offered_fingerprint
        return await self._async_after_url()

    # -- the server inside Home Assistant -----------------------------------

    async def async_step_embedded(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask which port the server listens on, and at which address browsers reach this machine."""
        self._async_abort_entries_match({CONF_MODE: MODE_EMBEDDED})
        schema = vol.Schema(
            {
                vol.Required(CONF_PORT, default=DEFAULT_PORT): NumberSelector(
                    NumberSelectorConfig(min=1024, max=65535, step=1, mode=NumberSelectorMode.BOX)
                ),
                vol.Required(CONF_HOST, default=default_host(self.hass)): SelectSelector(
                    SelectSelectorConfig(
                        options=host_options(self.hass), custom_value=True, mode=SelectSelectorMode.DROPDOWN
                    )
                ),
            }
        )
        if user_input is None:
            return self.async_show_form(step_id="embedded", data_schema=schema)
        try:
            self._host = normalize_host(user_input[CONF_HOST])
        except ValueError:
            return self.async_show_form(
                step_id="embedded",
                data_schema=self.add_suggested_values_to_schema(schema, user_input),
                errors={CONF_HOST: "invalid_host"},
            )
        self._mode = MODE_EMBEDDED
        self._port = int(user_input[CONF_PORT])
        self._server = embedded_server(self.hass, self._port)
        await self._server.async_load()
        self._url = self._server.url
        return await self.async_step_install()

    async def _async_install(self) -> None:
        assert self._server is not None
        if self._server.installed() is None:
            settings = release_settings(self.hass)
            try:
                releases = await fetch_releases(async_get_clientsession(self.hass), settings.url)
                release = pick_release(releases, DEFAULT_PRERELEASE, machine_asset())
            except ReleaseError as err:
                raise ServerError(str(err)) from err
            await self._server.install(release)
        elif not self._server.running:
            await self._server.start()

    async def async_step_install(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Download, verify and start the newest release."""
        if self._task is None:
            self._task = self.hass.async_create_task(self._async_install())
        if not self._task.done():
            return self.async_show_progress(
                step_id="install", progress_action="install", progress_task=self._task
            )
        task, self._task = self._task, None
        try:
            task.result()
        except ServerError:
            _LOGGER.exception("Installing the Prusa Connect server failed")
            return self.async_show_progress_done(next_step_id="install_failed")
        return self.async_show_progress_done(next_step_id="first_run")

    async def async_step_install_failed(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self._server is not None:
            await self._server.stop()
        return self.async_abort(reason="install_failed")

    async def _async_wait_first_run(self) -> None:
        client = self._client()
        loop = asyncio.get_running_loop()
        answered = loop.time()
        # The server announces nothing when the wizard ends, so it is asked.
        while True:
            try:
                status = await client.setup_status()
            except CannotConnectError:
                if loop.time() - answered > FIRST_RUN_RESTART_GRACE:
                    raise
            else:
                answered = loop.time()
                if status.done:
                    break
            await asyncio.sleep(FIRST_RUN_POLL)
        self._open = status.mode == "open"

    async def async_step_first_run(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Wait while the person finishes the server's first-run wizard in the browser."""
        if self._task is None:
            error = await self._async_probe()
            if error is not None:
                return self.async_abort(reason=error)
            self._task = self.hass.async_create_task(self._async_wait_first_run())
        if not self._task.done():
            assert self._server is not None
            return self.async_show_progress(
                step_id="first_run",
                progress_action="first_run",
                description_placeholders={
                    "url": self._browser_base(),
                    "code": self._server.setup_code() or "",
                },
                progress_task=self._task,
            )
        task, self._task = self._task, None
        try:
            task.result()
        except PrusaConnectError:
            _LOGGER.exception("Reading the first-run state failed")
            return self.async_show_progress_done(next_step_id="pairing_failed")
        await self._async_probe()
        if self._server_id:
            await self.async_set_unique_id(self._server_id, raise_on_progress=False)
        return self.async_show_progress_done(next_step_id="first_run_done")

    async def async_step_first_run_done(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        # A progress step may only end its progress, so a server already added
        # is turned away here. Aborting in the progress step leaves the window
        # waiting for good.
        self._abort_if_unique_id_configured()
        if self._open:
            # A server in open mode serves every request without a credential.
            return self._async_create("")
        return await self.async_step_pair()

    def _browser_base(self) -> str:
        """The embedded server as the browser reaches it."""
        return browser_url(self._host or default_host(self.hass), self._port)

    def _browser(self, url: str) -> str:
        """A link the server made for itself, as the browser reaches the embedded server."""
        if self._mode != MODE_EMBEDDED:
            return url
        parts, base = urlsplit(url), urlsplit(self._browser_base())
        return urlunsplit((base.scheme, base.netloc, parts.path, parts.query, parts.fragment))

    @callback
    def async_remove(self) -> None:
        """Stop polling, and stop a server this flow started but never kept."""
        for task in (self._pairing_task, self._task):
            if task is not None:
                task.cancel()
        if self._server is not None and not self._finished:
            self.hass.async_create_task(self._server.stop())

    # -- signing in --------------------------------------------------------

    async def async_step_auth(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Pair with a code approved in Prusa Connect, or paste a token."""
        return self.async_show_menu(
            step_id="auth", menu_options=["pair", "token"], description_placeholders={"url": self._url or ""}
        )

    def _app_name(self) -> str:
        location = self.hass.config.location_name
        return f"Home Assistant ({location})" if location else "Home Assistant"

    async def async_step_pair(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show a pairing code and wait until a person approves it in Prusa Connect."""
        if self._pairing_task is None:
            client = self._client()
            try:
                self._pairing = await client.request_app_pairing(self._app_name(), (SCOPE_OPERATE,))
            except CannotConnectError:
                return self.async_abort(reason="cannot_connect")
            except PrusaConnectError:
                _LOGGER.exception("The server refused to start a pairing")
                return self.async_abort(reason="pairing_unavailable")
            self._pairing_task = self.hass.async_create_task(client.wait_for_app_pairing(self._pairing))
        if not self._pairing_task.done():
            assert self._pairing is not None
            return self.async_show_progress(
                step_id="pair",
                progress_action="wait_for_approval",
                description_placeholders={
                    "code": self._pairing.user_code,
                    "url": self._browser(self._pairing.verification_uri_complete),
                },
                progress_task=self._pairing_task,
            )
        task, self._pairing_task = self._pairing_task, None
        try:
            self._token = task.result()
        except PairingDeniedError:
            return self.async_show_progress_done(next_step_id="pairing_denied")
        except PairingExpiredError:
            return self.async_show_progress_done(next_step_id="pairing_expired")
        except PrusaConnectError:
            _LOGGER.exception("Waiting for the pairing failed")
            return self.async_show_progress_done(next_step_id="pairing_failed")
        return self.async_show_progress_done(next_step_id="pair_finish")

    async def async_step_pair_finish(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """The person approved: keep the token the pairing handed out."""
        assert self._token is not None
        if (error := await self._async_check_token(self._token)) is not None:
            return self.async_abort(reason=error)
        if self.source == SOURCE_REAUTH:
            return self.async_update_reload_and_abort(
                self._get_reauth_entry(), data_updates={CONF_TOKEN: self._token}
            )
        return self._async_create(self._token)

    async def async_step_pairing_denied(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_abort(reason="pairing_denied")

    async def async_step_pairing_expired(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_abort(reason="pairing_expired")

    async def async_step_pairing_failed(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_abort(reason="cannot_connect")

    def _async_create(self, token: str) -> ConfigFlowResult:
        if self.source == SOURCE_RECONFIGURE:
            return self._async_update_reconfigured(token)
        assert self._url is not None
        data: dict[str, Any] = {
            CONF_URL: self._url,
            CONF_TOKEN: token,
            CONF_INSTANCE_ID: uuid.uuid4().hex,
            CONF_MODE: self._mode,
        }
        if self._mode == MODE_EMBEDDED:
            data[CONF_PORT] = self._port
            data[CONF_HOST] = self._host
        if self._fingerprint:
            data[CONF_FINGERPRINT] = self._fingerprint
        self._finished = True
        return self.async_create_entry(title=self._title or PRODUCT_NAME, data=data)

    async def async_step_token(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for an API token minted in Prusa Connect."""
        errors: dict[str, str] = {}
        if user_input is not None:
            token = user_input[CONF_TOKEN].strip()
            if (error := await self._async_check_token(token)) is None:
                return self._async_create(token)
            errors["base"] = error
        return self.async_show_form(
            step_id="token",
            data_schema=TOKEN_SCHEMA,
            errors=errors,
            description_placeholders={"url": self._url or ""},
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """The token stopped working."""
        self._url = entry_data[CONF_URL]
        self._fingerprint = entry_data.get(CONF_FINGERPRINT)
        if entry_data.get(CONF_MODE) == MODE_EMBEDDED:
            self._mode = MODE_EMBEDDED
            self._port = entry_data.get(CONF_PORT, DEFAULT_PORT)
            self._host = entry_data.get(CONF_HOST)
            self._finished = True
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Pair again, or paste a new token."""
        return self.async_show_menu(
            step_id="reauth_confirm",
            menu_options=["pair", "reauth_token"],
            description_placeholders={"url": self._url or ""},
        )

    async def async_step_reauth_token(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for a new token."""
        errors: dict[str, str] = {}
        if user_input is not None:
            token = user_input[CONF_TOKEN].strip()
            if (error := await self._async_check_token(token)) is None:
                return self.async_update_reload_and_abort(
                    self._get_reauth_entry(), data_updates={CONF_TOKEN: token}
                )
            errors["base"] = error
        return self.async_show_form(
            step_id="reauth_token",
            data_schema=TOKEN_SCHEMA,
            errors=errors,
            description_placeholders={"url": self._url or ""},
        )

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Change the server's address, for example after it moved."""
        entry = self._get_reconfigure_entry()
        if entry.data.get(CONF_MODE) == MODE_EMBEDDED:
            return self.async_abort(reason="embedded_has_no_address")
        if user_input is None:
            return self.async_show_form(
                step_id="reconfigure",
                data_schema=self.add_suggested_values_to_schema(URL_SCHEMA, {CONF_URL: entry.data[CONF_URL]}),
            )
        try:
            self._url = normalize_url(user_input[CONF_URL])
        except ValueError:
            return self.async_show_form(
                step_id="reconfigure", data_schema=URL_SCHEMA, errors={CONF_URL: "invalid_url"}
            )
        self._fingerprint = None
        return await self._async_after_url()

    async def _async_finish_reconfigure(self) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        if entry.unique_id and self._server_id and self._server_id != entry.unique_id:
            # A token that another server accepts would swap the printers under this entry.
            return self.async_abort(reason="wrong_server")
        error = await self._async_check_token(entry.data[CONF_TOKEN])
        if error == "invalid_auth":
            # A server that moved may have ended the token too: sign in again at the new address.
            return await self.async_step_auth()
        if error is not None:
            return self._url_form(error)
        return self._async_update_reconfigured(entry.data[CONF_TOKEN])

    def _async_update_reconfigured(self, token: str) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        data = {**entry.data, CONF_URL: self._url, CONF_TOKEN: token}
        data.pop(CONF_FINGERPRINT, None)
        if self._fingerprint:
            data[CONF_FINGERPRINT] = self._fingerprint
        return self.async_update_reload_and_abort(entry, data=data)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: PrusaConnectConfigEntry) -> OptionsFlowHandler:
        return OptionsFlowHandler()


class OptionsFlowHandler(OptionsFlowWithReload):
    """Options that change what the integration creates and how often it writes."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            panel = (user_input.get(CONF_PANEL_URL) or "").strip()
            if not panel or panel == default_panel_url(self.hass, self.config_entry):
                user_input.pop(CONF_PANEL_URL, None)
                return self.async_create_entry(data=user_input)
            parts = urlsplit(panel)
            if parts.scheme in ("http", "https") and parts.hostname:
                return self.async_create_entry(data={**user_input, CONF_PANEL_URL: panel})
            errors[CONF_PANEL_URL] = "invalid_url"
        schema = vol.Schema(
            {
                vol.Required(CONF_CAMERAS, default=DEFAULT_CAMERAS): bool,
                vol.Required(CONF_UPDATE_THROTTLE, default=DEFAULT_UPDATE_THROTTLE): NumberSelector(
                    NumberSelectorConfig(
                        min=0.2, max=30, step=0.1, unit_of_measurement="s", mode=NumberSelectorMode.BOX
                    )
                ),
                vol.Required(CONF_SIDEBAR, default=DEFAULT_SIDEBAR): bool,
                vol.Optional(CONF_PANEL_URL): TextSelector(TextSelectorConfig(type=TextSelectorType.URL)),
            }
        )
        if self.config_entry.data.get(CONF_MODE) == MODE_EMBEDDED:
            schema = schema.extend({vol.Required(CONF_PRERELEASE, default=DEFAULT_PRERELEASE): bool})
        suggested = {
            CONF_PANEL_URL: default_panel_url(self.hass, self.config_entry),
            **self.config_entry.options,
        }
        if user_input is not None:
            suggested.update(user_input)
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(schema, suggested),
            errors=errors,
        )
