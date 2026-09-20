from __future__ import annotations

import os
from pathlib import Path

import pytest

from tlmtry_collector.protocol import (
    DeviceManifest,
    MetricInfo,
    MetricType,
    SampleValue,
)
from tlmtry_collector.replicate import replicate
from tlmtry_collector.storage import Storage, is_postgres_dsn, mask_database_url

POSTGRES_URL = os.environ.get(
    "TLMTRY_TEST_PG",
    "postgresql://tlmtry:tlmtry@127.0.0.1:5432/tlmtry",
)


def _pg_available() -> bool:
    try:
        import psycopg
    except ImportError:
        return False
    try:
        with psycopg.connect(POSTGRES_URL, connect_timeout=2) as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False


requires_postgres = pytest.mark.skipif(
    not _pg_available(),
    reason="PostgreSQL not available (set TLMTRY_TEST_PG)",
)


def test_dsn_helpers():
    assert is_postgres_dsn("postgresql://u:p@localhost/db")
    assert is_postgres_dsn("postgres://localhost/db")
    assert not is_postgres_dsn("/var/lib/tlmtry/primary.db")
    assert (
        mask_database_url("postgresql://tlmtry:secret@db.example:5432/tlmtry")
        == "postgresql://tlmtry:***@db.example:5432/tlmtry"
    )


def _exercise_storage(storage: Storage, tmp_path: Path) -> None:
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
            ],
        )
    )
    storage.insert_samples(
        1,
        [
            SampleValue(0, MetricType.FLOAT, 21.5),
            SampleValue(1, MetricType.BOOL, True),
        ],
    )
    assert storage.sample_count() == 2
    devices = storage.list_devices()
    assert len(devices) == 1
    assert devices[0].name == "node-a"
    assert storage.list_metrics()[0].name == "temp_c"

    backup = tmp_path / "backup.db"
    r1 = replicate(storage, backup)
    assert r1["copied_samples"] == 2
    r2 = replicate(storage, backup)
    assert r2["copied_samples"] == 0
    storage.insert_samples(1, [SampleValue(0, MetricType.FLOAT, 22.0)])
    r3 = replicate(storage, backup)
    assert r3["copied_samples"] == 1


@requires_postgres
def test_postgres_storage_and_replicate(tmp_path: Path):
    import psycopg

    with psycopg.connect(POSTGRES_URL, autocommit=True) as conn:
        conn.execute(
            """
            DROP TABLE IF EXISTS samples CASCADE;
            DROP TABLE IF EXISTS metrics CASCADE;
            DROP TABLE IF EXISTS devices CASCADE;
            DROP TABLE IF EXISTS replication_state CASCADE;
            """
        )

    storage = Storage(POSTGRES_URL)
    try:
        assert storage.backend == "postgres"
        assert "***" in storage.location or "postgresql://" in storage.location
        _exercise_storage(storage, tmp_path)
    finally:
        storage.close()
