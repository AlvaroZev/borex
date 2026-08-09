#!/usr/bin/env python3
"""Force-fetch complete July (+ late June warmup) 1h bars from MT5 for all tradeable FX."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIVE = ROOT.parent / "borex_live"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(LIVE))

from dotenv import load_dotenv

load_dotenv(LIVE / ".env")


def main() -> int:
    from borex.viewerMT5.mt5_feed import (
        connect_mt5_client,
        ensure_live_on_path,
        fetch_mt5_candles,
        list_tradeable_yahoo_symbols,
    )

    ensure_live_on_path()
    # Cover warmup for Jul 6 week (needs ~Jun 29) through end of July / early Aug
    start = datetime(2026, 6, 15, tzinfo=timezone.utc)
    end = datetime(2026, 8, 3, tzinfo=timezone.utc)

    symbols = list_tradeable_yahoo_symbols()
    print(f"MT5 force fetch 1h | {len(symbols)} pairs | {start.date()} → {end.date()}", flush=True)

    client = connect_mt5_client()
    ok, fail = 0, []
    for i, sym in enumerate(symbols, 1):
        try:
            candles = fetch_mt5_candles(
                sym,
                "1h",
                start,
                end,
                client=client,
                use_cache=False,  # force broker pull
                write_cache=True,
            )
            if not candles:
                fail.append((sym, "0 bars"))
                print(f"  [{i}/{len(symbols)}] FAIL {sym}: 0 bars", flush=True)
                continue
            first, last = candles[0].timestamp, candles[-1].timestamp
            print(
                f"  [{i}/{len(symbols)}] OK {sym}: {len(candles)} bars  {first} → {last}",
                flush=True,
            )
            ok += 1
        except Exception as exc:
            fail.append((sym, str(exc)))
            print(f"  [{i}/{len(symbols)}] FAIL {sym}: {exc}", flush=True)

    print(f"\nMT5 done: ok={ok} fail={len(fail)}", flush=True)
    for sym, err in fail:
        print(f"  fail {sym}: {err}", flush=True)
    return 0 if ok >= 50 else 1


if __name__ == "__main__":
    raise SystemExit(main())
