"""Behavior tests for entry-scoped roller start staggering."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(
    0,
    str(
        Path(__file__).parent.parent
        / "custom_components"
        / "satel_integra_plus"
    ),
)

from roller_scheduler import RollerSchedulerClosedError, RollerStartScheduler


class PanelUnavailableError(RuntimeError):
    """Test-only disconnect error supplied to cancel_all()."""


class SenderError(RuntimeError):
    """Test-only injected panel command failure."""


async def _gather_tasks(*tasks: asyncio.Task[bool]) -> list[bool]:
    return list(await asyncio.gather(*tasks))


async def test_positive_delay_dispatches_pending_rollers_by_key() -> None:
    sent: list[int] = []

    async def send_start(output: int) -> None:
        sent.append(output)

    scheduler = RollerStartScheduler(send_start, delay=0.005)
    try:
        tasks = [
            asyncio.create_task(scheduler.async_start(key, output))
            for key, output in ((30, 300), (10, 100), (20, 200))
        ]
        assert await _gather_tasks(*tasks) == [True, True, True]
        assert sent == [100, 200, 300]
    finally:
        await scheduler.async_close()


async def test_positive_delay_never_overlaps_sender_calls() -> None:
    active = 0
    max_active = 0

    async def send_start(_output: int) -> None:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1

    scheduler = RollerStartScheduler(send_start, delay=0.001)
    try:
        await asyncio.gather(
            scheduler.async_start(3, 30),
            scheduler.async_start(1, 10),
            scheduler.async_start(2, 20),
        )
        assert max_active == 1
    finally:
        await scheduler.async_close()


async def test_first_dispatch_has_no_configured_delay() -> None:
    entered = asyncio.Event()

    async def send_start(_output: int) -> None:
        entered.set()

    scheduler = RollerStartScheduler(send_start, delay=10.0)
    try:
        task = asyncio.create_task(scheduler.async_start(1, 10))
        await asyncio.wait_for(entered.wait(), timeout=0.25)
        assert await task is True
    finally:
        await scheduler.async_close()


async def test_dispatch_starts_respect_minimum_interval() -> None:
    starts: list[float] = []
    loop = asyncio.get_running_loop()

    async def send_start(_output: int) -> None:
        starts.append(loop.time())

    delay = 0.04
    scheduler = RollerStartScheduler(send_start, delay=delay)
    try:
        await asyncio.gather(
            scheduler.async_start(1, 10),
            scheduler.async_start(2, 20),
        )
        assert starts[1] - starts[0] >= delay - 0.005
    finally:
        await scheduler.async_close()


async def test_sender_latency_consumes_inter_start_delay() -> None:
    first_finished = asyncio.Event()
    second_entered = asyncio.Event()
    calls = 0

    async def send_start(_output: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            await asyncio.sleep(0.12)
            first_finished.set()
        else:
            second_entered.set()

    scheduler = RollerStartScheduler(send_start, delay=0.10)
    try:
        first = asyncio.create_task(scheduler.async_start(1, 10))
        second = asyncio.create_task(scheduler.async_start(2, 20))
        await asyncio.wait_for(first_finished.wait(), timeout=0.5)
        # The first sender already consumed the interval, so a second full
        # 100 ms sleep here would be a bug.
        await asyncio.wait_for(second_entered.wait(), timeout=0.075)
        assert await asyncio.gather(first, second) == [True, True]
    finally:
        await scheduler.async_close()


async def test_spacing_survives_idle_worker_exit_within_same_epoch() -> None:
    calls = 0
    second_entered = asyncio.Event()

    async def send_start(_output: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            second_entered.set()

    scheduler = RollerStartScheduler(send_start, delay=0.08)
    try:
        assert await scheduler.async_start(1, 10) is True
        second = asyncio.create_task(scheduler.async_start(2, 20))
        await asyncio.sleep(0.02)
        assert not second_entered.is_set()
        assert await second is True
    finally:
        await scheduler.async_close()


async def test_lower_key_arriving_during_sleep_is_reselected() -> None:
    sent: list[int] = []

    async def send_start(output: int) -> None:
        sent.append(output)

    scheduler = RollerStartScheduler(send_start, delay=0.05)
    try:
        first = asyncio.create_task(scheduler.async_start(1, 10))
        high = asyncio.create_task(scheduler.async_start(30, 300))
        assert await first is True
        low = asyncio.create_task(scheduler.async_start(20, 200))
        assert await asyncio.gather(high, low) == [True, True]
        assert sent == [10, 200, 300]
    finally:
        await scheduler.async_close()


async def test_zero_delay_allows_concurrent_sender_calls() -> None:
    release = asyncio.Event()
    two_entered = asyncio.Event()
    active = 0
    max_active = 0

    async def send_start(_output: int) -> None:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        if active == 2:
            two_entered.set()
        try:
            await release.wait()
        finally:
            active -= 1

    scheduler = RollerStartScheduler(send_start, delay=0.0)
    try:
        one = asyncio.create_task(scheduler.async_start(1, 10))
        two = asyncio.create_task(scheduler.async_start(2, 20))
        await asyncio.wait_for(two_entered.wait(), timeout=0.25)
        release.set()
        assert await asyncio.gather(one, two) == [True, True]
        assert max_active == 2
    finally:
        release.set()
        await scheduler.async_close()


@pytest.mark.parametrize(
    ("older_output", "newer_output"),
    ((100, 101), (101, 100)),
)
async def test_last_queued_command_wins_for_one_roller(
    older_output: int, newer_output: int
) -> None:
    blocker_entered = asyncio.Event()
    release_blocker = asyncio.Event()
    sent: list[int] = []

    async def send_start(output: int) -> None:
        sent.append(output)
        if output == 1:
            blocker_entered.set()
            await release_blocker.wait()

    scheduler = RollerStartScheduler(send_start, delay=0.001)
    try:
        blocker = asyncio.create_task(scheduler.async_start(1, 1))
        await asyncio.wait_for(blocker_entered.wait(), timeout=0.25)
        older = asyncio.create_task(
            scheduler.async_start(10, older_output)
        )
        await asyncio.sleep(0)
        newer = asyncio.create_task(
            scheduler.async_start(10, newer_output)
        )
        assert await older is False
        release_blocker.set()
        assert await asyncio.gather(blocker, newer) == [True, True]
        assert sent == [1, newer_output]
    finally:
        release_blocker.set()
        await scheduler.async_close()


async def test_cancel_prevents_queued_start_and_returns_false() -> None:
    blocker_entered = asyncio.Event()
    release_blocker = asyncio.Event()
    sent: list[int] = []

    async def send_start(output: int) -> None:
        sent.append(output)
        if output == 1:
            blocker_entered.set()
            await release_blocker.wait()

    scheduler = RollerStartScheduler(send_start, delay=0.001)
    try:
        blocker = asyncio.create_task(scheduler.async_start(1, 1))
        await asyncio.wait_for(blocker_entered.wait(), timeout=0.25)
        queued = asyncio.create_task(scheduler.async_start(10, 100))
        await asyncio.sleep(0)
        assert scheduler.cancel(10) is True
        assert await queued is False
        assert scheduler.cancel(10) is False
        release_blocker.set()
        assert await blocker is True
        assert sent == [1]
    finally:
        release_blocker.set()
        await scheduler.async_close()


async def test_canceling_one_roller_leaves_other_pending_work() -> None:
    blocker_entered = asyncio.Event()
    release_blocker = asyncio.Event()
    sent: list[int] = []

    async def send_start(output: int) -> None:
        sent.append(output)
        if output == 1:
            blocker_entered.set()
            await release_blocker.wait()

    scheduler = RollerStartScheduler(send_start, delay=0.001)
    try:
        blocker = asyncio.create_task(scheduler.async_start(1, 1))
        await asyncio.wait_for(blocker_entered.wait(), timeout=0.25)
        canceled = asyncio.create_task(scheduler.async_start(10, 100))
        retained = asyncio.create_task(scheduler.async_start(20, 200))
        await asyncio.sleep(0)
        assert scheduler.cancel(10) is True
        assert await canceled is False
        release_blocker.set()
        assert await asyncio.gather(blocker, retained) == [True, True]
        assert sent == [1, 200]
    finally:
        release_blocker.set()
        await scheduler.async_close()


async def test_start_already_being_sent_is_not_cancellable_queue_work() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def send_start(_output: int) -> None:
        entered.set()
        await release.wait()

    scheduler = RollerStartScheduler(send_start, delay=0.01)
    try:
        start = asyncio.create_task(scheduler.async_start(1, 10))
        await asyncio.wait_for(entered.wait(), timeout=0.25)
        assert scheduler.cancel(1) is False
        release.set()
        assert await start is True
    finally:
        release.set()
        await scheduler.async_close()


async def test_external_cancellation_removes_exact_queued_item() -> None:
    blocker_entered = asyncio.Event()
    release_blocker = asyncio.Event()
    sent: list[int] = []

    async def send_start(output: int) -> None:
        sent.append(output)
        if output == 1:
            blocker_entered.set()
            await release_blocker.wait()

    scheduler = RollerStartScheduler(send_start, delay=0.001)
    try:
        blocker = asyncio.create_task(scheduler.async_start(1, 1))
        await asyncio.wait_for(blocker_entered.wait(), timeout=0.25)
        queued = asyncio.create_task(scheduler.async_start(10, 100))
        await asyncio.sleep(0)
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        assert scheduler.cancel(10) is False
        release_blocker.set()
        assert await blocker is True
        await asyncio.sleep(0.01)
        assert sent == [1]
    finally:
        release_blocker.set()
        await scheduler.async_close()


async def test_canceling_old_caller_cannot_remove_newer_replacement() -> None:
    blocker_entered = asyncio.Event()
    release_blocker = asyncio.Event()
    sent: list[int] = []

    async def send_start(output: int) -> None:
        sent.append(output)
        if output == 1:
            blocker_entered.set()
            await release_blocker.wait()

    scheduler = RollerStartScheduler(send_start, delay=0.001)
    try:
        blocker = asyncio.create_task(scheduler.async_start(1, 1))
        await asyncio.wait_for(blocker_entered.wait(), timeout=0.25)
        older = asyncio.create_task(scheduler.async_start(10, 100))
        await asyncio.sleep(0)
        newer = asyncio.create_task(scheduler.async_start(10, 101))
        older.cancel()
        with pytest.raises(asyncio.CancelledError):
            await older
        release_blocker.set()
        assert await asyncio.gather(blocker, newer) == [True, True]
        assert sent == [1, 101]
    finally:
        release_blocker.set()
        await scheduler.async_close()


async def test_zero_delay_caller_cancellation_does_not_cancel_sender() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    async def send_start(_output: int) -> None:
        await asyncio.sleep(0)
        entered.set()
        await release.wait()
        finished.set()

    scheduler = RollerStartScheduler(send_start, delay=0.0)
    try:
        caller = asyncio.create_task(scheduler.async_start(1, 10))
        await asyncio.wait_for(entered.wait(), timeout=0.25)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        release.set()
        await asyncio.wait_for(finished.wait(), timeout=0.25)
    finally:
        release.set()
        await scheduler.async_close()


async def test_inflight_caller_cancellation_consumes_late_sender_error() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    sender_finished = asyncio.Event()
    unhandled: list[dict] = []
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: unhandled.append(context))

    async def send_start(_output: int) -> None:
        entered.set()
        await release.wait()
        sender_finished.set()
        raise RuntimeError("late sender failure")

    scheduler = RollerStartScheduler(send_start, delay=0.01)
    try:
        caller = asyncio.create_task(scheduler.async_start(1, 10))
        await asyncio.wait_for(entered.wait(), timeout=0.25)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        release.set()
        await asyncio.wait_for(sender_finished.wait(), timeout=0.25)
        await asyncio.sleep(0)
        assert unhandled == []
    finally:
        release.set()
        await scheduler.async_close()
        loop.set_exception_handler(previous_handler)


async def test_sender_failure_fails_current_and_same_epoch_pending() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    sent: list[int] = []

    async def send_start(output: int) -> None:
        sent.append(output)
        entered.set()
        await release.wait()
        raise SenderError("panel command failed")

    scheduler = RollerStartScheduler(send_start, delay=0.001)
    try:
        current = asyncio.create_task(scheduler.async_start(1, 10))
        await asyncio.wait_for(entered.wait(), timeout=0.25)
        pending_one = asyncio.create_task(scheduler.async_start(2, 20))
        pending_two = asyncio.create_task(scheduler.async_start(3, 30))
        await asyncio.sleep(0)
        release.set()
        results = await asyncio.gather(
            current, pending_one, pending_two, return_exceptions=True
        )
        assert all(isinstance(result, SenderError) for result in results)
        assert sent == [10]
        assert scheduler.cancel(2) is False
        assert scheduler.cancel(3) is False
    finally:
        release.set()
        await scheduler.async_close()


async def test_cancel_all_fails_current_and_pending_without_replay() -> None:
    entered = asyncio.Event()
    sender_finished = asyncio.Event()
    sent: list[int] = []

    async def send_start(output: int) -> None:
        sent.append(output)
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            sender_finished.set()

    scheduler = RollerStartScheduler(send_start, delay=0.01)
    try:
        current = asyncio.create_task(scheduler.async_start(1, 10))
        await asyncio.wait_for(entered.wait(), timeout=0.25)
        pending = asyncio.create_task(scheduler.async_start(2, 20))
        await asyncio.sleep(0)
        scheduler.cancel_all(PanelUnavailableError("panel unavailable"))
        results = await asyncio.wait_for(
            asyncio.gather(current, pending, return_exceptions=True),
            timeout=0.25,
        )
        assert all(isinstance(result, PanelUnavailableError) for result in results)
        await asyncio.wait_for(sender_finished.wait(), timeout=0.25)
        assert sent == [10]
    finally:
        await scheduler.async_close()


async def test_disconnect_during_sleep_resets_delay_and_drops_old_work() -> None:
    sent: list[int] = []
    new_entered = asyncio.Event()

    async def send_start(output: int) -> None:
        sent.append(output)
        if output == 30:
            new_entered.set()

    scheduler = RollerStartScheduler(send_start, delay=10.0)
    try:
        assert await scheduler.async_start(1, 10) is True
        old_pending = asyncio.create_task(scheduler.async_start(2, 20))
        await asyncio.sleep(0)
        scheduler.cancel_all(PanelUnavailableError("panel unavailable"))
        with pytest.raises(PanelUnavailableError):
            await old_pending

        fresh = asyncio.create_task(scheduler.async_start(3, 30))
        await asyncio.wait_for(new_entered.wait(), timeout=0.25)
        assert await fresh is True
        assert sent == [10, 30]
    finally:
        await scheduler.async_close()


async def test_stale_old_epoch_failure_cannot_touch_new_work() -> None:
    old_entered = asyncio.Event()
    old_was_canceled = asyncio.Event()
    release_old = asyncio.Event()
    old_finished = asyncio.Event()
    new_entered = asyncio.Event()

    async def send_start(output: int) -> None:
        if output == 10:
            old_entered.set()
            try:
                await release_old.wait()
            except asyncio.CancelledError:
                old_was_canceled.set()
                await release_old.wait()
            finally:
                old_finished.set()
            raise SenderError("stale failure")
        new_entered.set()

    scheduler = RollerStartScheduler(send_start, delay=0.01)
    try:
        old = asyncio.create_task(scheduler.async_start(1, 10))
        await asyncio.wait_for(old_entered.wait(), timeout=0.25)
        scheduler.cancel_all(PanelUnavailableError("panel unavailable"))
        with pytest.raises(PanelUnavailableError):
            await asyncio.wait_for(old, timeout=0.25)

        fresh = asyncio.create_task(scheduler.async_start(2, 20))
        await asyncio.wait_for(new_entered.wait(), timeout=0.25)
        assert await fresh is True
        await asyncio.wait_for(old_was_canceled.wait(), timeout=0.25)
        release_old.set()
        await asyncio.wait_for(old_finished.wait(), timeout=0.25)
        await asyncio.sleep(0)
        assert scheduler.cancel(2) is False
    finally:
        release_old.set()
        await scheduler.async_close()


async def test_cancel_all_drains_zero_delay_sender_with_defined_error() -> None:
    entered = asyncio.Event()
    sender_finished = asyncio.Event()
    allow_fresh = False

    async def send_start(_output: int) -> None:
        if allow_fresh:
            return
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            sender_finished.set()

    scheduler = RollerStartScheduler(send_start, delay=0.0)
    try:
        caller = asyncio.create_task(scheduler.async_start(1, 10))
        await asyncio.wait_for(entered.wait(), timeout=0.25)
        scheduler.cancel_all(PanelUnavailableError("panel unavailable"))
        with pytest.raises(PanelUnavailableError):
            await asyncio.wait_for(caller, timeout=0.25)
        await asyncio.wait_for(sender_finished.wait(), timeout=0.25)

        allow_fresh = True
        assert await scheduler.async_start(2, 20) is True
    finally:
        await scheduler.async_close()


async def test_reentrant_cancel_all_cancels_current_old_epoch_sender() -> None:
    sender_finished = asyncio.Event()
    scheduler: RollerStartScheduler

    async def send_start(_output: int) -> None:
        scheduler.cancel_all(PanelUnavailableError("panel unavailable"))
        try:
            await asyncio.Event().wait()
        finally:
            sender_finished.set()

    scheduler = RollerStartScheduler(send_start, delay=0.01)
    try:
        caller = asyncio.create_task(scheduler.async_start(1, 10))
        with pytest.raises(PanelUnavailableError):
            await asyncio.wait_for(caller, timeout=0.25)
        await asyncio.wait_for(sender_finished.wait(), timeout=0.1)
        await asyncio.sleep(0)
        assert scheduler._active_tasks == {}
    finally:
        await scheduler.async_close()


@pytest.mark.parametrize("delay", (0.01, 0.0))
async def test_close_fails_current_sender_waiter_and_drains_task(
    delay: float,
) -> None:
    entered = asyncio.Event()
    sender_finished = asyncio.Event()

    async def send_start(_output: int) -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            sender_finished.set()

    scheduler = RollerStartScheduler(send_start, delay=delay)
    caller = asyncio.create_task(scheduler.async_start(1, 10))
    await asyncio.wait_for(entered.wait(), timeout=0.25)
    await scheduler.async_close()
    with pytest.raises(RollerSchedulerClosedError):
        await asyncio.wait_for(caller, timeout=0.25)
    await asyncio.wait_for(sender_finished.wait(), timeout=0.25)
    assert scheduler._active_tasks == {}


async def test_close_is_sequentially_and_concurrently_idempotent() -> None:
    entered = asyncio.Event()

    async def send_start(_output: int) -> None:
        entered.set()
        await asyncio.Event().wait()

    scheduler = RollerStartScheduler(send_start, delay=0.01)
    caller = asyncio.create_task(scheduler.async_start(1, 10))
    await asyncio.wait_for(entered.wait(), timeout=0.25)
    await asyncio.gather(scheduler.async_close(), scheduler.async_close())
    with pytest.raises(RollerSchedulerClosedError):
        await caller
    await scheduler.async_close()
    scheduler.cancel_all(PanelUnavailableError("ignored after close"))
    await scheduler.async_close()
    assert scheduler._active_tasks == {}
    with pytest.raises(RollerSchedulerClosedError):
        await scheduler.async_start(2, 20)


async def test_close_after_cancel_all_is_harmless() -> None:
    async def send_start(_output: int) -> None:
        return None

    scheduler = RollerStartScheduler(send_start, delay=0.01)
    scheduler.cancel_all(PanelUnavailableError("panel unavailable"))
    await asyncio.gather(scheduler.async_close(), scheduler.async_close())
    assert scheduler._active_tasks == {}


async def test_worker_exit_new_request_race_never_strands_future() -> None:
    sent: list[int] = []

    async def send_start(output: int) -> None:
        sent.append(output)

    scheduler = RollerStartScheduler(send_start, delay=0.0001)
    try:
        for output in range(40):
            assert await asyncio.wait_for(
                scheduler.async_start(output, output), timeout=0.25
            ) is True
        assert sent == list(range(40))
    finally:
        await scheduler.async_close()
