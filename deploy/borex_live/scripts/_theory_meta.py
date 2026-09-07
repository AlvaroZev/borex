#!/usr/bin/env python3
"""Dump theory shadow window metadata for the MT5 compare script."""
from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")


def main() -> int:
    eng = create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)
    with eng.connect() as c:
        state = dict(
            c.execute(
                text(
                    """
                    select strategy, config_hash, initial_capital, cash, equity,
                           last_master_ts, updated_at
                    from theory_state where id = 1
                    """
                )
            ).mappings().first()
        )
        bounds = dict(
            c.execute(
                text(
                    """
                    select count(*) as n,
                           min(entry_time) as first_entry,
                           max(entry_time) as last_entry,
                           sum(case when status='closed' then 1 else 0 end) as closed_n,
                           sum(case when status='open' then 1 else 0 end) as open_n,
                           coalesce(sum(case when status='closed' then pnl else 0 end), 0) as closed_pnl
                    from theory_trades
                    """
                )
            ).mappings().first()
        )
        week = dict(
            c.execute(
                text(
                    """
                    select count(*) as n,
                           sum(case when status='closed' then 1 else 0 end) as closed_n,
                           coalesce(sum(case when status='closed' then pnl else 0 end), 0) as closed_pnl
                    from theory_trades
                    where entry_time::timestamptz >= '2026-08-17 00:00:00+00'
                      and entry_time::timestamptz <  '2026-08-22 00:00:00+00'
                    """
                )
            ).mappings().first()
        )
    for d in (state, bounds, week):
        for k, v in list(d.items()):
            if hasattr(v, "isoformat"):
                d[k] = v.isoformat()
            elif hasattr(v, "__float__") and not isinstance(v, (int, float, bool)):
                d[k] = float(v)
    out = {"theory_state": state, "all_trades": bounds, "week_aug17": week}
    path = Path(os.environ["TEMP"]) / "borex_theory_meta.json"
    path.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    print(json.dumps(out, indent=2))
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
