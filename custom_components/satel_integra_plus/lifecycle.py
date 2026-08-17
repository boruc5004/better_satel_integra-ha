"""Home Assistant-free lifecycle helpers for the entry roller scheduler."""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from .pysatel.client import NotConnectedError
from .roller_scheduler import RollerStartScheduler

_LOGGER = logging.getLogger(__name__)


def make_availability_listener(
    hub: Any, scheduler: RollerStartScheduler
) -> Callable[[], None]:
    """Flush old-generation starts whenever the panel becomes unavailable."""

    def _availability_updated() -> None:
        if not hub.available:
            scheduler.cancel_all(NotConnectedError("panel unavailable"))

    return _availability_updated


async def async_stop_runtime(hub: Any, scheduler: RollerStartScheduler) -> None:
    """Stop protocol activity, then close and drain the roller scheduler."""
    try:
        await hub.stop()
    finally:
        await scheduler.async_close()


def make_stop_listener(hub: Any, scheduler: RollerStartScheduler):
    """Create the Home Assistant shutdown callback without importing HA."""

    async def _stop(_event: Any) -> None:
        await async_stop_runtime(hub, scheduler)

    return _stop


async def async_cleanup_failed_setup(
    hub: Any,
    scheduler: RollerStartScheduler,
    unsubscribe: Callable[[], None],
) -> None:
    """Best-effort setup rollback that cannot hide the setup exception."""
    try:
        unsubscribe()
    except BaseException:  # cancellation/cleanup must not skip later steps
        _LOGGER.exception("failed to unsubscribe panel availability listener")

    try:
        await hub.stop()
    except BaseException:
        _LOGGER.exception("failed to stop panel hub after setup failure")

    try:
        await scheduler.async_close()
    except BaseException:
        _LOGGER.exception("failed to close roller scheduler after setup failure")
