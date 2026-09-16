from __future__ import annotations

import struct

from tlmtry_collector.protocol import (
    Command,
    Frame,
    FrameDecoder,
    MetricType,
    build_discover,
    build_ping,
    build_read,
    crc16_modbus,
    parse_discover_ack,
    parse_read_ack,
)


def test_crc16_modbus_known_vector():
    # Empty body CRC is 0xFFFF with no bytes; one common vector: bytes [0x01,0x03,0x00,0x00,0x00,0x0A]
    data = bytes([0x01, 0x03, 0x00, 0x00, 0x00, 0x0A])
    assert crc16_modbus(data) == 0xCDC5


def test_frame_roundtrip():
    frame = Frame(addr=7, cmd=Command.PING, payload=b"")
    encoded = frame.encode()
    assert encoded[0] == 0xAA
    dec = FrameDecoder()
    out = dec.feed(encoded)
    assert len(out) == 1
    assert out[0].addr == 7
    assert out[0].cmd == Command.PING


def test_frame_decoder_rejects_bad_crc():
    frame = Frame(addr=1, cmd=Command.READ).encode()
    bad = bytearray(frame)
    bad[-1] ^= 0xFF
    assert FrameDecoder().feed(bytes(bad)) == []


def test_discover_ack_parse():
    # Build a minimal manifest payload
    payload = bytearray()
    payload += struct.pack("<H", 42)
    payload += bytes([1, 2])  # fw
    payload += bytes([4]) + b"node"
    payload += struct.pack("<I", 2500)
    payload.append(1)
    payload.append(0)  # metric id
    payload.append(int(MetricType.FLOAT))
    payload += bytes([1]) + b"C"
    payload += bytes([6]) + b"temp_c"
    m = parse_discover_ack(bytes(payload), address=3)
    assert m.device_id == 42
    assert m.name == "node"
    assert m.poll_interval_ms == 2500
    assert m.metrics[0].name == "temp_c"
    assert m.address == 3


def test_read_ack_parse_mixed():
    payload = bytearray()
    payload.append(3)
    payload += bytes([0, int(MetricType.INT32)]) + struct.pack("<i", -5)
    payload += bytes([1, int(MetricType.FLOAT)]) + struct.pack("<f", 12.5)
    payload += bytes([2, int(MetricType.BOOL)]) + bytes([1])
    samples = parse_read_ack(bytes(payload))
    assert samples[0].value == -5
    assert abs(samples[1].value - 12.5) < 1e-5
    assert samples[2].value is True


def test_builders():
    assert build_discover(0).cmd == Command.DISCOVER
    assert build_read(5).addr == 5
    assert build_ping(5).cmd == Command.PING
