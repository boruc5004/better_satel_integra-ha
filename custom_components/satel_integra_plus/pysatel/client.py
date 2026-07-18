"""Asyncio client for the Satel INTEGRA integration protocol (ETHM-1 / ETHM-1 Plus).

Reliability rules baked in (per Satel spec and field experience):

- exactly ONE outstanding command at a time; the next frame is sent only after
  the previous answer arrived (or timed out),
- the panel drops idle connections after ~25 s -> built-in keepalive,
- the integration port serves ONE client; a second client gets an ASCII
  "Busy!" banner -> surfaced as :class:`PanelBusyError`,
- output control requests are coalesced into a single bitmask command, so
  e.g. a Home Assistant cover-group action moves ALL covers with one frame.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from .const import (
    Cmd,
    EXTENDED_STATE_CMDS,
    INTEGRA_256_PLUS,
    INTEGRA_TYPES,
    RESULT_ACCEPTED,
    RESULT_ANSWERED_CMDS,
    RESULT_MESSAGES,
    RESULT_OK,
    encode_user_code,
)
from .frames import FrameDecoder, encode_frame

_LOGGER = logging.getLogger(__name__)

COMMAND_TIMEOUT = 5.0
# 0x7D (zone temperature) answers may take up to 5 s per spec
TEMPERATURE_TIMEOUT = 8.0
KEEPALIVE_INTERVAL = 10.0
# window during which parallel output-control requests merge into one frame
BATCH_WINDOW = 0.05


class SatelError(Exception):
    """Base error."""


class NotConnectedError(SatelError):
    """No live connection to the panel."""


class PanelBusyError(SatelError):
    """Another client occupies the integration port."""


class ResponseTimeoutError(SatelError):
    """Panel did not answer in time."""


class CommandRefusedError(SatelError):
    """Panel answered 0xEF with an error code."""

    def __init__(self, code: int) -> None:
        self.code = code
        super().__init__(f"panel refused command: 0x{code:02X} "
                         f"({RESULT_MESSAGES.get(code, 'unknown')})")


@dataclass
class PanelInfo:
    integra_type: int = -1
    model: str = "unknown"
    version: str = ""
    language: int = 0
    module_version: str = ""
    supports_32_bytes: bool = False

    @property
    def object_mask_len(self) -> int:
        """Bitmask length for zones/outputs commands."""
        return 32 if self.supports_32_bytes else 16


@dataclass
class _PendingBatch:
    action: int
    outputs: set[int] = field(default_factory=set)
    futures: list[asyncio.Future] = field(default_factory=list)


class SatelClient:
    """Single-connection protocol client. Reconnection is the owner's job."""

    def __init__(
        self,
        host: str,
        port: int,
        user_code: str = "",
        code_prefix: str = "",
    ) -> None:
        self._host = host
        self._port = port
        self._user_code = user_code
        self._code_prefix = code_prefix
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task | None = None
        self._keepalive_task: asyncio.Task | None = None
        self._decoder = FrameDecoder()
        self._cmd_lock = asyncio.Lock()
        self._response: asyncio.Future | None = None
        self._response_cmd: int | None = None
        self._response_match: Callable[[bytes], bool] | None = None
        self._got_valid_frame = False
        self._last_io = 0.0
        self._batches: list[_PendingBatch] = []
        self._batch_flusher: asyncio.Task | None = None
        self.panel = PanelInfo()
        self.on_unsolicited: Callable[[int, bytes], None] | None = None
        self.on_connection_lost: Callable[[Exception | None], None] | None = None

    # ------------------------------------------------------------ lifecycle

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def connect(self) -> None:
        """Open the connection and identify the panel."""
        if self.connected:
            return
        self._decoder = FrameDecoder()
        self._got_valid_frame = False
        self._reader, self._writer = await asyncio.open_connection(
            self._host, self._port
        )
        self._reader_task = asyncio.ensure_future(self._read_loop())
        try:
            await self._identify()
        except BaseException:
            await self.close()
            raise
        self._keepalive_task = asyncio.ensure_future(self._keepalive_loop())
        _LOGGER.info(
            "connected to %s (%s, fw %s, ext=%s)",
            self._host, self.panel.model, self.panel.version,
            self.panel.supports_32_bytes,
        )

    async def close(self) -> None:
        for task in (self._keepalive_task, self._batch_flusher):
            if task:
                task.cancel()
        self._keepalive_task = self._batch_flusher = None
        writer, self._writer = self._writer, None
        self._reader = None
        if self._reader_task:
            self._reader_task.cancel()
            self._reader_task = None
        if writer:
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, asyncio.CancelledError):
                pass
        self._fail_pending(NotConnectedError("connection closed"))

    async def _identify(self) -> None:
        data = await self.command(Cmd.INTEGRA_VERSION)
        if len(data) < 13:
            raise SatelError(f"malformed 0x7E answer: {data.hex()}")
        self.panel.integra_type = data[0]
        self.panel.model = INTEGRA_TYPES.get(data[0], f"unknown ({data[0]})")
        raw = data[1:12].decode("ascii", errors="replace")
        self.panel.version = f"{raw[0]}.{raw[1:3]} {raw[3:7]}-{raw[7:9]}-{raw[9:11]}"
        self.panel.language = data[12] if len(data) > 12 else 0
        try:
            mod = await self.command(Cmd.MODULE_VERSION)
            self.panel.module_version = mod[:11].decode("ascii", errors="replace")
            module_ext = bool(len(mod) > 11 and mod[11] & 0x01)
        except ResponseTimeoutError:
            # pre-2013 modules don't implement 0x7C
            module_ext = False
        self.panel.supports_32_bytes = (
            module_ext and self.panel.integra_type == INTEGRA_256_PLUS
        )

    # ------------------------------------------------------------ I/O core

    async def _read_loop(self) -> None:
        exc: Exception | None = None
        try:
            assert self._reader is not None
            while True:
                chunk = await self._reader.read(1024)
                if not chunk:
                    exc = ConnectionResetError("connection closed by panel")
                    break
                self._last_io = asyncio.get_event_loop().time()
                if not self._got_valid_frame and b"Busy" in chunk:
                    exc = PanelBusyError(
                        "integration port busy (another client connected)"
                    )
                    break
                for cmd, data in self._decoder.feed(chunk):
                    self._got_valid_frame = True
                    self._dispatch(cmd, data)
        except asyncio.CancelledError:
            return
        except OSError as err:
            exc = err
        self._fail_pending(exc or NotConnectedError("connection lost"))
        if self._writer is not None:
            # unexpected loss (not initiated by close())
            self._writer.close()
            self._writer = None
            if self.on_connection_lost:
                self.on_connection_lost(exc)

    def _dispatch(self, cmd: int, data: bytes) -> None:
        fut, wanted, match = self._response, self._response_cmd, self._response_match
        # A 0xEF frame answers the pending command when either (a) the pending
        # command is a control command (0xEF is its normal answer), or (b) the
        # 0xEF carries an ERROR code — the panel refuses reads this way too
        # (e.g. 0xEE for a nonexistent device answers 0xEF 0x08).  A *success*
        # 0xEF is never accepted for reads, so a late success result from a
        # timed-out control command cannot corrupt a pending state read.
        accepts_result = wanted in RESULT_ANSWERED_CMDS or (
            len(data) >= 1 and data[0] not in (RESULT_OK, RESULT_ACCEPTED)
        )
        if (
            fut
            and not fut.done()
            and (
                (cmd == Cmd.RESULT and accepts_result)
                or (cmd == wanted and (match is None or match(data)))
            )
        ):
            fut.set_result((cmd, data))
            return
        try:
            if self.on_unsolicited:
                self.on_unsolicited(cmd, data)
            else:
                _LOGGER.debug("unsolicited frame 0x%02X %s", cmd, data.hex())
        except Exception:  # noqa: BLE001 - a callback must never kill the reader
            _LOGGER.exception("unsolicited-frame callback failed")

    def _fail_pending(self, exc: Exception) -> None:
        if self._response and not self._response.done():
            self._response.set_exception(exc)
        for batch in self._batches:
            for fut in batch.futures:
                if not fut.done():
                    fut.set_exception(exc)
        self._batches.clear()

    async def command(
        self,
        cmd: int,
        data: bytes = b"",
        timeout: float = COMMAND_TIMEOUT,
        match: Callable[[bytes], bool] | None = None,
    ) -> bytes:
        """Send one command and return the answer payload.

        Control commands get their 0xEF result checked; state reads return
        the raw state bytes. `match` adds payload-level correlation (e.g.
        the zone number echoed in a 0x7D answer) so a late reply to an
        earlier, timed-out command can never satisfy the wrong request.
        """
        async with self._cmd_lock:
            if not self.connected:
                raise NotConnectedError
            loop = asyncio.get_event_loop()
            self._response = loop.create_future()
            self._response_cmd = cmd
            self._response_match = match
            try:
                assert self._writer is not None
                self._writer.write(encode_frame(cmd, data))
                await self._writer.drain()
                self._last_io = loop.time()
                try:
                    resp_cmd, resp_data = await asyncio.wait_for(
                        self._response, timeout
                    )
                except asyncio.TimeoutError:
                    raise ResponseTimeoutError(
                        f"no answer to 0x{cmd:02X} within {timeout}s"
                    ) from None
            finally:
                self._response = None
                self._response_cmd = None
                self._response_match = None
        if resp_cmd == Cmd.RESULT and cmd != Cmd.RESULT:
            code = resp_data[0] if resp_data else 0x08
            if code in (RESULT_OK, RESULT_ACCEPTED):
                return resp_data
            raise CommandRefusedError(code)
        return resp_data

    async def state_command(self, cmd: int) -> bytes:
        """State read, using the extended (32-byte) form when supported."""
        extended = self.panel.supports_32_bytes and cmd in EXTENDED_STATE_CMDS
        return await self.command(cmd, b"\x00" if extended else b"")

    async def new_data(self) -> bytes:
        """0x7F changed-state bitmap (5 or 6 bytes)."""
        return await self.command(
            Cmd.NEW_DATA, b"\x00" if self.panel.supports_32_bytes else b""
        )

    async def read_temperature(self, zone: int) -> float | None:
        """Read zone temperature in deg C; None when not yet determined.

        Raises ResponseTimeoutError for zones without temperature capability
        (the panel never answers those).
        """
        from .const import decode_temperature

        wire_zone = zone & 0xFF  # zone 256 is sent as 0
        data = await self.command(
            Cmd.ZONE_TEMPERATURE,
            bytes([wire_zone]),
            timeout=TEMPERATURE_TIMEOUT,
            match=lambda d: len(d) >= 3 and d[0] == wire_zone,
        )
        if len(data) < 3:
            raise SatelError(f"malformed 0x7D answer: {data.hex()}")
        return decode_temperature((data[1] << 8) | data[2])

    async def read_device_info(
        self, device_type: int, number: int
    ) -> tuple[int, str]:
        """0xEE: return (type/function byte, name) of a device.

        Zone/output number 256 is sent as 0 per spec.
        """
        wire_num = number & 0xFF
        data = await self.command(
            Cmd.READ_DEVICE_INFO,
            bytes([device_type, wire_num]),
            match=lambda d: len(d) >= 2 and d[0] == device_type and d[1] == wire_num,
        )
        if len(data) < 19:
            raise SatelError(f"malformed 0xEE answer: {data.hex()}")
        name = data[3:19].decode("cp1250", errors="replace").strip()
        return data[2], name

    # ------------------------------------------------------------ control

    def _code_field(self) -> bytes:
        if not self._user_code:
            raise SatelError("a user code is required for control commands")
        return encode_user_code(self._user_code, self._code_prefix)

    async def read_self_info(self) -> dict:
        """0xE0: read the connecting user's own record (rights diagnostics).

        Returns {'user_number', 'partitions', 'name'}. Requires the user to
        have the "GuardX using" right; otherwise the panel refuses (0x01).
        """
        from .frames import bitmask_to_numbers

        data = await self.command(Cmd.READ_SELF_INFO, self._code_field())
        if len(data) < 27:
            raise SatelError(f"malformed 0xE0 answer: {data.hex()}")
        return {
            "user_number": data[0],
            "partitions": sorted(bitmask_to_numbers(data[3:7])),
            "name": data[10:26].decode("cp1250", errors="replace").strip(),
            "raw": data.hex(),
        }

    async def arm(
        self,
        partition: int,
        mode: int = 0,
        force: bool = False,
        fallback_force: bool = False,
    ) -> None:
        """Arm a partition.

        With `fallback_force`, a panel refusal 0x11 ("cannot arm, but can use
        force arm" — e.g. violated zones in an outdoor partition) is retried
        as a force-arm automatically.
        """
        from .const import RESULT_CANNOT_ARM_USE_FORCE
        from .frames import numbers_to_bitmask

        base = Cmd.FORCE_ARM_MODE_0 if force else Cmd.ARM_MODE_0
        _LOGGER.info(
            "arming partition %d (mode %d, force=%s)", partition, mode, force
        )
        try:
            result = await self.command(
                base + mode, self._code_field() + numbers_to_bitmask({partition}, 4)
            )
            _LOGGER.info(
                "arm partition %d: panel answered 0x%02X",
                partition, result[0] if result else 0,
            )
        except CommandRefusedError as err:
            if (
                not force
                and fallback_force
                and err.code == RESULT_CANNOT_ARM_USE_FORCE
            ):
                _LOGGER.info(
                    "partition %d refused regular arm (violated zones) — "
                    "force-arming", partition,
                )
                await self.arm(partition, mode=mode, force=True)
            else:
                raise

    async def disarm(self, partition: int) -> None:
        from .frames import numbers_to_bitmask

        await self.command(
            Cmd.DISARM, self._code_field() + numbers_to_bitmask({partition}, 4)
        )

    async def clear_alarm(self, partition: int) -> None:
        from .frames import numbers_to_bitmask

        await self.command(
            Cmd.CLEAR_ALARM, self._code_field() + numbers_to_bitmask({partition}, 4)
        )

    async def control_outputs(self, action: int, outputs: set[int]) -> None:
        """Turn outputs on/off (0x88/0x89), coalescing concurrent requests.

        All requests for the same action arriving within BATCH_WINDOW are
        merged into ONE bitmask frame — a cover-group action becomes a single
        wire command instead of N racing ones.
        """
        if action not in (Cmd.OUTPUTS_ON, Cmd.OUTPUTS_OFF, Cmd.OUTPUTS_SWITCH):
            raise ValueError(f"not an output control command: {action:#x}")
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        # merge into the last pending batch if it has the same action
        if self._batches and self._batches[-1].action == action:
            batch = self._batches[-1]
        else:
            batch = _PendingBatch(action=action)
            self._batches.append(batch)
        batch.outputs |= outputs
        batch.futures.append(fut)
        if self._batch_flusher is None or self._batch_flusher.done():
            self._batch_flusher = asyncio.ensure_future(self._flush_batches())
        await fut

    async def _flush_batches(self) -> None:
        from .frames import numbers_to_bitmask

        try:
            await asyncio.sleep(BATCH_WINDOW)
            while self._batches:
                # keep the batch in the list until resolved so close()'s
                # _fail_pending can always reach it
                batch = self._batches[0]
                error: Exception | None = None
                try:
                    mask = numbers_to_bitmask(
                        batch.outputs, self.panel.object_mask_len
                    )
                    await self.command(batch.action, self._code_field() + mask)
                except Exception as err:  # noqa: BLE001 - propagate to awaiters
                    error = err
                if self._batches and self._batches[0] is batch:
                    self._batches.pop(0)
                for fut in batch.futures:
                    if not fut.done():
                        if error is None:
                            fut.set_result(None)
                        else:
                            fut.set_exception(error)
        except BaseException as exc:  # incl. CancelledError from close()
            fail = (
                exc
                if isinstance(exc, Exception)
                else NotConnectedError("connection closing")
            )
            self._fail_pending(fail)
            raise

    # ------------------------------------------------------------ keepalive

    async def _keepalive_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(2.0)
                loop = asyncio.get_event_loop()
                if loop.time() - self._last_io < KEEPALIVE_INTERVAL:
                    continue
                try:
                    await self.command(Cmd.RTC_AND_STATUS)
                except (NotConnectedError, OSError):
                    return  # reader loop handles the teardown
                except ResponseTimeoutError:
                    # half-open TCP session: the panel is gone but the socket
                    # looks alive — force a teardown so the owner reconnects
                    _LOGGER.warning("keepalive unanswered — dropping connection")
                    writer, self._writer = self._writer, None
                    if writer:
                        writer.close()
                    if self.on_connection_lost:
                        self.on_connection_lost(
                            ResponseTimeoutError("keepalive unanswered")
                        )
                    return
        except asyncio.CancelledError:
            pass
