from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tlmtry_collector.protocol import (
    DeviceManifest,
    MetricInfo,
    MetricType,
    SampleValue,
)
from tlmtry_collector.storage import Storage
from tlmtry_collector.virtual_readings import (
    ReadingsFileError,
    follow_virtual_readings,
    import_virtual_readings,
    parse_readings_csv,
)

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "import_virtual_readings.py"


def _seed(storage: Storage) -> None:
    storage.upsert_manifest(
        DeviceManifest(
            address=1,
            device_id=42,
            name="node-a",
            fw_major=1,
            fw_minor=0,
            poll_interval_ms=1000,
            metrics=[
                MetricInfo(0, MetricType.FLOAT, "C", "temp_c"),
                MetricInfo(1, MetricType.BOOL, "", "ok"),
                MetricInfo(2, MetricType.UINT16, "raw", "counts"),
            ],
        )
    )
    storage.insert_samples(1, [SampleValue(0, MetricType.FLOAT, 20.0)])


def test_parse_semicolon_lines_and_decimal_comma():
    text = """
unix_time; value;
# comment

1710000000; 21.5;
1710000060; 21,7;
1710000120; -3;
"""
    points = parse_readings_csv(text)
    assert [(p.ts, p.value) for p in points] == [
        (1710000000.0, 21.5),
        (1710000060.0, 21.7),
        (1710000120.0, -3.0),
    ]


def test_parse_rejects_a_bad_line():
    with pytest.raises(ReadingsFileError, match="line 2"):
        parse_readings_csv("1710000000; 1;\nnot-a-row\n")


def test_import_into_existing_metric_keeps_older_last_seen(tmp_path: Path):
    db = Storage(tmp_path / "primary.db")
    _seed(db)
    before = db.list_devices()[0].last_seen
    assert before is not None
    csv_path = tmp_path / "readings.csv"
    csv_path.write_text(
        "1000; 21,5;\n1001; 22.0;\n",
        encoding="utf-8",
    )
    try:
        result = import_virtual_readings(db, csv_path, 1, 0)
        assert result["inserted_samples"] == 2
        assert result["metric_name"] == "temp_c"
        rows = [row for row in db.iter_samples_after(0) if row.metric_id == 0]
        assert [(row.ts, row.value_real, row.quality) for row in rows] == [
            (pytest.approx(before), 20.0, 0),
            (1000.0, 21.5, 0),
            (1001.0, 22.0, 0),
        ]
        device = db.list_devices()[0]
        assert device.last_seen == pytest.approx(before)
        assert device.online is True
        assert db.get_replication_watermark() == 0
    finally:
        db.close()


def test_newer_timestamp_moves_last_seen_forward(tmp_path: Path):
    db = Storage(tmp_path / "primary.db")
    _seed(db)
    csv_path = tmp_path / "readings.csv"
    csv_path.write_text("4000000000; 19;\n", encoding="utf-8")
    try:
        import_virtual_readings(db, csv_path, 1, 0)
        device = db.list_devices()[0]
        assert device.last_seen == 4000000000.0
        assert device.online is True
    finally:
        db.close()


def test_bool_and_integer_metrics(tmp_path: Path):
    db = Storage(tmp_path / "primary.db")
    _seed(db)
    csv_path = tmp_path / "flags.csv"
    csv_path.write_text("1000; 0;\n1001; 1;\n", encoding="utf-8")
    counts = tmp_path / "counts.csv"
    counts.write_text("1002; 15;\n", encoding="utf-8")
    try:
        import_virtual_readings(db, csv_path, 1, 1)
        import_virtual_readings(db, counts, 1, 2)
        flags = [row for row in db.iter_samples_after(0) if row.metric_id == 1]
        assert [(row.value_real, row.value_text) for row in flags] == [
            (0.0, "false"),
            (1.0, "true"),
        ]
        raw = [row for row in db.iter_samples_after(0) if row.metric_id == 2]
        assert raw[0].value_real == 15.0
        assert raw[0].type == int(MetricType.UINT16)
    finally:
        db.close()


