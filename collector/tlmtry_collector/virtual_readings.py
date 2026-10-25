"""Load virtual readings from a CSV file into ``samples``.

Each line has the form ``unix_time; value;``. A trailing semicolon is part of
the form. A comma in the value is a decimal mark (``21,5``), so the separator
between columns stays a semicolon. Blank lines, ``#`` comments, and a header
``unix_time; value;`` are skipped.

Readings are stored like polled samples and are picked up by replication.
They do not mark the device online. ``last_seen`` moves only forward.

``import_virtual_readings`` writes the whole file in one transaction.
``follow_virtual_readings`` writes a sample every ``every`` seconds until
stopped. CSV rows are the moments when the value changes; between them the
previous value is held. Optional jitter adds a small sine around that value.
"""

from __future__ import annotations

import bisect
import logging
import math
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .protocol import MetricType
from .storage import MetricCatalogRow, Storage

log = logging.getLogger(__name__)

# One full wobble per this many interval steps. The phase is offset by half a
# step so a grid sample is not exactly the CSV value.
JITTER_PERIOD_STEPS = 12

# Seconds to wait. None waits until stop. Return True when stop was requested.
WaitFn = Callable[[float | None], bool]


class ReadingsFileError(ValueError):
    """The CSV file is missing, unreadable, or does not match the format."""


@dataclass(frozen=True)
class CsvReading:
    line: int
    ts: float
    value: float


def parse_readings_csv(text: str) -> list[CsvReading]:
    """Parse ``unix_time; value;`` lines into timestamped numbers."""
    readings: list[CsvReading] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip().lstrip("\ufeff")
        if not line or line.startswith("#"):
            continue
        fields = [part.strip() for part in line.split(";")]
        while fields and fields[-1] == "":
            fields.pop()
        if len(fields) != 2:
            raise ReadingsFileError(
                f"line {lineno}: expected unix_time; value; got {raw.strip()!r}"
            )
        if fields[0].lower() == "unix_time" and fields[1].lower() == "value":
            continue
        try:
            ts = _parse_number(fields[0])
            value = _parse_number(fields[1])
        except ValueError as exc:
            raise ReadingsFileError(f"line {lineno}: {exc}") from exc
        if not math.isfinite(ts) or not math.isfinite(value):
            raise ReadingsFileError(f"line {lineno}: non-finite number")
        readings.append(CsvReading(line=lineno, ts=ts, value=value))
    return readings


def import_virtual_readings(
    storage: Storage,
    csv_path: str | Path,
    address: int,
    metric_id: int,
    *,
    create_virtual: bool = False,
    device_name: str = "virtual",
    metric_name: str = "value",
    unit: str = "",
) -> dict[str, Any]:
    """Insert CSV readings for one metric.

    New rows are written in one transaction. A timestamp already stored for
    this metric is skipped, so loading the same file again does not duplicate
    it. Without ``create_virtual`` the metric must already be in the catalog.
    With it, a missing device and metric are created (type float) and an
    existing metric is kept as it is.
    """
    path, metric, rows = _prepare(
        storage,
        csv_path,
        address,
        metric_id,
        create_virtual=create_virtual,
        device_name=device_name,
        metric_name=metric_name,
        unit=unit,
    )
    present = storage.sample_timestamps_present(
        address, metric_id, [ts for ts, _, _, _ in rows]
    )
    pending = [row for row in rows if row[0] not in present]
    inserted = storage.insert_reading_rows(address, metric_id, pending)
    return _summary(
        metric,
        path,
        inserted=inserted,
        skipped=len(rows) - len(pending),
    )


