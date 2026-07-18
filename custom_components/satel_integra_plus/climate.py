"""Floor-heating climate entities from panel-native thermostat outputs.

The INTEGRA type-120 "Termostat" output runs hysteresis control inside the
panel: it switches heating based on a bound ABAX temperature zone and two
panel-configured thresholds (economical / comfort). The integration protocol
cannot read or change those thresholds, so these entities are heat-only with:

- current temperature: the bound zone's 0x7D reading,
- hvac action: heating while the thermostat output is active,
- comfort/eco preset: optionally, a shared bistable output that the panel
  logic uses to select which threshold applies (see the comfort output option).
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.climate import (
    PRESET_COMFORT,
    PRESET_ECO,
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SatelConfigEntry
from .const import CONF_COMFORT_OUTPUT, DEFAULT_COMFORT_OUTPUT
from .entity import SatelEntity
from .mapping import ClimateDesc
from .pysatel.const import Cmd
from .pysatel.monitor import SatelHub


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SatelConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    runtime = entry.runtime_data
    comfort_output = entry.options.get(CONF_COMFORT_OUTPUT, DEFAULT_COMFORT_OUTPUT)
    # only wire the preset if configured and the panel has such an output
    if not comfort_output or comfort_output not in runtime.hub.discovery.outputs:
        comfort_output = None
    async_add_entities(
        SatelThermostat(runtime.hub, entry.entry_id, desc, comfort_output)
        for desc in runtime.entity_map.climates
    )


class SatelThermostat(SatelEntity, ClimateEntity):
    _attr_hvac_modes = [HVACMode.HEAT]
    _attr_hvac_mode = HVACMode.HEAT
    _attr_temperature_unit = UnitOfTemperature.CELSIUS

    def __init__(
        self,
        hub: SatelHub,
        entry_id: str,
        desc: ClimateDesc,
        comfort_output: int | None,
    ) -> None:
        super().__init__(hub, entry_id, f"climate_{desc.output}", f"Ogrzewanie {desc.name}")
        self._output = desc.output
        self._temp_zone = desc.temp_zone
        self._comfort_output = comfort_output
        self._attr_supported_features = ClimateEntityFeature(0)
        if comfort_output is not None:
            self._attr_supported_features |= ClimateEntityFeature.PRESET_MODE
            self._attr_preset_modes = [PRESET_COMFORT, PRESET_ECO]
        self._attr_extra_state_attributes = {
            "thermostat_output": desc.output,
            "temperature_zone": desc.temp_zone,
        }

    def _state_snapshot(self) -> Any:
        return (
            self._hub.available,
            self._hub.output_active(self._output),
            self._temp_zone and self._hub.temperatures.get(self._temp_zone),
            self._comfort_output and self._hub.output_active(self._comfort_output),
        )

    @property
    def current_temperature(self) -> float | None:
        if self._temp_zone is None:
            return None
        return self._hub.temperatures.get(self._temp_zone)

    @property
    def hvac_action(self) -> HVACAction:
        return (
            HVACAction.HEATING
            if self._hub.output_active(self._output)
            else HVACAction.IDLE
        )

    @property
    def preset_mode(self) -> str | None:
        if self._comfort_output is None:
            return None
        return (
            PRESET_COMFORT
            if self._hub.output_active(self._comfort_output)
            else PRESET_ECO
        )

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        if self._comfort_output is None:
            return
        action = (
            Cmd.OUTPUTS_ON if preset_mode == PRESET_COMFORT else Cmd.OUTPUTS_OFF
        )
        await self._hub.client.control_outputs(action, {self._comfort_output})

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Heat-only: the panel owns the on/off logic."""
