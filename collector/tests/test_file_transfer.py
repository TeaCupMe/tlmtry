from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tlmtry_collector.file_transfer import (
    ReplicationFileError,
    export_since,
    import_file,
)
from tlmtry_collector.protocol import (
    DeviceManifest,
    MetricInfo,
    MetricType,
    SampleValue,
)
from tlmtry_collector.storage import Storage

ROOT = Path(__file__).resolve().parents[2]
EXPORT_SCRIPT = ROOT / "scripts" / "export_replication.py"
IMPORT_SCRIPT = ROOT / "scripts" / "import_replication.py"


def _seed(storage: Storage) -> None:
    storage.upsert_manifest(
        DeviceManifest(
            address=1,
            device_id=42,
            name="node-a",
            fw_major=1,
            fw_minor=2,
            poll_interval_ms=1000,
            metrics=[
                MetricInfo(0, MetricType.FLOAT, "C", "temp_c"),
                MetricInfo(1, MetricType.BOOL, "", "ok"),
            ],
        )
    )
    storage.insert_samples(
        1,
        [
            SampleValue(0, MetricType.FLOAT, 21.5),
            SampleValue(1, MetricType.BOOL, True),
            SampleValue(0, MetricType.FLOAT, 22.0),
        ],
    )


def test_export_from_id_and_import_only_new_rows(tmp_path: Path):
    primary = Storage(tmp_path / "primary.db")
    _seed(primary)
    assert primary.get_replication_watermark() == 0

    batch = tmp_path / "batch.json"
    exported = export_since(primary, 2, batch)
    assert exported["exported_samples"] == 2
    assert exported["max_id"] == 3
    assert exported["next_from_id"] == 4
    assert primary.get_replication_watermark() == 0

    payload = json.loads(batch.read_text(encoding="utf-8"))
    assert payload["format"] == "tlmtry-replication"
    assert payload["from_id"] == 2
    assert [row["id"] for row in payload["samples"]] == [2, 3]
    assert payload["devices"][0]["name"] == "node-a"
    assert {row["name"] for row in payload["metrics"]} == {"temp_c", "ok"}
    bool_row = next(row for row in payload["samples"] if row["id"] == 2)
    assert bool_row["value_text"] == "true"
    assert bool_row["value_real"] == 1.0

    backup = Storage(tmp_path / "backup.db")
    try:
        loaded = import_file(backup, batch)
        assert loaded["inserted_samples"] == 2
        assert loaded["skipped_samples"] == 0
        assert backup.sample_count() == 2
        assert [row.id for row in backup.iter_samples_after(0)] == [2, 3]
        assert backup.list_devices()[0].name == "node-a"
        assert {row.name for row in backup.list_metrics()} == {"temp_c", "ok"}

        again = import_file(backup, batch)
        assert again["inserted_samples"] == 0
        assert again["skipped_samples"] == 2
        assert backup.sample_count() == 2
    finally:
        backup.close()

    earlier = tmp_path / "earlier.json"
    export_since(primary, 1, earlier)
    backup = Storage(tmp_path / "backup.db")
    try:
        loaded = import_file(backup, earlier)
        assert loaded["inserted_samples"] == 1
        assert loaded["skipped_samples"] == 2
        rows = backup.iter_samples_after(0)
        assert [row.id for row in rows] == [1, 2, 3]
        assert rows[0].value_real == 21.5
    finally:
        backup.close()
        primary.close()


def test_export_without_new_samples_keeps_from_id(tmp_path: Path):
    primary = Storage(tmp_path / "primary.db")
    _seed(primary)
    try:
        exported = export_since(primary, 10, tmp_path / "empty.json")
    finally:
        primary.close()
    assert exported["exported_samples"] == 0
    assert exported["max_id"] is None
    assert exported["next_from_id"] == 10
    payload = json.loads((tmp_path / "empty.json").read_text(encoding="utf-8"))
    assert payload["samples"] == []
    assert len(payload["devices"]) == 1


def test_export_limit_bounds_a_replication_batch(tmp_path: Path):
    primary = Storage(tmp_path / "primary.db")
    _seed(primary)
    batch = tmp_path / "limited.json"
    try:
        exported = export_since(primary, 1, batch, limit=2)
    finally:
        primary.close()
    payload = json.loads(batch.read_text(encoding="utf-8"))
    assert exported["exported_samples"] == 2
    assert exported["max_id"] == 2
    assert exported["next_from_id"] == 3
    assert [row["id"] for row in payload["samples"]] == [1, 2]


def test_rejects_bad_file_and_missing_directory(tmp_path: Path):
    primary = Storage(tmp_path / "primary.db")
    _seed(primary)
    try:
        with pytest.raises(ReplicationFileError, match="does not exist"):
            export_since(primary, 1, tmp_path / "missing" / "batch.json")
        with pytest.raises(ValueError, match="from_id"):
            export_since(primary, -1, tmp_path / "batch.json")
    finally:
        primary.close()

    broken = tmp_path / "broken.json"
    broken.write_text('{"format": "tlmtry-replication", "version": 2}\n', encoding="utf-8")
    dest = Storage(tmp_path / "backup.db")
    try:
        with pytest.raises(ReplicationFileError, match="unsupported"):
            import_file(dest, broken)
    finally:
        dest.close()


def test_scripts_export_and_import(tmp_path: Path):
    primary_path = tmp_path / "primary.db"
    primary = Storage(primary_path)
    _seed(primary)
    primary.close()

    batch = tmp_path / "batch.json"
    backup_path = tmp_path / "backup.db"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "collector")

    exported = subprocess.run(
        [
            sys.executable,
            str(EXPORT_SCRIPT),
            "--database",
            str(primary_path),
            "--from-id",
            "2",
            "--output",
            str(batch),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert exported.returncode == 0, exported.stderr
    body = json.loads(exported.stdout)
    assert body["exported_samples"] == 2
    assert body["next_from_id"] == 4

    loaded = subprocess.run(
        [
            sys.executable,
            str(IMPORT_SCRIPT),
            "--database",
            str(backup_path),
            "--input",
            str(batch),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert loaded.returncode == 0, loaded.stderr
    imported = json.loads(loaded.stdout)
    assert imported["inserted_samples"] == 2

    missing = subprocess.run(
        [sys.executable, str(EXPORT_SCRIPT), "--from-id", "1", "--output", str(batch)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert missing.returncode == 2