def follow_virtual_readings(
    storage: Storage,
    csv_path: str | Path,
    address: int,
    metric_id: int,
    every: float,
    *,
    create_virtual: bool = False,
    device_name: str = "virtual",
    metric_name: str = "value",
    unit: str = "",
    stop: threading.Event | None = None,
    wait: WaitFn | None = None,
    jitter: float | None = None,
) -> dict[str, Any]:
    """Write one sample every ``every`` seconds until ``stop`` is set.

    Samples start at the earliest CSV timestamp and then step by ``every``.
    Each sample takes the value of the latest CSV point at or before its
    timestamp. After the last file point that value is held. The first missing
    sample is written immediately; each later sample waits ``every`` seconds.
    A restarted process continues after the last sample already stored on this
    grid.

    ``jitter`` is the peak amplitude of a sine added to the held value. It is
    only applied to float metrics. ``None`` stores the CSV value unchanged.
    """
    if not math.isfinite(every) or every <= 0:
        raise ValueError("every must be a positive number of seconds")
    if jitter is not None and (not math.isfinite(jitter) or jitter <= 0):
        raise ValueError("jitter amplitude must be a positive number")
    if stop is None:
        stop = threading.Event()
    if wait is None:
        def wait(seconds: float | None) -> bool:
            if seconds is None:
                stop.wait()
                return True
            return stop.wait(seconds)

    path, metric, rows = _prepare(
        storage,
        csv_path,
        address,
        metric_id,
        create_virtual=create_virtual,
        device_name=device_name,
        metric_name=metric_name,
        unit=unit,
    )
    if not rows:
        raise ReadingsFileError("CSV file has no readings")
    if jitter is not None and metric.type != int(MetricType.FLOAT):
        raise ValueError("jitter is only supported for float metrics")
    times, values = _hold_series(rows)
    start = times[0]
    step = _first_missing_step(storage, address, metric_id, start, every)
    inserted = 0
    stopped = False
    while True:
        if stop.is_set():
            stopped = True
            break
        ts = start + step * every
        held = _held_value(times, values, ts)
        if jitter is not None:
            held = _jittered(held, step, jitter)
        value_real, value_text = _encode_value(metric.type, held)
        storage.insert_reading_rows(
            address,
            metric_id,
            [(ts, value_real, value_text, metric.type)],
        )
        inserted += 1
        shown = value_real if value_real is not None else value_text
        log.info(
            "inserted virtual reading address=%s metric_id=%s ts=%s value=%s",
            address,
            metric_id,
            ts,
            shown,
        )
        step += 1
        if stop.is_set() or wait(every):
            stopped = True
            break

    extra: dict[str, Any] = {}
    if jitter is not None:
        extra["jitter"] = jitter
    return _summary(
        metric,
        path,
        inserted=inserted,
        skipped=step - inserted,
        every=every,
        stopped=stopped or stop.is_set(),
        next_ts=start + step * every,
        **extra,
    )


def _prepare(
    storage: Storage,
    csv_path: str | Path,
    address: int,
    metric_id: int,
    *,
    create_virtual: bool,
    device_name: str,
    metric_name: str,
    unit: str,
) -> tuple[Path, MetricCatalogRow, list[tuple[float, float | None, str | None, int]]]:
    if address < 1:
        raise ValueError("address must be >= 1")
    if metric_id < 0:
        raise ValueError("metric_id must be >= 0")

    path = Path(csv_path)
    if not path.is_file():
        raise ReadingsFileError(f"CSV file does not exist: {path}")
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ReadingsFileError(f"CSV file is not UTF-8: {path}") from exc

    points = parse_readings_csv(text)
    metric = storage.get_metric(address, metric_id)
    if metric is None:
        if not create_virtual:
            raise ReadingsFileError(
                f"metric address={address} metric_id={metric_id} not found; "
                "discover the device or pass --create-virtual"
            )
        # Validate the file before creating a catalog row.
        rows = _encode_points(int(MetricType.FLOAT), points)
        metric = storage.ensure_virtual_metric(
            address,
            metric_id,
            device_name=device_name,
            metric_name=metric_name,
            unit=unit,
            metric_type=int(MetricType.FLOAT),
        )
    else:
        rows = _encode_points(metric.type, points)
    return path, metric, rows


