"""Export and import a replication batch through an external JSON file.

The file carries the device and metric catalog plus samples with ``id`` greater
than or equal to a caller-supplied starting id. Sample ids are preserved.
Import inserts only ids that are not already in the destination.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from .storage import DeviceRow, MetricCatalogRow, SampleRow, Storage

log = logging.getLogger(__name__)

FORMAT = "tlmtry-replication"
VERSION = 1

_DEVICE_FIELDS = (
    "address",
    "device_id",
    "name",
    "fw_major",
    "fw_minor",
    "poll_interval_ms",
    "last_seen",
    "online",
    "last_error",
)
_METRIC_FIELDS = ("address", "metric_id", "name", "type", "unit")
_SAMPLE_FIELDS = (
    "id",
    "address",
    "metric_id",
    "ts",
    "value_real",
    "value_text",
    "type",
    "quality",
)


class ReplicationFileError(ValueError):
    """The replication file is missing, unreadable, or does not match the format."""


def export_since(source: Storage, from_id: int, output: str | Path) -> dict[str, Any]:
    """Write the catalog and samples with ``id >= from_id`` to ``output``.

    Does not read or update the source replication watermark. The caller keeps
    ``next_from_id`` and may advance it only after the file has been imported.
    """
    start = _require_from_id(from_id)
    path = Path(output)
    _require_output_path(path)

    rows = source.iter_samples_after(start - 1)
    devices = source.list_devices()
    metrics = source.list_metrics()
    max_id = max((row.id for row in rows), default=None)
    payload = {
        "format": FORMAT,
        "version": VERSION,
        "exported_at": time.time(),
        "from_id": start,
        "max_id": max_id,
        "devices": [_device_json(row) for row in devices],
        "metrics": [_metric_json(row) for row in metrics],
        "samples": [_sample_json(row) for row in rows],
    }
    _write_json(path, payload)
    result = {
        "from_id": start,
        "exported_samples": len(rows),
        "max_id": max_id,
        "next_from_id": (max_id + 1) if max_id is not None else start,
        "output": str(path),
    }
    log.info(
        "exported replication file: %s -> %s (%s)",
        source.location,
        path,
        result,
    )
    return result


def import_file(dest: Storage, source: str | Path) -> dict[str, Any]:
    """Load a replication file into ``dest``.

    Devices and metrics are upserted. Samples that already have the same ``id``
    are left unchanged. Returns how many sample rows were new.
    """
    path = Path(source)
    payload = _read_payload(path)
    devices = [_device_row(item) for item in payload["devices"]]
    metrics = [_metric_row(item) for item in payload["metrics"]]
    samples = [_sample_row(item, from_id=int(payload["from_id"])) for item in payload["samples"]]
    present = dest.sample_ids_present([row.id for row in samples])
    dest.apply_replication(devices, metrics, samples, replicated_at=time.time())
    inserted = len(samples) - len(present)
    max_id = max((row.id for row in samples), default=None)
    result = {
        "from_id": int(payload["from_id"]),
        "file_samples": len(samples),
        "inserted_samples": inserted,
        "skipped_samples": len(present),
        "max_id": max_id,
        "input": str(path),
    }
    log.info(
        "imported replication file: %s -> %s (%s)",
        path,
        dest.location,
        result,
    )
    return result


def _require_from_id(from_id: int) -> int:
    if isinstance(from_id, bool) or not isinstance(from_id, int) or from_id < 0:
        raise ValueError("from_id must be an integer >= 0")
    return from_id


def _require_output_path(path: Path) -> None:
    if path.exists() and path.is_dir():
        raise ReplicationFileError(f"output path is a directory: {path}")
    parent = path.parent
    if not parent.exists():
        raise ReplicationFileError(f"output directory does not exist: {parent}")
    if not parent.is_dir():
        raise ReplicationFileError(f"output parent is not a directory: {parent}")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.parent / f".{path.name}.tmp"
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _read_payload(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ReplicationFileError(f"replication file not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ReplicationFileError(f"replication file is not valid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise ReplicationFileError("replication file must be a JSON object")
    if payload.get("format") != FORMAT:
        raise ReplicationFileError("replication file format is not tlmtry-replication")
    if payload.get("version") != VERSION:
        raise ReplicationFileError(
            f"unsupported replication file version: {payload.get('version')!r}"
        )
    from_id = payload.get("from_id")
    if isinstance(from_id, bool) or not isinstance(from_id, int) or from_id < 0:
        raise ReplicationFileError("replication file from_id must be an integer >= 0")
    for name in ("devices", "metrics", "samples"):
        if not isinstance(payload.get(name), list):
            raise ReplicationFileError(f"replication file field {name} must be a list")
    seen: set[int] = set()
    for item in payload["samples"]:
        sample_id = _require_object(item, _SAMPLE_FIELDS, "sample")["id"]
        if isinstance(sample_id, bool) or not isinstance(sample_id, int):
            raise ReplicationFileError("sample id must be an integer")
        if sample_id < from_id:
            raise ReplicationFileError(f"sample id {sample_id} is below from_id {from_id}")
        if sample_id in seen:
            raise ReplicationFileError(f"duplicate sample id {sample_id}")
        seen.add(sample_id)
    return payload


def _require_object(value: Any, fields: tuple[str, ...], what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReplicationFileError(f"{what} must be an object")
    missing = [field for field in fields if field not in value]
    if missing:
        raise ReplicationFileError(f"{what} missing fields: {', '.join(missing)}")
    return value


def _as_int(value: Any, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ReplicationFileError(f"{what} must be an integer")
    return value


def _as_float(value: Any, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ReplicationFileError(f"{what} must be a number")
    return float(value)


def _as_optional_float(value: Any, what: str) -> float | None:
    if value is None:
        return None
    return _as_float(value, what)


def _as_optional_str(value: Any, what: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ReplicationFileError(f"{what} must be a string or null")
    return value


def _as_bool(value: Any, what: str) -> bool:
    if isinstance(value, bool):
        return value
    if value in (0, 1):
        return bool(value)
    raise ReplicationFileError(f"{what} must be a boolean")


def _device_json(row: DeviceRow) -> dict[str, Any]:
    return {
        "address": row.address,
        "device_id": row.device_id,
        "name": row.name,
        "fw_major": row.fw_major,
        "fw_minor": row.fw_minor,
        "poll_interval_ms": row.poll_interval_ms,
        "last_seen": row.last_seen,
        "online": row.online,
        "last_error": row.last_error,
    }


def _metric_json(row: MetricCatalogRow) -> dict[str, Any]:
    return {
        "address": row.address,
        "metric_id": row.metric_id,
        "name": row.name,
        "type": row.type,
        "unit": row.unit,
    }


def _sample_json(row: SampleRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "address": row.address,
        "metric_id": row.metric_id,
        "ts": row.ts,
        "value_real": row.value_real,
        "value_text": row.value_text,
        "type": row.type,
        "quality": row.quality,
    }


def _device_row(value: Any) -> DeviceRow:
    item = _require_object(value, _DEVICE_FIELDS, "device")
    return DeviceRow(
        address=_as_int(item["address"], "device address"),
        device_id=_as_int(item["device_id"], "device_id"),
        name=_as_optional_str(item["name"], "device name") or "",
        fw_major=_as_int(item["fw_major"], "fw_major"),
        fw_minor=_as_int(item["fw_minor"], "fw_minor"),
        poll_interval_ms=_as_int(item["poll_interval_ms"], "poll_interval_ms"),
        last_seen=_as_optional_float(item["last_seen"], "last_seen"),
        online=_as_bool(item["online"], "online"),
        last_error=_as_optional_str(item["last_error"], "last_error"),
    )


def _metric_row(value: Any) -> MetricCatalogRow:
    item = _require_object(value, _METRIC_FIELDS, "metric")
    return MetricCatalogRow(
        address=_as_int(item["address"], "metric address"),
        metric_id=_as_int(item["metric_id"], "metric_id"),
        name=_as_optional_str(item["name"], "metric name") or "",
        type=_as_int(item["type"], "metric type"),
        unit=_as_optional_str(item["unit"], "metric unit") or "",
    )


def _sample_row(value: Any, *, from_id: int) -> SampleRow:
    item = _require_object(value, _SAMPLE_FIELDS, "sample")
    sample_id = _as_int(item["id"], "sample id")
    if sample_id < from_id:
        raise ReplicationFileError(f"sample id {sample_id} is below from_id {from_id}")
    return SampleRow(
        id=sample_id,
        address=_as_int(item["address"], "sample address"),
        metric_id=_as_int(item["metric_id"], "sample metric_id"),
        ts=_as_float(item["ts"], "sample ts"),
        value_real=_as_optional_float(item["value_real"], "value_real"),
        value_text=_as_optional_str(item["value_text"], "value_text"),
        type=_as_int(item["type"], "sample type"),
        quality=_as_int(item["quality"], "sample quality"),
    )
