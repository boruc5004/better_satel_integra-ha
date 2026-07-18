"""Alarm control panels, one per partition."""
from __future__ import annotations

from homeassistant.components.alarm_control_panel import (
    AlarmControlPanelEntity,
    AlarmControlPanelEntityFeature,
    AlarmControlPanelState,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SatelConfigEntry
from .const import (
    CONF_ARM_HOME_MODE,
    CONF_ARM_NIGHT_MODE,
    CONF_FORCE_ARM,
    DEFAULT_ARM_HOME_MODE,
    DEFAULT_ARM_NIGHT_MODE,
    DEFAULT_FORCE_ARM,
)
from .entity import SatelEntity
from .pysatel.const import Cmd
from .pysatel.monitor import PartitionInfo, SatelHub


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SatelConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    runtime = entry.runtime_data
    home_mode = entry.options.get(CONF_ARM_HOME_MODE, DEFAULT_ARM_HOME_MODE)
    night_mode = entry.options.get(CONF_ARM_NIGHT_MODE, DEFAULT_ARM_NIGHT_MODE)
    force_arm = entry.options.get(CONF_FORCE_ARM, DEFAULT_FORCE_ARM)
    async_add_entities(
        SatelAlarmPanel(
            runtime.hub, entry.entry_id, part, home_mode, night_mode, force_arm
        )
        for part in runtime.hub.discovery.partitions.values()
    )


MODE_STATE_CMDS = {
    1: Cmd.PARTITIONS_ARMED_MODE_1,
    2: Cmd.PARTITIONS_ARMED_MODE_2,
    3: Cmd.PARTITIONS_ARMED_MODE_3,
}


class SatelAlarmPanel(SatelEntity, AlarmControlPanelEntity):
    _attr_code_arm_required = False
    _attr_supported_features = (
        AlarmControlPanelEntityFeature.ARM_AWAY
        | AlarmControlPanelEntityFeature.ARM_HOME
        | AlarmControlPanelEntityFeature.ARM_NIGHT
    )

    def __init__(
        self,
        hub: SatelHub,
        entry_id: str,
        partition: PartitionInfo,
        home_mode: int,
        night_mode: int,
        force_arm: bool,
    ) -> None:
        super().__init__(
            hub, entry_id, f"partition_{partition.number}", f"Alarm {partition.name}"
        )
        self._partition = partition.number
        self._home_mode = home_mode
        self._night_mode = night_mode
        self._force_arm = force_arm
        self._attr_extra_state_attributes = {"partition": partition.number}

    def _partition_in(self, cmd: int) -> bool:
        return self._hub.zone_active(cmd, self._partition)

    def _state_snapshot(self):
        return (
            self._hub.available,
            self.alarm_state,
        )

    @property
    def alarm_state(self) -> AlarmControlPanelState | None:
        if self._partition_in(Cmd.PARTITIONS_ALARM) or self._partition_in(
            Cmd.PARTITIONS_FIRE_ALARM
        ):
            return AlarmControlPanelState.TRIGGERED
        if self._partition_in(Cmd.PARTITIONS_ENTRY_TIME):
            return AlarmControlPanelState.PENDING
        if self._partition_in(Cmd.PARTITIONS_EXIT_TIME_LONG) or self._partition_in(
            Cmd.PARTITIONS_EXIT_TIME_SHORT
        ):
            return AlarmControlPanelState.ARMING
        if self._partition_in(Cmd.PARTITIONS_ARMED):
            for mode, cmd in MODE_STATE_CMDS.items():
                if self._partition_in(cmd):
                    if mode == self._home_mode:
                        return AlarmControlPanelState.ARMED_HOME
                    if mode == self._night_mode:
                        return AlarmControlPanelState.ARMED_NIGHT
                    return AlarmControlPanelState.ARMED_CUSTOM_BYPASS
            return AlarmControlPanelState.ARMED_AWAY
        return AlarmControlPanelState.DISARMED

    async def async_alarm_arm_away(self, code: str | None = None) -> None:
        await self._hub.client.arm(
            self._partition, mode=0, fallback_force=self._force_arm
        )

    async def async_alarm_arm_home(self, code: str | None = None) -> None:
        await self._hub.client.arm(
            self._partition, mode=self._home_mode, fallback_force=self._force_arm
        )

    async def async_alarm_arm_night(self, code: str | None = None) -> None:
        await self._hub.client.arm(
            self._partition, mode=self._night_mode, fallback_force=self._force_arm
        )

    async def async_alarm_disarm(self, code: str | None = None) -> None:
        in_alarm = self._partition_in(Cmd.PARTITIONS_ALARM) or self._partition_in(
            Cmd.PARTITIONS_FIRE_ALARM
        )
        await self._hub.client.disarm(self._partition)
        if in_alarm:
            await self._hub.client.clear_alarm(self._partition)
