"""The Prusa Connect server run inside Home Assistant.

Everything lives under ``<config>/prusa_connect_ce/``, so a Home Assistant backup
carries it:

- ``bin/prusa_connect-<version>``: the verified binaries, the installed one and
  the one before it, for a rollback
- ``installed.json``: which version is installed, and its SHA-256
- ``server/``: the folder the server starts in. It keeps ``config/`` (settings,
  database) and ``storage/`` (uploads) there, as a folder install does.
- ``backups/``: a copy of ``server/config`` taken before each update

This integration owns ``server/config/config.yaml``: it switches the server's
own updates off, because the update entity installs new versions, and sets the
setup code the first-run wizard asks for.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import secrets
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import aiohttp
from homeassistant.core import HomeAssistant

from .releases import (
    SIGNATURE,
    SUMS,
    Release,
    ReleaseError,
    VerificationError,
    expected_digest,
    machine_asset,
    verify_sums,
)

_LOGGER = logging.getLogger(__name__)
_SERVER_LOGGER = logging.getLogger(f"{__package__}.server.output")

FOLDER = "prusa_connect_ce"
# A server that exits is started again after this long, doubling to the cap.
RESTART_DELAY = 5.0
RESTART_DELAY_MAX = 300.0
# A run this long resets the delay: the server was working.
STABLE_RUN = 120.0
STOP_TIMEOUT = 20.0
HEALTH_TIMEOUT = 90.0
DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=600, sock_read=60)
# Readable letters only: no O, I or L, which the server reads as 0 and 1.
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


class ServerError(Exception):
    """The embedded server could not be installed, started or updated."""


@dataclass(frozen=True, slots=True)
class Installed:
    version: str
    sha256: str


def _setup_code() -> str:
    raw = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(12))
    return f"{raw[:4]}-{raw[4:8]}-{raw[8:]}"


class EmbeddedServer:
    """Installs, runs and updates one Prusa Connect server as a child process."""

    def __init__(
        self,
        hass: HomeAssistant,
        session: aiohttp.ClientSession,
        port: int,
        trusted_keys: tuple[str, ...],
        root: Path | None = None,
    ) -> None:
        self.hass = hass
        self._session = session
        self.port = port
        self._keys = trusted_keys
        self.root = root or Path(hass.config.path(FOLDER))
        self._process: asyncio.subprocess.Process | None = None
        self._supervisor: asyncio.Task[None] | None = None
        self._stopping = False
        self._lock = asyncio.Lock()
        self.health_timeout = HEALTH_TIMEOUT
        self.restart_delay = RESTART_DELAY
        # What installed.json and config.yaml say, read once in the executor by
        # async_load and kept current by every write.
        self._installed: Installed | None = None
        self._setup_code: str | None = None
        self._loaded = False

    # -- paths and state --------------------------------------------------

    @property
    def bin_dir(self) -> Path:
        return self.root / "bin"

    @property
    def server_dir(self) -> Path:
        return self.root / "server"

    @property
    def config_file(self) -> Path:
        return self.server_dir / "config" / "config.yaml"

    @property
    def url(self) -> str:
        """Where Home Assistant reaches the server."""
        return f"http://127.0.0.1:{self.port}"

    def _read_installed(self) -> Installed | None:
        try:
            data = json.loads((self.root / "installed.json").read_text())
            return Installed(str(data["version"]), str(data["sha256"]))
        except (OSError, ValueError, KeyError):
            return None

    def _read_setup_code(self) -> str | None:
        try:
            for line in self.config_file.read_text().splitlines():
                key, _, value = line.strip().partition(":")
                if key == "setup_code":
                    return value.strip().strip('"')
        except OSError:
            pass
        return None

    async def async_load(self) -> None:
        """Read what is installed, once, off the event loop."""
        if self._loaded:
            return
        self._installed = await self.hass.async_add_executor_job(self._read_installed)
        self._setup_code = await self.hass.async_add_executor_job(self._read_setup_code)
        self._loaded = True

    def installed(self) -> Installed | None:
        """The installed version, as async_load read it and later installs changed it."""
        return self._installed

    def setup_code(self) -> str | None:
        """The code the first-run wizard asks for, as written into config.yaml."""
        return self._setup_code

    def _binary(self, version: str) -> Path:
        return self.bin_dir / f"prusa_connect-{version}"

    @property
    def stopping(self) -> bool:
        """Whether a stop was asked for, so an exit is not a crash."""
        return self._stopping

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    # -- install ----------------------------------------------------------

    async def _download(self, url: str, target: Path) -> str:
        """Stream ``url`` into ``target`` and return its SHA-256."""
        digest = hashlib.sha256()
        try:
            async with self._session.get(url, timeout=DOWNLOAD_TIMEOUT) as response:
                response.raise_for_status()
                with await self.hass.async_add_executor_job(target.open, "wb") as file:
                    async for chunk in response.content.iter_chunked(1 << 16):
                        digest.update(chunk)
                        await self.hass.async_add_executor_job(file.write, chunk)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise ServerError(f"downloading {url} failed: {err}") from err
        return digest.hexdigest()

    async def _fetch(self, url: str) -> bytes:
        try:
            async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as response:
                response.raise_for_status()
                return await response.read()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise ServerError(f"downloading {url} failed: {err}") from err

    async def download(self, release: Release) -> Installed:
        """Download and verify one release's binary. Nothing runs it yet."""
        try:
            asset = machine_asset()
            sums = await self._fetch(release.asset_url(SUMS))
            signature = await self._fetch(release.asset_url(SIGNATURE))
            verify_sums(sums, signature, self._keys)
            want = expected_digest(sums, asset)
            binary_url = release.asset_url(asset)
        except (ReleaseError, VerificationError) as err:
            raise ServerError(str(err)) from err
        await self.hass.async_add_executor_job(lambda: self.bin_dir.mkdir(parents=True, exist_ok=True))
        partial = self.bin_dir / f".{asset}.partial"
        got = await self._download(binary_url, partial)
        if got != want:
            await self.hass.async_add_executor_job(partial.unlink)
            raise ServerError(f"{asset} of {release.version} has SHA-256 {got}, the signed sums say {want}")

        def place() -> None:
            os.chmod(partial, 0o700)
            os.replace(partial, self._binary(release.version))

        await self.hass.async_add_executor_job(place)
        _LOGGER.info("Downloaded and verified Prusa Connect %s (%s)", release.version, asset)
        return Installed(release.version, got)

    def _write_config(self) -> None:
        """Write the keys this integration owns, keeping a setup code once made."""
        self.config_file.parent.mkdir(parents=True, exist_ok=True)
        code = self._setup_code or self._read_setup_code() or _setup_code()
        self._setup_code = code
        self.config_file.write_text(
            "# Written by the Prusa Connect integration of Home Assistant on every start.\n"
            "# Home Assistant installs the server's updates, so its own are off.\n"
            "core:\n"
            "  upgrade:\n"
            "    enabled: false\n"
            "  auth:\n"
            f'    setup_code: "{code}"\n'
        )
        os.chmod(self.server_dir, 0o700)

    def _mark_installed(self, installed: Installed) -> None:
        (self.root / "installed.json").write_text(
            json.dumps({"version": installed.version, "sha256": installed.sha256})
        )
        self._installed = installed
        keep = {self._binary(installed.version).name}
        for old in sorted(self.bin_dir.glob("prusa_connect-*"), key=lambda p: p.stat().st_mtime)[:-2]:
            if old.name not in keep:
                old.unlink(missing_ok=True)

    async def install(self, release: Release) -> None:
        """Install a release on a server that has none yet, and start it."""
        await self.async_load()
        async with self._lock:
            installed = await self.download(release)
            await self.hass.async_add_executor_job(self._mark_installed, installed)
        await self.start()

    # -- run --------------------------------------------------------------

    async def start(self) -> None:
        """Start the installed server and wait until it answers."""
        await self.async_load()
        if self.installed() is None:
            raise ServerError("no Prusa Connect server is installed")
        await self.hass.async_add_executor_job(self._write_config)
        self._stopping = False
        if self._supervisor is None or self._supervisor.done():
            self._supervisor = self.hass.async_create_background_task(
                self._supervise(), f"{FOLDER} server", eager_start=True
            )
        await self.wait_healthy(self.health_timeout)

    async def _spawn(self) -> asyncio.subprocess.Process:
        installed = self.installed()
        if installed is None:
            raise ServerError("no Prusa Connect server is installed")
        binary = self._binary(installed.version)
        _LOGGER.info("Starting Prusa Connect %s on port %d", installed.version, self.port)
        try:
            return await asyncio.create_subprocess_exec(
                str(binary),
                "core",
                "-addr",
                f"0.0.0.0:{self.port}",
                cwd=self.server_dir,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except OSError as err:
            raise ServerError(f"cannot start {binary}: {err}") from err

    async def _forward_output(self, process: asyncio.subprocess.Process) -> None:
        assert process.stdout is not None
        async for line in process.stdout:
            _SERVER_LOGGER.info("%s", line.decode(errors="replace").rstrip())

    async def _supervise(self) -> None:
        """Keep the server running: start it, and start it again when it exits."""
        delay = self.restart_delay
        while not self._stopping:
            started = time.monotonic()
            try:
                self._process = await self._spawn()
            except ServerError:
                _LOGGER.exception("Prusa Connect did not start")
            else:
                await self._forward_output(self._process)
                code = await self._process.wait()
                if self.stopping:
                    return
                _LOGGER.warning("Prusa Connect exited with code %s", code)
            if time.monotonic() - started > STABLE_RUN:
                delay = self.restart_delay
            await asyncio.sleep(delay)
            delay = min(delay * 2, RESTART_DELAY_MAX)

    async def wait_healthy(self, limit: float = HEALTH_TIMEOUT) -> None:
        deadline = time.monotonic() + limit
        while True:
            with contextlib.suppress(aiohttp.ClientError, TimeoutError):
                async with self._session.get(
                    f"{self.url}/api/v1/health", timeout=aiohttp.ClientTimeout(total=5)
                ) as response:
                    if response.status == 200:
                        return
            if time.monotonic() > deadline:
                raise ServerError(f"Prusa Connect did not answer on port {self.port} within {limit:.0f} s")
            await asyncio.sleep(1)

    async def stop(self) -> None:
        """Stop the server and its supervisor."""
        self._stopping = True
        process = self._process
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), STOP_TIMEOUT)
            except TimeoutError:
                _LOGGER.warning("Prusa Connect did not stop in %.0f s; killing it", STOP_TIMEOUT)
                process.kill()
                await process.wait()
        if self._supervisor is not None:
            self._supervisor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._supervisor
            self._supervisor = None
        self._process = None

    # -- update -----------------------------------------------------------

    def _backup(self, version: str) -> Path:
        target = self.root / "backups" / f"{version}-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copytree(self.server_dir / "config", target / "config")
        backups = sorted((self.root / "backups").iterdir())
        for old in backups[:-3]:
            shutil.rmtree(old, ignore_errors=True)
        return target

    def _restore(self, backup: Path) -> None:
        shutil.rmtree(self.server_dir / "config", ignore_errors=True)
        shutil.copytree(backup / "config", self.server_dir / "config")

    async def update(self, release: Release, progress: Callable[[int], None] | None = None) -> None:
        """Replace the running server with ``release``, going back to the old one if it fails."""
        await self.async_load()
        async with self._lock:
            before = self.installed()
            if progress:
                progress(10)
            new = await self.download(release)
            if progress:
                progress(60)
            await self.stop()
            backup = None
            if before is not None and (self.server_dir / "config").exists():
                backup = await self.hass.async_add_executor_job(self._backup, before.version)
            await self.hass.async_add_executor_job(self._mark_installed, new)
            if progress:
                progress(80)
            try:
                await self.start()
            except ServerError:
                if before is None:
                    raise
                _LOGGER.exception(
                    "Prusa Connect %s did not start; going back to %s", new.version, before.version
                )
                await self.stop()
                if backup is not None:
                    await self.hass.async_add_executor_job(self._restore, backup)
                await self.hass.async_add_executor_job(self._mark_installed, before)
                await self.start()
                raise ServerError(
                    f"Prusa Connect {new.version} did not start; {before.version} runs again"
                ) from None
