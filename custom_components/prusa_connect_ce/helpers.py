"""Helpers shared by the platforms that send commands."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Coroutine
from functools import wraps
from typing import Any, Concatenate

from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN
from .library import (
    AuthenticationError,
    CommandRefusedError,
    ForbiddenError,
    PrusaConnectError,
    UnsupportedError,
)


def command[EntityT, **P](
    func: Callable[Concatenate[EntityT, P], Awaitable[Any]],
) -> Callable[Concatenate[EntityT, P], Coroutine[Any, Any, None]]:
    """Turn a failed command into an error a person can read."""

    @wraps(func)
    async def wrapper(entity: EntityT, /, *args: P.args, **kwargs: P.kwargs) -> None:
        try:
            await func(entity, *args, **kwargs)
        except CommandRefusedError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_refused",
                translation_placeholders={"reason": err.machine_reason or err.fault_code or ""},
            ) from err
        except (AuthenticationError, ForbiddenError) as err:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="command_forbidden") from err
        except UnsupportedError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="command_unsupported"
            ) from err
        except PrusaConnectError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_failed",
                translation_placeholders={"error": str(err)},
            ) from err

    return wrapper
