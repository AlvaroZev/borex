"""Probe DB + list mirror/theory tables for golden fixture export."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

LIVE = Path(__file__).resolve().parents[1].parent / "borex_live"
load_dotenv(LIVE / ".env")


def main() -> int:
    url = os.environ.get("MIRROR_DATABASE_URL") or os.environ.get("DATABASE_URL")
    print("url_tail", (url or "").split("@")[-1] if url else None)
    if not url:
        return 1
    eng = create_engine(url, pool_pre_ping=True)
    with eng.connect() as c:
        print("ok", c.execute(text("select 1")).scalar())
        tables = [
            r[0]
            for r in c.execute(
                text(
                    "select tablename from pg_tables "
                    "where schemaname='public' order by 1"
                )
            )
        ]
        print("tables", tables)
        for t in tables:
            try:
                n = c.execute(text(f'select count(*) from "{t}"')).scalar()
                print(f"  {t}: {n}")
            except Exception as e:
                print(f"  {t}: ERR {e}")
                c.rollback()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
