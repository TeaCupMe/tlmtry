"""Polling scheduler: one outstanding bus request, per-device intervals."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

from .serial_bus import Bus
from .storage import Storage

log = logging.getLogger(__name__)


@dataclass
class SchedulerConfig:
    rediscover_every_s: float = 300.0
    address_min: int = 1
    address_max: int = 100
    use_broadcast: bool = True
    unicast_fill: bool = True
    interval_overrides: dict[int, int] = field(default_factory=dict)


class CollectorScheduler:
    def __init__(
        self,
        bus: Bus,
        storage: Storage,
        config: SchedulerConfig | None = None,
    ) -> None:
        self.bus = bus
        self.storage = storage
        self.config = config or SchedulerConfig()
        self._stop = threading.Event()
        self._next_poll: dict[int, float] = {}
        self._next_discover = 0.0

    def stop(self) -> None:
        self._stop.set()

    def discover_now(self) -> int:
        if self._stop.is_set():
            return 0
        manifests = self.bus.discover_scan(
            address_min=self.config.address_min,
            address_max=self.config.address_max,
            use_broadcast=self.config.use_broadcast,
            unicast_fill=self.config.unicast_fill,
        )
        now = time.monotonic()
        for m in manifests:
            override = self.config.interval_overrides.get(m.address)
            self.storage.upsert_manifest(m, poll_interval_ms=override)
            interval_ms = override if override is not None else m.poll_interval_ms
            self._next_poll[m.address] = now  # poll soon
            log.info(
                "discovered addr=%s id=%s name=%r interval_ms=%s metrics=%s",
                m.address,
                m.device_id,
                m.name,
                interval_ms,
                len(m.metrics),
            )
        self._next_discover = now + self.config.rediscover_every_s
        return len(manifests)

    def _poll_device(self, address: int, interval_ms: int) -> None:
        samples = self.bus.read_metrics(address)
        if samples is None:
            self.storage.mark_online(address, False, error="read timeout/nack")
            log.warning("poll failed addr=%s", address)
        else:
            self.storage.insert_samples(address, samples)
            log.debug("polled addr=%s samples=%s", address, len(samples))
        self._next_poll[address] = time.monotonic() + (interval_ms / 1000.0)

    def run_forever(self) -> None:
        log.info("collector started")
        self.discover_now()
        while not self._stop.is_set():
            now = time.monotonic()
            if now >= self._next_discover:
                try:
                    self.discover_now()
                except Exception:
                    log.exception("rediscovery failed")
                    self._next_discover = now + self.config.rediscover_every_s

            devices = self.storage.list_devices()
            due = []
            for d in devices:
                nxt = self._next_poll.get(d.address, 0.0)
                if now >= nxt:
                    due.append(d)
            due.sort(key=lambda d: self._next_poll.get(d.address, 0.0))

            if due:
                d = due[0]
                try:
                    self._poll_device(d.address, d.poll_interval_ms)
                except Exception as exc:
                    log.exception("poll error addr=%s", d.address)
                    self.storage.mark_online(d.address, False, error=str(exc))
                    self._next_poll[d.address] = now + max(
                        1.0, d.poll_interval_ms / 1000.0
                    )
            else:
                # Sleep until next poll or discover, capped.
                wake = self._next_discover
                for d in devices:
                    wake = min(wake, self._next_poll.get(d.address, now + 1.0))
                sleep_s = max(0.01, min(0.5, wake - time.monotonic()))
                self._stop.wait(sleep_s)

        log.info("collector stopped")
