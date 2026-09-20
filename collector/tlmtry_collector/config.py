"""Load collector YAML configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class AppConfig:
    port: str = "/dev/ttyUSB0"
    baudrate: int = 115200
    # SQLite file path, or postgres:// / postgresql:// URL
    database: str = "data/primary.db"
    response_timeout_ms: int = 200
    rediscover_every_s: float = 300.0
    address_min: int = 1
    address_max: int = 100
    use_broadcast: bool = True
    interval_overrides: dict[int, int] = field(default_factory=dict)
    backup_path: str | None = None  # always a SQLite file path for offline backup
    replicate_every_s: float | None = None
    log_level: str = "INFO"


def load_config(path: str | Path | None) -> AppConfig:
    if path is None:
        return AppConfig()
    raw: dict[str, Any] = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    overrides = {int(k): int(v) for k, v in (raw.get("interval_overrides") or {}).items()}
    return AppConfig(
        port=str(raw.get("port", AppConfig.port)),
        baudrate=int(raw.get("baudrate", AppConfig.baudrate)),
        database=str(raw.get("database", AppConfig.database)),
        response_timeout_ms=int(
            raw.get("response_timeout_ms", AppConfig.response_timeout_ms)
        ),
        rediscover_every_s=float(
            raw.get("rediscover_every_s", AppConfig.rediscover_every_s)
        ),
        address_min=int(raw.get("address_min", AppConfig.address_min)),
        address_max=int(raw.get("address_max", AppConfig.address_max)),
        use_broadcast=bool(raw.get("use_broadcast", AppConfig.use_broadcast)),
        interval_overrides=overrides,
        backup_path=raw.get("backup_path"),
        replicate_every_s=(
            float(raw["replicate_every_s"])
            if raw.get("replicate_every_s") is not None
            else None
        ),
        log_level=str(raw.get("log_level", AppConfig.log_level)),
    )
