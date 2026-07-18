"""Tests for the Satel wire framing layer (pure, no network)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "custom_components" / "satel_integra_plus"))

from pysatel.frames import (
    FrameDecoder,
    bitmask_to_numbers,
    checksum,
    encode_frame,
    numbers_to_bitmask,
)


# Known-good vector from the Satel protocol documentation:
# querying partitions-armed state (cmd 0x09, no data) frames as FE FE 09 D7 EB FE 0D
KNOWN_FRAMES = [
    (0x09, b"", bytes.fromhex("fefe09d7ebfe0d")),
    # version query 0x7E, no data (CRC computed by the documented algorithm)
    (0x7E, b"", bytes.fromhex("fefe7ed860fe0d")),
]


@pytest.mark.parametrize("cmd,data,wire", KNOWN_FRAMES)
def test_encode_known_vectors(cmd, data, wire):
    assert encode_frame(cmd, data) == wire


@pytest.mark.parametrize("cmd,data,wire", KNOWN_FRAMES)
def test_decode_known_vectors(cmd, data, wire):
    dec = FrameDecoder()
    assert dec.feed(wire) == [(cmd, data)]
    assert dec.errors == 0


def test_roundtrip_with_escaping():
    """Payloads containing 0xFE must survive escaping both ways."""
    dec = FrameDecoder()
    for data in (b"\xfe", b"\xfe\xfe\xfe", b"\x00\xfe\x0d", b"\xfe\xf0", bytes(range(256))):
        frames = dec.feed(encode_frame(0x17, data))
        assert frames == [(0x17, data)]
    assert dec.errors == 0


def test_decoder_handles_fragmentation():
    wire = encode_frame(0x00, bytes(32))
    dec = FrameDecoder()
    collected = []
    for i in range(len(wire)):
        collected += dec.feed(wire[i : i + 1])
    assert collected == [(0x00, bytes(32))]


def test_decoder_resyncs_after_garbage():
    dec = FrameDecoder()
    wire = encode_frame(0x0A, b"\x01\x02\x03\x04")
    frames = dec.feed(b"\x10Busy!\r\n\xa5\xa5" + wire + b"\xff\xff" + wire)
    assert frames == [(0x0A, b"\x01\x02\x03\x04")] * 2


def test_decoder_drops_bad_crc_and_continues():
    good = encode_frame(0x17, b"\x01")
    bad = bytearray(encode_frame(0x17, b"\x02"))
    bad[3] ^= 0xFF  # corrupt data byte
    dec = FrameDecoder()
    frames = dec.feed(bytes(bad) + good)
    assert frames == [(0x17, b"\x01")]
    assert dec.errors == 1


def test_multiple_frames_single_chunk():
    dec = FrameDecoder()
    wire = encode_frame(0x00, b"\x01") + encode_frame(0x01, b"\x02") + encode_frame(0x02, b"")
    assert [c for c, _ in dec.feed(wire)] == [0x00, 0x01, 0x02]


def test_bitmask_roundtrip():
    numbers = {1, 8, 9, 100, 245, 256}
    mask = numbers_to_bitmask(numbers, 32)
    assert len(mask) == 32
    assert bitmask_to_numbers(mask) == numbers
    # satel bit order: object #1 is byte 0 bit 0
    assert numbers_to_bitmask({1}, 32)[0] == 0x01
    assert numbers_to_bitmask({8}, 32)[0] == 0x80
    assert numbers_to_bitmask({9}, 32)[1] == 0x01


def test_bitmask_range_check():
    with pytest.raises(ValueError):
        numbers_to_bitmask({129}, 16)
