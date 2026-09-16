"""CRC-16/Modbus and Tlmtry frame encode/decode."""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

SOF = 0xAA
PROTOCOL_VERSION = 0x01
ADDR_BROADCAST = 0
MAX_PAYLOAD = 256


class Command(IntEnum):
    DISCOVER = 0x01
    DISCOVER_ACK = 0x81
    READ = 0x02
    READ_ACK = 0x82
    PING = 0x03
    PONG = 0x83
    NACK = 0x7F


class MetricType(IntEnum):
    INT32 = 0
    FLOAT = 1
    BOOL = 2
    UINT16 = 3


class NackError(IntEnum):
    UNKNOWN_CMD = 1
    BAD_PAYLOAD = 2
    BUSY = 3
    INTERNAL = 4


def crc16_modbus(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc & 0xFFFF


@dataclass(frozen=True)
class Frame:
    addr: int
    cmd: int
    payload: bytes = b""
    ver: int = PROTOCOL_VERSION

    def encode(self) -> bytes:
        if len(self.payload) > MAX_PAYLOAD:
            raise ValueError("payload too large")
        body = bytes(
            [
                self.ver & 0xFF,
                self.addr & 0xFF,
                self.cmd & 0xFF,
                len(self.payload) & 0xFF,
                (len(self.payload) >> 8) & 0xFF,
            ]
        ) + self.payload
        crc = crc16_modbus(body)
        return bytes([SOF]) + body + struct.pack("<H", crc)


class FrameDecoder:
    """Incremental frame parser for a serial byte stream."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def reset(self) -> None:
        self._buf.clear()

    def feed(self, data: bytes) -> list[Frame]:
        self._buf.extend(data)
        frames: list[Frame] = []
        while True:
            frame = self._try_extract()
            if frame is None:
                break
            frames.append(frame)
        return frames

    def _try_extract(self) -> Frame | None:
        while self._buf and self._buf[0] != SOF:
            del self._buf[0]
        if len(self._buf) < 8:  # SOF+ver+addr+cmd+len(2)+crc(2) min empty
            return None
        length = self._buf[4] | (self._buf[5] << 8)
        if length > MAX_PAYLOAD:
            del self._buf[0]
            return None
        total = 1 + 5 + length + 2
        if len(self._buf) < total:
            return None
        raw = bytes(self._buf[:total])
        del self._buf[:total]
        body = raw[1 : 1 + 5 + length]
        got_crc = struct.unpack_from("<H", raw, 1 + 5 + length)[0]
        if crc16_modbus(body) != got_crc:
            return None
        ver, addr, cmd = body[0], body[1], body[2]
        payload = body[5:]
        if ver != PROTOCOL_VERSION:
            return None
        return Frame(addr=addr, cmd=cmd, payload=payload, ver=ver)


@dataclass
class MetricInfo:
    metric_id: int
    type: MetricType
    unit: str
    name: str


@dataclass
class DeviceManifest:
    device_id: int
    fw_major: int
    fw_minor: int
    name: str
    poll_interval_ms: int
    metrics: list[MetricInfo] = field(default_factory=list)
    address: int = 0


@dataclass
class SampleValue:
    metric_id: int
    type: MetricType
    value: Any


def _read_str(buf: bytes, offset: int) -> tuple[str, int]:
    if offset >= len(buf):
        raise ValueError("truncated string")
    n = buf[offset]
    offset += 1
    end = offset + n
    if end > len(buf):
        raise ValueError("truncated string body")
    return buf[offset:end].decode("utf-8", errors="replace"), end


def parse_discover_ack(payload: bytes, address: int = 0) -> DeviceManifest:
    offset = 0
    if len(payload) < 8:
        raise ValueError("discover_ack too short")
    device_id = struct.unpack_from("<H", payload, offset)[0]
    offset += 2
    fw_major = payload[offset]
    fw_minor = payload[offset + 1]
    offset += 2
    name, offset = _read_str(payload, offset)
    if offset + 5 > len(payload):
        raise ValueError("discover_ack truncated interval")
    poll_interval_ms = struct.unpack_from("<I", payload, offset)[0]
    offset += 4
    count = payload[offset]
    offset += 1
    metrics: list[MetricInfo] = []
    for _ in range(count):
        if offset + 2 > len(payload):
            raise ValueError("metric header truncated")
        metric_id = payload[offset]
        mtype = MetricType(payload[offset + 1])
        offset += 2
        unit, offset = _read_str(payload, offset)
        mname, offset = _read_str(payload, offset)
        metrics.append(
            MetricInfo(metric_id=metric_id, type=mtype, unit=unit, name=mname)
        )
    return DeviceManifest(
        device_id=device_id,
        fw_major=fw_major,
        fw_minor=fw_minor,
        name=name,
        poll_interval_ms=poll_interval_ms,
        metrics=metrics,
        address=address,
    )


def parse_read_ack(payload: bytes) -> list[SampleValue]:
    if not payload:
        raise ValueError("empty read_ack")
    count = payload[0]
    offset = 1
    samples: list[SampleValue] = []
    for _ in range(count):
        if offset + 2 > len(payload):
            raise ValueError("sample header truncated")
        metric_id = payload[offset]
        mtype = MetricType(payload[offset + 1])
        offset += 2
        if mtype == MetricType.INT32:
            if offset + 4 > len(payload):
                raise ValueError("i32 truncated")
            value = struct.unpack_from("<i", payload, offset)[0]
            offset += 4
        elif mtype == MetricType.FLOAT:
            if offset + 4 > len(payload):
                raise ValueError("f32 truncated")
            value = struct.unpack_from("<f", payload, offset)[0]
            offset += 4
        elif mtype == MetricType.BOOL:
            if offset + 1 > len(payload):
                raise ValueError("bool truncated")
            value = bool(payload[offset])
            offset += 1
        elif mtype == MetricType.UINT16:
            if offset + 2 > len(payload):
                raise ValueError("u16 truncated")
            value = struct.unpack_from("<H", payload, offset)[0]
            offset += 2
        else:
            raise ValueError(f"unknown metric type {mtype}")
        samples.append(SampleValue(metric_id=metric_id, type=mtype, value=value))
    return samples


def build_discover(addr: int = ADDR_BROADCAST) -> Frame:
    return Frame(addr=addr, cmd=Command.DISCOVER)


def build_read(addr: int) -> Frame:
    return Frame(addr=addr, cmd=Command.READ)


def build_ping(addr: int) -> Frame:
    return Frame(addr=addr, cmd=Command.PING)