def _summary(
    metric: MetricCatalogRow,
    path: Path,
    *,
    inserted: int,
    skipped: int,
    **extra: Any,
) -> dict[str, Any]:
    result = {
        "inserted_samples": inserted,
        "skipped_samples": skipped,
        "address": metric.address,
        "metric_id": metric.metric_id,
        "metric_name": metric.name,
        "metric_type": metric.type,
        "input": str(path),
    }
    result.update(extra)
    return result


def _hold_series(
    rows: list[tuple[float, float | None, str | None, int]],
) -> tuple[list[float], list[float]]:
    """CSV points sorted by time. A repeated timestamp keeps the later value."""
    ordered = sorted(rows, key=lambda row: row[0])
    times: list[float] = []
    values: list[float] = []
    for ts, value_real, _value_text, _metric_type in ordered:
        if value_real is None:
            raise ReadingsFileError(f"reading at {ts} has no numeric value")
        times.append(float(ts))
        values.append(float(value_real))
    return times, values


def _jittered(value: float, step: int, amplitude: float) -> float:
    phase = 2 * math.pi * (step + 0.5) / JITTER_PERIOD_STEPS
    return value + amplitude * math.sin(phase)


def _held_value(times: list[float], values: list[float], ts: float) -> float:
    index = bisect.bisect_right(times, ts) - 1
    if index < 0:
        index = 0
    return values[index]


def _first_missing_step(
    storage: Storage,
    address: int,
    metric_id: int,
    start: float,
    every: float,
) -> int:
    """Index of the first ``start + n * every`` sample that is not stored yet."""

    def exists(step: int) -> bool:
        ts = start + step * every
        return bool(storage.sample_timestamps_present(address, metric_id, [ts]))

    if not exists(0):
        return 0
    hi = 1
    while exists(hi):
        if hi >= 2**52:
            return hi
        hi *= 2
    lo = hi // 2
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if exists(mid):
            lo = mid
        else:
            hi = mid
    return hi


def _encode_points(
    metric_type: int, points: list[CsvReading]
) -> list[tuple[float, float | None, str | None, int]]:
    rows: list[tuple[float, float | None, str | None, int]] = []
    for point in points:
        try:
            value_real, value_text = _encode_value(metric_type, point.value)
        except ValueError as exc:
            raise ReadingsFileError(f"line {point.line}: {exc}") from exc
        rows.append((point.ts, value_real, value_text, metric_type))
    return rows


def _encode_value(metric_type: int, value: float) -> tuple[float | None, str | None]:
    try:
        mtype = MetricType(metric_type)
    except ValueError as exc:
        raise ValueError(f"unknown metric type {metric_type}") from exc
    if mtype == MetricType.BOOL:
        if value == 0.0:
            return 0.0, "false"
        if value == 1.0:
            return 1.0, "true"
        raise ValueError(f"bool metric expects 0 or 1, got {value}")
    if mtype == MetricType.FLOAT:
        return float(value), None
    if mtype == MetricType.INT32:
        return _require_int(value, -2147483648, 2147483647, "i32"), None
    if mtype == MetricType.UINT16:
        return _require_int(value, 0, 65535, "u16"), None
    raise ValueError(f"unsupported metric type {metric_type}")


def _require_int(value: float, low: int, high: int, label: str) -> float:
    if not value.is_integer():
        raise ValueError(f"{label} metric expects an integer, got {value}")
    as_int = int(value)
    if as_int < low or as_int > high:
        raise ValueError(
            f"{label} metric expects a value from {low} to {high}, got {as_int}"
        )
    return float(as_int)


def _parse_number(text: str) -> float:
    cleaned = text.replace(" ", "").replace("\u00a0", "")
    if "," in cleaned and "." in cleaned:
        raise ValueError(f"not a number: {text!r}")
    if "," in cleaned:
        cleaned = cleaned.replace(",", ".")
    try:
        return float(cleaned)
    except ValueError as exc:
        raise ValueError(f"not a number: {text!r}") from exc
