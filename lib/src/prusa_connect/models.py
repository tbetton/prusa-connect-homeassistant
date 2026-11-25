"""Typed models for the parts of the Prusa Connect API this library uses.

The shapes follow the OpenAPI document served at ``/openapi.json`` (API v1).
Only the members a client needs are modelled; everything else stays reachable
through the raw dictionaries.

A device's live readings are a flat state tree keyed by path, such as
``/tools/0/nozzle_temp``. The event stream patches that tree one path at a
time, so :class:`Device` keeps the tree and derives every reading, including
the tool tree, from it. A path that is absent is unknown and is never read as
zero, false or "nothing loaded".
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# Verbs this library knows by name. Every verb is a capability key, and a
# device lists the ones it accepts in ``capabilities``.
VERB_PAUSE_PRINT = "core.command.pause_print"
VERB_RESUME_PRINT = "core.command.resume_print"
VERB_STOP_PRINT = "core.command.stop_print"
VERB_SET_NOZZLE_TEMPERATURE = "core.command.set_nozzle_temperature"
VERB_SET_BED_TEMPERATURE = "core.command.set_bed_temperature"
VERB_SET_SPEED = "core.command.set_speed"
VERB_SET_FLOW = "core.command.set_flow"
VERB_SET_CHAMBER_TEMPERATURE = "core.command.set_chamber_target_temp"
VERB_SET_CHAMBER_FAN = "core.command.set_chamber_fan_target"
VERB_SET_CHAMBER_LIGHT = "core.command.set_chamber_led_intensity"

# The chamber fan target that hands the fans back to the printer's own control.
CHAMBER_FAN_AUTOMATIC = -1

# The argument that names the head a per-tool verb addresses. Its value is the
# 0-based head index, the same number as the head's tool id.
ARG_TOOL = "tool"

# Event types this library reacts to.
EVENT_TELEMETRY = "com.prusa.connect.device.telemetry.sampled"
EVENT_ARRIVED = "com.prusa.connect.device.arrived"
EVENT_DEPARTED = "com.prusa.connect.device.departed"
EVENT_PRESENCE = "com.prusa.connect.device.presence.changed"
EVENT_RECORD = "com.prusa.connect.device.record.changed"
EVENT_JOB_STARTED = "com.prusa.connect.device.job.started"
EVENT_JOB_SETTLED = "com.prusa.connect.device.job.settled"
EVENT_CONTINUITY_LOST = "com.prusa.connect.stream.continuity.lost"
EVENT_AUTH_EXPIRED = "prusa-connect.auth.expired"

# The Materials extension (spools and what each tool is loaded with), when the
# server has it installed.
MATERIALS_MODULE = "com.github.aux.prusa3d.materials"
EVENT_MATERIALS_SPOOL = f"{MATERIALS_MODULE}.spool"
EVENT_MATERIALS_TOOL = f"{MATERIALS_MODULE}.tool"

TOOL_TYPE_HEAD = "FFF_PRINT_HEAD"
TOOL_TYPE_SLOT = "FFF_FEEDER_SLOT"

_TOOL_ID = re.compile(r"^(0|[1-9][0-9]*)(?:\.(0|[1-9][0-9]*))?$")
_TOOLS_PREFIX = "/tools/"
_MATERIAL = "material_package_instance/package/material/abbreviation"


def _float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def parse_tool_id(tool_id: str) -> tuple[int, int | None] | None:
    """Split a tool id such as ``0`` or ``0.2`` into head and slot.

    Returns ``None`` for a string that is not a tool id.
    """
    match = _TOOL_ID.match(tool_id)
    if match is None:
        return None
    slot = match.group(2)
    return int(match.group(1)), (int(slot) if slot is not None else None)


@dataclass(frozen=True, slots=True)
class Health:
    """The answer of ``GET /api/v1/health``."""

    status: str
    version: str
    # The server's own id: random, written on its first start, kept across
    # restarts and address changes. The mDNS record carries it as ``id``.
    instance_id: str | None = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Health:
        return cls(
            status=str(data.get("status", "")),
            version=str(data.get("version", "")),
            instance_id=_str(data.get("instance_id")) or None,
        )


@dataclass(frozen=True, slots=True)
class AppPairing:
    """The answer of ``POST /api/v1/auth/app-pairings`` (RFC 8628 device authorization).

    ``device_code`` is secret and stays with the app. ``user_code`` and
    ``verification_uri_complete`` are what the person sees.
    """

    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str
    expires_in: int
    interval: int

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> AppPairing:
        verification_uri = str(data.get("verification_uri", ""))
        return cls(
            device_code=str(data["device_code"]),
            user_code=str(data["user_code"]),
            verification_uri=verification_uri,
            verification_uri_complete=str(data.get("verification_uri_complete") or verification_uri),
            expires_in=_int(data.get("expires_in")) or 600,
            interval=_int(data.get("interval")) or 5,
        )


@dataclass(frozen=True, slots=True)
class SetupStatus:
    """The subset of ``GET /api/v1/setup`` a client uses: has the first run finished?

    ``mode`` is ``required`` when every caller needs a credential and ``open``
    when the server serves every request without one.
    """

    done: bool
    mode: str | None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SetupStatus:
        return cls(done=data.get("state") == "done", mode=_str(data.get("mode")))


@dataclass(frozen=True, slots=True)
class AppSettings:
    """The subset of ``GET /api/v1/ui`` a client uses."""

    title: str
    enabled: bool
    default_language: str

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> AppSettings:
        return cls(
            title=str(data.get("title", "")),
            enabled=bool(data.get("enabled", False)),
            default_language=str(data.get("default_language", "")),
        )


@dataclass(frozen=True, slots=True)
class BuildVolume:
    """The space a print may fill, in millimetres."""

    x_mm: float
    y_mm: float
    z_mm: float


@dataclass(frozen=True, slots=True)
class Feeder:
    """The feeder on a head, such as a multi-material unit."""

    slot_count: int
    type: str | None = None
    model: str | None = None
    base_model: str | None = None
    mmu_progress_code: int | None = None
    mmu_command_in_progress: str | None = None


@dataclass(frozen=True, slots=True)
class Tool:
    """One head or feeder slot of the tool tree.

    ``nozzle_target_temp`` is ``0`` while the heater is off and ``None`` when
    the printer has not said. ``material`` is ``""`` when nothing is loaded and
    ``None`` when the printer has not said.
    """

    id: str
    head: int
    slot: int | None
    enabled: bool = True
    type: str | None = None
    active: bool | None = None
    nozzle_temp: float | None = None
    nozzle_target_temp: float | None = None
    print_fan_rpm: float | None = None
    heatbreak_fan_rpm: float | None = None
    nozzle_diameter: float | None = None
    nozzle_hardened: bool | None = None
    nozzle_high_flow: bool | None = None
    nozzle_high_temp: bool | None = None
    material: str | None = None
    consumed_weight: float | None = None
    odometer_e: float | None = None
    odometer_weight: float | None = None
    feeder: Feeder | None = None

    @property
    def is_head(self) -> bool:
        """Whether this is a head rather than a feeder slot."""
        return self.slot is None

    @property
    def head_id(self) -> str:
        """The tool id of the head this tool is, or sits on."""
        return str(self.head)


@dataclass(frozen=True, slots=True)
class ToolTree:
    """Every tool a printer reported, keyed by tool id."""

    tools: dict[str, Tool] = field(default_factory=dict)
    tool_count: int | None = None
    gcode_tool_map: dict[str, str] = field(default_factory=dict)

    @property
    def heads(self) -> list[Tool]:
        """The heads, in head order."""
        return sorted((t for t in self.tools.values() if t.is_head), key=lambda t: t.head)

    def slots(self, head: int) -> list[Tool]:
        """The feeder slots of one head, in slot order."""
        return sorted(
            (t for t in self.tools.values() if t.head == head and t.slot is not None),
            key=lambda t: t.slot or 0,
        )

    @property
    def active_head(self) -> Tool | None:
        """The head printing now: the active enabled head, else head 0."""
        for head in self.heads:
            if head.enabled and head.active:
                return head
        return self.tools.get("0")

    @classmethod
    def from_state(cls, state: Mapping[str, Any]) -> ToolTree:
        """Read the tool tree out of a flat state tree."""
        members: dict[str, dict[str, Any]] = {}
        gcode_map: dict[str, str] = {}
        for path, value in state.items():
            if path.startswith("/gcode_tool_map/"):
                if isinstance(value, str):
                    gcode_map[path.removeprefix("/gcode_tool_map/")] = value
                continue
            if not path.startswith(_TOOLS_PREFIX):
                continue
            tool_id, _, member = path.removeprefix(_TOOLS_PREFIX).partition("/")
            if member and parse_tool_id(tool_id) is not None:
                members.setdefault(tool_id, {})[member] = value
        tools = {tool_id: _tool_from_members(tool_id, m) for tool_id, m in members.items()}
        return cls(tools=tools, tool_count=_int(state.get("/tool_count")), gcode_tool_map=gcode_map)

    @classmethod
    def from_api(cls, data: Mapping[str, Any]) -> ToolTree:
        """Read the ``tools`` member of a device record.

        A tool the printer switched off is ``null`` there; it comes back as a
        tool with ``enabled`` false.
        """
        tools: dict[str, Tool] = {}
        for tool_id, raw in (data.get("tools") or {}).items():
            parsed = parse_tool_id(tool_id)
            if parsed is None:
                continue
            head, slot = parsed
            if raw is None:
                tools[tool_id] = Tool(id=tool_id, head=head, slot=slot, enabled=False)
                continue
            features = raw.get("features") or {}
            feeder_raw = raw.get("feeder")
            feeder = None
            if isinstance(feeder_raw, Mapping):
                feeder = Feeder(
                    slot_count=int(feeder_raw.get("slot_count", 0)),
                    type=_str(feeder_raw.get("type")),
                    model=_str(feeder_raw.get("model")),
                    base_model=_str(feeder_raw.get("base_model")),
                    mmu_progress_code=_int(feeder_raw.get("mmu_progress_code")),
                    mmu_command_in_progress=_str(feeder_raw.get("mmu_command_in_progress")),
                )
            material: str | None = None
            if "material_package_instance" in raw:
                mpi = raw["material_package_instance"]
                material = str(mpi["package"]["material"]["abbreviation"]) if isinstance(mpi, Mapping) else ""
            target: float | None = None
            if "nozzle_target_temp" in raw:
                target = _float(raw["nozzle_target_temp"]) or 0.0
            tools[tool_id] = Tool(
                id=tool_id,
                head=head,
                slot=slot,
                type=_str(raw.get("type")),
                active=_bool(raw.get("active")),
                nozzle_temp=_float(raw.get("nozzle_temp")),
                nozzle_target_temp=target,
                print_fan_rpm=_float(raw.get("print_fan_rpm")),
                heatbreak_fan_rpm=_float(raw.get("heatbreak_fan_rpm")),
                nozzle_diameter=_float(features.get("nozzle_diameter")),
                nozzle_hardened=_bool(features.get("nozzle_hardened")),
                nozzle_high_flow=_bool(features.get("nozzle_high_flow")),
                nozzle_high_temp=_bool(features.get("nozzle_high_temp")),
                material=material,
                consumed_weight=_float(raw.get("consumed_weight")),
                odometer_e=_float(raw.get("odometer_e")),
                odometer_weight=_float(raw.get("odometer_weight")),
                feeder=feeder,
            )
        return cls(
            tools=tools,
            tool_count=_int(data.get("tool_count")),
            gcode_tool_map=dict(data.get("gcode_tool_map") or {}),
        )


def _tool_from_members(tool_id: str, m: Mapping[str, Any]) -> Tool:
    parsed = parse_tool_id(tool_id)
    assert parsed is not None
    head, slot = parsed
    enabled = m.get("enabled")
    if enabled is False:
        return Tool(id=tool_id, head=head, slot=slot, enabled=False)
    feeder = None
    slot_count = _int(m.get("feeder/slot_count"))
    if slot_count:
        feeder = Feeder(
            slot_count=slot_count,
            type=_str(m.get("feeder/type")),
            model=_str(m.get("feeder/model")),
            base_model=_str(m.get("feeder/base_model")),
            mmu_progress_code=_int(m.get("feeder/mmu_progress_code")),
            mmu_command_in_progress=_str(m.get("feeder/mmu_command_in_progress")),
        )
    return Tool(
        id=tool_id,
        head=head,
        slot=slot,
        type=_str(m.get("type")),
        active=_bool(m.get("active")),
        nozzle_temp=_float(m.get("nozzle_temp")),
        nozzle_target_temp=_float(m.get("nozzle_target_temp")),
        print_fan_rpm=_float(m.get("print_fan_rpm")),
        heatbreak_fan_rpm=_float(m.get("heatbreak_fan_rpm")),
        nozzle_diameter=_float(m.get("features/nozzle_diameter")),
        nozzle_hardened=_bool(m.get("features/nozzle_hardened")),
        nozzle_high_flow=_bool(m.get("features/nozzle_high_flow")),
        nozzle_high_temp=_bool(m.get("features/nozzle_high_temp")),
        material=_str(m.get(_MATERIAL)),
        consumed_weight=_float(m.get("consumed_weight")),
        odometer_e=_float(m.get("odometer_e")),
        odometer_weight=_float(m.get("odometer_weight")),
        feeder=feeder,
    )


@dataclass(frozen=True, slots=True)
class JobSummary:
    """The print a device last reported."""

    id: str
    display_name: str
    path: str
    state: str
    progress: float
    time_remaining_ms: int
    running: bool

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> JobSummary:
        return cls(
            id=str(data.get("id", "")),
            display_name=str(data.get("display_name", "")),
            path=str(data.get("path", "")),
            state=str(data.get("state", "")),
            progress=float(data.get("progress", 0)),
            time_remaining_ms=int(data.get("time_remaining_ms", 0)),
            running=bool(data.get("running", False)),
        )


@dataclass(slots=True)
class Device:
    """One device record, with its state tree kept current by the caller.

    The record is mutable on purpose: :meth:`apply` patches the state tree
    from a telemetry event and :attr:`connected` follows presence events.
    """

    id: str
    name: str
    kind: str
    vendor: str
    model: str
    firmware: str
    serial: str
    driver: str
    connected: bool
    capabilities: frozenset[str]
    actions: frozenset[str]
    state: dict[str, Any]
    job: JobSummary | None = None
    departed: bool = False
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Device:
        job = data.get("job")
        return cls(
            id=str(data["id"]),
            name=str(data.get("name", "")),
            kind=str(data.get("kind", "printer")),
            vendor=str(data.get("vendor", "")),
            model=str(data.get("model", "")),
            firmware=str(data.get("firmware", "")),
            serial=str(data.get("serial", "")),
            driver=str(data.get("driver", "")),
            connected=bool(data.get("connected", False)),
            capabilities=frozenset(data.get("capabilities") or ()),
            actions=frozenset(data.get("actions") or ()),
            state=dict(data.get("state") or {}),
            job=JobSummary.from_dict(job) if isinstance(job, Mapping) else None,
            departed="departed" in data,
            raw=dict(data),
        )

    def apply(self, path: str, value: Any) -> None:
        """Apply one state patch. A ``None`` value removes the path."""
        if value is None:
            self.state.pop(path, None)
        else:
            self.state[path] = value

    def supports(self, verb: str) -> bool:
        """Whether the device lists this verb among its capabilities."""
        return verb in self.capabilities

    def arguments(self, verb: str) -> list[str]:
        """The argument names the device accepts for a verb, sorted."""
        prefix = verb + "#"
        return sorted(c.removeprefix(prefix) for c in self.capabilities if c.startswith(prefix))

    def may(self, action: str) -> bool:
        """Whether the caller holds an authorization action on this device."""
        return action in self.actions

    @property
    def tools(self) -> ToolTree:
        """The tool tree, read out of the state tree."""
        return ToolTree.from_state(self.state)

    @property
    def printer_state(self) -> str | None:
        """The machine state, in the machine's own vocabulary."""
        return _str(self.state.get("/state"))

    @property
    def bed_temp(self) -> float | None:
        return _float(self.state.get("/temperature/bed"))

    @property
    def bed_target_temp(self) -> float | None:
        return _float(self.state.get("/temperature/bed_target"))

    @property
    def speed(self) -> float | None:
        """The print speed factor, in percent."""
        return _float(self.state.get("/speed"))

    @property
    def flow(self) -> float | None:
        """The flow factor, in percent."""
        return _float(self.state.get("/flow"))

    @property
    def chamber_temp(self) -> float | None:
        return _float(self.state.get("/temperature/chamber"))

    @property
    def chamber_target_temp(self) -> float | None:
        """The chamber's target. ``0`` means no target.

        The chamber has fans and no heater, so the target is the temperature
        the fans hold the air under, and only while they run on their own.
        """
        return _float(self.state.get("/temperature/chamber_target"))

    @property
    def chamber_max_temp(self) -> int:
        """The highest chamber target the model takes: 60 on a CORE One L, 55 on a CORE One."""
        folded = "".join(c for c in self.model.lower() if c.isalnum())
        return 60 if folded.startswith("coreonel") else 55

    def chamber_fan_rpm(self, fan: int) -> int | None:
        """The speed of one chamber fan (``1`` or ``2``), in revolutions per minute."""
        return _int(self.state.get(f"/chamber/fan_{fan}_rpm"))

    @property
    def chamber_fan_target(self) -> int | None:
        """The chamber fan target in percent, or :data:`CHAMBER_FAN_AUTOMATIC`."""
        return _int(self.state.get("/chamber/fan_pwm_target"))

    @property
    def chamber_light(self) -> int | None:
        """The chamber light intensity, in percent. ``0`` is a light that is off."""
        return _int(self.state.get("/chamber/led_intensity"))

    @property
    def job_id(self) -> int | None:
        return _int(self.state.get("/job/id"))

    def axis(self, name: str) -> float | None:
        """The position of one axis (``x``, ``y`` or ``z``), in millimetres."""
        return _float(self.state.get(f"/axis/{name}"))

    @property
    def job_progress(self) -> float | None:
        """How far the current print has got, in percent."""
        return _float(self.state.get("/job/progress"))

    @property
    def job_time_remaining(self) -> int | None:
        """The time the machine thinks is left, in seconds."""
        return _int(self.state.get("/job/time_remaining"))

    @property
    def job_time_printing(self) -> int | None:
        """How long the current print has run, in seconds."""
        return _int(self.state.get("/job/time_printing"))

    @property
    def job_path(self) -> str | None:
        return _str(self.state.get("/job/path"))

    @property
    def job_name(self) -> str | None:
        """What the machine shows for the file being printed."""
        path = self.job_path
        if path is None:
            return None
        if self.job is not None and self.job.path == path and self.job.display_name:
            return self.job.display_name
        return path.rsplit("/", 1)[-1] or None

    @property
    def has_job(self) -> bool:
        """Whether the state tree carries a print."""
        return "/job/id" in self.state or "/job/path" in self.state

    @property
    def build_volume(self) -> BuildVolume | None:
        x = _float(self.state.get("/build_volume/x"))
        y = _float(self.state.get("/build_volume/y"))
        z = _float(self.state.get("/build_volume/z"))
        if x is None or y is None or z is None:
            return None
        return BuildVolume(x, y, z)


