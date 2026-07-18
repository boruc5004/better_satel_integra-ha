"""Panel supervision: discovery, state monitoring and reconnection.

The :class:`SatelHub` owns a :class:`SatelClient`, keeps it alive (backoff
reconnect), discovers the panel's devices via 0xEE, and maintains a state
cache refreshed through the cheap 0x7F "list of new data" command. State
changes are pushed to subscribers — no per-entity polling.
"""
from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from .client import (
    NotConnectedError,
    PanelBusyError,
    ResponseTimeoutError,
    SatelClient,
    SatelError,
)
from .const import (
    Cmd,
    DeviceType,
    OUTPUT_FUNC_UNUSED,
    ZONE_REACTION_24H_LOW_TEMPERATURE,
)
from .frames import bitmask_to_numbers

_LOGGER = logging.getLogger(__name__)

POLL_INTERVAL = 1.0
TEMP_READ_SPACING = 15.0        # one temperature zone read per this interval
FULL_REFRESH_INTERVAL = 120.0   # periodic resync of every monitored state block
RECONNECT_MIN = 1.0
RECONNECT_MAX = 60.0
BUSY_RETRY = 30.0

# state blocks the hub keeps refreshed (bit index in 0x7F == command code)
MONITORED_COMMANDS: tuple[int, ...] = (
    Cmd.ZONES_VIOLATION,
    Cmd.ZONES_TAMPER,
    Cmd.ZONES_ALARM,
    Cmd.ZONES_ALARM_MEMORY,
    Cmd.ZONES_BYPASS,
    Cmd.PARTITIONS_ARMED_SUPPRESSED,
    Cmd.PARTITIONS_ARMED,
    Cmd.PARTITIONS_ARMED_MODE_1,
    Cmd.PARTITIONS_ARMED_MODE_2,
    Cmd.PARTITIONS_ARMED_MODE_3,
    Cmd.PARTITIONS_ENTRY_TIME,
    Cmd.PARTITIONS_EXIT_TIME_LONG,
    Cmd.PARTITIONS_EXIT_TIME_SHORT,
    Cmd.PARTITIONS_ALARM,
    Cmd.PARTITIONS_FIRE_ALARM,
    Cmd.PARTITIONS_ALARM_MEMORY,
    Cmd.PARTITIONS_FIRE_ALARM_MEMORY,
    Cmd.OUTPUTS_STATE,
)

_DEFAULT_NAME_RE = re.compile(
    r"^(Wejście|Wyjście|Strefa|Zone|Output|Partition)\s+\d+$", re.IGNORECASE
)


@dataclass
class ZoneInfo:
    number: int
    name: str
    reaction: int
    partition: int | None = None

    @property
    def has_temperature(self) -> bool:
        return self.reaction == ZONE_REACTION_24H_LOW_TEMPERATURE


@dataclass
class OutputInfo:
    number: int
    name: str
    function: int


@dataclass
class PartitionInfo:
    number: int
    name: str


@dataclass
class Discovery:
    partitions: dict[int, PartitionInfo] = field(default_factory=dict)
    zones: dict[int, ZoneInfo] = field(default_factory=dict)
    outputs: dict[int, OutputInfo] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "partitions": {
                n: {"name": p.name} for n, p in self.partitions.items()
            },
            "zones": {
                n: {"name": z.name, "reaction": z.reaction, "partition": z.partition}
                for n, z in self.zones.items()
            },
            "outputs": {
                n: {"name": o.name, "function": o.function}
                for n, o in self.outputs.items()
            },
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Discovery":
        disc = cls()
        for n, p in data.get("partitions", {}).items():
            disc.partitions[int(n)] = PartitionInfo(int(n), p["name"])
        for n, z in data.get("zones", {}).items():
            disc.zones[int(n)] = ZoneInfo(
                int(n), z["name"], z["reaction"], z.get("partition")
            )
        for n, o in data.get("outputs", {}).items():
            disc.outputs[int(n)] = OutputInfo(int(n), o["name"], o["function"])
        return disc


StateListener = Callable[[], None]


