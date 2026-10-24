#!/usr/bin/env python3
"""Пример чтения телеметрии tlmtry из PostgreSQL через SQLAlchemy.

Требования:
  pip install "sqlalchemy>=2.0" "psycopg[binary]>=3.1"
  # пакет tlmtry-collector не обязателен — пример работает напрямую с БД

Переменная окружения:
  export TLMTRY_DATABASE='postgresql://tlmtry:secret@127.0.0.1:5432/tlmtry'

Запуск:
  python examples/read_telemetry_sqlalchemy.py
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone

from sqlalchemy import (
    Float,
    ForeignKey,
    Integer,
    MetaData,
    String,
    create_engine,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship

METRIC_TYPE_NAMES = {
    0: "i32",
    1: "f32",
    2: "bool",
    3: "u16",
}


class Base(DeclarativeBase):
    metadata = MetaData()


class Device(Base):
    __tablename__ = "devices"

    address: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_id: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String)
    fw_major: Mapped[int] = mapped_column(Integer)
    fw_minor: Mapped[int] = mapped_column(Integer)
    poll_interval_ms: Mapped[int] = mapped_column(Integer)
    last_seen: Mapped[float | None] = mapped_column(Float, nullable=True)
    online: Mapped[bool] = mapped_column(Integer)  # 0/1 в схеме БД
    last_error: Mapped[str | None] = mapped_column(String, nullable=True)

    metrics: Mapped[list[Metric]] = relationship(back_populates="device")


class Metric(Base):
    __tablename__ = "metrics"

    address: Mapped[int] = mapped_column(
        Integer, ForeignKey("devices.address"), primary_key=True
    )
    metric_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    type: Mapped[int] = mapped_column(Integer)
    unit: Mapped[str] = mapped_column(String)

    device: Mapped[Device] = relationship(back_populates="metrics")


class Sample(Base):
    __tablename__ = "samples"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    address: Mapped[int] = mapped_column(Integer)
    metric_id: Mapped[int] = mapped_column(Integer)
    ts: Mapped[float] = mapped_column(Float)
    value_real: Mapped[float | None] = mapped_column(Float, nullable=True)
    value_text: Mapped[str | None] = mapped_column(String, nullable=True)
    type: Mapped[int] = mapped_column(Integer)
    quality: Mapped[int] = mapped_column(Integer, default=0)


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


def normalize_dsn(dsn: str) -> str:
    """SQLAlchemy 2 + psycopg3 ожидает postgresql+psycopg://..."""
    if dsn.startswith("postgresql+psycopg://"):
        return dsn
    if dsn.startswith("postgresql://"):
        return "postgresql+psycopg://" + dsn.removeprefix("postgresql://")
    if dsn.startswith("postgres://"):
        return "postgresql+psycopg://" + dsn.removeprefix("postgres://")
    raise SystemExit(
        "Задайте PostgreSQL URL в TLMTRY_DATABASE "
        "(например postgresql://user:pass@127.0.0.1:5432/tlmtry)"
    )


def list_catalog(session: Session) -> None:
    devices = session.scalars(select(Device).order_by(Device.address)).all()
    print(f"devices: {len(devices)}")
    print("=== devices ===")
    for d in devices:
        print(
            f"  addr={d.address} name={d.name!r} online={bool(d.online)} "
            f"interval_ms={d.poll_interval_ms} last_seen={format_ts(d.last_seen)} "
            f"err={d.last_error!r}"
        )

    metrics = session.scalars(
        select(Metric).order_by(Metric.address, Metric.metric_id)
    ).all()
    print("\n=== metrics catalog ===")
    for m in metrics:
        type_name = METRIC_TYPE_NAMES.get(m.type, str(m.type))
        print(
            f"  addr={m.address} id={m.metric_id} "
            f"name={m.name!r} type={type_name} unit={m.unit!r}"
        )


def recent_samples(session: Session, *, last_minutes: int = 10, limit: int = 20) -> None:
    since = time.time() - last_minutes * 60
    stmt = (
        select(
            Sample.id,
            Sample.ts,
            Device.name.label("device"),
            Metric.name.label("metric"),
            Metric.unit,
            Sample.value_real,
            Sample.value_text,
            Sample.type,
        )
        .join(Device, Device.address == Sample.address)
        .join(
            Metric,
            (Metric.address == Sample.address)
            & (Metric.metric_id == Sample.metric_id),
        )
        .where(Sample.ts >= since)
        .order_by(Sample.id.desc())
        .limit(limit)
    )
    rows = session.execute(stmt).all()

    print(f"\n=== last samples (last {last_minutes} min, limit {limit}) ===")
    if not rows:
        print("  (нет строк — запустите collector или увеличьте окно)")
        return

    for row in rows:
        type_name = METRIC_TYPE_NAMES.get(int(row.type), str(row.type))
        value = sample_value(row.value_real, row.value_text)
        print(
            f"  id={row.id} t={format_ts(float(row.ts))} "
            f"{row.device}.{row.metric}={value} {row.unit} ({type_name})"
        )


def one_metric_series(
    session: Session,
    *,
    device_name: str = "lab-node-1",
    metric_name: str = "temp_c",
    last_minutes: int = 60,
) -> None:
    since = time.time() - last_minutes * 60
    stmt = (
        select(Sample.ts, Sample.value_real, Sample.value_text)
        .join(Device, Device.address == Sample.address)
        .join(
            Metric,
            (Metric.address == Sample.address)
            & (Metric.metric_id == Sample.metric_id),
        )
        .where(
            Device.name == device_name,
            Metric.name == metric_name,
            Sample.ts >= since,
        )
        .order_by(Sample.ts)
    )
    rows = session.execute(stmt).all()

    print(
        f"\n=== series {device_name!r}.{metric_name!r} "
        f"(last {last_minutes} min, {len(rows)} points) ==="
    )
    for row in rows[-10:]:
        print(
            f"  {format_ts(float(row.ts))}  "
            f"{sample_value(row.value_real, row.value_text)}"
        )
    if len(rows) > 10:
        print(f"  ... ({len(rows) - 10} earlier points omitted)")


def main() -> None:
    dsn = normalize_dsn(
        os.environ.get(
            "TLMTRY_DATABASE",
            "postgresql://tlmtry:secret@127.0.0.1:5432/tlmtry",
        )
    )
    engine = create_engine(dsn, pool_pre_ping=True)

    with Session(engine) as session:
        # Только чтение существующих таблиц collector'а — create_all не вызываем.
        count = session.scalar(select(Sample.id).limit(1))
        print(f"database: {engine.url.render_as_string(hide_password=True)}")
        print(f"has_samples: {count is not None}")
        print()
        list_catalog(session)
        recent_samples(session, last_minutes=10, limit=20)
        one_metric_series(session, device_name="lab-node-1", metric_name="temp_c")


if __name__ == "__main__":
    main()