@dataclass(frozen=True, slots=True)
class Camera:
    """One declared camera."""

    name: str
    device_id: str
    formats: tuple[str, ...]
    serving: bool
    snapshot: bool
    stream: bool
    watch: bool
    min_interval_ms: int | None = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Camera:
        return cls(
            name=str(data["name"]),
            device_id=str(data.get("device_id", "")),
            formats=tuple(data.get("formats") or ()),
            serving=bool(data.get("serving", False)),
            snapshot=bool(data.get("snapshot", False)),
            stream=bool(data.get("stream", False)),
            watch=bool(data.get("watch", False)),
            min_interval_ms=_int(data.get("min_interval_ms")),
        )


@dataclass(frozen=True, slots=True)
class Snapshot:
    """One still image of a camera."""

    content: bytes
    content_type: str
    last_modified: str | None = None


@dataclass(frozen=True, slots=True)
class Spool:
    """A spool the Materials extension keeps, with what is left on it."""

    id: int
    brand: str | None
    material_name: str | None
    type: str | None
    color_rgba: str | None
    material_uuid: str | None
    full_weight_g: float | None
    empty_weight_g: float | None
    remaining_weight_g: float | None
    location: str | None
    notes: str | None
    archived: bool
    # The tool it is loaded in: the device and the tool's 0-based id.
    device_id: str | None = None
    tool_id: str | None = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Spool:
        raw_tool = data.get("tool")
        tool: Mapping[str, Any] = raw_tool if isinstance(raw_tool, Mapping) else {}
        loaded = bool(tool) and not tool.get("gone")
        return cls(
            id=int(data["id"]),
            brand=_str(data.get("brand_name")) or None,
            material_name=_str(data.get("material_name")) or None,
            type=_str(data.get("type")) or None,
            color_rgba=_str(data.get("color_rgba")) or None,
            material_uuid=_str(data.get("material_uuid")) or None,
            full_weight_g=_float(data.get("full_weight_g")),
            empty_weight_g=_float(data.get("empty_weight_g")),
            remaining_weight_g=_float(data.get("remaining_weight_g")),
            location=_str(data.get("location")) or None,
            notes=_str(data.get("notes")) or None,
            archived=data.get("archived_at") is not None,
            device_id=_str(tool.get("device_id")) if loaded else None,
            tool_id=_str(tool.get("tool_id")) if loaded else None,
        )

    @property
    def net_weight_g(self) -> float | None:
        """The filament a full spool holds: full weight less the empty spool."""
        full, empty = self.full_weight_g, self.empty_weight_g or 0
        return full - empty if full and full > empty else None


