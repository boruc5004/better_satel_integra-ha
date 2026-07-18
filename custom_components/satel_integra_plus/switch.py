"""Switches: remaining controllable outputs (valves, MONO pulses, mode flags)."""
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SatelConfigEntry
from .entity import SatelEntity
from .mapping import SwitchDesc
from .pysatel.const import Cmd
from .pysatel.monitor import SatelHub


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SatelConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    runtime = entry.runtime_data
    async_add_entities(
        SatelSwitch(runtime.hub, entry.entry_id, desc)
        for desc in runtime.entity_map.switches
    )


class SatelSwitch(SatelEntity, SwitchEntity):
    _attr_device_class = SwitchDeviceClass.SWITCH

    def __init__(self, hub: SatelHub, entry_id: str, desc: SwitchDesc) -> None:
        super().__init__(hub, entry_id, f"output_{desc.output}", desc.name)
        self._output = desc.output
        self._attr_extra_state_attributes = {
            "output": desc.output,
            "momentary": desc.momentary,
        }

    def _state_snapshot(self) -> Any:
        return (self._hub.available, self._hub.output_active(self._output))

    @property
    def is_on(self) -> bool:
        return self._hub.output_active(self._output)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._hub.client.control_outputs(Cmd.OUTPUTS_ON, {self._output})

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._hub.client.control_outputs(Cmd.OUTPUTS_OFF, {self._output})
