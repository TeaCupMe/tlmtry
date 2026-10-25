#!/usr/bin/env python3
"""Load virtual readings from a CSV file into a database.

Each line is ``unix_time; value;``. Readings are inserted into ``samples`` for
one device address and metric. A timestamp already stored for that metric is
skipped.

``--every SECONDS`` writes a sample at that interval and runs until SIGINT or
SIGTERM. CSV rows set the value at those timestamps; the previous value is
held in between. ``--jitter`` adds a little noise around that value.
``--once`` writes every CSV row and exits. Stdout is one JSON object.
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_COLLECTOR = _ROOT / "collector"
if _COLLECTOR.is_dir():
    sys.path.insert(0, str(_COLLECTOR))

from tlmtry_collector.config import load_config  # noqa: E402
from tlmtry_collector.storage import Storage  # noqa: E402
from tlmtry_collector.virtual_readings import (  # noqa: E402
    follow_virtual_readings,
    import_virtual_readings,
)


def _database(args: argparse.Namespace) -> str:
    if args.database:
        return str(args.database)
    if args.config:
        return load_config(args.config).database
    print("database is required: --database or --config", file=sys.stderr)
    raise SystemExit(2)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="import_virtual_readings.py",
        description=(
            "Load virtual readings from a CSV file into samples. "
            "Each line is: unix_time; value;"
        ),
    )
    parser.add_argument(
        "--database",
        help="Destination database: SQLite path or postgres:// / postgresql:// URL",
    )
    parser.add_argument(
        "-c",
        "--config",
        help="Collector YAML; used for database when --database is omitted",
    )
    parser.add_argument("--input", required=True, help="CSV file of unix_time; value; lines")
    parser.add_argument("--address", required=True, type=int, help="Device address")
    parser.add_argument("--metric-id", required=True, type=int, help="Metric id on that device")
    parser.add_argument(
        "--create-virtual",
        action="store_true",
        help=(
            "Create a float metric when it is missing. "
            "Use an address outside the collector scan (default 1..100) "
            "so discovery does not replace it"
        ),
    )
    parser.add_argument(
        "--device-name",
        default="virtual",
        help="Name used only when --create-virtual inserts a new device (default: virtual)",
    )
    parser.add_argument(
        "--metric-name",
        default="value",
        help="Name used only when --create-virtual inserts a new metric (default: value)",
    )
    parser.add_argument(
        "--unit",
        default="",
        help="Unit used only when --create-virtual inserts a new metric",
    )
    pace = parser.add_mutually_exclusive_group(required=True)
    pace.add_argument(
        "--every",
        type=float,
        metavar="SECONDS",
        help=(
            "Write a sample every SECONDS and run until stopped. "
            "CSV rows change the held value; between them the last value is kept"
        ),
    )
    pace.add_argument(
        "--once",
        action="store_true",
        help="Write every pending CSV row in one pass and exit",
    )
    parser.add_argument(
        "--jitter",
        type=float,
        metavar="AMPLITUDE",
        help=(
            "Add noise of this amplitude around each interval sample. "
            "Float metrics only. Every sample stays within ±AMPLITUDE of the CSV value"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)
    database = _database(args)
    if args.jitter is not None and args.once:
        print("error: --jitter requires --every", file=sys.stderr)
        return 2
    try:
        dest = Storage(database)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        if args.once:
            result = import_virtual_readings(
                dest,
                args.input,
                args.address,
                args.metric_id,
                create_virtual=args.create_virtual,
                device_name=args.device_name,
                metric_name=args.metric_name,
                unit=args.unit,
            )
        else:
            stop = threading.Event()

            def _handle_signal(signum: int, frame: object) -> None:  # noqa: ARG001
                logging.getLogger(__name__).info("signal %s received, stopping", signum)
                stop.set()

            signal.signal(signal.SIGINT, _handle_signal)
            signal.signal(signal.SIGTERM, _handle_signal)
            result = follow_virtual_readings(
                dest,
                args.input,
                args.address,
                args.metric_id,
                args.every,
                create_virtual=args.create_virtual,
                device_name=args.device_name,
                metric_name=args.metric_name,
                unit=args.unit,
                stop=stop,
                jitter=args.jitter,
            )
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        dest.close()
    json.dump(result, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
