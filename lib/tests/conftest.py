"""Shared fixtures: a fake Prusa Connect server answering recorded responses."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from prusa_connect import PrusaConnect

FIXTURES = Path(__file__).parent / "fixtures"
TOKEN = "pcl_test_token"


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


class FakeServer:
    """Holds what the fake server answers and what it was asked."""

    def __init__(self) -> None:
        self.devices: list[dict[str, Any]] = load("devices.json")["items"]
        self.commands: list[tuple[str, dict[str, Any]]] = []
        self.command_answer: dict[str, Any] = {"ok": True, "command_id": "cmd-1"}
        self.event_bodies: list[str | int] = []
        self.event_requests: list[dict[str, str]] = []
        self.snapshot_status = 200
        self.thumbnail_requests: list[dict[str, str]] = []
        self.spools: list[dict[str, Any]] = [
            {
                "id": i,
                "remaining_weight_g": 100 + i,
                "full_weight_g": 1200,
                "empty_weight_g": 200,
                "tool": None,
            }
            for i in range(3)
        ]
        self.pairing_requests: list[dict[str, Any]] = []
        self.pairing_polls: list[dict[str, Any]] = []
        # What each poll answers, in order: a problem code, or a token.
        self.pairing_answers: list[str] = []

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/api/v1/health", self.health)
        app.router.add_get("/api/v1/ui", self.ui)
        app.router.add_get("/api/v1/setup", self.setup)
        app.router.add_get("/api/v1/devices", self.list_devices)
        app.router.add_get("/api/v1/devices/{id}", self.get_device)
        app.router.add_post("/api/v1/devices/{id}/commands", self.command)
        app.router.add_get("/api/v1/cameras", self.cameras)
        app.router.add_get("/api/v1/cameras/{name}/snapshot", self.snapshot)
        app.router.add_get("/api/v1/devices/{id}/files/thumbnail", self.thumbnail)
        materials = "/api/v1/modules/com.github.aux.prusa3d.materials"
        app.router.add_get(f"{materials}/spools", self.materials_spools)
        app.router.add_get(f"{materials}/printers/{{id}}/tools", self.materials_tools)
        app.router.add_get("/api/v1/vocabularies/device-states", self.states)
        app.router.add_get("/api/v1/events", self.events)
        app.router.add_post("/api/v1/auth/app-pairings", self.request_pairing)
        app.router.add_post("/api/v1/auth/app-pairings/token", self.collect_pairing)
        return app

    @staticmethod
    def _authorized(request: web.Request) -> bool:
        return request.headers.get("Authorization") == f"Bearer {TOKEN}"

    @staticmethod
    def _problem(status: int, code: str) -> web.Response:
        body = {"code": code, "title": "refused", "status": status, "params": {}, "retryable": False}
        return web.json_response(body, status=status, content_type="application/problem+json")

    async def request_pairing(self, request: web.Request) -> web.Response:
        self.pairing_requests.append(await request.json())
        base = f"http://{request.host}/pair"
        return web.json_response(
            {
                "device_code": "secret-device-code",
                "user_code": "BCDF-GHJK",
                "verification_uri": base,
                "verification_uri_complete": f"{base}?code=BCDF-GHJK",
                "expires_in": 600,
                "interval": 5,
            }
        )

    async def collect_pairing(self, request: web.Request) -> web.Response:
        self.pairing_polls.append(await request.json())
        answer = self.pairing_answers.pop(0)
        if answer.startswith("core."):
            return self._problem(400, answer)
        return web.json_response({"id": "cred-1", "name": "app", "scopes": ["operate"], "token": answer})

    async def health(self, request: web.Request) -> web.Response:
        return web.json_response(load("health.json"))

    async def setup(self, request: web.Request) -> web.Response:
        return web.json_response({"state": "done", "mode": "required", "version": "0.0.0-dev", "steps": []})

    async def ui(self, request: web.Request) -> web.Response:
        return web.json_response(load("ui.json"))

    async def states(self, request: web.Request) -> web.Response:
        return web.json_response(load("device_states.json"))

    async def list_devices(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return self._problem(401, "core.auth/TOKEN_INVALID")
        limit = int(request.query.get("limit", "50"))
        offset = int(request.query.get("offset", "0"))
        items = self.devices[offset : offset + limit]
        return web.json_response(
            {"items": items, "total": len(self.devices), "limit": limit, "offset": offset}
        )

    async def get_device(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return self._problem(401, "core.auth/TOKEN_INVALID")
        for device in self.devices:
            if device["id"] == request.match_info["id"]:
                return web.json_response(device)
        return self._problem(404, "core.devices/NOT_FOUND")

    async def command(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return self._problem(401, "core.auth/TOKEN_INVALID")
        self.commands.append((request.match_info["id"], await request.json()))
        return web.json_response(self.command_answer)

    async def cameras(self, request: web.Request) -> web.Response:
        data = load("cameras.json")
        device = request.query.get("device")
        if device:
            data["items"] = [c for c in data["items"] if c["device_id"] == device]
        return web.json_response(data)

    async def snapshot(self, request: web.Request) -> web.Response:
        if self.snapshot_status != 200:
            return web.Response(status=self.snapshot_status)
        return web.Response(
            body=(FIXTURES / "snapshot.jpg").read_bytes(),
            content_type="image/jpeg",
            headers={"Last-Modified": "Sat, 24 Oct 2026 16:16:38 GMT"},
        )

    async def materials_spools(self, request: web.Request) -> web.Response:
        offset, limit = int(request.query["offset"]), int(request.query["limit"])
        page = self.spools[offset : offset + limit]
        return web.json_response({"spools": page, "total": len(self.spools)})

    async def materials_tools(self, request: web.Request) -> web.Response:
        if request.match_info["id"] == "missing":
            return self._problem(404, "materials/NOT_FOUND")
        tools = [
            {"id": "0", "assignment": {"spool_id": 2, "brand_name": "Prusament", "type": "PETG"}},
            {"id": "1", "assignment": None},
        ]
        return web.json_response({"id": request.match_info["id"], "tools": tools})

    async def thumbnail(self, request: web.Request) -> web.Response:
        self.thumbnail_requests.append({"path": request.query["path"], **request.headers})
        if request.query["path"].endswith(".gcode"):
            return self._problem(404, "core.storage/NO_THUMBNAIL")
        if request.headers.get("If-None-Match") == '"v1"':
            return web.Response(status=304)
        return web.Response(body=b"\x89PNG", content_type="image/png", headers={"ETag": '"v1"'})

    async def events(self, request: web.Request) -> web.StreamResponse:
        self.event_requests.append(dict(request.headers))
        if not self._authorized(request):
            return self._problem(401, "core.auth/TOKEN_INVALID")
        body = self.event_bodies.pop(0) if self.event_bodies else ""
        if isinstance(body, int):
            return self._problem(body, "core.test/STATUS")
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        await response.write(body.encode())
        await response.write_eof()
        return response


@pytest.fixture
def fake() -> FakeServer:
    return FakeServer()


@pytest.fixture
async def server(fake: FakeServer) -> AsyncIterator[TestServer]:
    test_server = TestServer(fake.app())
    await test_server.start_server()
    yield test_server
    await test_server.close()


@pytest.fixture
async def session() -> AsyncIterator[aiohttp.ClientSession]:
    async with aiohttp.ClientSession() as client_session:
        yield client_session


@pytest.fixture
def make_client(server: TestServer, session: aiohttp.ClientSession) -> Callable[..., PrusaConnect]:
    def _make(token: str | None = TOKEN) -> PrusaConnect:
        return PrusaConnect(session, str(server.make_url("/")), token)

    return _make


Factory = Callable[..., Awaitable[Any]]
