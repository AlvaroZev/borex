#!/usr/bin/env python3
"""DB helpers: seed local from Railway, or push local snapshot to Railway."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from borex_live.store.backup_sync import (  # noqa: E402
    sync_backup_to_local,
    sync_local_to_backup,
    table_names,
)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "direction",
        choices=["to-railway", "from-railway"],
        help="to-railway: local→backup | from-railway: backup→local (seed)",
    )
    p.add_argument(
        "--local",
        default=os.environ.get("DATABASE_URL", ""),
        help="Primary local URL (default DATABASE_URL)",
    )
    p.add_argument(
        "--backup",
        default=os.environ.get("DATABASE_BACKUP_URL", "")
        or os.environ.get("RAILWAY_DATABASE_URL", ""),
        help="Railway backup URL (default DATABASE_BACKUP_URL)",
    )
    args = p.parse_args()
    if not args.local or not args.backup:
        print(
            "Need --local/--backup or DATABASE_URL + DATABASE_BACKUP_URL in .env",
            file=sys.stderr,
        )
        return 1
    print(f"tables: {', '.join(table_names())}")
    if args.direction == "to-railway":
        counts = sync_local_to_backup(args.local, args.backup)
        print("Pushed local → Railway:", counts)
    else:
        counts = sync_backup_to_local(args.backup, args.local)
        print("Seeded Railway → local:", counts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
