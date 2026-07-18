"""Client behavior tests against the fake panel."""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "custom_components" / "satel_integra_plus"))
sys.path.insert(0, str(Path(__file__).parent))

from fake_panel import FakePanel
from pysatel.client import PanelBusyError, ResponseTimeoutError, SatelClient
from pysatel.const import Cmd, encode_user_code

CODE = "12345678"


@pytest.fixture
async def panel():
    p = FakePanel()
    await p.start()
    yield p
    await p.stop()


@pytest.fixture
async def client(panel):
    c = SatelClient("127.0.0.1", panel.port, user_code=CODE)
    await c.connect()
    yield c
    await c.close()


async def test_identify(client):
    assert client.panel.model == "INTEGRA 256 Plus"
    assert client.panel.supports_32_bytes
    assert client.panel.object_mask_len == 32


async def test_busy_banner(panel):
    panel.busy = True
    c = SatelClient("127.0.0.1", panel.port)
    with pytest.raises(PanelBusyError):
        await c.connect()
    assert not c.connected


async def test_state_read_extended(client, panel):
    panel.violated_zones = {3, 14, 200}
    data = await client.state_command(Cmd.ZONES_VIOLATION)
    assert len(data) == 32
    from pysatel.frames import bitmask_to_numbers

    assert bitmask_to_numbers(data) == {3, 14, 200}


async def test_output_control_batches_into_one_frame(client, panel):
    """Concurrent ON requests (an HA cover group) must produce ONE 0x88 frame."""
    await asyncio.gather(
        client.control_outputs(Cmd.OUTPUTS_ON, {31}),
        client.control_outputs(Cmd.OUTPUTS_ON, {33}),
        client.control_outputs(Cmd.OUTPUTS_ON, {49}),
    )
    on_frames = [d for c, d in panel.received if c == Cmd.OUTPUTS_ON]
    assert len(on_frames) == 1
    assert panel.active_outputs == {31, 33, 49}
    # user code must be in the first 8 bytes
    assert on_frames[0][:8] == encode_user_code(CODE)
    assert len(on_frames[0]) == 8 + 32


async def test_output_on_off_kept_separate(client, panel):
    """ON and OFF must never merge (cover stop while another opens)."""
    await asyncio.gather(
        client.control_outputs(Cmd.OUTPUTS_ON, {10}),
        client.control_outputs(Cmd.OUTPUTS_OFF, {20}),
        client.control_outputs(Cmd.OUTPUTS_ON, {11}),
    )
    cmds = [c for c, _ in panel.received if c in (Cmd.OUTPUTS_ON, Cmd.OUTPUTS_OFF)]
    assert cmds.count(Cmd.OUTPUTS_OFF) == 1
    assert panel.active_outputs == {10, 11}


async def test_temperature_read_and_correlation(client, panel, monkeypatch):
    panel.temperatures[45] = 23.5
    assert await client.read_temperature(45) == 23.5
    # zone without temperature capability: the panel stays silent
    import pysatel.client as mod

    monkeypatch.setattr(mod, "TEMPERATURE_TIMEOUT", 0.3)
    with pytest.raises(ResponseTimeoutError):
        await client.read_temperature(26)


async def test_temperature_timeout_fast(client, panel, monkeypatch):
    """Non-temp zone: timeout raised, connection stays usable."""
    panel.temperatures[77] = -1.5
    with pytest.raises(ResponseTimeoutError):
        await client.command(Cmd.ZONE_TEMPERATURE, bytes([26]), timeout=0.3,
                             match=lambda d: d[0] == 26)
    # link still healthy afterwards
    assert await client.read_temperature(77) == -1.5


async def test_device_info(client, panel):
    panel.names[(4, 7)] = (105, "Rol Taras")
    func, name = await client.read_device_info(4, 7)
    assert (func, name) == (105, "Rol Taras")


async def test_device_info_polish_encoding(client, panel):
    panel.names[(4, 61)] = (105, "ROL Piętro")
    func, name = await client.read_device_info(4, 61)
    assert name == "ROL Piętro"


async def test_arm_disarm(client, panel):
    await client.arm(1)
    assert panel.armed_partitions == {1}
    await client.disarm(1)
    assert panel.armed_partitions == set()


async def test_commands_strictly_serialized(client, panel):
    """Even under concurrency the panel must never see overlapping commands."""
    panel.answer_delay = 0.02
    panel.temperatures[77] = 20.0
    await asyncio.gather(
        client.state_command(Cmd.ZONES_VIOLATION),
        client.read_temperature(77),
        client.state_command(Cmd.OUTPUTS_STATE),
        client.control_outputs(Cmd.OUTPUTS_ON, {5}),
    )
    # fake panel handles frames sequentially per connection; if we got here
    # without timeouts every response was matched to its command
    assert {c for c, _ in panel.received} >= {
        int(Cmd.ZONES_VIOLATION), int(Cmd.ZONE_TEMPERATURE),
        int(Cmd.OUTPUTS_STATE), int(Cmd.OUTPUTS_ON),
    }


async def test_connection_lost_callback(client, panel):
    lost = asyncio.Event()
    client.on_connection_lost = lambda exc: lost.set()
    panel.drop_after = len(panel.received)  # drop on next frame
    with pytest.raises(Exception):
        await client.command(Cmd.RTC_AND_STATUS, timeout=1.0)
    await asyncio.wait_for(lost.wait(), 2.0)
    assert not client.connected


async def test_arm_force_fallback(client, panel):
    """Refusal 0x11 (violated zones) retries as force-arm when enabled."""
    from pysatel.client import CommandRefusedError

    panel.refuse_arm_without_force = True
    # without fallback: refusal surfaces
    with pytest.raises(CommandRefusedError) as exc:
        await client.arm(1)
    assert exc.value.code == 0x11
    assert panel.armed_partitions == set()
    # with fallback: force-arm succeeds transparently
    await client.arm(1, fallback_force=True)
    assert panel.armed_partitions == {1}


async def test_read_self_info(client):
    info = await client.read_self_info()
    assert info["user_number"] == 3
    assert info["partitions"] == [1, 2]
    assert info["name"] == "HomeAssistant"
