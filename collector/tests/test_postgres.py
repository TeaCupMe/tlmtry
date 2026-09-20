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
from tlmtry_collector.replicate import backup_ready, replicate
from tlmtry_collector.storage import Storage, is_postgres_dsn, mask_database_url

POSTGRES_URL = os.environ.get(
    "TLMTRY_TEST_PG",
    "postgresql://tlmtry:tlmtry@127.0.0.1:5432/tlmtry",
)
POSTGRES_BACKUP_URL = os.environ.get(
    "TLMTRY_TEST_PG_BACKUP",
    "postgresql://tlmtry:tlmtry@127.0.0.1:5432/tlmtry_backup",
)


def _pg_available(url: str = POSTGRES_URL) -> bool:
    try:
        import psycopg
    except ImportError:
        return False
    try:
        with psycopg.connect(url, connect_timeout=2) as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False


requires_postgres = pytest.mark.skipif(
    not _pg_available(),
    reason="PostgreSQL not available (set TLMTRY_TEST_PG)",
)

requires_postgres_backup = pytest.mark.skipif(
    not (_pg_available() and _pg_available(POSTGRES_BACKUP_URL)),
    reason="PostgreSQL primary/backup DBs not available",
)


def _reset_pg(url: str) -> None:
    import psycopg

    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(
            """
            DROP TABLE IF EXISTS samples CASCADE;
            DROP TABLE IF EXISTS metrics CASCADE;
            DROP TABLE IF EXISTS devices CASCADE;
            DROP TABLE IF EXISTS replication_state CASCADE;
            """
        )


def test_dsn_helpers():
    assert is_postgres_dsn("postgresql://u:p@localhost/db")
    assert is_postgres_dsn("postgres://localhost/db")
    assert not is_postgres_dsn("/var/lib/tlmtry/primary.db")
    assert (
        mask_database_url("postgresql://tlmtry:secret@db.example:5432/tlmtry")
        == "postgresql://tlmtry:***@db.example:5432/tlmtry"
    )
    assert backup_ready("postgresql://tlmtry:x@127.0.0.1:5432/tlmtry")
    assert not backup_ready("/no/such/usb/mount/backup.db")


def _seed_primary(storage: Storage) -> None:
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


def _assert_incremental_replicate(primary: Storage, backup: str | Path) -> None:
    assert primary.sample_count() == 2
    r1 = replicate(primary, backup)
    assert r1["copied_samples"] == 2
    r2 = replicate(primary, backup)
    assert r2["copied_samples"] == 0
    primary.insert_samples(1, [SampleValue(0, MetricType.FLOAT, 22.0)])
    r3 = replicate(primary, backup)
    assert r3["copied_samples"] == 1
    dest = Storage(backup)
    try:
        assert dest.sample_count() == 3
        assert dest.list_devices()[0].name == "node-a"
        assert {m.name for m in dest.list_metrics()} == {"temp_c", "ok"}
    finally:
        dest.close()


@requires_postgres
def test_postgres_primary_to_sqlite_backup(tmp_path: Path):
    _reset_pg(POSTGRES_URL)
    storage = Storage(POSTGRES_URL)
    try:
        assert storage.backend == "postgres"
        assert "***" in storage.location or "postgresql://" in storage.location
        _seed_primary(storage)
        _assert_incremental_replicate(storage, tmp_path / "backup.db")
    finally:
        storage.close()


@requires_postgres
def test_sqlite_primary_to_postgres_backup(tmp_path: Path):
    _reset_pg(POSTGRES_URL)
    primary = Storage(tmp_path / "primary.db")
    try:
        _seed_primary(primary)
        _assert_incremental_replicate(primary, POSTGRES_URL)
    finally:
        primary.close()


@requires_postgres_backup
def test_postgres_to_postgres_replicate():
    _reset_pg(POSTGRES_URL)
    _reset_pg(POSTGRES_BACKUP_URL)
    primary = Storage(POSTGRES_URL)
    try:
        _seed_primary(primary)
        _assert_incremental_replicate(primary, POSTGRES_BACKUP_URL)
    finally:
        primary.close()
