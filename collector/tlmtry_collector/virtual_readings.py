"""Load virtual readings from a CSV file into ``samples``.

Each line has the form ``unix_time; value;``. A trailing semicolon is part of
the form. A comma in the value is a decimal mark (``21,5``), so the separator
between columns stays a semicolon. Blank lines, ``#`` comments, and a header
``unix_time; value;`` are skipped.

Readings are stored like polled samples and are picked up by replication.
They do not mark the device online. ``last_seen`` moves only forward.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .protocol import MetricType
from .storage import Storage


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
        pending = points
        skipped = 0
        # Validate the file before creating a catalog row.
        rows = _encode_points(int(MetricType.FLOAT), pending)
        metric = storage.ensure_virtual_metric(
            address,
            metric_id,
            device_name=device_name,
            metric_name=metric_name,
            unit=unit,
            metric_type=int(MetricType.FLOAT),
        )
    else:
        present = storage.sample_timestamps_present(
            address, metric_id, [point.ts for point in points]
        )
        pending = [point for point in points if point.ts not in present]
        skipped = len(points) - len(pending)
        rows = _encode_points(metric.type, pending)

    inserted = storage.insert_reading_rows(address, metric_id, rows)
    return {
        "inserted_samples": inserted,
        "skipped_samples": skipped,
        "address": address,
        "metric_id": metric_id,
        "metric_name": metric.name,
        "metric_type": metric.type,
        "input": str(path),
    }


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
