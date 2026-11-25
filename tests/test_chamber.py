"""Chamber, flow, start time and thumbnail of the printers that report them."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.components.fan import (
    ATTR_PERCENTAGE,
    ATTR_PRESET_MODE,
    SERVICE_SET_PERCENTAGE,
    SERVICE_SET_PRESET_MODE,
)
from homeassistant.components.fan import DOMAIN as FAN_DOMAIN
from homeassistant.components.light import ATTR_BRIGHTNESS
from homeassistant.components.light import DOMAIN as LIGHT_DOMAIN
from homeassistant.components.number import ATTR_VALUE, SERVICE_SET_VALUE
from homeassistant.components.number import DOMAIN as NUMBER_DOMAIN
from homeassistant.const import ATTR_ENTITY_ID, SERVICE_TURN_OFF, SERVICE_TURN_ON, STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.prusa_connect.const import DOMAIN

from .conftest import CORE_ONE, INSTANCE, KLIPPER, XL, FakeClient
from .helpers import flush, telemetry


def _entity_id(hass: HomeAssistant, platform: str, device_id: str, key: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{INSTANCE}:{device_id}:{key}")
    assert entity_id is not None, f"{platform} {key} of {device_id} is missing"
    return entity_id


def _has(hass: HomeAssistant, platform: str, device_id: str, key: str) -> bool:
    return (
        er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{INSTANCE}:{device_id}:{key}") is not None
    )


async def test_chamber_entities(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    def state(platform: str, key: str) -> str:
        s = hass.states.get(_entity_id(hass, platform, CORE_ONE, key))
        assert s is not None
        return s.state

    assert state("sensor", "chamber_temperature") == "31.5"
    assert state("sensor", "chamber_fan_1_speed") == "1200"
    assert state("sensor", "chamber_fan_2_speed") == "1180"
    assert state("number", "chamber_target_temperature") == "40.0"
    assert state("number", "flow") == "100.0"
    assert state("light", "chamber_light") == STATE_ON
    light = hass.states.get(_entity_id(hass, "light", CORE_ONE, "chamber_light"))
    assert light is not None
    assert light.attributes[ATTR_BRIGHTNESS] == round(60 * 255 / 100)
    fan = hass.states.get(_entity_id(hass, "fan", CORE_ONE, "chamber_fan"))
    assert fan is not None
    assert fan.state == STATE_ON
    assert fan.attributes[ATTR_PRESET_MODE] == "auto"
    # Printers without a chamber get none of it.
    for platform, key in (
        ("sensor", "chamber_temperature"),
        ("number", "chamber_target_temperature"),
        ("light", "chamber_light"),
        ("fan", "chamber_fan"),
    ):
        assert not _has(hass, platform, XL, key)
        assert not _has(hass, platform, KLIPPER, key)
    # Flow needs the verb: the XL has it, the Klipper driver does not.
    assert _has(hass, "number", XL, "flow")
    assert not _has(hass, "number", KLIPPER, "flow")


async def test_chamber_commands(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_client: FakeClient
) -> None:
    light = _entity_id(hass, "light", CORE_ONE, "chamber_light")
    fan = _entity_id(hass, "fan", CORE_ONE, "chamber_fan")
    calls = [
        (LIGHT_DOMAIN, SERVICE_TURN_OFF, {ATTR_ENTITY_ID: light}),
        (LIGHT_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: light}),
        (LIGHT_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: light, ATTR_BRIGHTNESS: 255}),
        (FAN_DOMAIN, SERVICE_SET_PERCENTAGE, {ATTR_ENTITY_ID: fan, ATTR_PERCENTAGE: 40}),
        (FAN_DOMAIN, SERVICE_TURN_OFF, {ATTR_ENTITY_ID: fan}),
        (FAN_DOMAIN, SERVICE_SET_PRESET_MODE, {ATTR_ENTITY_ID: fan, ATTR_PRESET_MODE: "auto"}),
        (FAN_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: fan}),
        (
            NUMBER_DOMAIN,
            SERVICE_SET_VALUE,
            {
                ATTR_ENTITY_ID: _entity_id(hass, "number", CORE_ONE, "chamber_target_temperature"),
                ATTR_VALUE: 45,
            },
        ),
        (
            NUMBER_DOMAIN,
            SERVICE_SET_VALUE,
            {ATTR_ENTITY_ID: _entity_id(hass, "number", CORE_ONE, "flow"), ATTR_VALUE: 95},
        ),
    ]
    for domain, service, data in calls:
        await hass.services.async_call(domain, service, data, blocking=True)
    led = "core.command.set_chamber_led_intensity"
    fan_verb = "core.command.set_chamber_fan_target"
    assert mock_client.commands == [
        (CORE_ONE, led, {"chamber.led_intensity": 0}),
        # A plain "turn on" brings back the last intensity.
        (CORE_ONE, led, {"chamber.led_intensity": 60}),
        (CORE_ONE, led, {"chamber.led_intensity": 100}),
        (CORE_ONE, fan_verb, {"chamber.fan_pwm_target": 40}),
        (CORE_ONE, fan_verb, {"chamber.fan_pwm_target": 0}),
        (CORE_ONE, fan_verb, {"chamber.fan_pwm_target": -1}),
        (CORE_ONE, fan_verb, {"chamber.fan_pwm_target": -1}),
        (CORE_ONE, "core.command.set_chamber_target_temp", {"chamber.target_temp": 45}),
        (CORE_ONE, "core.command.set_flow", {"flow": 95}),
    ]


async def test_light_and_fan_follow_telemetry(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_client: FakeClient
) -> None:
    mock_client.queue.put_nowait(telemetry(CORE_ONE, "/chamber/led_intensity", 0))
    mock_client.queue.put_nowait(telemetry(CORE_ONE, "/chamber/fan_pwm_target", 0))
    await flush(hass)
    light = hass.states.get(_entity_id(hass, "light", CORE_ONE, "chamber_light"))
    fan = hass.states.get(_entity_id(hass, "fan", CORE_ONE, "chamber_fan"))
    assert light is not None
    assert fan is not None
    assert light.state == STATE_OFF
    assert fan.state == STATE_OFF


async def test_start_time(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    state = hass.states.get(_entity_id(hass, "sensor", CORE_ONE, "start_time"))
    assert state is not None
    start = dt_util.parse_datetime(state.state)
    assert start is not None
    # The fixture's print has run 33 seconds; the start is kept to the minute.
    assert start.second == 0
    assert dt_util.utcnow() - start < timedelta(minutes=2)
    idle = hass.states.get(_entity_id(hass, "sensor", XL, "start_time"))
    assert idle is not None
    assert idle.state == "unknown"


async def test_thumbnail(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_client: FakeClient,
    hass_client: ClientSessionGenerator,
) -> None:
    entity_id = _entity_id(hass, "image", CORE_ONE, "thumbnail")
    state = hass.states.get(entity_id)
    assert state is not None
    client = await hass_client()
    response = await client.get(f"/api/image_proxy/{entity_id}")
    assert response.status == 200
    assert await response.read() == b"png:/usb/tangle.bgcode"
    assert mock_client.thumbnail_requests == [(CORE_ONE, "/usb/tangle.bgcode")]


async def test_thumbnail_follows_the_file(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_client: FakeClient,
    hass_client: ClientSessionGenerator,
) -> None:
    entity_id = _entity_id(hass, "image", CORE_ONE, "thumbnail")
    before = hass.states.get(entity_id)
    assert before is not None
    mock_client.queue.put_nowait(telemetry(CORE_ONE, "/job/path", "/usb/next.bgcode"))
    await flush(hass)
    after = hass.states.get(entity_id)
    assert after is not None
    assert after.state != before.state
    client = await hass_client()
    response = await client.get(f"/api/image_proxy/{entity_id}")
    assert await response.read() == b"png:/usb/next.bgcode"


async def test_observe_only_token_gets_no_chamber_controls(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: FakeClient
) -> None:
    for record in mock_client.records.values():
        record["actions"] = ["observe"]
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.async_entity_ids("light") == []
    assert hass.states.async_entity_ids("fan") == []
    assert hass.states.async_entity_ids("image") == []
    assert _has(hass, "sensor", CORE_ONE, "chamber_temperature")
