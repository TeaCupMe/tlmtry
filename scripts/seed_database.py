#!/usr/bin/env python3
"""Create the telemetry database and fill it with pseudo sensor readings.

Works from a git checkout without installation by putting ``collector/`` on
``sys.path``. After ``pip install -e ./collector`` the same command is
available as ``tlmtry-seed`` and ``tlmtry-collector seed``.

Examples:
    python scripts/seed_database.py --database data/primary.db
    python scripts/seed_database.py --database data/primary.db --reset --hours 6
    python scripts/seed_database.py \\
        --database 'postgresql://tlmtry:secret@127.0.0.1:5432/tlmtry' --reset
"""

from __future__ import annotations

import sys
from pathlib import Path

_COLLECTOR = Path(__file__).resolve().parents[1] / "collector"
if str(_COLLECTOR) not in sys.path:
    sys.path.insert(0, str(_COLLECTOR))

from tlmtry_collector.seed import main

if __name__ == "__main__":
    main()
