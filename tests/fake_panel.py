"""A fake INTEGRA 256 Plus speaking the integration protocol, for tests."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "custom_components" / "satel_integra_plus"))

from pysatel.const import Cmd
from pysatel.frames import FrameDecoder, encode_frame, numbers_to_bitmask

BUSY_BANNER = b"\x10Busy!\r\n\xa5\xa5\xa5\xa5\xa5\xa5\xa5\xa5"


class FakePanel:
    """Configurable panel simulator: INTEGRA 256 Plus + ETHM-1 Plus."""

    def __init__(self) -> None:
        self.server: asyncio.Server | None = None
        self.port = 0
        self.busy = False
        self.clients = 0
        self.received: list[tuple[int, bytes]] = []
        self.violated_zones: set[int] = set()
        self.active_outputs: set[int] = set()
        self.armed_partitions: set[int] = set()
        self.temperatures: dict[int, float] = {}  # zone -> deg C
        self.temp_delay = 0.0
        self.new_data_bits: set[int] = set()
        self.names: dict[tuple[int, int], tuple[int, str]] = {}
        # (devtype, number) pairs the panel refuses with 0xEF 0x08, like a
        # real INTEGRA answers 0xEE for nonexistent devices
        self.error_devices: set[tuple[int, int]] = set()
        # simulate violated zones: regular arm answers 0xEF 0x11, force works
        self.refuse_arm_without_force = False
        self.drop_after: int | None = None  # close connection after N frames
        self.ignore_cmds: set[int] = set()  # simulate lost replies
        self.answer_delay = 0.0
        self._frames_seen = 0

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self.server:
            self.server.close()
            await self.server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self.busy or self.clients >= 1:
            writer.write(BUSY_BANNER)
            await writer.drain()
            writer.close()
            return
        self.clients += 1
        decoder = FrameDecoder()
        try:
            while True:
                chunk = await reader.read(1024)
                if not chunk:
                    break
                for cmd, data in decoder.feed(chunk):
                    self.received.append((cmd, data))
                    self._frames_seen += 1
                    if self.drop_after is not None and self._frames_seen > self.drop_after:
                        return
                    if cmd in self.ignore_cmds:
                        continue
                    if self.answer_delay:
                        await asyncio.sleep(self.answer_delay)
                    reply = self._answer(cmd, data)
                    if reply is not None:
                        writer.write(reply)
                        await writer.drain()
        except (ConnectionResetError, asyncio.IncompleteReadError):
            pass
        finally:
            self.clients -= 1
            writer.close()

    def _answer(self, cmd: int, data: bytes) -> bytes | None:
        ext = len(data) >= 1  # extended variant requested
        if cmd == Cmd.INTEGRA_VERSION:
            payload = bytes([72]) + b"12120200101" + bytes([0]) + b"\xff"
            return encode_frame(cmd, payload)
        if cmd == Cmd.MODULE_VERSION:
            return encode_frame(cmd, b"21320201111" + bytes([0x01]))
        if cmd == Cmd.RTC_AND_STATUS:
            return encode_frame(cmd, bytes(9))
        if cmd == Cmd.NEW_DATA:
            width = 6 if ext else 5
            mask = bytearray(width)
            for bit in self.new_data_bits:
                if bit < width * 8:
                    mask[bit // 8] |= 1 << (bit % 8)
            self.new_data_bits.clear()
            return encode_frame(cmd, bytes(mask))
        if cmd == Cmd.ZONES_VIOLATION:
            return encode_frame(cmd, numbers_to_bitmask(self.violated_zones, 32 if ext else 16))
        if cmd == Cmd.OUTPUTS_STATE:
            return encode_frame(cmd, numbers_to_bitmask(self.active_outputs, 32 if ext else 16))
        if cmd in (
            Cmd.ZONES_TAMPER, Cmd.ZONES_ALARM, Cmd.ZONES_ALARM_MEMORY, Cmd.ZONES_BYPASS,
        ):
            return encode_frame(cmd, bytes(32 if ext else 16))
        if Cmd.PARTITIONS_ARMED_SUPPRESSED <= cmd <= Cmd.PARTITIONS_FIRE_ALARM_MEMORY or cmd in (
            Cmd.PARTITIONS_ARMED_MODE_1,
        ):
            active = self.armed_partitions if cmd == Cmd.PARTITIONS_ARMED else set()
            return encode_frame(cmd, numbers_to_bitmask(active, 4))
        if cmd == Cmd.ZONE_TEMPERATURE:
            zone = data[0] or 256
            if zone not in self.temperatures:
                return None  # panel never answers non-temperature zones
            raw = int(self.temperatures[zone] * 2 + 110)
            return encode_frame(cmd, bytes([data[0], raw >> 8, raw & 0xFF]))
        if cmd == Cmd.READ_SELF_INFO:
            # user 3, tel code 0, partitions 1+2, type/rights, name, object
            payload = bytes([3, 0, 0]) + bytes([0x03, 0, 0, 0]) + bytes([0, 0, 0])
            payload += "HomeAssistant".encode("cp1250").ljust(16)[:16] + bytes([1])
            return encode_frame(cmd, payload)
        if cmd == Cmd.READ_DEVICE_INFO:
            devtype, num = data[0], data[1]
            if (devtype, num) in self.error_devices:
                return encode_frame(Cmd.RESULT, b"\x08")
            func, name = self.names.get((devtype, num), (0, ""))
            if not name:
                defaults = {0: "Strefa", 1: "Wejście", 4: "Wyjście", 5: "Wejście"}
                name = f"{defaults.get(devtype, 'Obiekt')} {num or 256}"
            payload = bytes([devtype, num, func]) + name.encode("cp1250").ljust(16)[:16]
            if devtype == 5:
                payload += bytes([1])  # partition assignment
            return encode_frame(cmd, payload)
        if cmd in (Cmd.OUTPUTS_ON, Cmd.OUTPUTS_OFF, Cmd.OUTPUTS_SWITCH):
            mask = data[8:]
            outs = set()
            for i, byte in enumerate(mask):
                for j in range(8):
                    if byte & (1 << j):
                        outs.add(i * 8 + j + 1)
            if cmd == Cmd.OUTPUTS_ON:
                self.active_outputs |= outs
            elif cmd == Cmd.OUTPUTS_OFF:
                self.active_outputs -= outs
            else:
                self.active_outputs ^= outs
            self.new_data_bits.add(Cmd.OUTPUTS_STATE)
            return encode_frame(Cmd.RESULT, b"\xff")
        if cmd in (Cmd.ARM_MODE_0, Cmd.ARM_MODE_1, Cmd.ARM_MODE_2, Cmd.ARM_MODE_3) and self.refuse_arm_without_force:
            return encode_frame(Cmd.RESULT, b"\x11")
        if cmd in (
            Cmd.ARM_MODE_0, Cmd.ARM_MODE_1, Cmd.ARM_MODE_2, Cmd.ARM_MODE_3,
            Cmd.FORCE_ARM_MODE_0, Cmd.FORCE_ARM_MODE_1,
            Cmd.FORCE_ARM_MODE_2, Cmd.FORCE_ARM_MODE_3,
        ):
            for i, byte in enumerate(data[8:12]):
                for j in range(8):
                    if byte & (1 << j):
                        self.armed_partitions.add(i * 8 + j + 1)
            self.new_data_bits.add(Cmd.PARTITIONS_ARMED)
            return encode_frame(Cmd.RESULT, b"\x00")
        if cmd == Cmd.DISARM:
            for i, byte in enumerate(data[8:12]):
                for j in range(8):
                    if byte & (1 << j):
                        self.armed_partitions.discard(i * 8 + j + 1)
            self.new_data_bits.add(Cmd.PARTITIONS_ARMED)
            return encode_frame(Cmd.RESULT, b"\x00")
        return encode_frame(Cmd.RESULT, b"\x08")
