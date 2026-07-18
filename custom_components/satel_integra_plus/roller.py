"""Direction memory for roller covers (pure logic, no HA imports).

A roller pair gives no position feedback — only the up/down output states.
The tracker infers the endpoint from an *uninterrupted* falling edge (the
panel let the output run to the end of its configured time), and records
"unknown" when the falling edge was caused by an explicit stop command,
because the cover is then somewhere midway.
"""
from __future__ import annotations

OPEN = "open"
CLOSED = "closed"


class RollerStateTracker:
    """Tracks the last completed travel direction of one roller pair."""

    def __init__(self) -> None:
        self.last_direction: str | None = None  # OPEN / CLOSED / None=unknown
        self._stop_requested = False

    def note_stop_requested(self, moving: bool) -> None:
        """An explicit stop command was issued.

        Only counts when the cover was actually moving — stopping a
        stationary cover must not poison the next movement's endpoint.
        """
        if moving:
            self._stop_requested = True

    def clear_stop_request(self) -> None:
        """Forget a pending stop intent.

        Called when the stop command failed/was cancelled (the movement
        presumably continues) or when a new movement command supersedes it.
        """
        self._stop_requested = False

    def update(
        self,
        up_active: bool,
        down_active: bool,
        was_up_active: bool,
        was_down_active: bool,
    ) -> None:
        """Process an output-state transition.

        On a falling edge (movement -> both outputs off): an interrupted run
        leaves the position unknown; an uninterrupted one ended at the
        endpoint of its direction.
        """
        if up_active or down_active:
            return  # still (or again) moving; edges are handled when it ends
        if not (was_up_active or was_down_active):
            return  # no movement ended; nothing to learn
        if self._stop_requested:
            self.last_direction = None
        elif was_up_active:
            self.last_direction = OPEN
        elif was_down_active:
            self.last_direction = CLOSED
        self._stop_requested = False
