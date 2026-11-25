"""Entities mapped from the tool tree of each printer family."""

from __future__ import annotations

from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.prusa_connect_ce.const import DOMAIN

from .conftest import CORE_ONE, INSTANCE, KLIPPER, MK4S_MMU, XL, XL_DISABLED


def _entity_id(hass: HomeAssistant, platform: str, device_id: str, key: str) -> str:
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(platform, DOMAIN, f"{INSTANCE}:{device_id}:{key}")
    assert entity_id is not None, f"{platform} {key} of {device_id} is missing"
    return entity_id


def _state(hass: HomeAssistant, platform: str, device_id: str, key: str) -> str:
    state = hass.states.get(_entity_id(hass, platform, device_id, key))
    assert state is not None
    return state.state


def _has(hass: HomeAssistant, platform: str, device_id: str, key: str) -> bool:
    registry = er.async_get(hass)
    return registry.async_get_entity_id(platform, DOMAIN, f"{INSTANCE}:{device_id}:{key}") is not None


async def test_devices_and_hub(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    registry = dr.async_get(hass)
    hub = registry.async_get_device(identifiers={(DOMAIN, INSTANCE)})
    assert hub is not None
    assert hub.sw_version == "1.2.3"
    assert hub.configuration_url == "http://connect.local:8080"
    printer = registry.async_get_device(identifiers={(DOMAIN, f"{INSTANCE}:{XL}")})
    assert printer is not None
    assert printer.via_device_id == hub.id
    assert printer.model == "XL"
    assert printer.manufacturer == "Prusa"
    assert printer.serial_number == "DEMO-03"
    assert printer.sw_version == "6.4.0-0"


async def test_single_head(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    assert _state(hass, "sensor", CORE_ONE, "state") == "printing"
    assert _state(hass, "sensor", CORE_ONE, "progress") == "3.0"
    assert _state(hass, "sensor", CORE_ONE, "job_name") == "tangle.bgcode"
    assert _state(hass, "sensor", CORE_ONE, "bed_temperature") == "60.0"
    assert float(_state(hass, "sensor", CORE_ONE, "time_remaining")) == 831 / 60
    assert _state(hass, "sensor", CORE_ONE, "tool:0:nozzle_temperature") == "215.0"
    assert _state(hass, "sensor", CORE_ONE, "tool:0:material") == "PLA"
    assert _state(hass, "sensor", CORE_ONE, "tool:0:nozzle_diameter") == "0.4"
    assert _state(hass, "binary_sensor", CORE_ONE, "tool:0:active") == STATE_ON
    assert _state(hass, "binary_sensor", CORE_ONE, "connected") == STATE_ON
    assert _state(hass, "number", CORE_ONE, "tool:0:nozzle_target_temperature") == "215.0"
    assert _state(hass, "number", CORE_ONE, "bed_target_temperature") == "60.0"
    assert _state(hass, "number", CORE_ONE, "speed") == "100.0"
    assert not _has(hass, "sensor", CORE_ONE, "tool:0:feeder_progress")
    state = hass.states.get(_entity_id(hass, "sensor", CORE_ONE, "state"))
    assert state is not None
    assert state.attributes["build_volume_x"] == 250
    assert "printing" in state.attributes["options"]
    # Names count tools from 1, as the printer's screen does.
    nozzle = hass.states.get(_entity_id(hass, "sensor", CORE_ONE, "tool:0:nozzle_temperature"))
    assert nozzle is not None
    assert nozzle.attributes["friendly_name"] == "CoreOne DEMO-01 Tool 1 nozzle temperature"


async def test_buttons_follow_state(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    assert _state(hass, "button", CORE_ONE, "pause") != STATE_UNAVAILABLE
    assert _state(hass, "button", CORE_ONE, "resume") == STATE_UNAVAILABLE
    assert _state(hass, "button", CORE_ONE, "stop") != STATE_UNAVAILABLE
    assert _state(hass, "button", XL, "pause") == STATE_UNAVAILABLE
    assert _state(hass, "button", MK4S_MMU, "stop") != STATE_UNAVAILABLE


async def test_mmu(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    assert _state(hass, "sensor", MK4S_MMU, "state") == "attention"
    assert _state(hass, "sensor", MK4S_MMU, "tool:0:feeder_progress") == "0"
    assert _state(hass, "sensor", MK4S_MMU, "tool:0:feeder_command") == STATE_UNKNOWN
    # The head prints what the loaded slot carries, so it has no material of its own.
    assert not _has(hass, "sensor", MK4S_MMU, "tool:0:material")
    assert _state(hass, "sensor", MK4S_MMU, "tool:0.0:material") == "PLA"
    assert _state(hass, "sensor", MK4S_MMU, "tool:0.1:material") == "PETG"
    assert _state(hass, "sensor", MK4S_MMU, "tool:0.4:material") == STATE_UNKNOWN
    assert _state(hass, "binary_sensor", MK4S_MMU, "tool:0.0:active") == STATE_ON
    assert _state(hass, "binary_sensor", MK4S_MMU, "tool:0.3:active") == STATE_OFF
    assert _state(hass, "number", MK4S_MMU, "tool:0:nozzle_target_temperature") == "0.0"
    slot = hass.states.get(_entity_id(hass, "sensor", MK4S_MMU, "tool:0.2:material"))
    assert slot is not None
    assert slot.attributes["friendly_name"] == "MK4S DEMO-02 Tool 1 slot 3 material"


async def test_xl_multi_head(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    for tool in ("0", "1", "2", "3", "4"):
        assert _has(hass, "sensor", XL, f"tool:{tool}:nozzle_temperature")
        assert _has(hass, "number", XL, f"tool:{tool}:nozzle_target_temperature")
    assert _state(hass, "binary_sensor", XL, "tool:0:active") == STATE_ON
    assert _state(hass, "binary_sensor", XL, "tool:1:active") == STATE_OFF
    assert _state(hass, "binary_sensor", XL, "tool:1:nozzle_high_flow") == STATE_ON
    assert _state(hass, "sensor", XL, "tool:4:material") == "PVA"
    # Head 1 has not said what its target is.
    assert _state(hass, "number", XL, "tool:1:nozzle_target_temperature") == STATE_UNKNOWN
    second = hass.states.get(_entity_id(hass, "sensor", XL, "tool:1:nozzle_temperature"))
    assert second is not None
    assert second.attributes["friendly_name"] == "XL DEMO-03 Tool 2 nozzle temperature"


async def test_xl_disabled_head(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    assert not _has(hass, "sensor", XL_DISABLED, "tool:2:nozzle_temperature")
    assert not _has(hass, "number", XL_DISABLED, "tool:2:nozzle_target_temperature")
    assert _has(hass, "sensor", XL_DISABLED, "tool:3:nozzle_temperature")


async def test_other_vendor(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    assert _state(hass, "sensor", KLIPPER, "state") == "idle"
    assert _state(hass, "sensor", KLIPPER, "tool:0:nozzle_temperature") == "25.8"
    assert not _has(hass, "sensor", KLIPPER, "tool:0:nozzle_diameter")
    assert not _has(hass, "sensor", KLIPPER, "tool:0:material")
    assert not _has(hass, "number", KLIPPER, "bed_target_temperature")
    assert _has(hass, "number", KLIPPER, "tool:0:nozzle_target_temperature")
    assert not _has(hass, "button", KLIPPER, "pause")
    state = hass.states.get(_entity_id(hass, "sensor", KLIPPER, "state"))
    assert state is not None
    assert "build_volume_x" not in state.attributes


async def test_axes_disabled_by_default(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    entry = er.async_get(hass).async_get(_entity_id(hass, "sensor", CORE_ONE, "axis_z"))
    assert entry is not None
    assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION


async def test_camera(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    from homeassistant.components.camera import async_get_image

    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("camera", DOMAIN, f"{INSTANCE}:camera:cam-demo-01")
    assert entity_id is not None
    entry = registry.async_get(entity_id)
    assert entry is not None
    device = dr.async_get(hass).async_get(entry.device_id or "")
    assert device is not None
    assert (DOMAIN, f"{INSTANCE}:{CORE_ONE}") in device.identifiers
    image = await async_get_image(hass, entity_id)
    assert image.content.startswith(b"\xff\xd8")
