"""Admin page: virtual devices and manually reported readings."""

from __future__ import annotations

import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from tlmtry_collector.admin import make_server
from tlmtry_collector.protocol import MetricType, SampleValue
from tlmtry_collector.storage import Storage


def _post(base: str, path: str, fields: list[tuple[str, str]]) -> tuple[int, str]:
    data = urllib.parse.urlencode(fields).encode("utf-8")
    request = urllib.request.Request(base + path, data=data, method="POST")
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def test_admin_records_virtual_readings_and_rejects_bad_values(tmp_path: Path):
    storage = Storage(tmp_path / "admin.db")
    storage.insert_samples(1, [SampleValue(0, MetricType.FLOAT, 1.0)])
    bus_rows = storage.list_recent_samples()
    assert len(bus_rows) == 1
    assert bus_rows[0].quality == 0

    server = make_server(storage, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    base = f"http://{host}:{port}"
    try:
        status, page = _post(
            base,
            "/devices",
            [
                ("address", "7"),
                ("name", "virtual-node"),
                ("metric_name", "temp_c"),
                ("metric_type", "f32"),
                ("metric_unit", "C"),
                ("metric_name", "alarm"),
                ("metric_type", "bool"),
                ("metric_unit", ""),
                ("metric_name", ""),
                ("metric_type", "f32"),
                ("metric_unit", ""),
            ],
        )
        assert status == 200
        assert "Устройство записано" in page
        assert "virtual-node" in page

        status, page = _post(
            base,
            "/readings",
            [
                ("address", "7"),
                ("value_0", "21.5"),
                ("value_1", "1"),
                ("ts", ""),
            ],
        )
        assert status == 200
        assert "Показание записано" in page
        assert "виртуальное" in page
        assert "21.5" in page
        assert "true" in page

        rows = storage.list_recent_samples()
        virtual = [row for row in rows if row.address == 7]
        assert len(virtual) == 2
        by_metric = {row.metric_id: row for row in virtual}
        assert by_metric[0].quality == 1
        assert by_metric[0].value_real == 21.5
        assert by_metric[0].type == int(MetricType.FLOAT)
        assert by_metric[1].quality == 1
        assert by_metric[1].value_text == "true"
        assert by_metric[1].value_real == 1.0
        assert storage.list_devices()[0].online is True

        before = storage.sample_count()
        status, page = _post(
            base,
            "/readings",
            [
                ("address", "7"),
                ("value_0", "22"),
                ("value_1", "2"),
            ],
        )
        assert status == 400
        assert "нужно 0 или 1" in page
        assert storage.sample_count() == before

        status, page = _post(
            base,
            "/readings",
            [("address", "9"), ("value_0", "1")],
        )
        assert status == 400
        assert "не зарегистрировано" in page
        assert storage.sample_count() == before

        status, _page = _post(
            base,
            "/devices",
            [("address", "101"), ("name", "nope"), ("metric_name", "x"), ("metric_type", "i32"), ("metric_unit", "")],
        )
        assert status == 400
        assert storage.sample_count() == before
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        storage.close()
