from __future__ import annotations

import time

import pytest

from tlmtry_collector.__main__ import build_parser
from tlmtry_collector.debug_session import (
    BusSettings,
    DebugSession,
    format_metric_value,
    list_serial_ports,
)
from tlmtry_collector.protocol import MetricType


def _settings(**overrides) -> BusSettings:
    base = dict(
        port="",
        baudrate=115200,
        sim=True,
        address_min=1,
        address_max=5,
        response_timeout_ms=200,
        probe_timeout_ms=40,
        poll_interval_ms=100,
        unicast_fill=True,
    )
    base.update(overrides)
    return BusSettings(**base)


def test_format_metric_value():
    assert format_metric_value(MetricType.FLOAT, 12.5) == "12.5"
    assert format_metric_value(MetricType.FLOAT, 20.0) == "20"
    assert format_metric_value(MetricType.BOOL, True) == "true"
    assert format_metric_value(MetricType.BOOL, False) == "false"
    assert format_metric_value(MetricType.INT32, -5) == "-5"
    assert format_metric_value(MetricType.UINT16, 65535) == "65535"


def test_list_serial_ports_returns_pairs():
    ports = list_serial_ports()
    assert isinstance(ports, list)
    for device, label in ports:
        assert device
        assert device in label


def test_gui_command_is_registered():
    args = build_parser().parse_args(["gui"])
    assert args.func.__name__ == "cmd_gui"


def test_sim_discover_lists_metrics_and_read_fills_values():
    session = DebugSession()
    session.start()
    settings = _settings()
    try:
        session.connect(settings)
        connected = session.wait_for("connected", timeout=2)
        assert connected.flag is True

        session.discover(settings)
        discovered = session.wait_for("devices", timeout=3)
        by_addr = {item.address: item for item in discovered.devices}
        assert set(by_addr) == {1, 2}
        assert by_addr[1].name == "lab-node-1"
        assert [metric.name for metric in by_addr[1].metrics] == ["temp_c", "humidity"]
        assert [metric.type_name for metric in by_addr[1].metrics] == ["f32", "f32"]
        assert by_addr[1].metrics[0].unit == "C"
        assert by_addr[1].metrics[0].value == ""
        assert by_addr[1].link == "found"
        assert [metric.name for metric in by_addr[2].metrics] == ["door_open", "counter"]

        session.read_once()
        read = session.wait_for("devices", timeout=2)
        node = next(item for item in read.devices if item.address == 1)
        assert node.link == "online"
        assert node.metrics[0].value not in ("", "—")
        assert "temp_c=" in node.preview
    finally:
        session.close()


def test_polling_updates_counter():
    session = DebugSession()
    session.start()
    settings = _settings()
    try:
        session.connect(settings)
        assert session.wait_for("connected", timeout=2).flag is True
        session.discover(settings)
        session.wait_for("devices", timeout=3)
        session.set_polling(True, 100)
        assert session.wait_for("polling", timeout=2).flag is True
        seen: set[str] = set()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and len(seen) < 2:
            event = session.wait_for("devices", timeout=1)
            device = next(item for item in event.devices if item.address == 2)
            counter = next(metric for metric in device.metrics if metric.name == "counter")
            if counter.value:
                seen.add(counter.value)
        assert seen, "counter metric was never read"
    finally:
        session.close()


def test_missing_port_reports_error():
    session = DebugSession()
    session.start()
    try:
        session.connect(_settings(sim=False, port="/dev/no-such-tlmtry-port"))
        status = session.wait_for("status", timeout=2)
        assert "Ошибка порта" in status.text
        assert session.wait_for("connected", timeout=2).flag is False
    finally:
        session.close()


def _pump_until(root, predicate, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        root.update()
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_window_discover_expand_and_poll():
    pytest.importorskip("tkinter")
    import tkinter as tk

    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display for tkinter")
    root.withdraw()

    from tlmtry_collector.gui import DebugApp

    app = DebugApp(root)
    try:
        app.sim_var.set(True)
        app.addr_max_var.set("5")
        assert str(app.discover_btn.cget("state")) == "disabled"
        app._on_connect()
        assert _pump_until(root, lambda: app._connected, 2.0)
        assert str(app.discover_btn.cget("state")) == "normal"

        app._on_discover()
        assert _pump_until(root, lambda: len(app.tree.get_children("")) == 2, 3.0)

        parent = app.tree.get_children("")[0]
        assert app.tree.item(parent, "text") == "lab-node-1"
        assert not app.tree.item(parent, "open")
        metrics = app.tree.get_children(parent)
        assert [app.tree.item(item, "text") for item in metrics] == ["temp_c", "humidity"]
        assert app.tree.set(metrics[0], "mtype") == "f32"
        assert app.tree.set(metrics[0], "unit") == "C"
        assert app.tree.set(metrics[0], "value") == ""

        app._expand_all()
        assert app.tree.item(parent, "open")
        app._collapse_all()
        assert not app.tree.item(parent, "open")
        app.tree.item(parent, open=True)

        app.poll_var.set(True)
        assert _pump_until(
            root,
            lambda: app.tree.set(metrics[0], "value") not in ("", "—"),
            3.0,
        )
        assert app.tree.set(parent, "status") == "на связи"
        assert "temp_c=" in app.tree.set(parent, "value")
    finally:
        app.close()
