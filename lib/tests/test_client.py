"""The client against a fake server answering recorded responses."""

from __future__ import annotations

from collections.abc import Callable

import aiohttp
import pytest

from prusa_connect import (
    AuthenticationError,
    CannotConnectError,
    CommandRefusedError,
    Device,
    NotFoundError,
    PrusaConnect,
    UnsupportedError,
)

from .conftest import FakeServer, load

Make = Callable[..., PrusaConnect]
CORE_ONE = "637d5756-4645-4a08-906f-e8dbb9afc56f"
XL = "e34e1d60-3393-4d6b-a709-edb7f01cfaea"


async def test_health_and_settings(make_client: Make) -> None:
    client = make_client(token=None)
    health = await client.health()
    assert health.status == "ok"
    assert health.version == "0.0.0-dev"
    settings = await client.app_settings()
    assert settings.title


async def test_devices_walks_pages(
    make_client: Make, fake: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("prusa_connect.client.DEVICE_PAGE_SIZE", 2)
    devices = await make_client().devices()
    assert len(devices) == len(fake.devices) == 5
    assert {d.model for d in devices} >= {"XL", "CoreOne", "MK4S"}


async def test_device_and_errors(make_client: Make) -> None:
    device = await make_client().device(CORE_ONE)
    assert device.serial == "DEMO-01"
    with pytest.raises(NotFoundError):
        await make_client().device("missing")
    with pytest.raises(AuthenticationError) as err:
        await make_client(token="wrong").devices()
    assert err.value.code == "core.auth/TOKEN_INVALID"


async def test_cannot_connect(session: aiohttp.ClientSession) -> None:
    client = PrusaConnect(session, "http://127.0.0.1:9", "t", request_timeout=2)
    with pytest.raises(CannotConnectError):
        await client.health()


async def test_print_controls(make_client: Make, fake: FakeServer) -> None:
    client = make_client()
    await client.pause_print(CORE_ONE)
    await client.resume_print(CORE_ONE)
    await client.stop_print(CORE_ONE)
    assert [c[1]["verb"] for c in fake.commands] == [
        "core.command.pause_print",
        "core.command.resume_print",
        "core.command.stop_print",
    ]
    assert "args" not in fake.commands[0][1]


async def test_command_key_and_refusal(make_client: Make, fake: FakeServer) -> None:
    client = make_client()
    await client.command(CORE_ONE, "core.command.beep", key="k1")
    assert fake.commands[-1][1]["key"] == "k1"
    fake.command_answer = {"ok": False, "fault_code": "core.command/REFUSED", "machine_reason": "busy"}
    with pytest.raises(CommandRefusedError) as err:
        await client.pause_print(CORE_ONE)
    assert err.value.machine_reason == "busy"


async def test_nozzle_temperature_xl_uses_zero_based_tool(make_client: Make, fake: FakeServer) -> None:
    xl = Device.from_dict(load("device_xl.json"))
    await make_client().set_nozzle_temperature(xl, 215.4, tool="1")
    assert fake.commands[-1] == (
        XL,
        {"verb": "core.command.set_nozzle_temperature", "args": {"nozzle_temperature": "215", "tool": "1"}},
    )


async def test_nozzle_temperature_single_head(make_client: Make, fake: FakeServer) -> None:
    core_one = Device.from_dict(load("device_coreone.json"))
    client = make_client()
    await client.set_nozzle_temperature(core_one, 0, tool="0")
    assert fake.commands[-1][1]["args"] == {"nozzle_temperature": "0"}
    with pytest.raises(UnsupportedError):
        await client.set_nozzle_temperature(core_one, 200, tool="1")
    with pytest.raises(UnsupportedError):
        await client.set_nozzle_temperature(core_one, 200, tool="0.1")


async def test_value_argument_is_read_from_capabilities(make_client: Make, fake: FakeServer) -> None:
    klipper = Device.from_dict(load("device_klipper.json"))
    client = make_client()
    await client.set_nozzle_temperature(klipper, 200)
    assert fake.commands[-1][1]["args"] == {"celsius": "200"}
    with pytest.raises(UnsupportedError):
        await client.set_bed_temperature(klipper, 60)


async def test_bed_and_speed(make_client: Make, fake: FakeServer) -> None:
    core_one = Device.from_dict(load("device_coreone.json"))
    client = make_client()
    await client.set_bed_temperature(core_one, 60)
    await client.set_speed(core_one, 150)
    assert fake.commands[-2][1]["args"] == {"bed_temperature": "60"}
    assert fake.commands[-1][1]["args"] == {"speed": "150"}


async def test_flow_and_chamber(make_client: Make, fake: FakeServer) -> None:
    core_one = Device.from_dict(load("device_coreone.json"))
    client = make_client()
    await client.set_flow(core_one, 95)
    await client.set_chamber_temperature(core_one, 40.4)
    await client.set_chamber_fan(core_one, 30)
    await client.set_chamber_fan(core_one, None)
    await client.set_chamber_light(core_one, 0)
    assert [c[1] for c in fake.commands] == [
        {"verb": "core.command.set_flow", "args": {"flow": "95"}},
        {"verb": "core.command.set_chamber_target_temp", "args": {"chamber.target_temp": "40"}},
        {"verb": "core.command.set_chamber_fan_target", "args": {"chamber.fan_pwm_target": "30"}},
        {"verb": "core.command.set_chamber_fan_target", "args": {"chamber.fan_pwm_target": "-1"}},
        {"verb": "core.command.set_chamber_led_intensity", "args": {"chamber.led_intensity": "0"}},
    ]
    klipper = Device.from_dict(load("device_klipper.json"))
    with pytest.raises(UnsupportedError):
        await client.set_chamber_light(klipper, 50)


async def test_chamber_readings() -> None:
    core_one = Device.from_dict(load("device_coreone.json"))
    assert core_one.chamber_temp == 31.5
    assert core_one.chamber_target_temp == 40
    assert core_one.chamber_fan_rpm(1) == 1200
    assert core_one.chamber_fan_target == -1
    assert core_one.chamber_light == 60
    assert core_one.job_id == 3
    assert core_one.chamber_max_temp == 55
    core_one_l = Device.from_dict({**load("device_coreone.json"), "model": "CORE One L"})
    assert core_one_l.chamber_max_temp == 60
    xl = Device.from_dict(load("device_xl.json"))
    assert xl.chamber_temp is None
    assert xl.chamber_light is None


async def test_thumbnail(make_client: Make, fake: FakeServer) -> None:
    client = make_client()
    thumbnail = await client.thumbnail(CORE_ONE, "/usb/tangle.bgcode")
    assert thumbnail is not None
    assert thumbnail.content == b"\x89PNG"
    assert thumbnail.content_type == "image/png"
    assert thumbnail.etag == '"v1"'
    assert fake.thumbnail_requests[-1]["path"] == "/usb/tangle.bgcode"
    assert await client.thumbnail(CORE_ONE, "/usb/tangle.bgcode", etag='"v1"') is None
    assert await client.thumbnail(CORE_ONE, "/usb/plain.gcode") is None


async def test_materials(make_client: Make, fake: FakeServer, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("prusa_connect.client.SPOOL_PAGE_SIZE", 2)
    fake.spools[1]["tool"] = {"device_id": CORE_ONE, "tool_id": "0", "gone": False}
    fake.spools[2]["tool"] = {"device_id": CORE_ONE, "tool_id": "1", "gone": True}
    client = make_client()
    spools = await client.materials_spools()
    assert [s.id for s in spools] == [0, 1, 2]
    assert (spools[1].device_id, spools[1].tool_id) == (CORE_ONE, "0")
    assert spools[2].device_id is None
    assert spools[0].net_weight_g == 1000
    assert spools[0].remaining_weight_g == 100
    tools = await client.materials_assignments(CORE_ONE)
    assert [(t.tool_id, t.spool_id, t.type) for t in tools] == [("0", 2, "PETG"), ("1", None, None)]
    with pytest.raises(NotFoundError):
        await client.materials_assignments("missing")


async def test_cameras_and_snapshot(make_client: Make, fake: FakeServer) -> None:
    client = make_client()
    cameras = await client.cameras()
    assert len(cameras) == 5
    assert cameras[0].snapshot
    assert cameras[0].stream
    one = await client.cameras(CORE_ONE)
    assert [c.name for c in one] == ["cam-demo-01"]
    snap = await client.snapshot("cam-demo-01")
    assert snap is not None
    assert snap.content.startswith(b"\xff\xd8")
    assert snap.last_modified
    fake.snapshot_status = 204
    assert await client.snapshot("cam-demo-01") is None
    fake.snapshot_status = 304
    assert await client.snapshot("cam-demo-01", if_modified_since="x") is None


async def test_device_states(make_client: Make) -> None:
    vocab = await make_client().device_states()
    assert vocab.kinds == ()


async def test_setup_status(make_client: Make) -> None:
    status = await make_client(token=None).setup_status()
    assert status.done
    assert status.mode == "required"
