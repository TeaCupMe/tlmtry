#!/usr/bin/env python3
"""Export telemetry starting at a sample id into a replication file.

The file contains the device and metric catalog and every sample whose id is
greater than or equal to --from-id. The source watermark is not changed.
Stdout is one JSON object. The caller stores next_from_id only after the file
has been imported.
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
from tlmtry_collector.file_transfer import export_since  # noqa: E402
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
        prog="export_replication.py",
        description="Export samples starting at an id into a replication JSON file",
    )
    parser.add_argument(
        "--database",
        help="Source database: SQLite path or postgres:// / postgresql:// URL",
    )
    parser.add_argument("-c", "--config", help="Collector YAML; used for database when --database is omitted")
    parser.add_argument(
        "--from-id",
        type=int,
        required=True,
        help="First sample id to include (id >= this value). Use 1 to export from the beginning",
    )
    parser.add_argument("--output", required=True, help="Path of the replication JSON file to write")
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)
    database = _database(args)
    try:
        source = Storage(database)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        result = export_since(source, args.from_id, args.output)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        source.close()
    json.dump(result, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
