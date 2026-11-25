"""Choosing a release and proving it genuine."""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.prusa_connect_ce.releases import (
    Release,
    ReleaseError,
    VerificationError,
    expected_digest,
    fetch_releases,
    machine_asset,
    parse_releases,
    pick_release,
    verify_sums,
)

KEY = Ed25519PrivateKey.generate()
PUBLIC = base64.b64encode(KEY.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
KEY_ID = hashlib.sha256(base64.b64decode(PUBLIC)).hexdigest()[:32]
SUMS = (f"{'a' * 64}  prusa_connect_aarch64\n{'b' * 64}  prusa_connect_amd64\n").encode()


def sign(
    sums: bytes,
    signer: Ed25519PrivateKey | None = None,
    signer_id: str | None = None,
    algorithm: str = "ed25519",
) -> bytes:
    """A SHA256SUMS.sig over ``sums``, by the test key unless another is given."""
    signature = base64.b64encode((signer or KEY).sign(sums)).decode()
    detached = {"algorithm": algorithm, "key_id": signer_id or KEY_ID, "signature": signature}
    return json.dumps(detached).encode()


def release(
    tag: str, prerelease: bool = False, draft: bool = False, assets: tuple[str, ...] = ()
) -> dict[str, Any]:
    return {
        "tag_name": tag,
        "prerelease": prerelease,
        "draft": draft,
        "html_url": f"https://example/{tag}",
        "body": f"notes {tag}",
        "assets": [{"name": a, "browser_download_url": f"https://example/{tag}/{a}"} for a in assets],
    }


@pytest.mark.parametrize(
    ("machine", "asset"),
    [
        ("x86_64", "prusa_connect_amd64"),
        ("aarch64", "prusa_connect_aarch64"),
        ("armv7l", "prusa_connect_armv7"),
    ],
)
def test_machine_asset(machine: str, asset: str) -> None:
    assert machine_asset(machine) == asset


def test_machine_asset_unknown() -> None:
    with pytest.raises(ReleaseError):
        machine_asset("riscv64")


def test_parse_and_pick() -> None:
    releases = parse_releases(
        [
            release("v0.0.1-alpha.8", prerelease=True, assets=("prusa_connect_aarch64",)),
            release("v0.0.1-alpha.10", prerelease=True, assets=("prusa_connect_aarch64",)),
            release("v0.0.2", draft=True, assets=("prusa_connect_aarch64",)),
            release("nightly", assets=("prusa_connect_aarch64",)),
            release("v0.0.1", assets=("prusa_connect_amd64",)),
            "not a release",
        ]
    )
    assert [r.version for r in releases] == ["0.0.1-alpha.8", "0.0.1-alpha.10", "0.0.1"]
    newest = pick_release(releases, True, "prusa_connect_aarch64")
    assert newest.version == "0.0.1-alpha.10"
    assert newest.notes == "notes v0.0.1-alpha.10"
    assert newest.asset_url("prusa_connect_aarch64").endswith("/prusa_connect_aarch64")
    assert pick_release(releases, False, "prusa_connect_amd64").version == "0.0.1"
    with pytest.raises(ReleaseError):
        pick_release(releases, False, "prusa_connect_aarch64")
    with pytest.raises(ReleaseError):
        newest.asset_url("SHA256SUMS")
    assert parse_releases({"message": "rate limited"}) == []


def test_verify_sums() -> None:
    verify_sums(SUMS, sign(SUMS), [PUBLIC])
    assert expected_digest(SUMS, "prusa_connect_amd64") == "b" * 64


@pytest.mark.parametrize(
    "signature",
    [
        sign(SUMS + b"tampered"),
        sign(SUMS, signer=Ed25519PrivateKey.generate()),
        sign(SUMS, signer_id="0" * 32),
        sign(SUMS, algorithm="rsa"),
        b"not json",
        b'{"algorithm": "ed25519", "key_id": "' + KEY_ID.encode() + b'", "signature": "***"}',
    ],
)
def test_verify_sums_refuses(signature: bytes) -> None:
    with pytest.raises(VerificationError):
        verify_sums(SUMS, signature, [PUBLIC])


def test_expected_digest_missing() -> None:
    with pytest.raises(VerificationError):
        expected_digest(SUMS, "prusa_connect_armv7")


async def test_fetch_releases(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get("https://api.example/releases", json=[release("v0.0.1", assets=("SHA256SUMS",))])
    releases = await fetch_releases(async_get_clientsession(hass), "https://api.example/releases")
    assert releases == [
        Release(
            "0.0.1",
            False,
            "https://example/v0.0.1",
            "notes v0.0.1",
            {"SHA256SUMS": "https://example/v0.0.1/SHA256SUMS"},
        )
    ]


async def test_fetch_releases_fails(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get("https://api.example/releases", status=403)
    with pytest.raises(ReleaseError):
        await fetch_releases(async_get_clientsession(hass), "https://api.example/releases")
