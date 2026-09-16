"""In-memory fake RS-485 devices for tests and local demos."""

from __future__ import annotations

import math
import struct
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from tlmtry_collector.protocol import (
    ADDR_BROADCAST,
    Command,
    Frame,
    FrameDecoder,
    MetricType,
)


@dataclass
class FakeMetric:
    metric_id: int
    name: str
    type: MetricType
    unit: str
    reader: Callable[[], float]


@dataclass
class FakeDevice:
    address: int
    device_id: int
    name: str
    poll_interval_ms: int = 1000
    fw_major: int = 1
    fw_minor: int = 0
    metrics: list[FakeMetric] = field(default_factory=list)

    def handle(self, frame: Frame) -> Frame | None:
        for_me = frame.addr in (self.address, ADDR_BROADCAST)
        if not for_me:
            return None
        if frame.cmd == Command.DISCOVER:
            return Frame(addr=self.address, cmd=Command.DISCOVER_ACK, payload=self._manifest())
        if frame.addr == ADDR_BROADCAST:
            return None
        if frame.cmd == Command.READ:
            return Frame(addr=self.address, cmd=Command.READ_ACK, payload=self._samples())
        if frame.cmd == Command.PING:
            return Frame(addr=self.address, cmd=Command.PONG, payload=b"")
        return Frame(addr=self.address, cmd=Command.NACK, payload=bytes([1]))

    def _manifest(self) -> bytes:
        out = bytearray()
        out += struct.pack("<H", self.device_id)
        out.append(self.fw_major)
        out.append(self.fw_minor)
        name_b = self.name.encode("utf-8")
        out.append(len(name_b))
        out += name_b
        out += struct.pack("<I", self.poll_interval_ms)
        out.append(len(self.metrics))
        for m in self.metrics:
            out.append(m.metric_id)
            out.append(int(m.type))
            ub = m.unit.encode("utf-8")
            out.append(len(ub))
            out += ub
            nb = m.name.encode("utf-8")
            out.append(len(nb))
            out += nb
        return bytes(out)

    def _samples(self) -> bytes:
        out = bytearray()
        out.append(len(self.metrics))
        for m in self.metrics:
            out.append(m.metric_id)
            out.append(int(m.type))
            raw = float(m.reader())
            if m.type == MetricType.INT32:
                out += struct.pack("<i", int(raw))
            elif m.type == MetricType.FLOAT:
                out += struct.pack("<f", raw)
            elif m.type == MetricType.BOOL:
                out.append(1 if raw else 0)
            elif m.type == MetricType.UINT16:
                out += struct.pack("<H", int(raw) & 0xFFFF)
        return bytes(out)


class QueueTransport:
    """Thread-safe byte pipe that routes requests to fake devices."""

    def __init__(self, devices: list[FakeDevice]) -> None:
        self.devices = {d.address: d for d in devices}
        self._rx = bytearray()
        self._lock = threading.Lock()
        self._decoder = FrameDecoder()

    def write(self, data: bytes) -> None:
        frames = self._decoder.feed(data)
        replies: list[bytes] = []
        for frame in frames:
            targets = (
                list(self.devices.values())
                if frame.addr == ADDR_BROADCAST
                else [self.devices[frame.addr]]
                if frame.addr in self.devices
                else []
            )
            for dev in targets:
                reply = dev.handle(frame)
                if reply is not None:
                    # mimic broadcast stagger slightly for realism
                    if frame.addr == ADDR_BROADCAST and frame.cmd == Command.DISCOVER:
                        time.sleep(dev.address * 0.001)
                    replies.append(reply.encode())
        with self._lock:
            for r in replies:
                self._rx.extend(r)

    def read(self, size: int = 256, timeout: float | None = None) -> bytes:
        deadline = time.monotonic() + (timeout if timeout is not None else 0.0)
        while True:
            with self._lock:
                if self._rx:
                    chunk = bytes(self._rx[:size])
                    del self._rx[:size]
                    return chunk
            if timeout is None or timeout <= 0:
                return b""
            if time.monotonic() >= deadline:
                return b""
            time.sleep(0.001)

    def close(self) -> None:
        pass


def demo_devices() -> list[FakeDevice]:
    t0 = time.time()

    def temp() -> float:
        return 20.0 + 2.0 * math.sin(time.time() - t0)

    def hum() -> float:
        return 45.0 + math.cos(time.time() - t0)

    def door() -> float:
        return 1.0 if int(time.time()) % 10 < 5 else 0.0

    return [
        FakeDevice(
            address=1,
            device_id=101,
            name="lab-node-1",
            poll_interval_ms=500,
            metrics=[
                FakeMetric(0, "temp_c", MetricType.FLOAT, "C", temp),
                FakeMetric(1, "humidity", MetricType.FLOAT, "%", hum),
            ],
        ),
        FakeDevice(
            address=2,
            device_id=102,
            name="lab-node-2",
            poll_interval_ms=800,
            metrics=[
                FakeMetric(0, "door_open", MetricType.BOOL, "", door),
                FakeMetric(1, "counter", MetricType.UINT16, "cnt", lambda: int(time.time()) % 1000),
            ],
        ),
    ]