def test_bad_value_writes_nothing(tmp_path: Path):
    db = Storage(tmp_path / "primary.db")
    _seed(db)
    csv_path = tmp_path / "bad.csv"
    csv_path.write_text("1000; 1;\n1001; 2;\n", encoding="utf-8")
    try:
        before = db.sample_count()
        with pytest.raises(ReadingsFileError, match="line 2"):
            import_virtual_readings(db, csv_path, 1, 1)
        assert db.sample_count() == before
        fractional = tmp_path / "fraction.csv"
        fractional.write_text("1000; 1,5;\n", encoding="utf-8")
        with pytest.raises(ReadingsFileError, match="integer"):
            import_virtual_readings(db, fractional, 1, 2)
        assert db.sample_count() == before
    finally:
        db.close()


def test_missing_metric_requires_create_virtual(tmp_path: Path):
    db = Storage(tmp_path / "primary.db")
    csv_path = tmp_path / "readings.csv"
    csv_path.write_text("1710000000; 3,25;\n", encoding="utf-8")
    try:
        with pytest.raises(ReadingsFileError, match="not found"):
            import_virtual_readings(db, csv_path, 101, 0)
        result = import_virtual_readings(
            db,
            csv_path,
            101,
            0,
            create_virtual=True,
            device_name="lab-virtual",
            metric_name="temp_c",
            unit="C",
        )
        assert result["inserted_samples"] == 1
        device = db.list_devices()[0]
        assert device.address == 101
        assert device.name == "lab-virtual"
        assert device.online is False
        assert device.last_seen == 1710000000.0
        metric = db.get_metric(101, 0)
        assert metric is not None
        assert metric.name == "temp_c"
        assert metric.unit == "C"
        assert metric.type == int(MetricType.FLOAT)
        row = db.iter_samples_after(0)[0]
        assert row.value_real == 3.25
        assert row.ts == 1710000000.0
    finally:
        db.close()


def test_create_virtual_does_not_replace_existing_metric(tmp_path: Path):
    db = Storage(tmp_path / "primary.db")
    _seed(db)
    csv_path = tmp_path / "readings.csv"
    csv_path.write_text("1000; 1;\n", encoding="utf-8")
    try:
        import_virtual_readings(
            db,
            csv_path,
            1,
            0,
            create_virtual=True,
            metric_name="other",
            unit="K",
        )
        metric = db.get_metric(1, 0)
        assert metric is not None
        assert metric.name == "temp_c"
        assert metric.unit == "C"
        assert db.list_devices()[0].name == "node-a"
    finally:
        db.close()


