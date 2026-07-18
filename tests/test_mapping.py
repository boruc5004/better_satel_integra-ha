"""Mapping tests driven by a fictional but realistic installation fixture."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from custom_components.satel_integra_plus.const import DEFAULT_SKIP_ZONE_PATTERNS
from custom_components.satel_integra_plus.mapping import build_entity_map
from custom_components.satel_integra_plus.pysatel.monitor import Discovery

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "example_installation.json").read_text()
)


def example_discovery() -> Discovery:
    """Build a Discovery as the panel itself would report it."""
    return Discovery.from_dict(
        {
            "partitions": FIXTURE["partitions"],
            "zones": FIXTURE["zones"],
            "outputs": FIXTURE["outputs"],
        }
    )


def build(**kwargs):
    kwargs.setdefault("skip_zone_patterns", DEFAULT_SKIP_ZONE_PATTERNS)
    return build_entity_map(example_discovery(), **kwargs)


def test_covers_paired():
    m = build()
    regular = [c for c in m.covers if not c.is_group]
    groups = [c for c in m.covers if c.is_group]
    assert {(c.up_output, c.down_output) for c in regular} == {(1, 2), (3, 4), (5, 6)}
    assert {(g.up_output, g.down_output) for g in groups} == {(10, 11)}
    assert all(c.down_output == c.up_output + 1 for c in m.covers)
    by_up = {c.up_output: c for c in m.covers}
    assert by_up[1].name == "Rol Salon 1"
    assert by_up[10].name == "ROL Parter" and by_up[10].is_group


def test_gates_and_state_zone_name_matching():
    m = build()
    gates = {g.output: g for g in m.gates}
    assert set(gates) == {20, 21}
    # bound to reed zones purely by name matching
    assert gates[20].state_zone == 9   # Brama wjazdowa
    assert gates[21].state_zone == 10  # Brama garażowa
    assert gates[21].garage and not gates[20].garage


def test_gate_state_zone_options_override():
    m = build(gate_state_zones={20: 11})
    gates = {g.output: g for g in m.gates}
    assert gates[20].state_zone == 11


def test_climates_bound_by_name():
    m = build()
    bindings = {c.output: c.temp_zone for c in m.climates}
    assert bindings == {40: 30, 41: 31, 42: None}  # Łazienka górna has no name twin


def test_climate_bindings_option_fills_gap():
    m = build(climate_bindings={42: 32})
    bindings = {c.output: c.temp_zone for c in m.climates}
    assert bindings[42] == 32


def test_lights_and_switches():
    m = build()
    assert {l.output for l in m.lights} == {30, 31, 32}
    switch_outputs = {s.output: s for s in m.switches}
    assert set(switch_outputs) == {22, 33, 50}
    assert switch_outputs[22].momentary       # MONO valve
    assert not switch_outputs[50].momentary   # BI comfort toggle
    # gate outputs and alarm outputs must not become switches
    assert not set(switch_outputs) & {20, 21, 60}


def test_binary_sensors_device_classes():
    m = build()
    by_zone = {b.zone: b for b in m.binary_sensors}
    assert by_zone[1].device_class == "motion"
    assert by_zone[4].device_class == "sound"        # glass break
    assert by_zone[5].device_class == "moisture"     # water leak reaction
    assert by_zone[6].device_class == "smoke"
    assert by_zone[7].device_class == "gas"          # fire reaction, gas name
    assert by_zone[9].device_class in ("door", "garage_door")
    assert by_zone[30].device_class == "cold"        # temperature zone
    assert by_zone[40].device_class == "safety"      # panic
    # wall-button inputs skipped by default patterns
    assert 20 not in by_zone and 21 not in by_zone and 22 not in by_zone


def test_temp_sensors():
    m = build()
    assert {t.zone for t in m.temp_sensors} == {30, 31, 32}


def test_partition_passthrough():
    d = example_discovery()
    assert d.partitions[1].name == "Dom"
    assert d.zones[8].partition == 3
