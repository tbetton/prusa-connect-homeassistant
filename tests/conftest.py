"""Fixtures for the integration tests: a client that answers recorded records."""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import AsyncIterator, Callable, Generator, Iterable, Mapping
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.const import CONF_TOKEN, CONF_URL
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.prusa_connect.const import CONF_INSTANCE_ID, DOMAIN
from custom_components.prusa_connect.library import (
    AppPairing,
    AppSettings,
    Camera,
    CommandResult,
    Device,
    DeviceStates,
    Health,
    MaterialAssignment,
    NotFoundError,
    PrusaConnect,
    SetupStatus,
    Snapshot,
    Spool,
    StreamItem,
    Thumbnail,
)
from custom_components.prusa_connect.releases import Release
from custom_components.prusa_connect.server import Installed

FIXTURES = Path(__file__).parent.parent / "lib" / "tests" / "fixtures"

CORE_ONE = "637d5756-4645-4a08-906f-e8dbb9afc56f"
MK4S_MMU = "6d3ea1c6-dfef-4de7-8823-82cc20c3b14e"
XL = "e34e1d60-3393-4d6b-a709-edb7f01cfaea"
XL_DISABLED = "e34e1d60-3393-4d6b-a709-edb7f01cfae2"
KLIPPER = "ec55cbbb-8976-4cde-87ba-b96e1b7c948b"
INSTANCE = "0123456789abcdef0123456789abcdef"
URL = "http://connect.local:8080"
TOKEN = "pcl_test"
PAIRING = AppPairing(
    device_code="secret-device-code",
    user_code="BCDF-GHJK",
    verification_uri=f"{URL}/pair",
    verification_uri_complete=f"{URL}/pair?code=BCDF-GHJK",
    expires_in=600,
    interval=5,
)


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


