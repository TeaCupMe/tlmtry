#!/usr/bin/env python3
"""Пример чтения телеметрии tlmtry из PostgreSQL.

Требования:
  pip install "tlmtry-collector[postgres]"
  # или из корня репозитория:
  #   cd collector && pip install -e ".[postgres]"

Переменная окружения (или правка DSN ниже):
  export TLMTRY_DATABASE='postgresql://tlmtry:secret@127.0.0.1:5432/tlmtry'

Запуск:
  python examples/read_telemetry_postgres.py
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone

from tlmtry_collector.protocol import MetricType
from tlmtry_collector.storage import Storage

# Типы метрик (как в docs/protocol.md)
METRIC_TYPE_NAMES = {
    int(MetricType.INT32): "i32",
    int(MetricType.FLOAT): "f32",
    int(MetricType.BOOL): "bool",
    int(MetricType.UINT16): "u16",
}


def format_ts(ts: float | None) -> str:
    if ts is None:
        return "-"
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def sample_value(value_real: float | None, value_text: str | None) -> str:
    if value_text is not None:
        return value_text
    if value_real is None:
        return "NULL"
    return str(value_real)


def via_storage_api(dsn: str) -> None:
    """Высокоуровневый доступ через Storage (устройства, метрики, счётчик)."""
    storage = Storage(dsn)
    try:
        print(f"database: {storage.location} ({storage.backend})")
        print(f"devices:  {len(storage.list_devices())}")
        print(f"samples:  {storage.sample_count()}")
        print(f"watermark:{storage.get_replication_watermark()}")
        print()

        print("=== devices ===")
        for d in storage.list_devices():
            print(
                f"  addr={d.address} name={d.name!r} online={d.online} "
                f"interval_ms={d.poll_interval_ms} last_seen={format_ts(d.last_seen)} "
                f"err={d.last_error!r}"
            )

        print("\n=== metrics catalog ===")
        for m in storage.list_metrics():
            type_name = METRIC_TYPE_NAMES.get(m.type, str(m.type))
            print(
                f"  addr={m.address} id={m.metric_id} "
                f"name={m.name!r} type={type_name} unit={m.unit!r}"
            )
    finally:
        storage.close()


def via_sql(dsn: str, *, last_minutes: int = 10, limit: int = 20) -> None:
    """Прямой SQL через сырое соединение Storage (psycopg).

    Удобно для JOIN samples ↔ devices ↔ metrics и фильтров по времени.
    """
    storage = Storage(dsn)
    try:
        if storage.backend != "postgres":
            raise SystemExit(
                f"Ожидался PostgreSQL URL, получено backend={storage.backend!r}"
            )

        conn = storage.connection()
        since = time.time() - last_minutes * 60

        sql = """
            SELECT
                s.id,
                s.ts,
                d.name AS device,
                m.name AS metric,
                m.unit,
                s.value_real,
                s.value_text,
                s.type
            FROM samples s
            JOIN devices d ON d.address = s.address
            JOIN metrics m
              ON m.address = s.address AND m.metric_id = s.metric_id
            WHERE s.ts >= %s
            ORDER BY s.id DESC
            LIMIT %s
        """
        with conn.cursor() as cur:
            cur.execute(sql, (since, limit))
            rows = cur.fetchall()

        print(f"\n=== last samples (last {last_minutes} min, limit {limit}) ===")
        if not rows:
            print("  (нет строк — запустите collector или увеличьте окно)")
            return

        for r in rows:
            # psycopg dict_row → dict; иначе tuple
            if isinstance(r, dict):
                rid, ts, device, metric, unit = (
                    r["id"],
                    r["ts"],
                    r["device"],
                    r["metric"],
                    r["unit"],
                )
                value = sample_value(r["value_real"], r["value_text"])
                type_name = METRIC_TYPE_NAMES.get(int(r["type"]), str(r["type"]))
            else:
                rid, ts, device, metric, unit, value_real, value_text, mtype = r
                value = sample_value(value_real, value_text)
                type_name = METRIC_TYPE_NAMES.get(int(mtype), str(mtype))

            print(
                f"  id={rid} t={format_ts(float(ts))} "
                f"{device}.{metric}={value} {unit} ({type_name})"
            )
    finally:
        storage.close()


def one_metric_series(
    dsn: str,
    *,
    device_name: str = "lab-node-1",
    metric_name: str = "temp_c",
    last_minutes: int = 60,
) -> None:
    """Временной ряд одной метрики по имени устройства и метрики."""
    storage = Storage(dsn)
    try:
        conn = storage.connection()
        since = time.time() - last_minutes * 60
        sql = """
            SELECT s.ts, s.value_real, s.value_text
            FROM samples s
            JOIN devices d ON d.address = s.address
            JOIN metrics m
              ON m.address = s.address AND m.metric_id = s.metric_id
            WHERE d.name = %s AND m.name = %s AND s.ts >= %s
            ORDER BY s.ts
        """
        with conn.cursor() as cur:
            cur.execute(sql, (device_name, metric_name, since))
            rows = list(cur.fetchall())

        print(
            f"\n=== series {device_name!r}.{metric_name!r} "
            f"(last {last_minutes} min, {len(rows)} points) ==="
        )
        for r in rows[-10:]:  # хвост для краткости
            if isinstance(r, dict):
                ts, value = r["ts"], sample_value(r["value_real"], r["value_text"])
            else:
                ts, value_real, value_text = r
                value = sample_value(value_real, value_text)
            print(f"  {format_ts(float(ts))}  {value}")
        if len(rows) > 10:
            print(f"  ... ({len(rows) - 10} earlier points omitted)")
    finally:
        storage.close()


def main() -> None:
    dsn = os.environ.get(
        "TLMTRY_DATABASE",
        "postgresql://tlmtry:secret@127.0.0.1:5432/tlmtry",
    )
    if not dsn.startswith(("postgres://", "postgresql://")):
        raise SystemExit(
            "Задайте PostgreSQL URL в TLMTRY_DATABASE "
            "(например postgresql://user:pass@127.0.0.1:5432/tlmtry)"
        )

    via_storage_api(dsn)
    via_sql(dsn, last_minutes=10, limit=20)
    # Имена из симулятора (run --sim); для стенда подставьте свои
    one_metric_series(dsn, device_name="lab-node-1", metric_name="temp_c")


if __name__ == "__main__":
    main()
