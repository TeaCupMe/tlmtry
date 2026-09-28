"""Create the telemetry schema and fill it with pseudo sensor history.

The series are deterministic functions of unix time, so the same ``now``,
``hours``, and ``step_s`` always produce the same readings. Devices match the
lab nodes used by the in-process simulator, plus one air-quality node.
"""

from __future__ import annotations

import argparse
import math
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from .config import load_config
from .protocol import DeviceManifest, MetricInfo, MetricType, SampleValue
from .storage import Storage

# Refuse accidental multi-million inserts unless the caller opts in.
MAX_SAMPLE_ROWS = 2_000_000

ValueFn = Callable[[float], int | float | bool]


@dataclass(frozen=True)
class SeedMetric:
    metric_id: int
    name: str
    type: MetricType
    unit: str
    value_at: ValueFn


@dataclass(frozen=True)
class SeedDevice:
    address: int
    device_id: int
    name: str
    poll_interval_ms: int
    metrics: tuple[SeedMetric, ...]
    fw_major: int = 1
    fw_minor: int = 0


def _temp_c(ts: float) -> float:
    daily = math.sin(2 * math.pi * ts / 86400.0)
    hourly = math.sin(2 * math.pi * ts / 3600.0)
    return round(22.0 + 2.4 * daily + 0.35 * hourly, 2)


def _humidity(ts: float) -> float:
    daily = math.cos(2 * math.pi * ts / 86400.0)
    return round(48.0 + 12.0 * daily + 2.0 * math.sin(2 * math.pi * ts / 5400.0), 1)


def _door_open(ts: float) -> bool:
    # Open for the first 30 seconds of every 10 minutes.
    return (int(ts) % 600) < 30


