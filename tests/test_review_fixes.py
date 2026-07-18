"""Regression tests for the code-review findings."""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "custom_components" / "satel_integra_plus"))
sys.path.insert(0, str(Path(__file__).parent))

import pysatel.monitor as monitor_mod
from fake_panel import FakePanel
from pysatel.client import NotConnectedError, SatelClient, SatelError
from pysatel.const import Cmd
from pysatel.monitor import SatelHub


@pytest.fixture
async def panel():
    p = FakePanel()
    await p.start()
    yield p
    await p.stop()


async def test_close_during_batch_does_not_hang(panel):
    """Finding 1: futures of an in-flight batch must resolve on close()."""
    client = SatelClient("127.0.0.1", panel.port, user_code="12345678")
    await client.connect()
    panel.answer_delay = 5.0  # the 0x88 answer will never arrive in time
    task = asyncio.ensure_future(
        client.control_outputs(Cmd.OUTPUTS_ON, {5})
    )
    await asyncio.sleep(0.2)  # batch flushed, command in flight
    await client.close()
    with pytest.raises((SatelError, OSError)):
        await asyncio.wait_for(task, 1.0)  # must NOT time out


async def test_late_result_frame_cannot_satisfy_state_read(panel):
    """Finding 3: 0xEF only completes control commands."""
    client = SatelClient("127.0.0.1", panel.port, user_code="12345678")
    await client.connect()
    loop = asyncio.get_event_loop()
    # simulate a pending state read and a stray late 0xEF
    client._response = loop.create_future()
    client._response_cmd = int(Cmd.OUTPUTS_STATE)
    client._response_match = None
    client._dispatch(int(Cmd.RESULT), b"\xff")
    assert not client._response.done()  # rejected
    client._dispatch(int(Cmd.OUTPUTS_STATE), bytes(32))
    assert client._response.done()  # echo accepted
    # …but a control command still accepts 0xEF
    client._response = loop.create_future()
    client._response_cmd = int(Cmd.OUTPUTS_ON)
    client._dispatch(int(Cmd.RESULT), b"\x00")
    assert client._response.done()
    client._response = None
    await client.close()


async def test_discovery_aborts_on_disconnect_and_retries(panel, monkeypatch):
    """Finding 2: partial discovery must never be committed."""
    monkeypatch.setattr(monitor_mod, "RECONNECT_MIN", 0.1)
    monkeypatch.setattr(monitor_mod, "POLL_INTERVAL", 0.05)
    panel.names[(0, 1)] = (0, "Dom")
    panel.names[(4, 7)] = (105, "Rol Taras")
    panel.names[(4, 8)] = (106, "Rol Taras")
    panel.drop_after = 40  # dies mid zone enumeration on first attempt
    hub = SatelHub("127.0.0.1", panel.port, user_code="12345678",
                   temperature_zones=[])
    await hub.start()
    try:
        deadline = asyncio.get_event_loop().time() + 15.0
        while not hub.available:
            assert asyncio.get_event_loop().time() < deadline
            # after the first drop, let the retry run against a healthy panel
            if panel.clients == 0:
                panel.drop_after = None
                panel._frames_seen = 0
            await asyncio.sleep(0.05)
        # discovery completed only on the healthy attempt -> complete data
        assert 7 in hub.discovery.outputs and 8 in hub.discovery.outputs
        assert hub.discovery.partitions[1].name == "Dom"
    finally:
        await hub.stop()


async def test_supervisor_survives_unexpected_exception(panel, monkeypatch):
    """Finding 6: a non-SatelError must not kill the supervisor."""
    monkeypatch.setattr(monitor_mod, "RECONNECT_MIN", 0.05)
    hub = SatelHub("127.0.0.1", panel.port, user_code="12345678",
                   temperature_zones=[])
    calls = {"n": 0}
    orig = hub._refresh_all

    async def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        await orig()

    hub._refresh_all = flaky
    await hub.start()
    try:
        deadline = asyncio.get_event_loop().time() + 10.0
        while not hub.available:
            assert asyncio.get_event_loop().time() < deadline
            await asyncio.sleep(0.05)
        assert calls["n"] >= 2  # recovered after the unexpected error
    finally:
        await hub.stop()


async def test_error_result_completes_read_immediately(panel):
    """Field bug: 0xEE for a nonexistent device answers 0xEF 0x08 — that must
    resolve the read instantly (as a refusal), not burn the 5 s timeout."""
    from pysatel.client import CommandRefusedError

    panel.error_devices.add((5, 180))
    client = SatelClient("127.0.0.1", panel.port, user_code="12345678")
    await client.connect()
    loop = asyncio.get_event_loop()
    t0 = loop.time()
    with pytest.raises(CommandRefusedError):
        await client.read_device_info(5, 180)
    assert loop.time() - t0 < 1.0  # no timeout burned
    await client.close()


async def test_discovery_skips_refused_devices_fast(panel, monkeypatch):
    """Discovery over a panel that refuses many device reads must still finish
    quickly (each refusal resolves immediately)."""
    monkeypatch.setattr(monitor_mod, "POLL_INTERVAL", 0.05)
    panel.names[(0, 1)] = (0, "Dom")
    panel.names[(4, 7)] = (105, "Rol Taras")
    panel.names[(4, 8)] = (106, "Rol Taras")
    for z in range(100, 200):
        panel.error_devices.add((5, z))
    hub = SatelHub("127.0.0.1", panel.port, user_code="12345678",
                   temperature_zones=[])
    loop = asyncio.get_event_loop()
    t0 = loop.time()
    await hub.start()
    try:
        while not hub.available:
            assert loop.time() - t0 < 30.0, "discovery too slow"
            await asyncio.sleep(0.05)
        assert 7 in hub.discovery.outputs
        assert not any(100 <= z < 200 for z in hub.discovery.zones)
    finally:
        await hub.stop()
