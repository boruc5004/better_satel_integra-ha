"""Turn discovered panel devices into Home Assistant entity descriptors.

Everything here is pure: `build_entity_map(discovery, options)` -> descriptors.
The rules are "satel-first": the panel's own configuration (output functions,
zone reaction types, names) decides what an object is.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from fnmatch import fnmatch

from .pysatel.const import (
    OUTPUT_FUNC_BI,
    OUTPUT_FUNC_MONO,
    OUTPUT_FUNC_ROLLER_DOWN,
    OUTPUT_FUNC_ROLLER_UP,
    OUTPUT_FUNC_THERMOSTAT,
)
from .pysatel.monitor import Discovery

# zone reaction types (DLOADX numbering) -> semantic groups
REACTION_FIRE_SMOKE = {32, 33, 34, 35}          # 24h fire variants
REACTION_WATER = {52}                            # 24h auxiliary - water leak
REACTION_LOW_TEMP = {56}
REACTION_PANIC = {12, 13}                        # panic loud/silent
REACTION_TAMPER = {9, 11}                        # 24h tamper


@dataclass
class CoverDesc:
    name: str
    up_output: int
    down_output: int
    is_group: bool = False


@dataclass
class GateDesc:
    name: str
    output: int
    state_zone: int | None
    garage: bool = False


@dataclass
class LightDesc:
    name: str
    output: int


@dataclass
class SwitchDesc:
    name: str
    output: int
    momentary: bool = False


@dataclass
class ClimateDesc:
    name: str
    output: int
    temp_zone: int | None


@dataclass
class BinarySensorDesc:
    name: str
    zone: int
    device_class: str | None  # HA BinarySensorDeviceClass value
    partition: int | None


@dataclass
class TempSensorDesc:
    name: str
    zone: int


@dataclass
class EntityMap:
    covers: list[CoverDesc] = field(default_factory=list)
    gates: list[GateDesc] = field(default_factory=list)
    lights: list[LightDesc] = field(default_factory=list)
    switches: list[SwitchDesc] = field(default_factory=list)
    climates: list[ClimateDesc] = field(default_factory=list)
    binary_sensors: list[BinarySensorDesc] = field(default_factory=list)
    temp_sensors: list[TempSensorDesc] = field(default_factory=list)


def _norm(name: str) -> str:
    """Normalize a panel name for matching: lowercase, strip accents/punct."""
    name = unicodedata.normalize("NFKD", name)
    name = "".join(c for c in name if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "", name.lower())


_GATE_RE = re.compile(r"\bbrama\b|\bfurtka\b", re.IGNORECASE)
_LIGHT_RE = re.compile(r"^o[śs]w\b|^o[śs]wietlenie", re.IGNORECASE)
_GARAGE_RE = re.compile(r"gara[żz]|garage", re.IGNORECASE)
_MOTION_RE = re.compile(r"^ruch\b|^kurtyna\b|^czujka\b", re.IGNORECASE)
_GLASS_RE = re.compile(r"^szk[łl]o\b|zbicie", re.IGNORECASE)
_SMOKE_RE = re.compile(r"^dym\b", re.IGNORECASE)
_GAS_RE = re.compile(r"^metan\b|^gaz\b|^co\b", re.IGNORECASE)
_WATER_RE = re.compile(r"^woda\b|wyciek|zalanie", re.IGNORECASE)
_DOOR_RE = re.compile(r"^drzwi\b|^brama\b|^furtka\b|^okno\b", re.IGNORECASE)


def zone_device_class(name: str, reaction: int) -> str | None:
    """Infer the HA binary_sensor device class. Reaction type wins over name."""
    if reaction in REACTION_WATER:
        return "moisture"
    if reaction in REACTION_FIRE_SMOKE:
        # reaction is fire; the sensor kind comes from the name
        if _GAS_RE.search(name):
            return "gas"
        return "smoke"
    if reaction in REACTION_LOW_TEMP:
        return "cold"
    if reaction in REACTION_PANIC:
        return "safety"
    if reaction in REACTION_TAMPER:
        return "tamper"
    if _MOTION_RE.search(name):
        return "motion"
    if _GLASS_RE.search(name):
        return "sound"
    if _SMOKE_RE.search(name):
        return "smoke"
    if _GAS_RE.search(name):
        return "gas"
    if _WATER_RE.search(name):
        return "moisture"
    if _GARAGE_RE.search(name) and _GATE_RE.search(name):
        return "garage_door"
    if _DOOR_RE.search(name):
        return "door"
    return None


def build_entity_map(
    discovery: Discovery,
    *,
    gate_state_zones: dict[int, int] | None = None,
    climate_bindings: dict[int, int] | None = None,
    skip_zone_patterns: list[str] | None = None,
) -> EntityMap:
    gate_state_zones = gate_state_zones or {}
    climate_bindings = climate_bindings or {}
    skip_zone_patterns = skip_zone_patterns or []
    result = EntityMap()
    outputs = discovery.outputs
    zones = discovery.zones

    zone_by_norm_name: dict[str, int] = {}
    for z in zones.values():
        zone_by_norm_name.setdefault(_norm(z.name), z.number)

    consumed: set[int] = set()

    # --- covers: a 105-output whose successor is the matching 106-output
    for n in sorted(outputs):
        if n in consumed or outputs[n].function != OUTPUT_FUNC_ROLLER_UP:
            continue
        down = outputs.get(n + 1)
        if down and down.function == OUTPUT_FUNC_ROLLER_DOWN:
            # installer convention: whole-floor group roller outputs are often
            # named with an uppercase "ROL " prefix ("ROL Parter" etc.)
            is_group = bool(re.match(r"^ROL\s", outputs[n].name))
            result.covers.append(
                CoverDesc(
                    name=outputs[n].name,
                    up_output=n,
                    down_output=n + 1,
                    is_group=is_group,
                )
            )
            consumed |= {n, n + 1}

    # --- gates: MONO outputs that look like gates; state zone via options,
    #     then exact name match against zones
    for n, out in sorted(outputs.items()):
        if n in consumed or out.function != OUTPUT_FUNC_MONO:
            continue
        if not _GATE_RE.search(out.name):
            continue
        state_zone = gate_state_zones.get(n)
        if state_zone is None:
            state_zone = zone_by_norm_name.get(_norm(out.name))
        result.gates.append(
            GateDesc(
                name=out.name,
                output=n,
                state_zone=state_zone,
                garage=bool(_GARAGE_RE.search(out.name)),
            )
        )
        consumed.add(n)

    # --- climate: thermostat outputs; bound temperature zone via options,
    #     then exact name match against low-temperature zones
    temp_zone_numbers = {
        z.number for z in zones.values() if z.reaction in REACTION_LOW_TEMP
    }
    for n, out in sorted(outputs.items()):
        if n in consumed or out.function != OUTPUT_FUNC_THERMOSTAT:
            continue
        bound = climate_bindings.get(n)
        if bound is None:
            candidate = zone_by_norm_name.get(_norm(out.name))
            if candidate in temp_zone_numbers:
                bound = candidate
        result.climates.append(ClimateDesc(name=out.name, output=n, temp_zone=bound))
        consumed.add(n)

    # --- lights & switches
    for n, out in sorted(outputs.items()):
        if n in consumed:
            continue
        if out.function == OUTPUT_FUNC_BI:
            if _LIGHT_RE.search(out.name):
                result.lights.append(LightDesc(name=out.name, output=n))
            else:
                result.switches.append(SwitchDesc(name=out.name, output=n))
            consumed.add(n)
        elif out.function == OUTPUT_FUNC_MONO:
            result.switches.append(SwitchDesc(name=out.name, output=n, momentary=True))
            consumed.add(n)
        # other output functions (alarm signalling, status indicators…) are
        # panel internals — not exposed as entities

    # --- zones -> binary sensors + temperature sensors
    for n, zone in sorted(zones.items()):
        if any(fnmatch(zone.name, pat) for pat in skip_zone_patterns):
            continue
        if zone.reaction in REACTION_LOW_TEMP:
            result.temp_sensors.append(TempSensorDesc(name=zone.name, zone=n))
        result.binary_sensors.append(
            BinarySensorDesc(
                name=zone.name,
                zone=n,
                device_class=zone_device_class(zone.name, zone.reaction),
                partition=zone.partition,
            )
        )
    return result
