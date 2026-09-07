#!/usr/bin/env python3
"""One-shot export of live vs theory trades for weekly review."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")


def main() -> int:
    start = datetime(2026, 8, 14, tzinfo=timezone.utc)
    eng = create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)
    with eng.connect() as c:
        live = c.execute(
            text(
                """
                select id, symbol, side, pattern, entry_price, stop_loss, take_profit,
                       margin, rr_used, status, entry_time, exit_price, exit_time,
                       exit_reason, pnl, expected_win_usd, expected_loss_usd, mt5_ticket
                from live_trades
                where coalesce(nullif(entry_time, ''), '1970-01-01')::timestamptz >= :s
                   or coalesce(nullif(exit_time, ''), '1970-01-01')::timestamptz >= :s
                order by id
                """
            ),
            {"s": start},
        ).mappings().all()
        theory = c.execute(
            text(
                """
                select id, symbol, side, pattern, entry_price, stop_loss, take_profit,
                       margin, rr_used, commission, status, entry_time, exit_price,
                       exit_time, exit_reason, pnl
                from theory_trades
                where coalesce(nullif(entry_time, ''), '1970-01-01')::timestamptz >= :s
                   or coalesce(nullif(exit_time, ''), '1970-01-01')::timestamptz >= :s
                order by id
                """
            ),
            {"s": start},
        ).mappings().all()
        state = c.execute(
            text(
                """
                select strategy, config_hash, initial_capital, cash, equity,
                       last_master_ts, updated_at
                from theory_state where id = 1
                """
            )
        ).mappings().first()
        pf = c.execute(
            text("select cash, initial_capital, updated_at from portfolio_state where id = 1")
        ).mappings().first()
        ghosts = c.execute(
            text("select count(*) from pending_ghosts where status = 'waiting'")
        ).scalar()

    def clean(row):
        d = dict(row)
        for k, v in list(d.items()):
            if hasattr(v, "isoformat"):
                d[k] = v.isoformat()
        return d

    out = {
        "window_start": start.isoformat(),
        "portfolio": clean(pf) if pf else None,
        "theory_state": clean(state) if state else None,
        "waiting_ghosts": int(ghosts or 0),
        "live": [clean(r) for r in live],
        "theory": [clean(r) for r in theory],
    }
    path = Path(os.environ["TEMP"]) / "borex_week_trades.json"
    path.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    print(f"live={len(out['live'])} theory={len(out['theory'])} ghosts={out['waiting_ghosts']}")
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
