"""RS-485 serial transport with request/response helpers."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Protocol

from .protocol import (
    ADDR_BROADCAST,
    Command,
    Frame,
    FrameDecoder,
    build_discover,
    build_ping,
    build_read,
    parse_discover_ack,
    parse_read_ack,
    DeviceManifest,
    SampleValue,
)

log = logging.getLogger(__name__)


class ByteTransport(Protocol):
    def write(self, data: bytes) -> None: ...
    def read(self, size: int = 256, timeout: float | None = None) -> bytes: ...
    def close(self) -> None: ...


class SerialTransport:
    def __init__(
        self,
        port: str,
        baudrate: int = 115200,
        timeout: float = 0.05,
    ) -> None:
        import serial

        self._ser = serial.Serial(
            port=port,
            baudrate=baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=timeout,
        )

    def write(self, data: bytes) -> None:
        self._ser.reset_input_buffer()
        self._ser.write(data)
        self._ser.flush()

    def read(self, size: int = 256, timeout: float | None = None) -> bytes:
        if timeout is not None:
            old = self._ser.timeout
            self._ser.timeout = timeout
            try:
                return self._ser.read(size)
            finally:
                self._ser.timeout = old
        return self._ser.read(size)

    def close(self) -> None:
        self._ser.close()


class Bus:
    def __init__(
        self,
        transport: ByteTransport,
        response_timeout_s: float = 0.5,
        probe_timeout_s: float = 0.3,
        inter_frame_gap_s: float = 0.01,
    ) -> None:
        self.transport = transport
        self.response_timeout_s = response_timeout_s
        self.probe_timeout_s = probe_timeout_s
        self.inter_frame_gap_s = inter_frame_gap_s
        self._decoder = FrameDecoder()

    def close(self) -> None:
        self.transport.close()

    def transact(
        self,
        request: Frame,
        expect_cmd: int | None = None,
        timeout_s: float | None = None,
    ) -> Frame | None:
        self._decoder.reset()
        self.transport.write(request.encode())
        deadline = time.monotonic() + (
            self.response_timeout_s if timeout_s is None else timeout_s
        )
        while time.monotonic() < deadline:
            remaining = max(0.0, deadline - time.monotonic())
            chunk = self.transport.read(256, timeout=min(0.05, remaining))
            if not chunk:
                continue
            for frame in self._decoder.feed(chunk):
                if request.addr != ADDR_BROADCAST and frame.addr not in (
                    request.addr,
                    ADDR_BROADCAST,
                ):
                    continue
                if expect_cmd is not None and frame.cmd != expect_cmd:
                    if frame.cmd == Command.NACK:
                        return frame
                    continue
                time.sleep(self.inter_frame_gap_s)
                return frame
        return None

    def collect_broadcast(
        self, request: Frame, listen_s: float = 0.6
    ) -> list[Frame]:
        self._decoder.reset()
        self.transport.write(request.encode())
        deadline = time.monotonic() + listen_s
        frames: list[Frame] = []
        seen: set[int] = set()
        while time.monotonic() < deadline:
            remaining = max(0.0, deadline - time.monotonic())
            chunk = self.transport.read(256, timeout=min(0.05, remaining))
            if not chunk:
                continue
            for frame in self._decoder.feed(chunk):
                if frame.cmd != Command.DISCOVER_ACK:
                    continue
                if frame.addr in seen:
                    continue
                seen.add(frame.addr)
                frames.append(frame)
        time.sleep(self.inter_frame_gap_s)
        return frames

    def discover_one(self, addr: int, *, timeout_s: float | None = None) -> DeviceManifest | None:
        resp = self.transact(
            build_discover(addr),
            expect_cmd=Command.DISCOVER_ACK,
            timeout_s=timeout_s,
        )
        if resp is None or resp.cmd != Command.DISCOVER_ACK:
            return None
        return parse_discover_ack(resp.payload, address=resp.addr or addr)

    def discover_scan(
        self,
        address_min: int = 1,
        address_max: int = 100,
        use_broadcast: bool = True,
        unicast_fill: bool = True,
        on_progress: Callable[[str], None] | None = None,
    ) -> list[DeviceManifest]:
        """Scan the bus. ``on_progress`` receives ``broadcast`` or ``unicast:addr:i:n``."""
        found: dict[int, DeviceManifest] = {}
        if use_broadcast:
            if on_progress is not None:
                on_progress("broadcast")
            # Listen long enough for staggered replies (addr * 5 ms on device).
            listen_s = max(0.6, address_max * 0.005 + 0.2)
            for frame in self.collect_broadcast(
                build_discover(ADDR_BROADCAST), listen_s=listen_s
            ):
                try:
                    manifest = parse_discover_ack(frame.payload, address=frame.addr)
                    found[manifest.address] = manifest
                except ValueError as exc:
                    log.warning("bad discover_ack from %s: %s", frame.addr, exc)
        if unicast_fill:
            pending = [
                addr
                for addr in range(address_min, address_max + 1)
                if addr not in found
            ]
            for index, addr in enumerate(pending, start=1):
                if on_progress is not None:
                    on_progress(f"unicast:{addr}:{index}:{len(pending)}")
                try:
                    # Short probe timeout — empty addresses should fail fast.
                    manifest = self.discover_one(addr, timeout_s=self.probe_timeout_s)
                except ValueError as exc:
                    log.warning("bad discover_ack from %s: %s", addr, exc)
                    continue
                if manifest is not None:
                    found[addr] = manifest
        return [found[k] for k in sorted(found)]

    def read_metrics(self, addr: int) -> list[SampleValue] | None:
        resp = self.transact(build_read(addr), expect_cmd=Command.READ_ACK)
        if resp is None:
            return None
        if resp.cmd == Command.NACK:
            log.warning("NACK from addr %s: %s", addr, list(resp.payload))
            return None
        return parse_read_ack(resp.payload)

    def ping(self, addr: int) -> bool:
        resp = self.transact(build_ping(addr), expect_cmd=Command.PONG)
        return resp is not None and resp.cmd == Command.PONG
