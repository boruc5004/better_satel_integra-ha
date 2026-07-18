"""Base entity: push updates from the hub, panel device info."""
from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN
from .pysatel.monitor import SatelHub


class SatelEntity(Entity):
    """Push-updated entity backed by the hub's state cache."""

    _attr_should_poll = False

    def __init__(self, hub: SatelHub, entry_id: str, unique_suffix: str, name: str) -> None:
        self._hub = hub
        self._attr_unique_id = f"{entry_id}_{unique_suffix}"
        self._attr_name = name
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name="Satel INTEGRA",
            manufacturer="Satel",
            model=hub.client.panel.model,
            sw_version=hub.client.panel.version,
        )
        self._snapshot: Any = object()  # never equal on first update

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._hub.subscribe(self._hub_updated))
        self._snapshot = self._state_snapshot()

    def _hub_updated(self) -> None:
        """Write state only when this entity's slice of the cache changed."""
        snapshot = self._state_snapshot()
        if snapshot != self._snapshot:
            self._snapshot = snapshot
            self.async_write_ha_state()

    def _state_snapshot(self) -> Any:
        """Hashable summary of everything this entity renders."""
        return self._hub.available

    @property
    def available(self) -> bool:
        return self._hub.available
