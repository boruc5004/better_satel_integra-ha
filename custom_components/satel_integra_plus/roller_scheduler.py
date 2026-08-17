"""Entry-scoped scheduling for individual roller start commands."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

SendStart = Callable[[int], Awaitable[None]]


class RollerSchedulerClosedError(RuntimeError):
    """Raised when work is submitted to or drained from a closed scheduler."""


@dataclass(slots=True)
class _PendingStart:
    output: int
    future: asyncio.Future[bool]
    epoch: int


def _consume_future(future: asyncio.Future[Any]) -> None:
    """Retrieve a detached future's exception, if any."""
    if not future.cancelled():
        future.exception()


class RollerStartScheduler:
    """Optionally serialize individual roller starts for one config entry."""

    def __init__(self, send_start: SendStart, delay: float) -> None:
        self._send_start = send_start
        self._delay = max(0.0, float(delay))
        self._pending: dict[int, _PendingStart] = {}
        self._epoch = 0
        self._worker: asyncio.Task[None] | None = None
        self._worker_epoch: int | None = None
        self._active_tasks: dict[asyncio.Task[Any], int] = {}
        self._inflight: dict[asyncio.Task[Any], _PendingStart] = {}
        self._last_start_epoch: int | None = None
        self._last_start_time: float | None = None
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None

    async def async_start(self, roller_key: int, output: int) -> bool:
        """Queue or dispatch a start; return false if it is superseded."""
        if self._closed:
            raise RollerSchedulerClosedError("roller scheduler is closed")

        epoch = self._epoch
        item = self._new_item(output, epoch)
        if self._delay <= 0.0:
            # Zero delay deliberately retains one sender per HA caller so the
            # client's existing 50 ms batching window can coalesce requests.
            task = asyncio.create_task(self._run_direct(item))
            self._inflight[task] = item
            self._track_task(task, epoch)
            return await self._await_item(None, item)

        previous = self._pending.get(roller_key)
        self._pending[roller_key] = item
        if previous is not None:
            self._set_result(previous, False)

        self._ensure_worker(epoch)
        return await self._await_item(roller_key, item)

    def cancel(self, roller_key: int) -> bool:
        """Cancel a start that has not yet been dispatched."""
        item = self._pending.pop(roller_key, None)
        if item is None:
            return False
        self._set_result(item, False)
        return True

    def cancel_all(self, exc: Exception) -> None:
        """Fail old-generation starts after a disconnect, then stay reusable."""
        if self._closed:
            return

        old_epoch = self._epoch
        self._epoch += 1
        self._reset_spacing()
        self._fail_pending_through(old_epoch, exc)
        self._fail_inflight_through(old_epoch, exc)

        # Detach ownership before cancellation. A new epoch may start while an
        # old sender is still unwinding (or even suppressing cancellation).
        if self._worker is not None and (
            self._worker_epoch is None or self._worker_epoch <= old_epoch
        ):
            self._worker = None
            self._worker_epoch = None

        for task, epoch in list(self._active_tasks.items()):
            if epoch <= old_epoch:
                task.cancel()

    async def async_close(self) -> None:
        """Permanently stop the scheduler and drain every sender task."""
        if self._close_task is None:
            self._closed = True
            self._close_task = asyncio.create_task(self._close_impl())
        await asyncio.shield(self._close_task)

    def _new_item(self, output: int, epoch: int) -> _PendingStart:
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        future.add_done_callback(_consume_future)
        return _PendingStart(output=output, future=future, epoch=epoch)

    async def _await_item(
        self, roller_key: int | None, item: _PendingStart
    ) -> bool:
        try:
            return await asyncio.shield(item.future)
        except asyncio.CancelledError:
            # Remove only this exact queued item. An old caller must never
            # remove a newer replacement stored under the same roller key.
            if roller_key is not None and self._pending.get(roller_key) is item:
                self._pending.pop(roller_key)
                self._set_result(item, False)
            # An in-flight wire action is no longer assumed preventable. Its
            # independently tracked sender continues and its result is consumed.
            raise

    async def _run_direct(self, item: _PendingStart) -> None:
        try:
            await self._send_start(item.output)
        except asyncio.CancelledError:
            if not item.future.done():
                self._set_exception(
                    item,
                    RollerSchedulerClosedError("roller start task was canceled"),
                )
            raise
        except Exception as exc:  # noqa: BLE001 - propagate to this caller
            self._set_exception(item, exc)
        else:
            self._set_result(item, True)

    def _ensure_worker(self, epoch: int) -> None:
        if (
            self._worker is not None
            and not self._worker.done()
            and self._worker_epoch == epoch
        ):
            return
        task = asyncio.create_task(self._run_worker(epoch))
        self._worker = task
        self._worker_epoch = epoch
        self._track_task(task, epoch)

    def _track_task(self, task: asyncio.Task[Any], epoch: int) -> None:
        self._active_tasks[task] = epoch

        def _done(done: asyncio.Task[Any]) -> None:
            self._active_tasks.pop(done, None)
            self._inflight.pop(done, None)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(_done)

    async def _run_worker(self, epoch: int) -> None:
        current = asyncio.current_task()
        assert current is not None
        current_item: _PendingStart | None = None
        try:
            # Collect HA group member calls scheduled in this event-loop turn.
            await asyncio.sleep(0)
            loop = asyncio.get_running_loop()
            while not self._closed and epoch == self._epoch:
                if not any(item.epoch == epoch for item in self._pending.values()):
                    return

                if (
                    self._last_start_epoch == epoch
                    and self._last_start_time is not None
                ):
                    remaining = self._delay - (loop.time() - self._last_start_time)
                    if remaining > 0:
                        await asyncio.sleep(remaining)
                        if self._closed or epoch != self._epoch:
                            return

                # Select only after the sleep so late lower keys, replacements,
                # and Stop cancellations can change the next dispatch.
                keys = [
                    key
                    for key, item in self._pending.items()
                    if item.epoch == epoch
                ]
                if not keys:
                    return
                roller_key = min(keys)
                current_item = self._pending.pop(roller_key)
                self._inflight[current] = current_item
                self._last_start_epoch = epoch
                self._last_start_time = loop.time()
                try:
                    await self._send_start(current_item.output)
                except asyncio.CancelledError:
                    if not current_item.future.done():
                        self._set_exception(
                            current_item,
                            RollerSchedulerClosedError(
                                "roller start task was canceled"
                            ),
                        )
                    raise
                except Exception as exc:  # noqa: BLE001 - fail this epoch
                    self._set_exception(current_item, exc)
                    self._fail_pending(epoch, exc)
                    return
                else:
                    self._set_result(current_item, True)
                finally:
                    if self._inflight.get(current) is current_item:
                        self._inflight.pop(current, None)
                    current_item = None
        finally:
            if current_item is not None and self._inflight.get(current) is current_item:
                self._inflight.pop(current, None)
            if current is self._worker:
                self._worker = None
                self._worker_epoch = None

    async def _close_impl(self) -> None:
        self._epoch += 1
        self._reset_spacing()
        error = RollerSchedulerClosedError("roller scheduler is closed")
        self._fail_all_pending(error)
        self._fail_all_inflight(error)
        self._worker = None
        self._worker_epoch = None

        tasks = list(self._active_tasks)
        current = asyncio.current_task()
        for task in tasks:
            if task is not current:
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for task in tasks:
            self._active_tasks.pop(task, None)
            self._inflight.pop(task, None)

    def _reset_spacing(self) -> None:
        self._last_start_epoch = None
        self._last_start_time = None

    @staticmethod
    def _set_result(item: _PendingStart, value: bool) -> None:
        if not item.future.done():
            item.future.set_result(value)

    @staticmethod
    def _set_exception(item: _PendingStart, exc: Exception) -> None:
        if not item.future.done():
            item.future.set_exception(exc)

    def _fail_pending(self, epoch: int, exc: Exception) -> None:
        doomed = [
            (key, item)
            for key, item in self._pending.items()
            if item.epoch == epoch
        ]
        for key, item in doomed:
            if self._pending.get(key) is item:
                self._pending.pop(key)
            self._set_exception(item, exc)

    def _fail_pending_through(self, epoch: int, exc: Exception) -> None:
        doomed = [
            (key, item)
            for key, item in self._pending.items()
            if item.epoch <= epoch
        ]
        for key, item in doomed:
            if self._pending.get(key) is item:
                self._pending.pop(key)
            self._set_exception(item, exc)

    def _fail_inflight_through(self, epoch: int, exc: Exception) -> None:
        for item in list(self._inflight.values()):
            if item.epoch <= epoch:
                self._set_exception(item, exc)

    def _fail_all_pending(self, exc: Exception) -> None:
        items = list(self._pending.values())
        self._pending.clear()
        for item in items:
            self._set_exception(item, exc)

    def _fail_all_inflight(self, exc: Exception) -> None:
        for item in list(self._inflight.values()):
            self._set_exception(item, exc)
