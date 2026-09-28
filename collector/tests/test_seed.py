from __future__ import annotations

import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

from tlmtry_collector.__main__ import main
from tlmtry_collector.protocol import MetricType
from tlmtry_collector.seed import SEED_DEVICES, planned_samples, sample_grid, seed_database
from tlmtry_collector.storage import Storage

NOW = 1_700_000_000.0

MODULE_NAMES = {
    1: "Главный модуль",
    2: "Жилой модуль 1",
    3: "Жилой модуль 2",
    4: "Био-модуль",
    5: "Инженерный модуль",
    6: "За бортом",
}


def _expected_names(module_index: int | None) -> list[str]:
    if module_index is None:
        return [
            "Температура за бортом",
            "Освещённость за бортом",
            "Влажность за бортом",
            "Давление за бортом",
        ]
    return [
        f"Температура в модуле {module_index}",
        f"Освещённость в модуле {module_index}",
        f"Влажность в модуле {module_index}",
        f"Давление в модуле {module_index}",
        f"Содержание CO2 в модуле {module_index}",
        f"Содержание O2 в модуле {module_index}",
    ]


def test_sample_grid_known_hour():
    first, last, count = sample_grid(NOW, hours=1, step_s=600)
    assert count == 6
    assert last <= NOW
    assert first < last
    assert last - first == 600 * (count - 1)


def test_catalog_matches_telemetry_fields():
    assert [device.name for device in SEED_DEVICES] == list(MODULE_NAMES.values())
    assert len(SEED_DEVICES) == 6
    for device in SEED_DEVICES:
        module_index = None if device.address == 6 else device.address
        metrics = device.metrics
        assert [metric.name for metric in metrics] == _expected_names(module_index)
        assert [metric.per_hour for metric in metrics[:4]] == [120, 60, 60, 30]
        assert [metric.unit for metric in metrics[:4]] == ["C", "lx", "%", "kPa"]
        assert metrics[0].type == MetricType.FLOAT
        assert metrics[1].type == MetricType.INT32
        assert metrics[2].type == MetricType.FLOAT
        assert metrics[3].type == MetricType.FLOAT
        assert device.poll_interval_ms == 30_000
        if module_index is None:
            assert len(metrics) == 4
        else:
            assert len(metrics) == 6
            assert metrics[4].name.startswith("Содержание CO2")
            assert metrics[5].name.startswith("Содержание O2")
            assert metrics[4].per_hour == 30
            assert metrics[5].per_hour == 30
            assert metrics[4].unit == "ppm"
            assert metrics[5].unit == "%"
            assert metrics[4].type == MetricType.FLOAT
            assert metrics[5].type == MetricType.FLOAT


def test_seed_writes_spec_fields_at_native_rates(tmp_path: Path):
    db = tmp_path / "primary.db"
    report = seed_database(str(db), hours=1, step_s=None, now=NOW, reset=True)
    _first, _last, expected = planned_samples(NOW, 1, None)
    assert report.samples == expected
    assert report.devices == 6
    assert report.metrics == 34
    assert report.step_s is None

    storage = Storage(db)
    try:
        assert storage.sample_count() == expected
        devices = storage.list_devices()
        assert [d.name for d in devices] == list(MODULE_NAMES.values())
        assert all(d.online and d.last_error is None for d in devices)
        assert all(d.poll_interval_ms == 30_000 for d in devices)
        assert devices[0].last_seen == report.end_ts

        catalog = {(m.address, m.metric_id): m for m in storage.list_metrics()}
        rows = storage.iter_samples_after(0)
        counts: Counter[tuple[int, str]] = Counter()
        for row in rows:
            metric = catalog[(row.address, row.metric_id)]
            counts[(row.address, metric.name)] += 1
            if metric.name.startswith("Температура"):
                assert row.value_real is not None
                assert -15.0 <= row.value_real <= 40.0
            elif metric.name.startswith("Освещённость"):
                assert row.value_real is not None
                assert row.value_real == int(row.value_real)
                assert 0 <= row.value_real <= 100_000
                assert row.type == int(MetricType.INT32)
            elif metric.name.startswith("Влажность"):
                assert row.value_real is not None
                assert 0.0 <= row.value_real <= 100.0
            elif metric.name.startswith("Давление"):
                assert row.value_real is not None
                assert 95.0 <= row.value_real <= 110.0
            elif metric.name.startswith("Содержание CO2"):
                assert row.value_real is not None
                assert 300.0 <= row.value_real <= 2000.0
            elif metric.name.startswith("Содержание O2"):
                assert row.value_real is not None
                assert 19.0 <= row.value_real <= 23.0

        assert counts[(1, "Температура в модуле 1")] == sample_grid(NOW, 1, 30)[2]
        assert counts[(1, "Освещённость в модуле 1")] == sample_grid(NOW, 1, 60)[2]
        assert counts[(1, "Влажность в модуле 1")] == sample_grid(NOW, 1, 60)[2]
        assert counts[(1, "Давление в модуле 1")] == sample_grid(NOW, 1, 120)[2]
        assert counts[(1, "Содержание CO2 в модуле 1")] == sample_grid(NOW, 1, 120)[2]
        assert counts[(1, "Содержание O2 в модуле 1")] == sample_grid(NOW, 1, 120)[2]
        outside = {name for address, name in counts if address == 6}
        assert outside == set(_expected_names(None))
        assert not any(name.startswith("Содержание") for _address, name in counts if _address == 6)
    finally:
        storage.close()