class SatelHub:
    """Owns the client; exposes cached state + control to the HA layer."""

    def __init__(
        self,
        host: str,
        port: int,
        user_code: str = "",
        code_prefix: str = "",
        temperature_zones: list[int] | None = None,
    ) -> None:
        self.client = SatelClient(host, port, user_code, code_prefix)
        self.client.on_connection_lost = self._connection_lost
        self.discovery = Discovery()
        self.states: dict[int, set[int]] = {c: set() for c in MONITORED_COMMANDS}
        self.temperatures: dict[int, float] = {}
        self.available = False
        self._explicit_temp_zones = temperature_zones
        self._temp_rotation: list[int] = []
        self._temp_unsupported: set[int] = set()
        self._supervisor: asyncio.Task | None = None
        self._user_rights_logged = False
        self._monitor_wakeup = asyncio.Event()
        self._listeners: list[StateListener] = []
        self._stopping = False

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        self._stopping = False
        if self._supervisor is None or self._supervisor.done():
            self._supervisor = asyncio.ensure_future(self._supervise())

    async def stop(self) -> None:
        self._stopping = True
        if self._supervisor:
            self._supervisor.cancel()
            try:
                await self._supervisor
            except asyncio.CancelledError:
                pass
            self._supervisor = None
        await self.client.close()
        self._set_available(False)

    def subscribe(self, listener: StateListener) -> Callable[[], None]:
        self._listeners.append(listener)

        def _unsub() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return _unsub

    def _notify(self) -> None:
        for listener in list(self._listeners):
            try:
                listener()
            except Exception:  # noqa: BLE001 - a listener must not kill the loop
                _LOGGER.exception("state listener failed")

    def _set_available(self, value: bool) -> None:
        if self.available != value:
            self.available = value
            self._notify()

    def _connection_lost(self, exc: Exception | None) -> None:
        _LOGGER.warning("panel connection lost: %s", exc)
        self._set_available(False)
        self._monitor_wakeup.set()

    # ------------------------------------------------------------ supervisor

    async def _supervise(self) -> None:
        delay = RECONNECT_MIN
        loop = asyncio.get_event_loop()
        while not self._stopping:
            session_start = loop.time()
            try:
                await self.client.connect()
                await self._log_user_rights()
                if not self.discovery.outputs:
                    await self._discover()
                self._build_temp_rotation()
                await self._refresh_all()
                self._set_available(True)
                await self._monitor_loop()
            except asyncio.CancelledError:
                raise
            except PanelBusyError:
                _LOGGER.warning(
                    "integration port busy (another client, e.g. old "
                    "integration, is connected) — retrying in %ss", BUSY_RETRY
                )
                await self.client.close()
                self._set_available(False)
                await asyncio.sleep(BUSY_RETRY)
                continue
            except (SatelError, OSError) as err:
                _LOGGER.warning("connection/monitoring failed: %s", err)
            except Exception:  # noqa: BLE001 - the supervisor must survive
                _LOGGER.exception("unexpected error — reconnecting")
            finally:
                self._set_available(False)
                await self.client.close()
            if self._stopping:
                break
            # a session that stayed up resets the backoff; rapid failures grow it
            if loop.time() - session_start > 30.0:
                delay = RECONNECT_MIN
            _LOGGER.debug("reconnecting in %.0fs", delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_MAX)

    async def _log_user_rights(self) -> None:
        """One-time diagnostic: which partitions can our panel user control?"""
        if self._user_rights_logged or not self.client._user_code:
            return
        self._user_rights_logged = True
        try:
            info = await self.client.read_self_info()
        except (SatelError, ResponseTimeoutError) as err:
            _LOGGER.info("could not read panel-user rights (0xE0): %s", err)
            return
        _LOGGER.warning(
            "panel user #%d %r has access to partitions %s (raw 0xE0: %s) — "
            "arming any other partition is silently ignored by the panel",
            info["user_number"], info["name"], info["partitions"], info["raw"],
        )

    # ------------------------------------------------------------ discovery

    async def _discover(self) -> None:
        """Enumerate partitions, zones and outputs from the panel itself."""
        _LOGGER.info("discovering panel devices (one-time, takes ~1 min)")
        disc = Discovery()
        max_objects = 256 if self.client.panel.supports_32_bytes else 128
        # NOTE: NotConnectedError must abort the whole enumeration (and is NOT
        # swallowed below) — otherwise a mid-discovery disconnect would commit
        # and cache a truncated device list.
        for n in range(1, 33):
            try:
                _, name = await self.client.read_device_info(
                    DeviceType.PARTITION, n
                )
            except NotConnectedError:
                raise
            except (SatelError, ResponseTimeoutError):
                continue
            if name and not _DEFAULT_NAME_RE.match(name):
                disc.partitions[n] = PartitionInfo(n, name)
        for n in range(1, max_objects + 1):
            try:
                reaction, name, extra = await self._read_zone_info(n)
            except NotConnectedError:
                raise
            except (SatelError, ResponseTimeoutError):
                continue
            if name and not _DEFAULT_NAME_RE.match(name):
                disc.zones[n] = ZoneInfo(n, name, reaction, extra)
        for n in range(1, max_objects + 1):
            try:
                function, name = await self.client.read_device_info(
                    DeviceType.OUTPUT, n
                )
            except NotConnectedError:
                raise
            except (SatelError, ResponseTimeoutError):
                continue
            if function != OUTPUT_FUNC_UNUSED and name and not _DEFAULT_NAME_RE.match(name):
                disc.outputs[n] = OutputInfo(n, name, function)
        _LOGGER.info(
            "discovered %d partitions, %d zones, %d outputs",
            len(disc.partitions), len(disc.zones), len(disc.outputs),
        )
        self.discovery = disc

    async def _read_zone_info(self, number: int) -> tuple[int, str, int | None]:
        """Read zone reaction type, name and partition (device type 5)."""
        data = await self.client.command(
            Cmd.READ_DEVICE_INFO, bytes([DeviceType.ZONE_WITH_PARTITION, number & 0xFF])
        )
        if len(data) < 19:
            raise SatelError(f"malformed 0xEE answer: {data.hex()}")
        name = data[3:19].decode("cp1250", errors="replace").strip()
        partition = data[19] if len(data) > 19 else None
        return data[2], name, partition

    def _build_temp_rotation(self) -> None:
        if self._explicit_temp_zones is not None:
            zones = list(self._explicit_temp_zones)
        else:
            zones = [
                z.number for z in self.discovery.zones.values() if z.has_temperature
            ]
        self._temp_rotation = [z for z in zones if z not in self._temp_unsupported]

    # ------------------------------------------------------------ monitoring

    async def _refresh_all(self) -> None:
        for cmd in MONITORED_COMMANDS:
            await self._refresh_state(cmd)

    async def _refresh_state(self, cmd: int) -> bool:
        data = await self.client.state_command(cmd)
        current = bitmask_to_numbers(data)
        if current != self.states.get(cmd):
            self.states[cmd] = current
            return True
        return False

    async def _monitor_loop(self) -> None:
        temp_index = 0
        last_temp_read = 0.0
        last_full_refresh = 0.0
        loop = asyncio.get_event_loop()
        while self.client.connected:
            self._monitor_wakeup.clear()
            changed = False
            # periodic full resync: insurance against 0x7F bits the module
            # doesn't report (e.g. commands >= 0x28 on non-extended modules)
            if loop.time() - last_full_refresh >= FULL_REFRESH_INTERVAL:
                last_full_refresh = loop.time()
                for cmd in MONITORED_COMMANDS:
                    if await self._refresh_state(cmd):
                        changed = True
            new_data = await self.client.new_data()
            pending = {
                cmd for cmd in MONITORED_COMMANDS
                if cmd < len(new_data) * 8
                and new_data[cmd // 8] & (1 << (cmd % 8))
            }
            for cmd in pending:
                if await self._refresh_state(cmd):
                    changed = True
            now = loop.time()
            if self._temp_rotation and now - last_temp_read >= TEMP_READ_SPACING:
                last_temp_read = now
                temp_index %= len(self._temp_rotation)
                zone = self._temp_rotation[temp_index]
                temp_index += 1
                try:
                    value = await self.client.read_temperature(zone)
                except ResponseTimeoutError:
                    _LOGGER.info(
                        "zone %d does not answer temperature reads; skipping it",
                        zone,
                    )
                    self._temp_unsupported.add(zone)
                    self._build_temp_rotation()
                else:
                    if value is not None and self.temperatures.get(zone) != value:
                        self.temperatures[zone] = value
                        changed = True
            if changed:
                self._notify()
            try:
                await asyncio.wait_for(
                    self._monitor_wakeup.wait(), timeout=POLL_INTERVAL
                )
            except asyncio.TimeoutError:
                pass

    # ------------------------------------------------------------ accessors

    def output_active(self, number: int) -> bool:
        return number in self.states[Cmd.OUTPUTS_STATE]

    def zone_active(self, cmd: int, number: int) -> bool:
        return number in self.states.get(cmd, set())
