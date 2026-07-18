"""Covers: roller-blind output pairs (105/106), panel group outputs, gates.

Group behavior: HA cover groups call every member concurrently; the hub's
client coalesces those into a single 0x88 bitmask frame, so all covers
actually move. The panel's own group outputs (ROL Parter / ROL Piętro) are
additionally exposed as first-class covers.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.cover import (
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import SatelConfigEntry
from .entity import SatelEntity
from .mapping import CoverDesc, GateDesc
from .pysatel.const import Cmd
from .pysatel.monitor import SatelHub


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SatelConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    runtime = entry.runtime_data
    entities: list[CoverEntity] = [
        SatelRollerCover(runtime.hub, entry.entry_id, desc)
        for desc in runtime.entity_map.covers
    ]
    entities += [
        SatelGateCover(runtime.hub, entry.entry_id, desc)
        for desc in runtime.entity_map.gates
    ]
    async_add_entities(entities)


class SatelRollerCover(SatelEntity, RestoreEntity, CoverEntity):
    """A roller blind driven by an up/down output pair.

    The protocol exposes only output states, so position is unknown; we track
    the last completed direction to report open/closed and show movement
    while an output is active.
    """

    _attr_device_class = CoverDeviceClass.SHUTTER
    _attr_supported_features = (
        CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE | CoverEntityFeature.STOP
    )

    def __init__(self, hub: SatelHub, entry_id: str, desc: CoverDesc) -> None:
        super().__init__(hub, entry_id, f"cover_{desc.up_output}", desc.name)
        self._up = desc.up_output
        self._down = desc.down_output
        self._is_group = desc.is_group
        self._last_direction: str | None = None  # "open" / "closed"
        self._attr_extra_state_attributes = {
            "up_output": desc.up_output,
            "down_output": desc.down_output,
            "group": desc.is_group,
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            if last.state in ("open", "closed"):
                self._last_direction = last.state

    def _state_snapshot(self) -> Any:
        return (
            self._hub.available,
            self._hub.output_active(self._up),
            self._hub.output_active(self._down),
            self._last_direction,
        )

    def _hub_updated(self) -> None:
        # falling edge of a movement output => remember the final position
        if not self._hub.output_active(self._up) and not self._hub.output_active(self._down):
            was = self._snapshot
            if isinstance(was, tuple) and len(was) >= 3:
                if was[1]:  # up was running
                    self._last_direction = "open"
                elif was[2]:  # down was running
                    self._last_direction = "closed"
        super()._hub_updated()

    @property
    def is_opening(self) -> bool:
        return self._hub.output_active(self._up)

    @property
    def is_closing(self) -> bool:
        return self._hub.output_active(self._down)

    @property
    def is_closed(self) -> bool | None:
        if self.is_opening or self.is_closing:
            return False
        if self._last_direction is None:
            return None
        return self._last_direction == "closed"

    async def async_open_cover(self, **kwargs: Any) -> None:
        await self._hub.client.control_outputs(Cmd.OUTPUTS_ON, {self._up})

    async def async_close_cover(self, **kwargs: Any) -> None:
        await self._hub.client.control_outputs(Cmd.OUTPUTS_ON, {self._down})

    async def async_stop_cover(self, **kwargs: Any) -> None:
        # one frame clears both directions
        await self._hub.client.control_outputs(
            Cmd.OUTPUTS_OFF, {self._up, self._down}
        )


class SatelGateCover(SatelEntity, CoverEntity):
    """A gate/garage door: MONO pulse output + reed-contact state zone."""

    def __init__(self, hub: SatelHub, entry_id: str, desc: GateDesc) -> None:
        super().__init__(hub, entry_id, f"gate_{desc.output}", desc.name)
        self._output = desc.output
        self._state_zone = desc.state_zone
        self._attr_device_class = (
            CoverDeviceClass.GARAGE if desc.garage else CoverDeviceClass.GATE
        )
        self._attr_supported_features = (
            CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE
        )
        self._attr_extra_state_attributes = {
            "output": desc.output,
            "state_zone": desc.state_zone,
        }

    def _state_snapshot(self) -> Any:
        return (
            self._hub.available,
            self._state_zone is not None
            and self._hub.zone_active(Cmd.ZONES_VIOLATION, self._state_zone),
        )

    @property
    def is_closed(self) -> bool | None:
        if self._state_zone is None:
            return None
        # reed contact zone is violated while the gate is open
        return not self._hub.zone_active(Cmd.ZONES_VIOLATION, self._state_zone)

    async def _pulse(self) -> None:
        await self._hub.client.control_outputs(Cmd.OUTPUTS_ON, {self._output})

    async def async_open_cover(self, **kwargs: Any) -> None:
        if self.is_closed is False:
            return
        await self._pulse()

    async def async_close_cover(self, **kwargs: Any) -> None:
        if self.is_closed is True:
            return
        await self._pulse()
