from __future__ import annotations

from pathlib import Path

from tlmtry_collector.protocol import MetricType, SampleValue
from tlmtry_collector.replicate import replicate
from tlmtry_collector.scheduler import CollectorScheduler, SchedulerConfig
from tlmtry_collector.serial_bus import Bus
from tlmtry_collector.sim import QueueTransport, demo_devices
from tlmtry_collector.storage import Storage


def test_storage_and_replicate(tmp_path: Path):
    primary = Storage(tmp_path / "primary.db")
    primary.insert_samples(
        1,
        [
            SampleValue(0, MetricType.FLOAT, 21.5),
            SampleValue(1, MetricType.BOOL, True),
        ],
    )
    assert primary.sample_count() == 2
    backup = tmp_path / "backup.db"
    r1 = replicate(primary, backup)
    assert r1["copied_samples"] == 2
    r2 = replicate(primary, backup)
    assert r2["copied_samples"] == 0
    primary.insert_samples(1, [SampleValue(0, MetricType.FLOAT, 22.0)])
    r3 = replicate(primary, backup)
    assert r3["copied_samples"] == 1
    primary.close()


def test_sim_discover_and_poll(tmp_path: Path):
    devices = demo_devices()
    bus = Bus(QueueTransport(devices), response_timeout_s=0.2)
    found = bus.discover_scan(address_min=1, address_max=5, use_broadcast=True)
    assert {d.address for d in found} == {1, 2}
    samples = bus.read_metrics(1)
    assert samples is not None and len(samples) == 2
    assert bus.ping(2) is True
    storage = Storage(tmp_path / "p.db")
    for m in found:
        storage.upsert_manifest(m)
    storage.insert_samples(1, samples)
    assert storage.sample_count() == 2
    bus.close()
    storage.close()


def test_scheduler_collects_samples(tmp_path: Path):
    devices = demo_devices()
    bus = Bus(QueueTransport(devices), response_timeout_s=0.2)
    storage = Storage(tmp_path / "sched.db")
    sched = CollectorScheduler(
        bus,
        storage,
        SchedulerConfig(
            rediscover_every_s=60,
            address_min=1,
            address_max=5,
            use_broadcast=True,
            unicast_fill=False,
        ),
    )
    assert sched.discover_now() == 2
    # Manually poll a few times
    for d in storage.list_devices():
        sched._poll_device(d.address, d.poll_interval_ms)
    assert storage.sample_count() >= 4
    bus.close()
    storage.close()
