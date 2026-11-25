"""The embedded server, end to end: a signed release, a real child process, an update and a rollback.

The "binary" is a small Python program that answers /api/v1/health on the port
it is given, so the process handling runs for real.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import socket
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import patch

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from homeassistant.core import HomeAssistant

from custom_components.prusa_connect_ce.releases import Release
from custom_components.prusa_connect_ce.server import EmbeddedServer, ServerError

from .test_releases import PUBLIC, sign

ASSET = "prusa_connect_aarch64"

GOOD = f"""#!{sys.executable}
import http.server, sys
port = int(sys.argv[sys.argv.index("-addr") + 1].rsplit(":", 1)[1])
class Health(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200 if self.path == "/api/v1/health" else 404)
        self.end_headers()
        self.wfile.write(b'{{"status": "ok"}}')
    def log_message(self, *args):
        pass
print("listening on", port, flush=True)
http.server.HTTPServer(("127.0.0.1", port), Health).serve_forever()
""".encode()

BROKEN = f"#!{sys.executable}\nimport sys\nprint('cannot start', flush=True)\nsys.exit(3)\n".encode()


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class Releases:
    """A web server that publishes releases as GitHub does: binary, sums, signature."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.base = ""

    def publish(
        self, version: str, binary: bytes, *, signed: bool = True, sums_binary: bytes | None = None
    ) -> Release:
        digest = hashlib.sha256(sums_binary if sums_binary is not None else binary).hexdigest()
        sums = f"{digest}  {ASSET}\n".encode()
        self.files[f"{version}/{ASSET}"] = binary
        self.files[f"{version}/SHA256SUMS"] = sums
        self.files[f"{version}/SHA256SUMS.sig"] = sign(sums) if signed else sign(b"other")
        names = (ASSET, "SHA256SUMS", "SHA256SUMS.sig")
        return Release(version, True, "", "", {n: f"{self.base}/{version}/{n}" for n in names})

    async def handle(self, request: web.Request) -> web.Response:
        body = self.files.get(request.match_info["path"])
        return web.Response(body=body) if body is not None else web.Response(status=404)


@pytest.fixture
async def releases(socket_enabled: None) -> AsyncIterator[Releases]:
    published = Releases()
    app = web.Application()
    app.router.add_get("/{path:.+}", published.handle)
    server = TestServer(app, host="127.0.0.1")
    await server.start_server()
    published.base = str(server.make_url("")).rstrip("/")
    yield published
    await server.close()


@pytest.fixture
async def embedded(
    hass: HomeAssistant, tmp_path: Path, socket_enabled: None
) -> AsyncIterator[EmbeddedServer]:
    async with aiohttp.ClientSession() as session:
        server = EmbeddedServer(hass, session, free_port(), (PUBLIC,), root=tmp_path / "prusa_connect")
        server.health_timeout = 15
        server.restart_delay = 0.2
        with patch("custom_components.prusa_connect_ce.server.machine_asset", return_value=ASSET):
            yield server
        await server.stop()


async def test_install_start_stop(embedded: EmbeddedServer, releases: Releases) -> None:
    await embedded.install(releases.publish("0.0.1-alpha.1", GOOD))
    assert embedded.running
    assert embedded.installed() is not None
    assert embedded.installed().version == "0.0.1-alpha.1"  # type: ignore[union-attr]
    config = embedded.config_file.read_text()
    assert "enabled: false" in config
    code = embedded.setup_code()
    assert code is not None
    assert len(code) == 14
    await embedded.stop()
    assert not embedded.running
    await embedded.start()
    assert embedded.setup_code() == code


async def test_unsigned_release_is_refused(embedded: EmbeddedServer, releases: Releases) -> None:
    with pytest.raises(ServerError, match="SHA256SUMS"):
        await embedded.install(releases.publish("0.0.1", GOOD, signed=False))
    assert embedded.installed() is None


async def test_binary_that_is_not_the_signed_one(embedded: EmbeddedServer, releases: Releases) -> None:
    with pytest.raises(ServerError, match="SHA-256"):
        await embedded.install(releases.publish("0.0.1", GOOD, sums_binary=b"another build"))
    assert embedded.installed() is None
    assert not list(embedded.bin_dir.glob(".*partial"))


async def test_missing_download(embedded: EmbeddedServer, releases: Releases) -> None:
    release = releases.publish("0.0.1", GOOD)
    del releases.files[f"0.0.1/{ASSET}"]
    with pytest.raises(ServerError, match="downloading"):
        await embedded.install(release)


async def test_start_without_install(embedded: EmbeddedServer) -> None:
    with pytest.raises(ServerError, match="installed"):
        await embedded.start()


async def test_update(embedded: EmbeddedServer, releases: Releases) -> None:
    await embedded.install(releases.publish("0.0.1-alpha.1", GOOD))
    steps: list[int] = []
    await embedded.update(releases.publish("0.0.1-alpha.2", GOOD + b"\n# v2\n"), steps.append)
    assert embedded.installed().version == "0.0.1-alpha.2"  # type: ignore[union-attr]
    assert embedded.running
    assert steps == [10, 60, 80]
    assert [p.name.split("-2")[0] for p in (embedded.root / "backups").iterdir()] == ["0.0.1-alpha.1"]


async def test_update_that_does_not_start_goes_back(embedded: EmbeddedServer, releases: Releases) -> None:
    await embedded.install(releases.publish("0.0.1-alpha.1", GOOD))
    embedded.health_timeout = 3
    with pytest.raises(ServerError, match=r"0\.0\.1-alpha\.1 runs again"):
        await embedded.update(releases.publish("0.0.1-alpha.2", BROKEN))
    assert embedded.installed().version == "0.0.1-alpha.1"  # type: ignore[union-attr]
    assert embedded.running


async def test_a_crash_is_restarted(embedded: EmbeddedServer, releases: Releases) -> None:
    await embedded.install(releases.publish("0.0.1", GOOD))
    first = embedded._process
    assert first is not None
    first.kill()
    for _ in range(100):
        if embedded._process is not first and embedded.running:
            break
        await asyncio.sleep(0.1)
    assert embedded._process is not first
    await embedded.wait_healthy(10)


async def test_installed_reads_nothing_odd(tmp_path: Path, hass: HomeAssistant) -> None:
    server = EmbeddedServer(hass, None, 1, (), root=tmp_path)  # type: ignore[arg-type]
    (tmp_path / "installed.json").write_text(json.dumps({"version": "1"}))
    await server.async_load()
    assert server.installed() is None
    assert server.setup_code() is None
    assert server.url == "http://127.0.0.1:1"
