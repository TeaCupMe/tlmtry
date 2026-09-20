"""Incremental replication from primary DB to a backup SQLite or PostgreSQL target."""

from __future__ import annotations

import logging
import time
from pathlib import Path

from .storage import Storage, is_postgres_dsn, mask_database_url

log = logging.getLogger(__name__)


def backup_ready(backup: str | Path) -> bool:
    """
    Return False when a SQLite backup parent path is missing (e.g. USB unmounted).

    PostgreSQL URLs are always considered ready; connectivity is checked on open.
    """
    text = str(backup)
    if is_postgres_dsn(text):
        return True
    return Path(text).parent.exists()


def replicate(primary: Storage, backup: str | Path) -> dict[str, int]:
    """
    Copy device/metric metadata and new samples to backup.

    Uses primary replication_state watermark `samples_id`. Destination may be a
    SQLite file path or a postgres:// / postgresql:// URL.
    """
    watermark = primary.get_replication_watermark()
    rows = primary.iter_samples_after(watermark)
    devices = primary.list_devices()
    metrics = primary.list_metrics()

    dest = Storage(backup)
    try:
        dest.apply_replication(devices, metrics, rows, replicated_at=time.time())
        if rows:
            primary.set_replication_watermark(max(r.id for r in rows))

        result = {
            "copied_samples": len(rows),
            "watermark_before": watermark,
            "watermark_after": primary.get_replication_watermark(),
        }
        dest_label = (
            mask_database_url(str(backup))
            if is_postgres_dsn(backup)
            else str(backup)
        )
        log.info(
            "replication complete: %s -> %s (%s)",
            primary.location,
            dest_label,
            result,
        )
        return result
    finally:
        dest.close()
