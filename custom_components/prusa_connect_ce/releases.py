"""Prusa Connect releases: which one to install, and proof that it is genuine.

A release publishes one binary per platform, ``SHA256SUMS`` over them and
``SHA256SUMS.sig``, a detached Ed25519 signature over ``SHA256SUMS``. A binary is
installed only when a trusted key signed the sums and its SHA-256 is the one
the sums name, the same check the server makes for its own updates.
"""

from __future__ import annotations

import base64
import hashlib
import json
import platform
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import aiohttp
from awesomeversion import AwesomeVersion, AwesomeVersionException, AwesomeVersionStrategy
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

SUMS = "SHA256SUMS"
SIGNATURE = "SHA256SUMS.sig"
ASSET_PREFIX = "prusa_connect_"

# uname -m, as the Home Assistant container reports it, to the release's
# platform suffix.
_ARCHES = {
    "x86_64": "amd64",
    "amd64": "amd64",
    "aarch64": "aarch64",
    "arm64": "aarch64",
    "armv7l": "armv7",
    "armv8l": "armv7",
    "armv6l": "armhf",
}


class ReleaseError(Exception):
    """No release could be read, or none fits this machine."""


class VerificationError(Exception):
    """A download is not what a trusted key signed."""


@dataclass(frozen=True, slots=True)
class Release:
    """One published release of the server."""

    version: str
    prerelease: bool
    url: str
    notes: str
    assets: Mapping[str, str]

    def asset_url(self, name: str) -> str:
        try:
            return self.assets[name]
        except KeyError as err:
            raise ReleaseError(f"release {self.version} has no {name}") from err


def machine_asset(machine: str | None = None) -> str:
    """The binary for this machine, such as ``prusa_connect_aarch64``."""
    machine = (machine or platform.machine()).lower()
    try:
        return ASSET_PREFIX + _ARCHES[machine]
    except KeyError as err:
        raise ReleaseError(f"Prusa Connect has no build for {machine}") from err


def parse_releases(data: Any) -> list[Release]:
    """Read the GitHub releases API answer, leaving drafts and odd tags out."""
    releases = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict) or item.get("draft"):
            continue
        version = str(item.get("tag_name", "")).removeprefix("v")
        try:
            AwesomeVersion(version, ensure_strategy=[AwesomeVersionStrategy.SEMVER])
        except AwesomeVersionException:
            continue
        assets = {
            str(a["name"]): str(a["browser_download_url"])
            for a in item.get("assets", [])
            if isinstance(a, dict) and "name" in a and "browser_download_url" in a
        }
        releases.append(
            Release(
                version=version,
                prerelease=bool(item.get("prerelease")),
                url=str(item.get("html_url", "")),
                notes=str(item.get("body") or ""),
                assets=assets,
            )
        )
    return releases


def pick_release(releases: Iterable[Release], prerelease: bool, asset: str) -> Release:
    """The newest release that has a binary for this machine."""
    fitting = [r for r in releases if asset in r.assets and (prerelease or not r.prerelease)]
    if not fitting:
        raise ReleaseError(f"no release offers {asset}")
    return max(fitting, key=lambda r: AwesomeVersion(r.version))


async def fetch_releases(session: aiohttp.ClientSession, url: str) -> list[Release]:
    try:
        async with session.get(
            url,
            params={"per_page": "30"},
            headers={"Accept": "application/vnd.github+json"},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as response:
            response.raise_for_status()
            return parse_releases(await response.json(content_type=None))
    except (aiohttp.ClientError, TimeoutError, json.JSONDecodeError) as err:
        raise ReleaseError(f"cannot read the releases at {url}: {err}") from err


def _public_keys(keys: Iterable[str]) -> dict[str, Ed25519PublicKey]:
    """Trusted keys by their id: the first 128 bits of SHA-256 over the key, in hex."""
    trusted = {}
    for encoded in keys:
        raw = base64.b64decode(encoded)
        trusted[hashlib.sha256(raw).hexdigest()[:32]] = Ed25519PublicKey.from_public_bytes(raw)
    return trusted


def verify_sums(sums: bytes, signature: bytes, keys: Iterable[str]) -> None:
    """Check that a trusted key signed ``SHA256SUMS``."""
    try:
        detached = json.loads(signature)
        if detached.get("algorithm") != "ed25519":
            raise VerificationError(f"{SIGNATURE} uses {detached.get('algorithm')!r}")
        key = _public_keys(keys).get(str(detached.get("key_id")))
        if key is None:
            raise VerificationError(f"{SIGNATURE} is signed with an unknown key")
        key.verify(base64.b64decode(detached["signature"], validate=True), sums)
    except (ValueError, KeyError, AttributeError, TypeError) as err:
        raise VerificationError(f"{SIGNATURE} cannot be read: {err}") from err
    except InvalidSignature as err:
        raise VerificationError(f"{SIGNATURE} does not match {SUMS}") from err


def expected_digest(sums: bytes, name: str) -> str:
    """The SHA-256 ``SHA256SUMS`` names for one file."""
    for line in sums.decode(errors="replace").splitlines():
        match = re.fullmatch(r"([0-9a-fA-F]{64})\s+\*?(\S+)", line.strip())
        if match and match.group(2) == name:
            return match.group(1).lower()
    raise VerificationError(f"{SUMS} does not name {name}")
