"""Temperature sensors from ABAX temperature-capable zones (cmd 0x7D)."""
from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SatelConfigEntry
from .entity import SatelEntity
from .mapping import TempSensorDesc
from .pysatel.monitor import SatelHub


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SatelConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    runtime = entry.runtime_data
    async_add_entities(
        SatelTemperatureSensor(runtime.hub, entry.entry_id, desc)
        for desc in runtime.entity_map.temp_sensors
    )


class SatelTemperatureSensor(SatelEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_suggested_display_precision = 1

    def __init__(self, hub: SatelHub, entry_id: str, desc: TempSensorDesc) -> None:
        super().__init__(
            hub, entry_id, f"zone_{desc.zone}_temperature", f"Temperatura {desc.name}"
        )
        self._zone = desc.zone
        self._attr_extra_state_attributes = {"zone": desc.zone}

    def _state_snapshot(self):
        return (self._hub.available, self._hub.temperatures.get(self._zone))

    @property
    def native_value(self) -> float | None:
        return self._hub.temperatures.get(self._zone)