@dataclass(frozen=True, slots=True)
class MaterialAssignment:
    """The material a tool is set to in the Materials extension."""

    tool_id: str
    spool_id: int | None
    brand: str | None
    material_name: str | None
    type: str | None
    color_rgba: str | None
    material_uuid: str | None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> MaterialAssignment:
        assignment = data.get("assignment")
        a: Mapping[str, Any] = assignment if isinstance(assignment, Mapping) else {}
        return cls(
            tool_id=str(data["id"]),
            spool_id=_int(a.get("spool_id")),
            brand=_str(a.get("brand_name")) or None,
            material_name=_str(a.get("material_name")) or None,
            type=_str(a.get("type")) or _str(a.get("abbreviation")) or None,
            color_rgba=_str(a.get("color_rgba")) or None,
            material_uuid=_str(a.get("material_uuid")) or None,
        )


@dataclass(frozen=True, slots=True)
class Thumbnail:
    """The picture inside a print file."""

    content: bytes
    content_type: str
    etag: str | None = None


@dataclass(frozen=True, slots=True)
class CommandResult:
    """What the server said about one command."""

    ok: bool
    command_id: str | None = None
    answer: str | None = None
    duplicate: bool = False
    fault_code: str | None = None
    machine_reason: str | None = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CommandResult:
        return cls(
            ok=bool(data.get("ok", False)),
            command_id=_str(data.get("command_id")),
            answer=_str(data.get("answer")),
            duplicate=bool(data.get("duplicate", False)),
            fault_code=_str(data.get("fault_code")),
            machine_reason=_str(data.get("machine_reason")),
        )


