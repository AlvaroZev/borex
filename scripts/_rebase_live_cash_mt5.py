#!/usr/bin/env python3
"""One-shot: rebase live DB cash to MT5 balance minus pretend open margins."""
from __future__ import annotations

import os
import sys
from pathlib import Path

LIVE = Path(r"C:\Users\azeva\OneDrive\Documentos\work\trading\borex_live")
sys.path.insert(0, str(LIVE))
os.chdir(LIVE)

from dotenv import load_dotenv

load_dotenv(LIVE / ".env")

import MetaTrader5 as mt5
from sqlalchemy import create_engine, text

DB = os.environ.get("DATABASE_URL") or "postgresql://borex:borex@127.0.0.1:5433/borex_live"

ok = mt5.initialize(timeout=60_000)
acc = mt5.account_info()
if not ok or acc is None:
    raise SystemExit(f"mt5 fail {ok} {mt5.last_error()}")
bal = float(acc.balance)
free = float(acc.margin_free)
pos = mt5.positions_get() or ()
print(f"mt5 login={acc.login} bal={bal:.2f} free={free:.2f} positions={len(pos)}")
for p in pos:
    print(f"  mt5_pos {p.symbol} ticket={p.ticket} vol={p.volume} profit={p.profit}")
mt5.shutdown()

eng = create_engine(DB)
with eng.begin() as conn:
    open_margin = conn.execute(
        text("select coalesce(sum(margin),0) from live_trades where status='open'")
    ).scalar()
    open_n = conn.execute(
        text("select count(*) from live_trades where status='open'")
    ).scalar()
    closed_n = conn.execute(
        text("select count(*) from live_trades where status='closed'")
    ).scalar()
    old = conn.execute(text("select cash from portfolio_state where id=1")).scalar()
    new_cash = max(0.0, bal - float(open_margin or 0.0))
    conn.execute(
        text("update portfolio_state set cash=:c, initial_capital=:b where id=1"),
        {"c": new_cash, "b": bal},
    )
    print(
        f"db open={open_n} closed={closed_n} open_margin={float(open_margin):.2f} "
        f"cash {float(old):.2f} -> {new_cash:.2f}"
    )
print("OK")
