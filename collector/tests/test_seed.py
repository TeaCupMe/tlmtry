from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from tlmtry_collector.__main__ import main
from tlmtry_collector.seed import sample_grid, seed_database
from tlmtry_collector.storage import Storage

NOW = 1_700_000_000.0


def test_sample_grid_known_hour():
    first, last, count = sample_grid(NOW, hours=1, step_s=600)
    assert count == 6
    assert last <= NOW
    assert first < last
    assert last - first == 600 * (count - 1)


def test_seed_writes_catalog_and_samples(tmp_path: Path):
    db = tmp_path / "primary.db"
    report = seed_database(str(db), hours=1, step_s=60, now=NOW, reset=True)
    assert report.samples == 360
    assert report.devices == 3
    assert report.metrics == 6
    assert report.reset is True

    storage = Storage(db)
    try:
        assert storage.sample_count() == 360
        devices = storage.list_devices()
        assert [d.name for d in devices] == ["lab-node-1", "lab-node-2", "lab-node-3"]
        assert all(d.online for d in devices)
        assert all(d.last_error is None for d in devices)
        assert devices[0].last_seen == report.end_ts

        rows = storage.iter_samples_after(0)
        temps = [r for r in rows if r.address == 1 and r.metric_id == 0]
        assert temps
        assert all(15.0 <= r.value_real <= 30.0 for r in temps)
        assert all(r.value_text is None for r in temps)

        doors = [r for r in rows if r.address == 2 and r.metric_id == 0]
        assert {r.value_real for r in doors} == {0.0, 1.0}
        assert {r.value_text for r in doors} == {"true", "false"}
        for door in doors:
            opened = (int(door.ts) % 600) < 30
            assert door.value_text == ("true" if opened else "false")

        counters = [r for r in rows if r.address == 2 and r.metric_id == 1]
        assert all(0 <= r.value_real < 65536 for r in counters)
        assert counters == sorted(counters, key=lambda r: r.ts)

        co2 = [r for r in rows if r.address == 3 and r.metric_id == 1]
        assert all(400 <= r.value_real <= 5000 for r in co2)
    finally:
        storage.close()


def test_seed_is_deterministic_and_reset_replaces(tmp_path: Path):
    db = tmp_path / "primary.db"
    seed_database(str(db), hours=1, step_s=60, now=NOW, reset=True)
    first = Storage(db)
    try:
        snapshot = [
            (r.address, r.metric_id, r.ts, r.value_real, r.value_text, r.type)
            for r in first.iter_samples_after(0)
        ]
    finally:
        first.close()

    again = seed_database(str(db), hours=1, step_s=60, now=NOW, reset=True)
    assert again.samples == 360
    second = Storage(db)
    try:
        assert second.sample_count() == 360
        assert [
            (r.address, r.metric_id, r.ts, r.value_real, r.value_text, r.type)
            for r in second.iter_samples_after(0)
        ] == snapshot
    finally:
        second.close()

    appended = seed_database(str(db), hours=1, step_s=60, now=NOW, reset=False)
    assert appended.samples == 360
    third = Storage(db)
    try:
        assert third.sample_count() == 720
        assert len(third.list_devices()) == 3
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
    assert "lab-node-1" in captured.out
    storage = Storage(db)
    try:
        assert storage.sample_count() == inserted
        assert len(storage.list_devices()) == 3
    finally:
        storage.close()


def test_cli_seed_rejects_bad_hours(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    with pytest.raises(SystemExit) as exc:
        main(["seed", "--database", str(tmp_path / "x.db"), "--hours", "0"])
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert "hours" in captured.err


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
