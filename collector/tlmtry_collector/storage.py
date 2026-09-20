"""Storage for devices, metrics, samples, and replication watermark.

Supports SQLite (local file path) and PostgreSQL (postgres:// or
postgresql:// connection URL).
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Literal
from urllib.parse import urlparse, urlunparse

from .protocol import DeviceManifest, MetricType, SampleValue

Backend = Literal["sqlite", "postgres"]

SQLITE_SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS devices (
    address INTEGER PRIMARY KEY,
    device_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    fw_major INTEGER NOT NULL,
    fw_minor INTEGER NOT NULL,
    poll_interval_ms INTEGER NOT NULL,
    last_seen REAL,
    online INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
);

CREATE TABLE IF NOT EXISTS metrics (
    address INTEGER NOT NULL,
    metric_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    type INTEGER NOT NULL,
    unit TEXT NOT NULL,
    PRIMARY KEY (address, metric_id),
    FOREIGN KEY (address) REFERENCES devices(address)
);

CREATE TABLE IF NOT EXISTS samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    address INTEGER NOT NULL,
    metric_id INTEGER NOT NULL,
    ts REAL NOT NULL,
    value_real REAL,
    value_text TEXT,
    type INTEGER NOT NULL,
    quality INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_samples_addr_ts ON samples(address, ts);
CREATE INDEX IF NOT EXISTS idx_samples_id ON samples(id);

CREATE TABLE IF NOT EXISTS replication_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Alias kept for replicate.py / callers that apply schema to a SQLite backup.
SCHEMA = SQLITE_SCHEMA

POSTGRES_SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    address INTEGER PRIMARY KEY,
    device_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    fw_major INTEGER NOT NULL,
    fw_minor INTEGER NOT NULL,
    poll_interval_ms INTEGER NOT NULL,
    last_seen DOUBLE PRECISION,
    online INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
);

CREATE TABLE IF NOT EXISTS metrics (
    address INTEGER NOT NULL,
    metric_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    type INTEGER NOT NULL,
    unit TEXT NOT NULL,
    PRIMARY KEY (address, metric_id),
    FOREIGN KEY (address) REFERENCES devices(address)
);

CREATE TABLE IF NOT EXISTS samples (
    id BIGSERIAL PRIMARY KEY,
    address INTEGER NOT NULL,
    metric_id INTEGER NOT NULL,
    ts DOUBLE PRECISION NOT NULL,
    value_real DOUBLE PRECISION,
    value_text TEXT,
    type INTEGER NOT NULL,
    quality INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_samples_addr_ts ON samples(address, ts);

CREATE TABLE IF NOT EXISTS replication_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def is_postgres_dsn(database: str | Path) -> bool:
    text = str(database).strip()
    return text.startswith(("postgres://", "postgresql://"))


def mask_database_url(database: str) -> str:
    """Hide password in a Postgres URL for logs and status output."""
    if not is_postgres_dsn(database):
        return database
    parsed = urlparse(database)
    if not parsed.password:
        return database
    user = parsed.username or ""
    host = parsed.hostname or ""
    netloc = f"{user}:***@{host}"
    if parsed.port is not None:
        netloc += f":{parsed.port}"
    return urlunparse(
        (parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment)
    )


@dataclass
class DeviceRow:
    address: int
    device_id: int
    name: str
    fw_major: int
    fw_minor: int
    poll_interval_ms: int
    last_seen: float | None
    online: bool
    last_error: str | None


@dataclass
class SampleRow:
    id: int
    address: int
    metric_id: int
    ts: float
    value_real: float | None
    value_text: str | None
    type: int
    quality: int


@dataclass
class MetricCatalogRow:
    address: int
    metric_id: int
    name: str
    type: int
    unit: str


class Storage:
    def __init__(self, database: str | Path) -> None:
        self._database = str(database)
        if is_postgres_dsn(self._database):
            self._init_postgres(self._database)
        else:
            self._init_sqlite(self._database)

    def _init_sqlite(self, path: str) -> None:
        self.backend: Backend = "sqlite"
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Any = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SQLITE_SCHEMA)
        self._conn.commit()

    def _init_postgres(self, dsn: str) -> None:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:  # pragma: no cover - exercised when extra missing
            raise ImportError(
                "PostgreSQL support requires the 'postgres' extra: "
                'pip install "tlmtry-collector[postgres]"'
            ) from exc

        self.backend = "postgres"
        # Keep path as a Path-like display token for callers that stringify it.
        self.path = Path(mask_database_url(dsn))
        self._conn = psycopg.connect(dsn, row_factory=dict_row, autocommit=False)
        statements = [s.strip() for s in POSTGRES_SCHEMA.split(";") if s.strip()]
        with self._conn.cursor() as cur:
            for statement in statements:
                cur.execute(statement)
        self._conn.commit()

    @property
    def location(self) -> str:
        if self.backend == "postgres":
            return mask_database_url(self._database)
        return str(self.path)

    def close(self) -> None:
        self._conn.close()

    def _sql(self, sql: str) -> str:
        if self.backend == "postgres":
            return sql.replace("?", "%s")
        return sql

    def _execute(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> Any:
        if self.backend == "postgres":
            with self._conn.cursor() as cur:
                cur.execute(self._sql(sql), params)
            return None
        return self._conn.execute(self._sql(sql), params)

    def _fetchone(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> Any:
        if self.backend == "postgres":
            with self._conn.cursor() as cur:
                cur.execute(self._sql(sql), params)
                return cur.fetchone()
        return self._conn.execute(self._sql(sql), params).fetchone()

    def _fetchall(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> list[Any]:
        if self.backend == "postgres":
            with self._conn.cursor() as cur:
                cur.execute(self._sql(sql), params)
                return list(cur.fetchall())
        return list(self._conn.execute(self._sql(sql), params).fetchall())

    def _row_get(self, row: Any, key: str) -> Any:
        if isinstance(row, dict):
            return row[key]
        return row[key]

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def upsert_manifest(
        self, manifest: DeviceManifest, poll_interval_ms: int | None = None
    ) -> None:
        interval = (
            poll_interval_ms
            if poll_interval_ms is not None
            else manifest.poll_interval_ms
        )
        now = time.time()
        with self.transaction():
            self._execute(
                """
                INSERT INTO devices(
                    address, device_id, name, fw_major, fw_minor,
                    poll_interval_ms, last_seen, online, last_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, NULL)
                ON CONFLICT(address) DO UPDATE SET
                    device_id=excluded.device_id,
                    name=excluded.name,
                    fw_major=excluded.fw_major,
                    fw_minor=excluded.fw_minor,
                    poll_interval_ms=excluded.poll_interval_ms,
                    last_seen=excluded.last_seen,
                    online=1,
                    last_error=NULL
                """,
                (
                    manifest.address,
                    manifest.device_id,
                    manifest.name,
                    manifest.fw_major,
                    manifest.fw_minor,
                    interval,
                    now,
                ),
            )
            self._execute("DELETE FROM metrics WHERE address = ?", (manifest.address,))
            for m in manifest.metrics:
                self._execute(
                    """
                    INSERT INTO metrics(address, metric_id, name, type, unit)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (manifest.address, m.metric_id, m.name, int(m.type), m.unit),
                )

    def list_devices(self) -> list[DeviceRow]:
        rows = self._fetchall(
            """
            SELECT address, device_id, name, fw_major, fw_minor,
                   poll_interval_ms, last_seen, online, last_error
            FROM devices ORDER BY address
            """
        )
        return [
            DeviceRow(
                address=int(self._row_get(r, "address")),
                device_id=int(self._row_get(r, "device_id")),
                name=str(self._row_get(r, "name")),
                fw_major=int(self._row_get(r, "fw_major")),
                fw_minor=int(self._row_get(r, "fw_minor")),
                poll_interval_ms=int(self._row_get(r, "poll_interval_ms")),
                last_seen=self._row_get(r, "last_seen"),
                online=bool(self._row_get(r, "online")),
                last_error=self._row_get(r, "last_error"),
            )
            for r in rows
        ]

    def list_metrics(self) -> list[MetricCatalogRow]:
        rows = self._fetchall(
            "SELECT address, metric_id, name, type, unit FROM metrics"
        )
        return [
            MetricCatalogRow(
                address=int(self._row_get(r, "address")),
                metric_id=int(self._row_get(r, "metric_id")),
                name=str(self._row_get(r, "name")),
                type=int(self._row_get(r, "type")),
                unit=str(self._row_get(r, "unit")),
            )
            for r in rows
        ]

    def mark_online(self, address: int, online: bool, error: str | None = None) -> None:
        now = time.time()
        with self.transaction():
            if online:
                self._execute(
                    """
                    UPDATE devices SET online=1, last_seen=?, last_error=NULL
                    WHERE address=?
                    """,
                    (now, address),
                )
            else:
                self._execute(
                    """
                    UPDATE devices SET online=0, last_error=?
                    WHERE address=?
                    """,
                    (error, address),
                )

    def insert_samples(
        self, address: int, samples: list[SampleValue], ts: float | None = None
    ) -> None:
        if not samples:
            return
        stamp = ts if ts is not None else time.time()
        with self.transaction():
            for s in samples:
                value_real: float | None
                value_text: str | None
                if s.type == MetricType.BOOL:
                    value_real = 1.0 if s.value else 0.0
                    value_text = "true" if s.value else "false"
                elif s.type in (MetricType.INT32, MetricType.UINT16, MetricType.FLOAT):
                    value_real = float(s.value)
                    value_text = None
                else:
                    value_real = None
                    value_text = str(s.value)
                self._execute(
                    """
                    INSERT INTO samples(
                        address, metric_id, ts, value_real, value_text, type, quality
                    ) VALUES (?, ?, ?, ?, ?, ?, 0)
                    """,
                    (
                        address,
                        s.metric_id,
                        stamp,
                        value_real,
                        value_text,
                        int(s.type),
                    ),
                )
            self._execute(
                "UPDATE devices SET last_seen=?, online=1, last_error=NULL WHERE address=?",
                (stamp, address),
            )

    def sample_count(self) -> int:
        row = self._fetchone("SELECT COUNT(*) AS c FROM samples")
        return int(self._row_get(row, "c"))

    def get_replication_watermark(self, key: str = "samples_id") -> int:
        row = self._fetchone(
            "SELECT value FROM replication_state WHERE key = ?", (key,)
        )
        if not row:
            return 0
        return int(self._row_get(row, "value"))

    def set_replication_watermark(self, value: int, key: str = "samples_id") -> None:
        with self.transaction():
            self._execute(
                """
                INSERT INTO replication_state(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (key, str(value)),
            )

    def iter_samples_after(self, after_id: int) -> list[SampleRow]:
        rows = self._fetchall(
            """
            SELECT id, address, metric_id, ts, value_real, value_text, type, quality
            FROM samples WHERE id > ? ORDER BY id
            """,
            (after_id,),
        )
        return [
            SampleRow(
                id=int(self._row_get(r, "id")),
                address=int(self._row_get(r, "address")),
                metric_id=int(self._row_get(r, "metric_id")),
                ts=float(self._row_get(r, "ts")),
                value_real=self._row_get(r, "value_real"),
                value_text=self._row_get(r, "value_text"),
                type=int(self._row_get(r, "type")),
                quality=int(self._row_get(r, "quality")),
            )
            for r in rows
        ]

    def apply_replication(
        self,
        devices: list[DeviceRow],
        metrics: list[MetricCatalogRow],
        samples: list[SampleRow],
        *,
        replicated_at: float | None = None,
    ) -> None:
        """Upsert catalog rows and insert samples by explicit id (idempotent)."""
        stamp = replicated_at if replicated_at is not None else time.time()
        sample_sql = (
            """
            INSERT INTO samples(
                id, address, metric_id, ts, value_real, value_text, type, quality
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO NOTHING
            """
            if self.backend == "postgres"
            else """
            INSERT OR IGNORE INTO samples(
                id, address, metric_id, ts, value_real, value_text, type, quality
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """
        )
        with self.transaction():
            for d in devices:
                self._execute(
                    """
                    INSERT INTO devices(
                        address, device_id, name, fw_major, fw_minor,
                        poll_interval_ms, last_seen, online, last_error
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(address) DO UPDATE SET
                        device_id=excluded.device_id,
                        name=excluded.name,
                        fw_major=excluded.fw_major,
                        fw_minor=excluded.fw_minor,
                        poll_interval_ms=excluded.poll_interval_ms,
                        last_seen=excluded.last_seen,
                        online=excluded.online,
                        last_error=excluded.last_error
                    """,
                    (
                        d.address,
                        d.device_id,
                        d.name,
                        d.fw_major,
                        d.fw_minor,
                        d.poll_interval_ms,
                        d.last_seen,
                        1 if d.online else 0,
                        d.last_error,
                    ),
                )
            for m in metrics:
                self._execute(
                    """
                    INSERT INTO metrics(address, metric_id, name, type, unit)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(address, metric_id) DO UPDATE SET
                        name=excluded.name,
                        type=excluded.type,
                        unit=excluded.unit
                    """,
                    (m.address, m.metric_id, m.name, m.type, m.unit),
                )
            for r in samples:
                self._execute(
                    sample_sql,
                    (
                        r.id,
                        r.address,
                        r.metric_id,
                        r.ts,
                        r.value_real,
                        r.value_text,
                        r.type,
                        r.quality,
                    ),
                )
            if samples and self.backend == "postgres":
                # Keep BIGSERIAL in sync after explicit id inserts.
                self._fetchone(
                    """
                    SELECT setval(
                        pg_get_serial_sequence('samples', 'id'),
                        COALESCE((SELECT MAX(id) FROM samples), 1)
                    ) AS v
                    """
                )
            self._execute(
                """
                INSERT INTO replication_state(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                ("last_replicate_ts", str(stamp)),
            )

    def connection(self) -> Any:
        """Return the raw DB-API connection (sqlite3 or psycopg)."""
        return self._conn
