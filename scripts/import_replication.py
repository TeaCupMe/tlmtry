#!/usr/bin/env python3
"""Load new telemetry from a replication file into a database.

Devices and metrics are upserted. Sample rows whose id is already present are
skipped. Stdout is one JSON object with inserted_samples and skipped_samples.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_COLLECTOR = _ROOT / "collector"
if _COLLECTOR.is_dir():
    sys.path.insert(0, str(_COLLECTOR))

from tlmtry_collector.config import load_config  # noqa: E402
from tlmtry_collector.file_transfer import import_file  # noqa: E402
from tlmtry_collector.storage import Storage  # noqa: E402


def _database(args: argparse.Namespace) -> str:
    if args.database:
        return str(args.database)
    if args.config:
        return load_config(args.config).database
    print("database is required: --database or --config", file=sys.stderr)
    raise SystemExit(2)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="import_replication.py",
        description="Load new samples from a replication JSON file into a database",
    )
    parser.add_argument(
        "--database",
        help="Destination database: SQLite path or postgres:// / postgresql:// URL",
    )
    parser.add_argument("-c", "--config", help="Collector YAML; used for database when --database is omitted")
    parser.add_argument("--input", required=True, help="Replication JSON file to load")
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)
    database = _database(args)
    try:
        dest = Storage(database)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        result = import_file(dest, args.input)
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
