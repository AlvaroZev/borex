"""Compare live_candles DB vs MT5 cache for 8792 window (A9 data check)."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]
LIVE = ROOT.parent / "borex_live"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(LIVE))
load_dotenv(LIVE / ".env")


def main() -> int:
    url = os.environ.get("MIRROR_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        print("no DATABASE_URL")
        return 1
    print("db", url.split("@")[-1])
    eng = create_engine(url, pool_pre_ping=True)
    with eng.connect() as c:
        n = c.execute(
            text(
                "select count(*) as n from live_candles "
                "where interval='1h' and ts>='2026-08-27' and ts<'2026-08-29'"
            )
        ).scalar()
        print("live_candles H1 in window", n)
        syms = c.execute(
            text(
                "select count(distinct symbol) from live_candles "
                "where interval='1h' and ts>='2026-08-27' and ts<'2026-08-29'"
            )
        ).scalar()
        print("symbols", syms)
        st = c.execute(
            text(
                "select strategy, config_hash, initial_capital, cash, equity, "
                "last_master_ts, updated_at from theory_state where id=1"
            )
        ).mappings().first()
        print("theory_state", dict(st) if st else None)
        rows = c.execute(
            text(
                "select ts, open, high, low, close from live_candles "
                "where symbol='EURUSD=X' and interval='1h' "
                "and ts>='2026-08-27' and ts<'2026-08-28' order by ts"
            )
        ).mappings().all()
    print("db eurusd bars", len(rows))

    from borex.viewerMT5.mt5_feed import _cache_path, _frame_to_candles, _slice_candles

    df = pd.read_parquet(_cache_path("EURUSD=X", "1h"))
    cache = _slice_candles(
        _frame_to_candles(df),
        datetime(2026, 8, 27, tzinfo=timezone.utc),
        datetime(2026, 8, 28, tzinfo=timezone.utc),
    )
    cmap = {pd.Timestamp(c.timestamp).isoformat(): c for c in cache}
    mism = 0
    missing = 0
    for r in rows:
        ts = pd.Timestamp(r["ts"])
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        else:
            ts = ts.tz_convert("UTC")
        cc = cmap.get(ts.isoformat())
        if cc is None:
            missing += 1
            if missing <= 3:
                print("cache missing", ts)
            continue
        for f in ("open", "high", "low", "close"):
            if abs(float(getattr(cc, f)) - float(r[f])) > 1e-9:
                mism += 1
                if mism <= 5:
                    print("mismatch", ts, f, getattr(cc, f), r[f])
                break
    print("ohlc mismatched bars", mism, "missing_in_cache", missing, "of", len(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
