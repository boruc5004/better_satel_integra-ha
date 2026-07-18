"""Zone binary sensors (motion, glass break, flood, smoke, gas, doors…)."""
from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SatelConfigEntry
from .entity import SatelEntity
from .mapping import BinarySensorDesc
from .pysatel.const import Cmd
from .pysatel.monitor import SatelHub


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SatelConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    runtime = entry.runtime_data
    async_add_entities(
        SatelZoneBinarySensor(runtime.hub, entry.entry_id, desc)
        for desc in runtime.entity_map.binary_sensors
    )


class SatelZoneBinarySensor(SatelEntity, BinarySensorEntity):
    def __init__(self, hub: SatelHub, entry_id: str, desc: BinarySensorDesc) -> None:
        super().__init__(hub, entry_id, f"zone_{desc.zone}", desc.name)
        self._zone = desc.zone
        if desc.device_class:
            self._attr_device_class = BinarySensorDeviceClass(desc.device_class)
        self._attr_extra_state_attributes = {"zone": desc.zone}
        if desc.partition is not None:
            self._attr_extra_state_attributes["partition"] = desc.partition

    def _state_snapshot(self):
        return (
            self._hub.available,
            self._hub.zone_active(Cmd.ZONES_VIOLATION, self._zone),
            self._hub.zone_active(Cmd.ZONES_TAMPER, self._zone),
            self._hub.zone_active(Cmd.ZONES_ALARM, self._zone),
            self._hub.zone_active(Cmd.ZONES_ALARM_MEMORY, self._zone),
            self._hub.zone_active(Cmd.ZONES_BYPASS, self._zone),
        )

    @property
    def is_on(self) -> bool:
        return self._hub.zone_active(Cmd.ZONES_VIOLATION, self._zone)

    @property
    def extra_state_attributes(self) -> dict:
        return {
            **self._attr_extra_state_attributes,
            "tamper": self._hub.zone_active(Cmd.ZONES_TAMPER, self._zone),
            "alarm": self._hub.zone_active(Cmd.ZONES_ALARM, self._zone),
            "alarm_memory": self._hub.zone_active(Cmd.ZONES_ALARM_MEMORY, self._zone),
            "bypassed": self._hub.zone_active(Cmd.ZONES_BYPASS, self._zone),
        }
