"""Smooth virtual schedules: interpolation, noise, and the admin form."""

from __future__ import annotations

import random
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from tlmtry_collector.admin import make_server
from tlmtry_collector.protocol import MetricType
from tlmtry_collector.schedule import Keyframe, add_noise, interpolate, noise_at, quantize
from tlmtry_collector.storage import Storage


def test_interpolate_is_linear_and_holds_outside():
    points = [Keyframe(0.0, 10.0), Keyframe(10.0, 30.0)]
    assert interpolate(points, -5) == 10.0
    assert interpolate(points, 0) == 10.0
    assert interpolate(points, 2.5) == 15.0
    assert interpolate(points, 10) == 30.0
    assert interpolate(points, 40) == 30.0


def test_noise_stays_inside_amplitude_and_zero_is_exact():
    assert add_noise(21.5, 0, random.Random(1)) == 21.5
    rng = random.Random(2)
    for _ in range(50):
        noisy = add_noise(10.0, 1.5, rng)
        assert 8.5 <= noisy <= 11.5
    assert quantize(MetricType.INT32, 1.6) == 2
    assert quantize(MetricType.UINT16, -3.2) == 0
    assert quantize(MetricType.UINT16, 70000) == 65535


def test_noise_at_uses_left_endpoint_of_each_interval():
    points = [
        Keyframe(0.0, 10.0, noise=1.0),
        Keyframe(10.0, 20.0, noise=2.5),
        Keyframe(20.0, 30.0, noise=0.5),
    ]
    assert noise_at(points, -1) == 1.0
    assert noise_at(points, 5) == 1.0
    assert noise_at(points, 10) == 1.0
    assert noise_at(points, 15) == 2.5
    assert noise_at(points, 25) == 0.5
    assert noise_at(points, 100) == 0.5


def test_schedule_emits_on_step_with_midpoint_value(tmp_path: Path):
    storage = Storage(tmp_path / "sched.db")
    server = make_server(storage, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    base = f"http://{host}:{port}"
    try:
        status, _page = _post(
            base,
            "/devices",
            [
                ("address", "4"),
                ("name", "ramp"),
                ("metric_id", "5"),
                ("metric_name", "temp_c"),
                ("metric_type", "f32"),
                ("metric_unit", "C"),
            ],
        )
        assert status == 200
        status, page = _post(
            base,
            "/schedules",
            [
                ("address", "4"),
                ("metric_id", "5"),
                ("step_s", "10"),
                ("kf_ts", "1000"),
                ("kf_value", "10"),
                ("kf_noise", "0"),
                ("kf_ts", "1010"),
                ("kf_value", "30"),
                ("kf_noise", "0"),
                ("kf_ts", ""),
                ("kf_value", ""),
                ("kf_noise", ""),
            ],
        )
        assert status == 200
        assert "Расписание записано" in page
        assert "temp_c #5" in page
        assert "device-group" in page
        assert "Обновить расписание" in page
        assert 'class="kf-chart"' in page
        assert "form.schedule-form" in page
        assert "noise-band" in page

        app = server.admin_app
        assert app.emit_due(now=1002.5) == 1
        rows = storage.list_recent_samples()
        assert len(rows) == 1
        assert rows[0].metric_id == 5
        assert rows[0].value_real == 15.0
        assert rows[0].quality == 1
        assert app.emit_due(now=1007.5) == 0
        assert storage.sample_count() == 1
        assert app.emit_due(now=1012.5) == 1
        assert storage.sample_count() == 2
        latest = storage.list_recent_samples()[0]
        assert latest.value_real == 30.0

        status, page = _post(
            base,
            "/schedules",
            [
                ("address", "4"),
                ("metric_id", "5"),
                ("step_s", "5"),
                ("kf_ts", "2000"),
                ("kf_value", "0"),
                ("kf_noise", "1.5"),
                ("kf_ts", "2010"),
                ("kf_value", "10"),
                ("kf_noise", "0.25"),
            ],
        )
        assert status == 200
        assert "Расписание записано" in page
        schedules = storage.list_schedules()
        assert len(schedules) == 1
        assert schedules[0].step_s == 5.0
        assert schedules[0].keyframes[0].noise == 1.5
        assert schedules[0].keyframes[1].noise == 0.25
        assert "value=\"5\"" in page or 'value="5"' in page

        status, page = _post(
            base,
            "/schedules/stop",
            [("address", "4"), ("metric_id", "5")],
        )
        assert status == 200
        assert "Расписание остановлено" in page
        assert storage.list_schedules() == []
        assert app.emit_due(now=2000) == 0
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        storage.close()


def test_legacy_global_noise_fills_empty_interval_slots(tmp_path: Path):
    storage = Storage(tmp_path / "legacy-noise.db")
    server = make_server(storage, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    base = f"http://{host}:{port}"
    try:
        _post(
            base,
            "/devices",
            [
                ("address", "2"),
                ("name", "legacy"),
                ("metric_id", "1"),
                ("metric_name", "level"),
                ("metric_type", "f32"),
                ("metric_unit", ""),
            ],
        )
        status, _page = _post(
            base,
            "/schedules",
            [
                ("address", "2"),
                ("metric_id", "1"),
                ("step_s", "1"),
                ("noise", "0.75"),
                ("kf_ts", "100"),
                ("kf_value", "1"),
                ("kf_ts", "200"),
                ("kf_value", "2"),
            ],
        )
        assert status == 200
        schedule = storage.list_schedules()[0]
        assert schedule.keyframes[0].noise == 0.75
        assert schedule.keyframes[1].noise == 0.75
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        storage.close()


def test_keyframe_noise_migrates_from_schedule_noise(tmp_path: Path):
    db = tmp_path / "old.db"
    import sqlite3

    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE devices (
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
        CREATE TABLE metrics (
            address INTEGER NOT NULL,
            metric_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            type INTEGER NOT NULL,
            unit TEXT NOT NULL,
            PRIMARY KEY (address, metric_id)
        );
        CREATE TABLE samples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            address INTEGER NOT NULL,
            metric_id INTEGER NOT NULL,
            ts REAL NOT NULL,
            value_real REAL,
            value_text TEXT,
            type INTEGER NOT NULL,
            quality INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE replication_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE virtual_schedules (
            address INTEGER NOT NULL,
            metric_id INTEGER NOT NULL,
            step_s REAL NOT NULL,
            noise REAL NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            last_emit_ts REAL,
            PRIMARY KEY (address, metric_id)
        );
        CREATE TABLE virtual_keyframes (
            address INTEGER NOT NULL,
            metric_id INTEGER NOT NULL,
            ts REAL NOT NULL,
            value REAL NOT NULL,
            PRIMARY KEY (address, metric_id, ts)
        );
        INSERT INTO virtual_schedules(address, metric_id, step_s, noise, enabled)
        VALUES (1, 2, 1.0, 0.4, 1);
        INSERT INTO virtual_keyframes(address, metric_id, ts, value)
        VALUES (1, 2, 10.0, 5.0), (1, 2, 20.0, 15.0);
        """
    )
    conn.commit()
    conn.close()

    storage = Storage(db)
    try:
        schedules = storage.list_schedules()
        assert len(schedules) == 1
        assert schedules[0].keyframes[0].noise == 0.4
        assert schedules[0].keyframes[1].noise == 0.4
    finally:
        storage.close()


def _post(base: str, path: str, fields: list[tuple[str, str]]) -> tuple[int, str]:
    data = urllib.parse.urlencode(fields).encode("utf-8")
    request = urllib.request.Request(base + path, data=data, method="POST")
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")
