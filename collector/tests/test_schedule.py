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
from tlmtry_collector.schedule import Keyframe, add_noise, interpolate, quantize
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
                ("noise", "0"),
                ("kf_ts", "1000"),
                ("kf_value", "10"),
                ("kf_ts", "1010"),
                ("kf_value", "30"),
                ("kf_ts", ""),
                ("kf_value", ""),
            ],
        )
        assert status == 200
        assert "Расписание записано" in page
        assert "temp_c #5" in page

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


def _post(base: str, path: str, fields: list[tuple[str, str]]) -> tuple[int, str]:
    data = urllib.parse.urlencode(fields).encode("utf-8")
    request = urllib.request.Request(base + path, data=data, method="POST")
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")
