"""SQLite storage for devices, metrics, samples, and replication watermark."""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .protocol import DeviceManifest, MetricType, SampleValue


SCHEMA = """
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


class Storage:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
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
            self._conn.execute(
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
            self._conn.execute(
                "DELETE FROM metrics WHERE address = ?", (manifest.address,)
            )
            for m in manifest.metrics:
                self._conn.execute(
                    """
                    INSERT INTO metrics(address, metric_id, name, type, unit)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (manifest.address, m.metric_id, m.name, int(m.type), m.unit),
                )

    def list_devices(self) -> list[DeviceRow]:
        rows = self._conn.execute(
            """
            SELECT address, device_id, name, fw_major, fw_minor,
                   poll_interval_ms, last_seen, online, last_error
            FROM devices ORDER BY address
            """
        ).fetchall()
        return [
            DeviceRow(
                address=r["address"],
                device_id=r["device_id"],
                name=r["name"],
                fw_major=r["fw_major"],
                fw_minor=r["fw_minor"],
                poll_interval_ms=r["poll_interval_ms"],
                last_seen=r["last_seen"],
                online=bool(r["online"]),
                last_error=r["last_error"],
            )
            for r in rows
        ]

    def mark_online(self, address: int, online: bool, error: str | None = None) -> None:
        now = time.time()
        with self.transaction():
            if online:
                self._conn.execute(
                    """
                    UPDATE devices SET online=1, last_seen=?, last_error=NULL
                    WHERE address=?
                    """,
                    (now, address),
                )
            else:
                self._conn.execute(
                    """
                    UPDATE devices SET online=0, last_error=?
                    WHERE address=?
                    """,
                    (error, address),
                )

    def insert_samples(self, address: int, samples: list[SampleValue], ts: float | None = None) -> None:
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
                self._conn.execute(
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
            self._conn.execute(
                "UPDATE devices SET last_seen=?, online=1, last_error=NULL WHERE address=?",
                (stamp, address),
            )

    def sample_count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS c FROM samples").fetchone()
        return int(row["c"])

    def get_replication_watermark(self, key: str = "samples_id") -> int:
        row = self._conn.execute(
            "SELECT value FROM replication_state WHERE key = ?", (key,)
        ).fetchone()
        if not row:
            return 0
        return int(row["value"])

    def set_replication_watermark(self, value: int, key: str = "samples_id") -> None:
        with self.transaction():
            self._conn.execute(
                """
                INSERT INTO replication_state(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (key, str(value)),
            )

    def iter_samples_after(self, after_id: int) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                """
                SELECT id, address, metric_id, ts, value_real, value_text, type, quality
                FROM samples WHERE id > ? ORDER BY id
                """,
                (after_id,),
            ).fetchall()
        )

    def connection(self) -> sqlite3.Connection:
        return self._conn
