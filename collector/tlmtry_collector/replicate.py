"""Incremental replication from primary DB to a backup SQLite file."""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

from .storage import SCHEMA, Storage

log = logging.getLogger(__name__)


def replicate(primary: Storage, backup_path: str | Path) -> dict[str, int]:
    """
    Copy device/metric metadata and new samples to backup_path.

    Uses primary replication_state watermark `samples_id`. Safe to call when
    backup media is only occasionally available. Destination is always SQLite
    (portable file for USB/offline backup), regardless of primary backend.
    """
    backup_path = Path(backup_path)
    backup_path.parent.mkdir(parents=True, exist_ok=True)

    watermark = primary.get_replication_watermark()
    rows = primary.iter_samples_after(watermark)
    devices = primary.list_devices()
    metrics = primary.list_metrics()

    bconn = sqlite3.connect(backup_path)
    try:
        bconn.executescript(SCHEMA)
        with bconn:
            for d in devices:
                bconn.execute(
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
                bconn.execute(
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

            max_id = watermark
            for r in rows:
                bconn.execute(
                    """
                    INSERT OR IGNORE INTO samples(
                        id, address, metric_id, ts, value_real, value_text, type, quality
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
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
                max_id = max(max_id, int(r.id))

            bconn.execute(
                """
                INSERT INTO replication_state(key, value) VALUES ('last_replicate_ts', ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (str(time.time()),),
            )

        if rows:
            primary.set_replication_watermark(max_id)

        result = {
            "copied_samples": len(rows),
            "watermark_before": watermark,
            "watermark_after": primary.get_replication_watermark(),
        }
        log.info(
            "replication complete: %s -> %s (%s)",
            primary.location,
            backup_path,
            result,
        )
        return result
    finally:
        bconn.close()
