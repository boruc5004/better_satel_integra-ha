"""HA-free tests for scheduler lifecycle wiring."""
from __future__ import annotations

from pathlib import Path

import pytest

from custom_components.satel_integra_plus.lifecycle import (
    async_cleanup_failed_setup,
    async_stop_runtime,
    make_availability_listener,
    make_stop_listener,
)
from custom_components.satel_integra_plus.pysatel.client import NotConnectedError
from custom_components.satel_integra_plus.pysatel.monitor import SatelHub

ROOT = Path(__file__).parent.parent
INIT_SRC = (
    ROOT / "custom_components" / "satel_integra_plus" / "__init__.py"
).read_text(encoding="utf-8")


class FakeScheduler:
    def __init__(self, events: list[str] | None = None) -> None:
        self.events = events if events is not None else []
        self.cancel_errors: list[Exception] = []
        self.close_error: BaseException | None = None

    def cancel_all(self, exc: Exception) -> None:
        self.cancel_errors.append(exc)

    async def async_close(self) -> None:
        self.events.append("scheduler.close")
        if self.close_error is not None:
            raise self.close_error


class FakeHub:
    def __init__(self, events: list[str] | None = None) -> None:
        self.events = events if events is not None else []
        self.available = True
        self.stop_error: BaseException | None = None

    async def stop(self) -> None:
        self.events.append("hub.stop")
        if self.stop_error is not None:
            raise self.stop_error


def test_actual_hub_availability_callback_and_unsubscribe() -> None:
    hub = SatelHub("127.0.0.1", 7094)
    hub.available = True
    scheduler = FakeScheduler()
    unsubscribe = hub.subscribe(make_availability_listener(hub, scheduler))

    hub._set_available(False)
    assert len(scheduler.cancel_errors) == 1
    assert isinstance(scheduler.cancel_errors[0], NotConnectedError)
    assert str(scheduler.cancel_errors[0]) == "panel unavailable"

    hub._set_available(True)
    assert len(scheduler.cancel_errors) == 1
    unsubscribe()
    hub._set_available(False)
    assert len(scheduler.cancel_errors) == 1


async def test_stop_runtime_always_stops_hub_before_scheduler() -> None:
    events: list[str] = []
    await async_stop_runtime(FakeHub(events), FakeScheduler(events))
    assert events == ["hub.stop", "scheduler.close"]


async def test_stop_runtime_closes_scheduler_when_hub_stop_fails() -> None:
    events: list[str] = []
    hub = FakeHub(events)
    hub.stop_error = RuntimeError("stop failed")
    with pytest.raises(RuntimeError, match="stop failed"):
        await async_stop_runtime(hub, FakeScheduler(events))
    assert events == ["hub.stop", "scheduler.close"]


async def test_stop_listener_uses_shared_stop_then_close_cleanup() -> None:
    events: list[str] = []
    listener = make_stop_listener(FakeHub(events), FakeScheduler(events))
    await listener(object())
    assert events == ["hub.stop", "scheduler.close"]


async def test_partial_setup_cleanup_attempts_every_step_in_order() -> None:
    events: list[str] = []
    hub = FakeHub(events)
    hub.stop_error = RuntimeError("stop cleanup failed")
    scheduler = FakeScheduler(events)
    scheduler.close_error = RuntimeError("close cleanup failed")

    def unsubscribe() -> None:
        events.append("unsubscribe")
        raise RuntimeError("unsubscribe cleanup failed")

    # Cleanup errors are logged/retrieved but must not mask the original setup
    # BaseException re-raised by async_setup_entry().
    await async_cleanup_failed_setup(hub, scheduler, unsubscribe)
    assert events == ["unsubscribe", "hub.stop", "scheduler.close"]


def test_setup_source_owns_one_entry_scoped_scheduler() -> None:
    assert "roller_scheduler: RollerStartScheduler" in INIT_SRC
    assert INIT_SRC.count("RollerStartScheduler(") == 1
    assert (
        "entry.options.get(\n"
        "            CONF_ROLLER_START_DELAY, DEFAULT_ROLLER_START_DELAY"
        in INIT_SRC
    )
    assert "await hub.client.control_outputs(Cmd.OUTPUTS_ON, {output})" in INIT_SRC
    runtime_at = INIT_SRC.index("entry.runtime_data = SatelRuntime(")
    forward_at = INIT_SRC.index("async_forward_entry_setups")
    assert runtime_at < forward_at


def test_setup_source_wires_disconnect_and_partial_failure_cleanup() -> None:
    assert "hub.subscribe(availability_listener)" in INIT_SRC
    assert "if not hub.available:" in INIT_SRC
    assert "availability_listener()" in INIT_SRC
    assert "except BaseException:" in INIT_SRC
    assert "await async_cleanup_failed_setup(" in INIT_SRC
    assert "entry.runtime_data = None" in INIT_SRC
    assert "make_stop_listener(hub, roller_scheduler)" in INIT_SRC
    assert "entry.async_on_unload(hub_unsubscribe)" in INIT_SRC


def test_unload_source_always_stops_hub_and_scheduler() -> None:
    start = INIT_SRC.index("async def async_unload_entry")
    end = INIT_SRC.index("async def _async_options_updated", start)
    unload = INIT_SRC[start:end]
    assert "try:" in unload and "finally:" in unload
    assert "await async_stop_runtime(runtime.hub, runtime.roller_scheduler)" in unload
