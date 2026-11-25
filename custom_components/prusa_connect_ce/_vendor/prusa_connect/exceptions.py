"""Exceptions raised by the Prusa Connect client."""

from __future__ import annotations

from typing import Any


class PrusaConnectError(Exception):
    """Base class for every error this library raises."""


class CannotConnectError(PrusaConnectError):
    """The server could not be reached, or it did not answer in time."""


class CertificateError(CannotConnectError):
    """The server's TLS certificate was not accepted.

    ``fingerprint`` is the SHA-256 fingerprint of the certificate the server
    presented, when it could be read, so a caller can offer to pin it.
    """

    def __init__(self, message: str, fingerprint: str | None = None) -> None:
        super().__init__(message)
        self.fingerprint = fingerprint


class ApiError(PrusaConnectError):
    """The server answered with an RFC 9457 problem document."""

    def __init__(
        self,
        status: int,
        code: str | None = None,
        title: str | None = None,
        detail: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> None:
        self.status = status
        self.code = code
        self.title = title
        self.detail = detail
        self.params = params or {}
        message = f"HTTP {status}"
        if code:
            message += f" {code}"
        if detail or title:
            message += f": {detail or title}"
        super().__init__(message)


class AuthenticationError(ApiError):
    """The token is missing, invalid, revoked or expired (HTTP 401)."""


class ForbiddenError(ApiError):
    """The token is valid but lacks the permission for this call (HTTP 403)."""


class NotFoundError(ApiError):
    """The addressed resource does not exist (HTTP 404)."""


class PairingDeniedError(PrusaConnectError):
    """The person denied the app pairing in Prusa Connect."""


class PairingExpiredError(PrusaConnectError):
    """The app pairing expired, or its code is used or unknown: ask for a new one."""


class CommandRefusedError(PrusaConnectError):
    """The printer did not accept a command."""

    def __init__(
        self,
        verb: str,
        fault_code: str | None = None,
        machine_reason: str | None = None,
    ) -> None:
        self.verb = verb
        self.fault_code = fault_code
        self.machine_reason = machine_reason
        reason = machine_reason or fault_code or "refused"
        super().__init__(f"{verb}: {reason}")


class UnsupportedError(PrusaConnectError):
    """The device does not offer the verb or argument that was asked for."""
