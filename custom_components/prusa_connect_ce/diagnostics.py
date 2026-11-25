"""Diagnostics for Prusa Connect."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_TOKEN, CONF_URL
from homeassistant.core import HomeAssistant

from .const import CONF_PANEL_URL
from .coordinator import PrusaConnectConfigEntry

# People paste diagnostics into public issues: the addresses name their network.
TO_REDACT_ENTRY = {CONF_TOKEN, CONF_URL, CONF_PANEL_URL}
# Identifiers of a machine and of the network it sits on.
TO_REDACT_DEVICE = {"serial", "unit_uid", "name"}
REDACTED_STATE_PREFIXES = ("/network/",)


def _device(raw: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    record = async_redact_data({k: v for k, v in raw.items() if k != "state"}, TO_REDACT_DEVICE)
    record["state"] = {
        path: "**REDACTED**" if path.startswith(REDACTED_STATE_PREFIXES) else value
        for path, value in sorted(state.items())
    }
    return record


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: PrusaConnectConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data
    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT_ENTRY),
            "options": async_redact_data(dict(entry.options), TO_REDACT_ENTRY),
        },
        "server_version": coordinator.server_version,
        "stream_connected": coordinator.stream_connected,
        "last_update_success": coordinator.last_update_success,
        "device_states": [asdict(kind) for kind in coordinator.vocabulary.kinds],
        "devices": [_device(device.raw, device.state) for device in coordinator.data.devices.values()],
        "cameras": [asdict(camera) for camera in coordinator.data.cameras.values()],
    }