@dataclass(frozen=True, slots=True)
class DeviceStateTransition:
    from_state: str
    to_state: str
    verb: str | None


@dataclass(frozen=True, slots=True)
class DeviceStateKind:
    """The states and transitions one device kind declared."""

    kind: str
    namespace: str
    states: tuple[str, ...]
    display_names: dict[str, str]
    transitions: tuple[DeviceStateTransition, ...]


@dataclass(frozen=True, slots=True)
class DeviceStates:
    """The answer of ``GET /api/v1/vocabularies/device-states``."""

    kinds: tuple[DeviceStateKind, ...] = ()

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> DeviceStates:
        kinds = []
        for item in data.get("items") or ():
            states = [s for s in item.get("states") or () if isinstance(s, Mapping)]
            kinds.append(
                DeviceStateKind(
                    kind=str(item.get("kind", "")),
                    namespace=str(item.get("namespace", "")),
                    states=tuple(str(s["state"]) for s in states),
                    display_names={str(s["state"]): str(s.get("display_name", "")) for s in states},
                    transitions=tuple(
                        DeviceStateTransition(str(t["from"]), str(t["to"]), _str(t.get("verb")))
                        for t in item.get("transitions") or ()
                    ),
                )
            )
        return cls(kinds=tuple(kinds))

    @property
    def states(self) -> set[str]:
        """Every state any kind declared."""
        return {s for k in self.kinds for s in k.states}

    def verbs_from(self, state: str) -> set[str] | None:
        """The verbs that move a device out of ``state``.

        ``None`` means the vocabulary does not know the state, so a client
        cannot tell and should let the server decide.
        """
        known = False
        verbs: set[str] = set()
        for kind in self.kinds:
            if state in kind.states:
                known = True
            for t in kind.transitions:
                if t.from_state == state and t.verb:
                    verbs.add(t.verb)
        return verbs if known else None


@dataclass(frozen=True, slots=True)
class Event:
    """One frame of the event stream: a CloudEvents envelope."""

    type: str
    envelope: dict[str, Any]
    id: str | None = None

    @property
    def data(self) -> dict[str, Any]:
        data = self.envelope.get("data")
        return data if isinstance(data, dict) else {}

    @property
    def subject(self) -> str | None:
        return _str(self.envelope.get("subject"))

    @property
    def device_id(self) -> str | None:
        """The device the event is about, read from ``subject``."""
        subject = self.subject
        if subject and subject.startswith("device:"):
            return subject.removeprefix("device:")
        device_id = self.data.get("device_id")
        return device_id if isinstance(device_id, str) else None

    @property
    def time(self) -> datetime | None:
        raw = _str(self.envelope.get("time"))
        if raw is None:
            return None
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None


@dataclass(frozen=True, slots=True)
class StreamConnected:
    """Yielded each time the event stream (re)connects.

    Telemetry is never replayed, so a reader that cares about current values
    refetches them after every reconnect. ``reconnect`` is false on the first
    connection of a stream.
    """

    reconnect: bool


def devices_by_id(devices: Iterable[Device]) -> dict[str, Device]:
    """Index devices by id."""
    return {d.id: d for d in devices}
