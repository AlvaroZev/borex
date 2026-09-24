"""Probe Railway backup DB for mirror theory state / candles (no secrets printed)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

LIVE = Path(__file__).resolve().parents[1].parent / "borex_live"
load_dotenv(LIVE / ".env")


def probe(url: str, label: str) -> None:
    host = url.split("@")[-1] if url else ""
    print(f"=== {label} host={host} ===")
    if not url:
        print("missing")
        return
    try:
        eng = create_engine(url, pool_pre_ping=True, connect_args={"connect_timeout": 15})
        with eng.connect() as c:
            print("connected")
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
            for t in (
                "mirror_theory_state",
                "mirror_theory_trades",
                "theory_state",
                "theory_trades",
                "live_candles",
                "service_runs",
                "mirror_portfolio",
            ):
                if t not in tables:
                    continue
                n = c.execute(text(f'select count(*) from "{t}"')).scalar()
                print(f"  {t}: {n}")
            if "mirror_theory_state" in tables:
                row = c.execute(
                    text(
                        "select strategy, config_hash, initial_capital, cash, equity, "
                        "last_master_ts, updated_at, length(state_blob) as blob_len "
                        "from mirror_theory_state where id=1"
                    )
                ).mappings().first()
                print("  mirror_theory_state row", dict(row) if row else None)
            if "theory_state" in tables:
                row = c.execute(
                    text(
                        "select strategy, config_hash, initial_capital, cash, equity, "
                        "last_master_ts, updated_at, length(state_blob) as blob_len "
                        "from theory_state where id=1"
                    )
                ).mappings().first()
                print("  theory_state row", dict(row) if row else None)
            if "live_candles" in tables:
                n = c.execute(
                    text(
                        "select count(*) from live_candles "
                        "where interval='1h' and ts>='2026-08-27' and ts<'2026-08-29'"
                    )
                ).scalar()
                print("  live_candles H1 in 8792 window", n)
            if "mirror_theory_trades" in tables:
                n = c.execute(
                    text(
                        "select count(*) from mirror_theory_trades "
                        "where entry_time>='2026-08-27' and entry_time<'2026-08-29'"
                    )
                ).scalar()
                print("  mirror_theory_trades in window", n)
            if "theory_trades" in tables:
                n = c.execute(
                    text(
                        "select count(*) from theory_trades "
                        "where entry_time::text>='2026-08-27' "
                        "and entry_time::text<'2026-08-29'"
                    )
                ).scalar()
                print("  theory_trades in window", n)
    except Exception as e:
        print("FAIL", type(e).__name__, str(e)[:300])


def main() -> int:
    probe(os.environ.get("MIRROR_DATABASE_URL") or os.environ.get("DATABASE_URL") or "", "local")
    probe(
        os.environ.get("MIRROR_DATABASE_BACKUP_URL")
        or os.environ.get("DATABASE_BACKUP_URL")
        or os.environ.get("RAILWAY_DATABASE_URL")
        or "",
        "backup",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
