"""Satel INTEGRA integration protocol framing.

Pure functions: no I/O. Frame format (Satel "Integration protocol"):

    0xFE 0xFE | cmd | data... | crc_hi crc_lo | 0xFE 0x0D

Every 0xFE byte in cmd/data/crc is escaped as 0xFE 0xF0 on the wire.
"""
from __future__ import annotations

HEADER = b"\xFE\xFE"
FOOTER = b"\xFE\x0D"
ESCAPE = b"\xFE\xF0"

# Response command byte the panel uses to report errors / rejected commands.
CMD_RESULT = 0xEF


def checksum(payload: bytes) -> int:
    """Satel CRC over cmd+data bytes."""
    crc = 0x147A
    for byte in payload:
        crc = ((crc << 1) & 0xFFFF) | (crc >> 15)  # rotate left by 1
        crc ^= 0xFFFF
        crc = (crc + (crc >> 8) + byte) & 0xFFFF
    return crc


def encode_frame(cmd: int, data: bytes = b"") -> bytes:
    """Build a complete wire frame for a command."""
    payload = bytes([cmd]) + data
    crc = checksum(payload)
    body = payload + bytes([crc >> 8, crc & 0xFF])
    return HEADER + body.replace(b"\xFE", ESCAPE) + FOOTER


class FrameError(Exception):
    """Malformed or corrupt frame."""


class FrameDecoder:
    """Incremental decoder: feed raw socket bytes, yields (cmd, data) payloads.

    Never raises on corrupt input — it resyncs on the next header and counts
    dropped frames in `errors`.
    """

    MAX_BUFFER = 4096

    # _find_footer sentinel results
    _INCOMPLETE = -1

    def __init__(self) -> None:
        self._buf = bytearray()
        self.errors = 0

    def feed(self, chunk: bytes) -> list[tuple[int, bytes]]:
        self._buf += chunk
        if len(self._buf) > self.MAX_BUFFER:
            # runaway garbage; keep the tail in case a frame straddles it
            self.errors += 1
            del self._buf[: len(self._buf) - 256]
        frames: list[tuple[int, bytes]] = []
        while True:
            start = self._buf.find(HEADER)
            if start == -1:
                # no header: keep at most one trailing 0xFE that may start one
                if self._buf and self._buf[-1] == 0xFE:
                    del self._buf[:-1]
                else:
                    self._buf.clear()
                break
            # skip repeated 0xFE (header is "at least two 0xFE")
            body_start = start + 2
            while body_start < len(self._buf) and self._buf[body_start] == 0xFE:
                body_start += 1
            end, resync = self._find_footer(body_start)
            if end == self._INCOMPLETE:
                del self._buf[:start]
                break
            if resync:
                # unescaped 0xFE 0xFE mid-frame: corrupt, resync at new header
                self.errors += 1
                del self._buf[:end]
                continue
            raw = bytes(self._buf[body_start:end])
            del self._buf[: end + 2]
            body = raw.replace(ESCAPE, b"\xFE")
            if len(body) < 3:
                self.errors += 1
                continue
            payload, crc_hi, crc_lo = body[:-2], body[-2], body[-1]
            if checksum(payload) != (crc_hi << 8) | crc_lo:
                self.errors += 1
                continue
            frames.append((payload[0], payload[1:]))
        return frames

    def _find_footer(self, start: int) -> tuple[int, bool]:
        """Locate the frame footer starting at `start`.

        Returns (pos, resync): pos of the footer's 0xFE and resync=False on
        success; (pos_of_new_header, True) when an unescaped header interrupts
        the frame; (_INCOMPLETE, False) when more bytes are needed.
        """
        i = start
        buf = self._buf
        while i < len(buf):
            if buf[i] != 0xFE:
                i += 1
                continue
            if i + 1 >= len(buf):
                break  # need more data to interpret this 0xFE
            nxt = buf[i + 1]
            if nxt == 0x0D:
                return i, False
            if nxt == 0xFE:
                return i, True
            # 0xF0 escape or any other byte: consume the pair
            i += 2
        return self._INCOMPLETE, False


def bitmask_to_numbers(data: bytes) -> set[int]:
    """Decode a Satel bitmask (byte 0 bit 0 => object #1) into 1-based numbers."""
    result: set[int] = set()
    for i, byte in enumerate(data):
        if not byte:
            continue
        for j in range(8):
            if byte & (1 << j):
                result.add(i * 8 + j + 1)
    return result


def numbers_to_bitmask(numbers: set[int] | frozenset[int], length: int) -> bytes:
    """Encode 1-based object numbers into a bitmask of `length` bytes."""
    mask = bytearray(length)
    for n in numbers:
        if not 1 <= n <= length * 8:
            raise ValueError(f"object number {n} out of range for {length}-byte mask")
        mask[(n - 1) // 8] |= 1 << ((n - 1) % 8)
    return bytes(mask)
