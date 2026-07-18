"""Hub tests: discovery, state propagation, temperature rotation, reconnect."""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "custom_components" / "satel_integra_plus"))
sys.path.insert(0, str(Path(__file__).parent))

import pysatel.monitor as monitor_mod
from fake_panel import FakePanel
from pysatel.const import Cmd
from pysatel.monitor import Discovery, SatelHub


@pytest.fixture
async def panel():
    p = FakePanel()
    await p.start()
    # a small fictional installation
    p.names[(0, 1)] = (0, "Dom")
    p.names[(5, 12)] = (3, "Ruch salon")        # motion zone, reaction 3
    p.names[(5, 45)] = (56, "Kuchnia")          # temperature zone, reaction 56
    p.names[(4, 7)] = (105, "Rol Taras")        # cover up
    p.names[(4, 8)] = (106, "Rol Taras")        # cover down
    p.names[(4, 15)] = (25, "Ośw ganek")        # light
    p.names[(4, 60)] = (120, "Kuchnia")         # thermostat
    p.temperatures[45] = 21.5
    yield p
    await p.stop()


def make_hub(panel, **kwargs):
    return SatelHub("127.0.0.1", panel.port, user_code="12345678", **kwargs)


async def wait_for(predicate, timeout=5.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while not predicate():
        if asyncio.get_event_loop().time() > deadline:
            raise TimeoutError("condition not met in time")
        await asyncio.sleep(0.02)


async def test_discovery_and_states(panel, monkeypatch):
    monkeypatch.setattr(monitor_mod, "TEMP_READ_SPACING", 0.05)
    hub = make_hub(panel)
    await hub.start()
    try:
        await wait_for(lambda: hub.available)
        assert hub.discovery.partitions[1].name == "Dom"
        assert hub.discovery.zones[12].reaction == 3
        assert hub.discovery.zones[45].has_temperature
        assert hub.discovery.outputs[7].function == 105
        assert hub.discovery.outputs[60].function == 120
        # default-named devices must be skipped
        assert 2 not in hub.discovery.partitions
        assert 13 not in hub.discovery.zones
        assert 8 in hub.discovery.outputs  # named, real
        assert 9 not in hub.discovery.outputs
        # temperature must arrive via rotation
        await wait_for(lambda: hub.temperatures.get(45) == 21.5)
    finally:
        await hub.stop()


async def test_state_change_pushes(panel, monkeypatch):
    monkeypatch.setattr(monitor_mod, "POLL_INTERVAL", 0.05)
    hub = make_hub(panel, temperature_zones=[])
    events = []
    hub.subscribe(lambda: events.append(hub.states[Cmd.ZONES_VIOLATION].copy()))
    await hub.start()
    try:
        await wait_for(lambda: hub.available)
        panel.violated_zones = {12}
        panel.new_data_bits.add(Cmd.ZONES_VIOLATION)
        await wait_for(lambda: hub.zone_active(Cmd.ZONES_VIOLATION, 12))
        assert {12} in events
    finally:
        await hub.stop()


async def test_output_state_after_control(panel, monkeypatch):
    monkeypatch.setattr(monitor_mod, "POLL_INTERVAL", 0.05)
    hub = make_hub(panel, temperature_zones=[])
    await hub.start()
    try:
        await wait_for(lambda: hub.available)
        await hub.client.control_outputs(Cmd.OUTPUTS_ON, {15})
        await wait_for(lambda: hub.output_active(15))
    finally:
        await hub.stop()


async def test_reconnects_after_drop(panel, monkeypatch):
    monkeypatch.setattr(monitor_mod, "POLL_INTERVAL", 0.05)
    monkeypatch.setattr(monitor_mod, "RECONNECT_MIN", 0.1)
    hub = make_hub(panel, temperature_zones=[])
    await hub.start()
    try:
        await wait_for(lambda: hub.available)
        panel.drop_after = 0  # drop on next received frame
        await wait_for(lambda: not hub.available)
        panel.drop_after = None
        await wait_for(lambda: hub.available, timeout=10.0)
    finally:
        await hub.stop()


async def test_temp_unsupported_zone_removed(panel, monkeypatch):
    monkeypatch.setattr(monitor_mod, "TEMP_READ_SPACING", 0.05)
    monkeypatch.setattr(monitor_mod, "POLL_INTERVAL", 0.05)
    import pysatel.client as client_mod

    monkeypatch.setattr(client_mod, "TEMPERATURE_TIMEOUT", 0.3)
    hub = make_hub(panel, temperature_zones=[12, 45])  # 12 has no sensor
    await hub.start()
    try:
        await wait_for(lambda: hub.available)
        await wait_for(lambda: 12 in hub._temp_unsupported, timeout=10.0)
        await wait_for(lambda: hub.temperatures.get(45) == 21.5, timeout=10.0)
    finally:
        await hub.stop()


def test_discovery_roundtrip():
    d = Discovery.from_dict({
        "partitions": {"1": {"name": "Dom"}},
        "zones": {"12": {"name": "Ruch salon", "reaction": 3, "partition": 1}},
        "outputs": {"7": {"name": "Rol Taras", "function": 105}},
    })
    assert Discovery.from_dict(d.as_dict()).as_dict() == d.as_dict()