def _counter(ts: float) -> int:
    return (int(ts) // 30) % 65536


def _pressure_hpa(ts: float) -> float:
    return round(1013.25 + 3.5 * math.sin(2 * math.pi * ts / 21600.0), 2)


def _co2_ppm(ts: float) -> int:
    val = 620.0 + 180.0 * math.sin(2 * math.pi * ts / 10800.0)
    if (int(ts) // 3600) % 5 == 0:
        val += 250.0
    return max(400, min(5000, int(val)))


SEED_DEVICES: tuple[SeedDevice, ...] = (
    SeedDevice(
        address=1,
        device_id=101,
        name="lab-node-1",
        poll_interval_ms=500,
        metrics=(
            SeedMetric(0, "temp_c", MetricType.FLOAT, "C", _temp_c),
            SeedMetric(1, "humidity", MetricType.FLOAT, "%", _humidity),
        ),
    ),
    SeedDevice(
        address=2,
        device_id=102,
        name="lab-node-2",
        poll_interval_ms=800,
        metrics=(
            SeedMetric(0, "door_open", MetricType.BOOL, "", _door_open),
            SeedMetric(1, "counter", MetricType.UINT16, "cnt", _counter),
        ),
    ),
    SeedDevice(
        address=3,
        device_id=103,
        name="lab-node-3",
        poll_interval_ms=2000,
        metrics=(
            SeedMetric(0, "pressure_hpa", MetricType.FLOAT, "hPa", _pressure_hpa),
            SeedMetric(1, "co2_ppm", MetricType.UINT16, "ppm", _co2_ppm),
        ),
    ),
)


@dataclass(frozen=True)
class SeedReport:
    location: str
    backend: str
    devices: int
    metrics: int
    samples: int
    start_ts: float
    end_ts: float
    step_s: float
    reset: bool


def sample_grid(end: float, hours: float, step_s: float) -> tuple[float, float, int]:
    """Return ``(first_ts, last_ts, point_count)`` on a millisecond grid."""
    if hours <= 0:
        raise ValueError("hours must be positive")
    if step_s <= 0:
        raise ValueError("step_s must be positive")
    step_ms = int(round(step_s * 1000))
    if step_ms <= 0:
        raise ValueError("step_s must be at least 1 ms")
    end_ms = int(round(end * 1000))
    start_ms = int(round((end - hours * 3600.0) * 1000))
    if start_ms < 0:
        raise ValueError("history must start at or after the unix epoch")
    first_ms = ((start_ms + step_ms - 1) // step_ms) * step_ms
    if first_ms > end_ms:
        return end, end, 1
    count = (end_ms - first_ms) // step_ms + 1
    last_ms = first_ms + (count - 1) * step_ms
    return first_ms / 1000.0, last_ms / 1000.0, count


def iter_sample_times(end: float, hours: float, step_s: float) -> Iterator[float]:
    first, _last, count = sample_grid(end, hours, step_s)
    step_ms = int(round(step_s * 1000))
    first_ms = int(round(first * 1000))
    for i in range(count):
        yield (first_ms + i * step_ms) / 1000.0


def _metric_count() -> int:
    return sum(len(device.metrics) for device in SEED_DEVICES)


def seed_database(
    database: str,
    *,
    hours: float = 24.0,
    step_s: float = 60.0,
    reset: bool = False,
    force: bool = False,
    now: float | None = None,
) -> SeedReport:
    """Open ``database``, ensure the schema, and insert pseudo sensor samples.

    ``database`` is a SQLite path or a ``postgres://`` / ``postgresql://`` URL.
    ``reset`` deletes existing catalog and sample rows before inserting.
    """
    end = time.time() if now is None else now
    _first, last, points = sample_grid(end, hours, step_s)
    rows = points * _metric_count()
    if rows > MAX_SAMPLE_ROWS and not force:
        raise ValueError(
            f"refusing to insert {rows} samples (limit {MAX_SAMPLE_ROWS}). "
            "Increase step_s, reduce hours, or pass force=True."
        )

    storage = Storage(database)
    try:
        if reset:
            storage.clear()
        for device in SEED_DEVICES:
            storage.upsert_manifest(
                DeviceManifest(
                    device_id=device.device_id,
                    fw_major=device.fw_major,
                    fw_minor=device.fw_minor,
                    name=device.name,
                    poll_interval_ms=device.poll_interval_ms,
                    metrics=[
                        MetricInfo(
                            metric_id=metric.metric_id,
                            type=metric.type,
                            unit=metric.unit,
                            name=metric.name,
                        )
                        for metric in device.metrics
                    ],
                    address=device.address,
                )
            )

        def batches() -> Iterator[tuple[int, list[SampleValue], float]]:
            for device in SEED_DEVICES:
                for ts in iter_sample_times(end, hours, step_s):
                    yield (
                        device.address,
                        [
                            SampleValue(
                                metric_id=metric.metric_id,
                                type=metric.type,
                                value=metric.value_at(ts),
                            )
                            for metric in device.metrics
                        ],
                        ts,
                    )

        inserted = storage.insert_many_samples(batches())
        return SeedReport(
            location=storage.location,
            backend=storage.backend,
            devices=len(storage.list_devices()),
            metrics=len(storage.list_metrics()),
            samples=inserted,
            start_ts=_first,
            end_ts=last,
            step_s=step_s,
            reset=reset,
        )
    finally:
        storage.close()


def format_report(report: SeedReport) -> str:
    start = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(report.start_ts))
    end = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(report.end_ts))
    lines = [
        f"database: {report.location} ({report.backend})",
        f"reset: {'yes' if report.reset else 'no'}",
        f"span: {start} .. {end} (step {report.step_s:g}s)",
        f"devices: {report.devices}",
        f"metrics: {report.metrics}",
        f"samples inserted: {report.samples}",
    ]
    for device in SEED_DEVICES:
        names = ", ".join(metric.name for metric in device.metrics)
        lines.append(f"  addr={device.address} name={device.name!r} metrics=[{names}]")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tlmtry-seed",
        description=(
            "Create the telemetry database and fill it with pseudo sensor readings"
        ),
    )
    add_seed_arguments(parser, include_config=True)
    return parser


def add_seed_arguments(
    parser: argparse.ArgumentParser, *, include_config: bool
) -> None:
    if include_config:
        parser.add_argument("-c", "--config", help="Path to YAML config")
    parser.add_argument(
        "--database",
        help=(
            "SQLite path or postgres:// / postgresql:// URL "
            "(default: database from config, or data/primary.db)"
        ),
    )
    parser.add_argument(
        "--hours",
        type=float,
        default=24.0,
        help="How many hours of history to generate (default: 24)",
    )
    parser.add_argument(
        "--step-seconds",
        type=float,
        default=60.0,
        help="Seconds between samples (default: 60)",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Delete existing devices, metrics, and samples before loading",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=f"Allow more than {MAX_SAMPLE_ROWS} sample rows",
    )


def run_seed(args: argparse.Namespace) -> int:
    cfg = load_config(getattr(args, "config", None))
    database = args.database or cfg.database
    try:
        report = seed_database(
            database,
            hours=args.hours,
            step_s=args.step_seconds,
            reset=args.reset,
            force=args.force,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(format_report(report))
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    raise SystemExit(run_seed(args))


if __name__ == "__main__":
    main()