def test_step_override_uses_one_grid(tmp_path: Path):
    db = tmp_path / "override.db"
    seed_database(str(db), hours=1, step_s=600, now=NOW, reset=True)
    storage = Storage(db)
    try:
        rows = [r for r in storage.iter_samples_after(0) if r.address == 1]
        stamps = Counter(r.ts for r in rows)
        assert len(stamps) == sample_grid(NOW, 1, 600)[2]
        assert set(stamps.values()) == {6}
    finally:
        storage.close()


def test_seed_is_deterministic_and_reset_replaces(tmp_path: Path):
    db = tmp_path / "primary.db"
    seed_database(str(db), hours=1, step_s=600, now=NOW, reset=True)
    first = Storage(db)
    try:
        snapshot = [
            (r.address, r.metric_id, r.ts, r.value_real, r.value_text, r.type)
            for r in first.iter_samples_after(0)
        ]
    finally:
        first.close()

    again = seed_database(str(db), hours=1, step_s=600, now=NOW, reset=True)
    assert again.samples == len(snapshot)
    second = Storage(db)
    try:
        assert second.sample_count() == len(snapshot)
        assert [
            (r.address, r.metric_id, r.ts, r.value_real, r.value_text, r.type)
            for r in second.iter_samples_after(0)
        ] == snapshot
    finally:
        second.close()

    appended = seed_database(str(db), hours=1, step_s=600, now=NOW, reset=False)
    assert appended.samples == len(snapshot)
    third = Storage(db)
    try:
        assert third.sample_count() == len(snapshot) * 2
        assert len(third.list_devices()) == 6
    finally:
        third.close()


def test_seed_rejects_huge_or_invalid_ranges(tmp_path: Path):
    db = tmp_path / "primary.db"
    with pytest.raises(ValueError, match="hours"):
        seed_database(str(db), hours=0, step_s=60, now=NOW)
    with pytest.raises(ValueError, match="step_s"):
        seed_database(str(db), hours=1, step_s=0, now=NOW)
    with pytest.raises(ValueError, match="refusing to insert"):
        seed_database(str(db), hours=24, step_s=0.05, now=NOW)
    assert not db.exists()


def test_cli_seed(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    db = tmp_path / "cli.db"
    with pytest.raises(SystemExit) as exc:
        main(
            [
                "seed",
                "--database",
                str(db),
                "--hours",
                "1",
                "--step-seconds",
                "600",
                "--reset",
            ]
        )
    assert exc.value.code == 0
    captured = capsys.readouterr()
    match = re.search(r"samples inserted: (\d+)", captured.out)
    assert match is not None
    inserted = int(match.group(1))
    assert inserted > 0
    assert "Главный модуль" in captured.out
    assert "Температура в модуле 1" in captured.out
    storage = Storage(db)
    try:
        assert storage.sample_count() == inserted
        assert len(storage.list_devices()) == 6
    finally:
        storage.close()


def test_cli_seed_native_intervals(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    db = tmp_path / "native.db"
    with pytest.raises(SystemExit) as exc:
        main(["seed", "--database", str(db), "--hours", "1", "--reset"])
    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert "native intervals" in captured.out
    assert "Содержание O2 в модуле 5" in captured.out
    storage = Storage(db)
    try:
        assert storage.sample_count() > 0
        assert len(storage.list_metrics()) == 34
        names = {metric.name for metric in storage.list_metrics()}
        assert "Содержание CO2 в модуле 5" in names
        assert "Содержание O2 в модуле 5" in names
        assert "Температура за бортом" in names
        assert "Содержание CO2 за бортом" not in names
        assert "Содержание O2 за бортом" not in names
    finally:
        storage.close()


def test_cli_seed_rejects_bad_hours(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    with pytest.raises(SystemExit) as exc:
        main(["seed", "--database", str(tmp_path / "x.db"), "--hours", "0"])
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert "hours" in captured.err


def test_up_postgres_help():
    script = Path(__file__).resolve().parents[2] / "scripts" / "up_postgres.sh"
    proc = subprocess.run(
        ["bash", str(script), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert "tlmtry-postgres" in proc.stdout
    assert "pseudo telemetry" in proc.stdout
    assert proc.stderr == ""


def test_seed_script_help():
    script = Path(__file__).resolve().parents[2] / "scripts" / "seed_database.py"
    proc = subprocess.run(
        [sys.executable, str(script), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert "pseudo sensor" in proc.stdout
    assert "--reset" in proc.stdout
    assert "120/h" in proc.stdout