def test_script_imports_csv(tmp_path: Path):
    db_path = tmp_path / "primary.db"
    csv_path = tmp_path / "readings.csv"
    csv_path.write_text("1710000000; 21,5;\n1710000060; 22;\n", encoding="utf-8")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "collector")
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--database",
            str(db_path),
            "--address",
            "101",
            "--metric-id",
            "0",
            "--create-virtual",
            "--once",
            "--input",
            str(csv_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    body = json.loads(proc.stdout)
    assert body["inserted_samples"] == 2
    assert body["address"] == 101

    missing = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--address",
            "101",
            "--metric-id",
            "0",
            "--input",
            str(csv_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert missing.returncode == 2


def test_repeat_import_skips_existing_timestamps(tmp_path: Path):
    db = Storage(tmp_path / "primary.db")
    _seed(db)
    csv_path = tmp_path / "readings.csv"
    csv_path.write_text("1000; 21,5;\n1001; 22;\n", encoding="utf-8")
    try:
        first = import_virtual_readings(db, csv_path, 1, 0)
        assert first["inserted_samples"] == 2
        assert first["skipped_samples"] == 0
        second = import_virtual_readings(db, csv_path, 1, 0)
        assert second["inserted_samples"] == 0
        assert second["skipped_samples"] == 2
        assert db.sample_count() == 3
    finally:
        db.close()


def test_follow_writes_one_row_per_interval_until_stopped(tmp_path: Path):
    db = Storage(tmp_path / "primary.db")
    _seed(db)
    csv_path = tmp_path / "readings.csv"
    csv_path.write_text("1000; 21,5;\n1001; 22;\n1002; 23;\n", encoding="utf-8")
    waits: list[float | None] = []

    def wait(seconds: float | None) -> bool:
        waits.append(seconds)
        return seconds is not None

    try:
        result = follow_virtual_readings(db, csv_path, 1, 0, 5, wait=wait)
        assert result["inserted_samples"] == 1
        assert result["skipped_samples"] == 0
        assert result["remaining_samples"] == 2
        assert result["stopped"] is True
        assert waits == [5]
        stored = [row for row in db.iter_samples_after(0) if row.ts == 1000]
        assert stored[0].value_real == 21.5
        assert db.sample_count() == 2
    finally:
        db.close()


def test_follow_skips_stored_rows_without_waiting(tmp_path: Path):
    db = Storage(tmp_path / "primary.db")
    _seed(db)
    csv_path = tmp_path / "readings.csv"
    csv_path.write_text("1000; 21;\n1001; 22;\n1002; 23;\n", encoding="utf-8")
    import_virtual_readings(db, csv_path, 1, 0)
    waits: list[float | None] = []

    def wait(seconds: float | None) -> bool:
        waits.append(seconds)
        return False

    try:
        before = db.sample_count()
        result = follow_virtual_readings(db, csv_path, 1, 0, 5, wait=wait)
        assert result["inserted_samples"] == 0
        assert result["skipped_samples"] == 3
        assert result["remaining_samples"] == 0
        assert waits == [None]
        assert db.sample_count() == before
    finally:
        db.close()


def test_follow_waits_after_the_file_is_loaded(tmp_path: Path):
    db = Storage(tmp_path / "primary.db")
    _seed(db)
    csv_path = tmp_path / "readings.csv"
    csv_path.write_text("1000; 21;\n1001; 22;\n", encoding="utf-8")
    waits: list[float | None] = []

    def wait(seconds: float | None) -> bool:
        waits.append(seconds)
        return False

    try:
        result = follow_virtual_readings(db, csv_path, 1, 0, 2.5, wait=wait)
        assert result["inserted_samples"] == 2
        assert result["remaining_samples"] == 0
        assert waits == [2.5, None]
        loaded = [
            (row.ts, row.value_real)
            for row in db.iter_samples_after(0)
            if row.ts in (1000.0, 1001.0)
        ]
        assert loaded == [(1000.0, 21.0), (1001.0, 22.0)]
    finally:
        db.close()


def test_follow_rejects_bad_interval_and_bad_values(tmp_path: Path):
    db = Storage(tmp_path / "primary.db")
    _seed(db)
    csv_path = tmp_path / "bad.csv"
    csv_path.write_text("1000; 1;\n1001; 2;\n", encoding="utf-8")
    called = False

    def wait(seconds: float | None) -> bool:
        nonlocal called
        called = True
        return True

    try:
        with pytest.raises(ValueError, match="every"):
            follow_virtual_readings(db, csv_path, 1, 1, 0, wait=wait)
        before = db.sample_count()
        with pytest.raises(ReadingsFileError, match="line 2"):
            follow_virtual_readings(db, csv_path, 1, 1, 1, wait=wait)
        assert db.sample_count() == before
        assert called is False
    finally:
        db.close()


def test_script_stops_before_loading_the_rest(tmp_path: Path):
    import signal
    import time

    db_path = tmp_path / "primary.db"
    csv_path = tmp_path / "readings.csv"
    csv_path.write_text("1710000000; 21,5;\n1710000060; 22;\n", encoding="utf-8")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "collector")
    proc = subprocess.Popen(
        [
            sys.executable,
            str(SCRIPT),
            "--database",
            str(db_path),
            "--address",
            "101",
            "--metric-id",
            "0",
            "--create-virtual",
            "--every",
            "60",
            "--input",
            str(csv_path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    deadline = time.time() + 15
    seen = False
    while time.time() < deadline:
        if db_path.exists():
            db = Storage(db_path)
            try:
                seen = db.sample_count() >= 1
            finally:
                db.close()
            if seen:
                break
        if proc.poll() is not None:
            break
        time.sleep(0.05)
    proc.send_signal(signal.SIGTERM)
    stdout, stderr = proc.communicate(timeout=10)
    assert proc.returncode == 0, stderr
    assert seen, stderr
    body = json.loads(stdout)
    assert body["inserted_samples"] == 1
    assert body["remaining_samples"] == 1
    db = Storage(db_path)
    try:
        rows = db.iter_samples_after(0)
        assert [(row.ts, row.value_real) for row in rows] == [(1710000000.0, 21.5)]
    finally:
        db.close()


def test_empty_file_inserts_nothing(tmp_path: Path):
    db = Storage(tmp_path / "primary.db")
    _seed(db)
    csv_path = tmp_path / "empty.csv"
    csv_path.write_text("# nothing\n\n", encoding="utf-8")
    try:
        result = import_virtual_readings(db, csv_path, 1, 0)
        assert result["inserted_samples"] == 0
        assert db.sample_count() == 1
    finally:
        db.close()
