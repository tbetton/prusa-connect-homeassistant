"""Helpers for trusting a server certificate by its fingerprint."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import re
import ssl

from .exceptions import CannotConnectError

_HEX = re.compile(r"^[0-9a-f]{64}$")


def normalize_fingerprint(fingerprint: str) -> str:
    """Spell a SHA-256 fingerprint as upper-case hex pairs joined by colons.

    Accepts the colon, space or dash separated spellings browsers and
    ``openssl x509 -fingerprint`` print. Raises :class:`ValueError` for
    anything that is not 32 bytes of hex.
    """
    digits = re.sub(r"[\s:\-]", "", fingerprint).lower()
    if not _HEX.match(digits):
        raise ValueError("a SHA-256 fingerprint is 64 hex digits")
    return ":".join(digits[i : i + 2] for i in range(0, 64, 2)).upper()


def fingerprint_to_bytes(fingerprint: str) -> bytes:
    """The 32 raw bytes of a fingerprint in any accepted spelling."""
    return bytes.fromhex(normalize_fingerprint(fingerprint).replace(":", ""))


def certificate_fingerprint(der: bytes) -> str:
    """The SHA-256 fingerprint of a DER encoded certificate."""
    return normalize_fingerprint(hashlib.sha256(der).hexdigest())


def _unverified_context() -> ssl.SSLContext:
    # No certificate store is loaded: the point is to read whatever the server
    # presents so a person can compare it, not to trust it.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


async def fetch_certificate_fingerprint(host: str, port: int, connect_timeout: float = 10.0) -> str:
    """Connect without verifying and return the server certificate's fingerprint."""
    try:
        async with asyncio.timeout(connect_timeout):
            _, writer = await asyncio.open_connection(
                host, port, ssl=_unverified_context(), server_hostname=host
            )
    except (OSError, TimeoutError, ssl.SSLError) as err:
        raise CannotConnectError(f"cannot read the certificate of {host}:{port}: {err!r}") from err
    try:
        ssl_object = writer.get_extra_info("ssl_object")
        der = ssl_object.getpeercert(binary_form=True) if ssl_object else None
    finally:
        writer.close()
        with contextlib.suppress(OSError, ssl.SSLError):
            await writer.wait_closed()
    if not der:
        raise CannotConnectError(f"{host}:{port} presented no certificate")
    return certificate_fingerprint(der)
