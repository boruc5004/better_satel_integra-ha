"""Regression tests: stopping a roller midway must not fake an endpoint.

Covers the 2026-07-18 field issue: an explicit cover.stop_cover marked the
cover fully open/closed, and without assumed_state the HA frontend then
disabled one direction. An interrupted run records "open" (partially open),
never "closed".
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from custom_components.satel_integra_plus.roller import (
    CLOSED,
    OPEN,
    RollerStateTracker,
)

COVER_SRC = (
    Path(__file__).parent.parent
    / "custom_components"
    / "satel_integra_plus"
    / "cover.py"
).read_text()


def edge(tracker, was_up=False, was_down=False, up=False, down=False):
    tracker.update(up, down, was_up_active=was_up, was_down_active=was_down)


def test_natural_completion_records_endpoint():
    t = RollerStateTracker()
    edge(t, was_up=True)            # up ran to the end of its panel time
    assert t.last_direction == OPEN
    edge(t, was_down=True)
    assert t.last_direction == CLOSED


def test_explicit_stop_records_partially_open():
    t = RollerStateTracker()
    edge(t, was_down=True)          # fully closed
    t.note_stop_requested(moving=True)   # user hits Stop mid-travel up...
    edge(t, was_up=True)                 # ...and the up output falls
    assert t.last_direction == OPEN      # partially open = open, never "closed"
    t.note_stop_requested(moving=True)   # and stopping mid-close...
    edge(t, was_down=True)
    assert t.last_direction == OPEN      # ...must NOT record "closed"


def test_stop_while_stationary_does_not_poison_next_movement():
    t = RollerStateTracker()
    t.note_stop_requested(moving=False)  # Stop pressed while nothing moves
    edge(t, was_up=True)                 # later, a full uninterrupted run
    assert t.last_direction == OPEN


def test_failed_stop_command_keeps_endpoint_inference():
    t = RollerStateTracker()
    t.note_stop_requested(moving=True)
    t.clear_stop_request()               # the Satel command raised/cancelled
    edge(t, was_up=True)                 # movement continued to the end
    assert t.last_direction == OPEN


def test_new_movement_supersedes_pending_stop():
    t = RollerStateTracker()
    t.note_stop_requested(moving=True)   # Stop...
    t.clear_stop_request()               # ...then Open before any edge seen
    edge(t, was_up=True)                 # that run completes naturally
    assert t.last_direction == OPEN


def test_stop_intent_consumed_once():
    t = RollerStateTracker()
    t.note_stop_requested(moving=True)
    edge(t, was_up=True)                 # interrupted run -> partially open
    assert t.last_direction == OPEN
    edge(t, was_down=True)               # next run completes naturally
    assert t.last_direction == CLOSED


def test_canceled_queued_start_must_preserve_explicit_stop_intent():
    t = RollerStateTracker()
    t.note_stop_requested(moving=True)
    sent = False
    if sent:
        t.clear_stop_request()
    edge(t, was_down=True)
    assert t.last_direction == OPEN


def test_direction_swap_without_pause_records_final_leg():
    t = RollerStateTracker()
    # up running, user commands close: panel interlock swaps outputs
    edge(t, was_up=True, down=True)      # not a falling edge: still moving
    assert t.last_direction is None
    edge(t, was_down=True)               # the down leg completes
    assert t.last_direction == CLOSED


def test_no_movement_no_learning():
    t = RollerStateTracker()
    edge(t)                              # idle notify, nothing was running
    assert t.last_direction is None
    edge(t, was_down=True)
    assert t.last_direction == CLOSED
    edge(t)                              # idle notify must not erase memory
    assert t.last_direction == CLOSED


def _class_body(name: str) -> str:
    match = re.search(
        rf"class {name}\b.*?(?=\nclass |\Z)", COVER_SRC, re.DOTALL
    )
    assert match, f"class {name} not found in cover.py"
    return match.group(0)


def test_roller_cover_is_assumed_state_but_gate_is_not():
    assert "_attr_assumed_state = True" in _class_body("SatelRollerCover")
    # gates have real reed-contact feedback — they must NOT be assumed-state
    assert "_attr_assumed_state" not in _class_body("SatelGateCover")


def test_stop_handler_flags_only_when_moving_and_unflags_on_failure():
    body = _class_body("SatelRollerCover")
    stop = body[body.index("async def async_stop_cover"):]
    assert "note_stop_requested(self.is_opening or self.is_closing)" in stop
    assert "except BaseException" in stop and "clear_stop_request" in stop


def test_movement_handlers_clear_stop_intent_only_after_sent_start():
    body = _class_body("SatelRollerCover")
    start = body.index("async def _async_start")
    end = body.index("async def async_open_cover", start)
    helper = body[start:end]
    assert "if sent:" in helper
    assert "clear_stop_request()" in helper
    for handler in ("async_open_cover", "async_close_cover"):
        start = body.index(f"async def {handler}")
        end = body.index("async def", start + 10)
        assert "_async_start" in body[start:end], handler
        assert "clear_stop_request()" not in body[start:end], handler


def test_individual_roller_start_routing_uses_stable_up_key():
    setup = COVER_SRC[COVER_SRC.index("async def async_setup_entry"):]
    setup = setup[:setup.index("class SatelRollerCover")]
    assert (
        "SatelRollerCover(\n"
        "            runtime.hub, runtime.roller_scheduler, entry.entry_id, desc"
        in setup
    )

    body = _class_body("SatelRollerCover")
    assert "self._scheduler = roller_scheduler" in body
    assert "self._scheduler.async_start(self._up, output)" in body
    assert "await self._async_start(self._up)" in body
    assert "await self._async_start(self._down)" in body


def test_native_group_bypasses_scheduler_and_clear_depends_on_sent():
    body = _class_body("SatelRollerCover")
    helper_start = body.index("async def _async_start")
    helper_end = body.index("async def async_open_cover", helper_start)
    helper = body[helper_start:helper_end]
    assert "if self._is_group:" in helper
    direct = helper.index("self._hub.client.control_outputs")
    scheduled = helper.index("self._scheduler.async_start")
    assert direct < scheduled
    assert "if sent:\n            self._tracker.clear_stop_request()" in helper


def test_stop_cancels_pending_individual_before_direct_off():
    body = _class_body("SatelRollerCover")
    start = body.index("async def async_stop_cover")
    stop = body[start:]
    cancel_at = stop.index("self._scheduler.cancel(self._up)")
    off_at = stop.index("Cmd.OUTPUTS_OFF")
    assert "if not self._is_group:" in stop[:cancel_at]
    assert cancel_at < off_at


def test_gate_cover_remains_scheduler_free():
    assert "scheduler" not in _class_body("SatelGateCover").lower()