RECORDS = {
    CORE_ONE: "device_coreone.json",
    MK4S_MMU: "device_mk4s_mmu.json",
    XL: "device_xl.json",
    XL_DISABLED: "device_xl_disabled_head.json",
    KLIPPER: "device_klipper.json",
}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load the integration from custom_components."""


class FakeClient(PrusaConnect):
    """The library client with its network replaced by recorded records.

    Commands go through the library's own argument handling and are recorded,
    so a test sees exactly the verb and arguments the server would get.
    """

    def __init__(self, records: Iterable[str] = RECORDS.values()) -> None:
        self.records: dict[str, dict[str, Any]] = {}
        for name in records:
            record = load(name)
            self.records[record["id"]] = record
        self.cameras_data = load("cameras.json")["items"]
        self.commands: list[tuple[str, str, dict[str, Any]]] = []
        self.thumbnail_requests: list[tuple[str, str]] = []
        # The Materials extension: absent unless a test sets these.
        self.spools: list[dict[str, Any]] | None = None
        self.material_tools: dict[str, list[dict[str, Any]]] = {}
        self.command_error: Exception | None = None
        self.error: Exception | None = None
        self.devices_calls = 0
        self.device_calls: list[str] = []
        self.queue: asyncio.Queue[StreamItem | Exception] = asyncio.Queue()
        # What a stream that asks for some event types only reads.
        self.typed_queue: asyncio.Queue[StreamItem | Exception] = asyncio.Queue()
        self.url = URL
        # Pairing: what was asked, and what the wait ends with once ``approve`` is set.
        self.pairing_requests: list[tuple[str, tuple[str, ...]]] = []
        self.pairing_error: Exception | None = None
        self.pairing_outcome: str | Exception = "pcl_paired"
        self.approve = asyncio.Event()
        self.pairing_cancelled = False
        self.server_id: str | None = None
        # The first-run state: it reads pending until ``first_run`` is set.
        self.first_run = asyncio.Event()
        self.first_run.set()
        self.setup_mode = "required"

    def record(self, device_id: str) -> dict[str, Any]:
        return self.records[device_id]

    async def health(self) -> Health:
        if self.error:
            raise self.error
        return Health("ok", "1.2.3", self.server_id)

    async def app_settings(self) -> AppSettings:
        if self.error:
            raise self.error
        return AppSettings("Workshop", True, "en")

    async def setup_status(self) -> SetupStatus:
        if self.error:
            raise self.error
        if not self.first_run.is_set():
            await self.first_run.wait()
            return SetupStatus(False, None)
        return SetupStatus(True, self.setup_mode)

    async def request_app_pairing(self, app_name: str, scopes: Iterable[str] = ("operate",)) -> AppPairing:
        if self.pairing_error:
            raise self.pairing_error
        self.pairing_requests.append((app_name, tuple(scopes)))
        return PAIRING

    async def wait_for_app_pairing(self, pairing: AppPairing) -> str:
        try:
            await self.approve.wait()
        except asyncio.CancelledError:
            self.pairing_cancelled = True
            raise
        if isinstance(self.pairing_outcome, Exception):
            raise self.pairing_outcome
        return self.pairing_outcome

    async def device_states(self) -> DeviceStates:
        return DeviceStates()

    async def devices(self) -> list[Device]:
        self.devices_calls += 1
        if self.error:
            raise self.error
        return [Device.from_dict(copy.deepcopy(r)) for r in self.records.values()]

    async def device(self, device_id: str) -> Device:
        self.device_calls.append(device_id)
        if self.error:
            raise self.error
        return Device.from_dict(copy.deepcopy(self.records[device_id]))

    async def cameras(self, device_id: str | None = None) -> list[Camera]:
        if self.error:
            raise self.error
        return [Camera.from_dict(c) for c in self.cameras_data]

    async def snapshot(self, camera: str, if_modified_since: str | None = None) -> Snapshot | None:
        if self.error:
            raise self.error
        return Snapshot((FIXTURES / "snapshot.jpg").read_bytes(), "image/jpeg", "Sat, 24 Oct 2026")

    async def materials_spools(self) -> list[Spool]:
        if self.spools is None:
            raise NotFoundError(404, code="core.validation/ROUTE_NOT_FOUND")
        if self.error:
            raise self.error
        return [Spool.from_dict(s) for s in self.spools]

    async def materials_assignments(self, device_id: str) -> list[MaterialAssignment]:
        if self.spools is None or device_id not in self.material_tools:
            raise NotFoundError(404, code="materials/NOT_FOUND")
        return [MaterialAssignment.from_dict(t) for t in self.material_tools[device_id]]

    async def thumbnail(self, device_id: str, path: str, etag: str | None = None) -> Thumbnail | None:
        self.thumbnail_requests.append((device_id, path))
        if self.error:
            raise self.error
        return Thumbnail(b"png:" + path.encode(), "image/png", '"v1"')

    async def command(
        self,
        device_id: str,
        verb: str,
        args: Mapping[str, str | float | bool] | None = None,
        *,
        key: str | None = None,
    ) -> CommandResult:
        if self.command_error:
            raise self.command_error
        self.commands.append((device_id, verb, dict(args or {})))
        return CommandResult(ok=True, command_id="cmd")

    async def events(self, **kwargs: Any) -> AsyncIterator[StreamItem]:  # type: ignore[override]
        queue = self.typed_queue if kwargs.get("types") else self.queue
        while True:
            item = await queue.get()
            if isinstance(item, Exception):
                raise item
            yield item


@pytest.fixture
def client() -> FakeClient:
    return FakeClient()


@pytest.fixture
def mock_client(client: FakeClient) -> Generator[FakeClient]:
    with (
        patch("custom_components.prusa_connect.PrusaConnect", return_value=client),
        patch("custom_components.prusa_connect.config_flow.PrusaConnect", return_value=client),
    ):
        yield client


@pytest.fixture
def config_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="Workshop",
        data={CONF_URL: URL, CONF_TOKEN: TOKEN, CONF_INSTANCE_ID: INSTANCE},
    )


@pytest.fixture
async def setup_integration(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: FakeClient
) -> MockConfigEntry:
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    return config_entry


class FakeEmbedded:
    """The embedded server, without a process: records what was asked of it."""

    def __init__(self) -> None:
        self.port = 8080
        self.url = "http://127.0.0.1:8080"
        self.running = False
        self.version: str | None = None
        self.error: Exception | None = None
        self.installs: list[Release] = []
        self.updates: list[Release] = []
        self.stops = 0

    def installed(self) -> Installed | None:
        return Installed(self.version, "0" * 64) if self.version else None

    def setup_code(self) -> str:
        return "ABCD-EFGH-JKMN"

    async def async_load(self) -> None:
        pass

    async def install(self, release: Release) -> None:
        if self.error:
            raise self.error
        self.installs.append(release)
        self.version = release.version
        self.running = True

    async def start(self) -> None:
        if self.error:
            raise self.error
        self.running = True

    async def stop(self) -> None:
        self.stops += 1
        self.running = False

    async def update(self, release: Release, progress: Callable[[int], None] | None = None) -> None:
        if progress:
            progress(50)
        if self.error:
            raise self.error
        self.updates.append(release)
        self.version = release.version


ASSET = "prusa_connect_aarch64"
RELEASE = Release(
    "0.0.1-alpha.2",
    True,
    "https://github.com/prusa3d/prusa-connect/releases/tag/v0.0.1-alpha.2",
    "What changed",
    {ASSET: "https://example/bin"},
)


@pytest.fixture
def embedded() -> Generator[FakeEmbedded]:
    server = FakeEmbedded()
    with (
        patch("custom_components.prusa_connect.embedded_server", return_value=server),
        patch("custom_components.prusa_connect.config_flow.embedded_server", return_value=server),
        patch("custom_components.prusa_connect.update.embedded_server", return_value=server),
        patch("custom_components.prusa_connect.config_flow.fetch_releases", return_value=[RELEASE]),
        patch("custom_components.prusa_connect.update.fetch_releases", return_value=[RELEASE]),
        patch("custom_components.prusa_connect.config_flow.machine_asset", return_value=ASSET),
        patch("custom_components.prusa_connect.update.machine_asset", return_value=ASSET),
    ):
        yield server
