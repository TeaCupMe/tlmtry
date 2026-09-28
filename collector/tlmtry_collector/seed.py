"""Create the telemetry schema and fill it with pseudo sensor history.

The catalog follows the telemetry field list: temperature, illuminance,
humidity, pressure, CO2, and O2 at each habitat module, plus the outside
station (no CO2/O2). Each parameter keeps its own measurement rate.

Series are deterministic functions of unix time, so the same ``now``,
``hours``, and ``step_s`` always produce the same readings.
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

# Fastest spec rate (temperature, 120 measurements per hour).
POLL_INTERVAL_MS = 30_000

ValueFn = Callable[[float], int | float | bool]


@dataclass(frozen=True)
class SeedMetric:
    metric_id: int
    name: str
    type: MetricType
    unit: str
    per_hour: int
    value_at: ValueFn

    def interval_s(self, step_s: float | None) -> float:
        if step_s is not None:
            return step_s
        return 3600.0 / self.per_hour


@dataclass(frozen=True)
class SeedDevice:
    address: int
    device_id: int
    name: str
    poll_interval_ms: int
    metrics: tuple[SeedMetric, ...]
    fw_major: int = 1
    fw_minor: int = 0


@dataclass(frozen=True)
class _Profile:
    phase: float
    temp_base: float
    temp_amp: float
    lux_peak: int
    outside_light: bool
    rh_base: float
    rh_amp: float
    pressure_base: float
    pressure_amp: float
    co2_base: float | None = None
    co2_amp: float = 0.0
    o2_base: float | None = None
    o2_amp: float = 0.0


def _temp_c(ts: float, profile: _Profile) -> float:
    daily = math.sin(2 * math.pi * ts / 86400.0 + profile.phase)
    hourly = math.sin(2 * math.pi * ts / 3600.0)
    return round(profile.temp_base + profile.temp_amp * daily + 0.25 * hourly, 2)


def _illuminance_lx(ts: float, profile: _Profile) -> int:
    day = max(0.0, math.sin(2 * math.pi * ts / 86400.0 + profile.phase))
    if profile.outside_light:
        return int(round(100_000 * day))
    return int(round(20 + profile.lux_peak * day))


def _humidity_pct(ts: float, profile: _Profile) -> float:
    daily = math.cos(2 * math.pi * ts / 86400.0 + profile.phase)
    return round(profile.rh_base + profile.rh_amp * daily, 1)


def _pressure_kpa(ts: float, profile: _Profile) -> float:
    wave = math.sin(2 * math.pi * ts / 21600.0 + profile.phase)
    return round(profile.pressure_base + profile.pressure_amp * wave, 2)


def _co2_ppm(ts: float, profile: _Profile) -> float:
    wave = math.sin(2 * math.pi * ts / 10800.0 + profile.phase)
    return round(float(profile.co2_base) + profile.co2_amp * wave, 1)


def _o2_pct(ts: float, profile: _Profile) -> float:
    wave = math.sin(2 * math.pi * ts / 10800.0 + profile.phase + 1.0)
    return round(float(profile.o2_base) + profile.o2_amp * wave, 2)


def _bind(fn: Callable[[float, _Profile], int | float], profile: _Profile) -> ValueFn:
    def value_at(ts: float) -> int | float:
        return fn(ts, profile)

    return value_at


def _parameter_name(kind: str, module_index: int | None) -> str:
    if module_index is None:
        return {
            "temp": "Температура за бортом",
            "lux": "Освещённость за бортом",
            "rh": "Влажность за бортом",
            "pressure": "Давление за бортом",
        }[kind]
    return {
        "temp": f"Температура в модуле {module_index}",
        "lux": f"Освещённость в модуле {module_index}",
        "rh": f"Влажность в модуле {module_index}",
        "pressure": f"Давление в модуле {module_index}",
        "co2": f"Содержание CO2 в модуле {module_index}",
        "o2": f"Содержание O2 в модуле {module_index}",
    }[kind]


def _device(
    address: int,
    device_id: int,
    name: str,
    module_index: int | None,
    profile: _Profile,
) -> SeedDevice:
    metrics = [
        SeedMetric(
            0,
            _parameter_name("temp", module_index),
            MetricType.FLOAT,
            "C",
            120,
            _bind(_temp_c, profile),
        ),
        SeedMetric(
            1,
            _parameter_name("lux", module_index),
            MetricType.INT32,
            "lx",
            60,
            _bind(_illuminance_lx, profile),
        ),
        SeedMetric(
            2,
            _parameter_name("rh", module_index),
            MetricType.FLOAT,
            "%",
            60,
            _bind(_humidity_pct, profile),
        ),
        SeedMetric(
            3,
            _parameter_name("pressure", module_index),
            MetricType.FLOAT,
            "kPa",
            30,
            _bind(_pressure_kpa, profile),
        ),
    ]
    if module_index is not None:
        metrics.extend(
            [
                SeedMetric(
                    4,
                    _parameter_name("co2", module_index),
                    MetricType.FLOAT,
                    "ppm",
                    30,
                    _bind(_co2_ppm, profile),
                ),
                SeedMetric(
                    5,
                    _parameter_name("o2", module_index),
                    MetricType.FLOAT,
                    "%",
                    30,
                    _bind(_o2_pct, profile),
                ),
            ]
        )
    return SeedDevice(
        address=address,
        device_id=device_id,
        name=name,
        poll_interval_ms=POLL_INTERVAL_MS,
        metrics=tuple(metrics),
    )


# Locations and rates match the telemetry field list:
# temperature 120/h, illuminance and humidity 60/h, pressure/CO2/O2 30/h.
# CO2 and O2 are not collected outside the habitat.
SEED_DEVICES: tuple[SeedDevice, ...] = (
    _device(
        1,
        101,
        "Главный модуль",
        1,
        _Profile(0.1, 22.0, 1.5, 700, False, 50.0, 8.0, 101.30, 0.20, 750.0, 180.0, 20.90, 0.15),
    ),
    _device(
        2,
        102,
        "Жилой модуль 1",
        2,
        _Profile(0.6, 22.5, 1.6, 500, False, 55.0, 10.0, 101.25, 0.25, 950.0, 350.0, 20.70, 0.25),
    ),
    _device(
        3,
        103,
        "Жилой модуль 2",
        3,
        _Profile(1.1, 22.2, 1.4, 450, False, 53.0, 9.0, 101.20, 0.25, 900.0, 300.0, 20.75, 0.20),
    ),
    _device(
        4,
        104,
        "Био-модуль",
        4,
        _Profile(1.7, 24.0, 1.2, 8000, False, 72.0, 8.0, 101.40, 0.15, 520.0, 80.0, 21.15, 0.20),
    ),
    _device(
        5,
        105,
        "Инженерный модуль",
        5,
        _Profile(2.3, 26.5, 1.8, 350, False, 38.0, 6.0, 101.10, 0.30, 680.0, 160.0, 20.80, 0.15),
    ),
    _device(
        6,
        106,
        "За бортом",
        None,
        _Profile(3.0, 8.0, 10.0, 0, True, 65.0, 25.0, 101.30, 1.20),
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
    step_s: float | None
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


def planned_samples(
    end: float, hours: float, step_s: float | None
) -> tuple[float, float, int]:
    """Return ``(first_ts, last_ts, row_count)`` for the whole catalog."""
    first = math.inf
    last = -math.inf
    total = 0
    for device in SEED_DEVICES:
        for metric in device.metrics:
            metric_first, metric_last, count = sample_grid(
                end, hours, metric.interval_s(step_s)
            )
            first = min(first, metric_first)
            last = max(last, metric_last)
            total += count
    return float(first), float(last), total


def seed_database(
    database: str,
    *,
    hours: float = 24.0,
    step_s: float | None = None,
    reset: bool = False,
    force: bool = False,
    now: float | None = None,
) -> SeedReport:
    """Open ``database``, ensure the schema, and insert pseudo sensor samples.

    ``database`` is a SQLite path or a ``postgres://`` / ``postgresql://`` URL.
    ``reset`` deletes existing catalog and sample rows before inserting.
    ``step_s`` overrides every parameter's native interval. By default each
    parameter uses the telemetry spec rate.
    """
    end = time.time() if now is None else now
    first, last, rows = planned_samples(end, hours, step_s)
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
                for metric in device.metrics:
                    interval = metric.interval_s(step_s)
                    for ts in iter_sample_times(end, hours, interval):
                        yield (
                            device.address,
                            [
                                SampleValue(
                                    metric_id=metric.metric_id,
                                    type=metric.type,
                                    value=metric.value_at(ts),
                                )
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
            start_ts=first,
            end_ts=last,
            step_s=step_s,
            reset=reset,
        )
    finally:
        storage.close()


def format_report(report: SeedReport) -> str:
    start = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(report.start_ts))
    end = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(report.end_ts))
    if report.step_s is None:
        step_text = (
            "native intervals (temp 30s, illuminance/humidity 60s, "
            "pressure/CO2/O2 120s)"
        )
    else:
        step_text = f"step {report.step_s:g}s"
    lines = [
        f"database: {report.location} ({report.backend})",
        f"reset: {'yes' if report.reset else 'no'}",
        f"span: {start} .. {end} ({step_text})",
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
        default=None,
        help=(
            "Override every parameter with one interval in seconds. "
            "Default follows the telemetry spec: temperature 30s (120/h), "
            "illuminance and humidity 60s (60/h), pressure, CO2 and O2 120s (30/h)"
        ),
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
