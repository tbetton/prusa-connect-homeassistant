"""Builders for event stream items."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.prusa_connect_ce.library import Event


def event(event_type: str, device_id: str | None, data: dict[str, Any], event_id: str | None = None) -> Event:
    envelope: dict[str, Any] = {"type": event_type, "data": data}
    if device_id is not None:
        envelope["subject"] = f"device:{device_id}"
    return Event(type=event_type, envelope=envelope, id=event_id)


def telemetry(device_id: str, path: str, value: Any) -> Event:
    return event("com.prusa.connect.device.telemetry.sampled", device_id, {"path": path, "value": value})


async def flush(hass: HomeAssistant, seconds: float = 2) -> None:
    """Let queued stream items run and the throttled write fire."""
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done()
