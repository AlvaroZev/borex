#!/usr/bin/env python3
"""One-day alexg8 backtest with long warmup — same knobs as the $2k W1 live.

Loads ~400d of MT5 H1 (AOI/HTF), primes ghosts for 72 bars like live, then
only *enters* on the target Friday. Run after Friday's last H1 has closed.

  python -u scripts/run_friday_1d_alexg8.py
  python -u scripts/run_friday_1d_alexg8.py --friday 2026-08-28 --capital 1000
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
LIVE = ROOT.parent / "borex_live"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(LIVE))
load_dotenv(LIVE / ".env")

OUT_DIR = ROOT / "data" / "runs" / "alexg8_friday_1d"
WARMUP_DAYS = 400
GHOST_PRIME_BARS = 72

# Same engine as the $2k W1 / mirror live launch.
KNOBS = dict(
    strategy="alexg8",
    rr_mode="dynamic",
    true_sl_rr=2.0,
    rr_factor=1.0,
    rr_min=2.0,
    rr_max=6.0,
    risk_include_commission=True,
    commission_at_entry=False,
    force_flat_friday=False,
    force_flat_daily=True,
)


def _ts(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    if t.tzinfo is None:
        return t.tz_localize("UTC")
    return t.tz_convert("UTC")


def last_completed_friday(now: pd.Timestamp) -> pd.Timestamp:
    """Friday 00:00 UTC of the last Friday that has already ended (Sat 00:00 UTC)."""
    d = now.normalize()
    # weekday: Mon=0 … Fri=4
    days_since_fri = (d.weekday() - 4) % 7
    friday = d - timedelta(days=days_since_fri)
    if now < friday + timedelta(days=1):
        friday = friday - timedelta(days=7)
    return friday


def load_mt5(start: datetime, end: datetime) -> dict:
    from borex.viewerMT5.mt5_feed import (
        connect_mt5_client,
        fetch_mt5_candles,
        list_tradeable_yahoo_symbols,
    )

    print(f"Loading MT5 H1 {start.date()} → {end.date()}", flush=True)
    client = connect_mt5_client()
    raw: dict = {}
    try:
        symbols = list_tradeable_yahoo_symbols()
        for i, sym in enumerate(symbols, 1):
            bars = fetch_mt5_candles(
                sym,
                "1h",
                start,
                end,
                client=client,
                use_cache=True,
                write_cache=True,
            )
            if len(bars) >= 120:
                raw[sym] = bars
            if i % 15 == 0 or i == len(symbols):
                print(f"  loaded {len(raw)}/{i}", flush=True)
    finally:
        client.disconnect()
    print(f"Pairs {len(raw)}", flush=True)
    return raw


def prime_ghosts(strategy, data: dict, master: str, friday_i: int) -> int:
    """Live rebuilds `_pending` on the last 72 H1 bars without opening trades."""
    from borex.alexg.multi_market import MultiMarketContext, align_symbols_to_timeline

    master_bars = data[master]
    start = max(int(strategy.min_bars), friday_i - GHOST_PRIME_BARS)
    stop = friday_i
    if stop <= start:
        return 0
    ts_maps = align_symbols_to_timeline(master_bars, data)
    n = 0
    for master_i in range(start, stop):
        ctx = MultiMarketContext.at_master_bar(
            master_i,
            master_bars,
            data,
            ts_maps,
            strength_lookback=strategy.strength_lookback,
            min_currency_edge=strategy.min_currency_edge,
            min_confirming_pairs=strategy.min_confirming_pairs,
        )
        for symbol, index in ctx.indices.items():
            strategy.set_context(symbol, ctx)
            strategy.on_bar(index, data[symbol], None)
            n += 1
    return n


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--friday", default="", help="YYYY-MM-DD (UTC). Default: last completed Friday")
    parser.add_argument("--capital", type=float, default=1000.0)
    parser.add_argument("--warmup-days", type=int, default=WARMUP_DAYS)
    args = parser.parse_args()

    now = pd.Timestamp(datetime.now(timezone.utc))
    if args.friday:
        friday = _ts(args.friday).normalize()
    else:
        friday = last_completed_friday(now)
    friday_end = friday + timedelta(days=1)
    if now < friday_end:
        print(
            f"Friday {friday.date()} has not closed yet (need Sat 00:00 UTC). "
            f"Re-run after the last Friday H1 is in the feed.",
            flush=True,
        )
        return 2

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=int(args.warmup_days))
    data = load_mt5(start, end)
    if not data:
        raise SystemExit("no MT5 bars")

    from borex.alexg import AlexG8Strategy
    from borex.alexg.multi_market import pick_master_symbol
    from borex.backtest.engine import BacktestConfig
    from borex.backtest.multi_market_engine import MultiMarketEngine

    master = pick_master_symbol(data)
    master_bars = data[master]
    friday_i = next(
        (i for i, c in enumerate(master_bars) if _ts(c.timestamp) >= friday),
        None,
    )
    if friday_i is None:
        raise SystemExit(f"no master bar on/after {friday.date()}")
    # Drop bars after Friday so end_of_data is Friday close, not weekend/Monday.
    cut = {
        sym: [c for c in bars if _ts(c.timestamp) < friday_end]
        for sym, bars in data.items()
    }
    cut = {k: v for k, v in cut.items() if len(v) >= 120}
    master_bars = cut[master]
    friday_i = next(
        (i for i, c in enumerate(master_bars) if _ts(c.timestamp) >= friday),
        None,
    )
    if friday_i is None or friday_i >= len(master_bars):
        raise SystemExit(f"Friday {friday.date()} not in sliced feed")

    strategy = AlexG8Strategy(
        min_rr=float(KNOBS["true_sl_rr"]),
        execution_interval="1h",
        peer_blend=0.0,
    )
    primed = prime_ghosts(strategy, cut, master, friday_i)
    strategy.min_bars = max(int(strategy.min_bars), int(friday_i))
    print(
        f"Friday {friday.date()} | entries from bar {strategy.min_bars}/{len(master_bars)} "
        f"| primed ghosts on {primed} symbol-bars | warmup {args.warmup_days}d",
        flush=True,
    )

    config = BacktestConfig(
        initial_capital=float(args.capital),
        leverage=5000.0,
        position_size_pct=0.01,
        size_mode="margin",
        true_sl=True,
        true_sl_rr=float(KNOBS["true_sl_rr"]),
        rr_mode=KNOBS["rr_mode"],
        rr_factor=float(KNOBS["rr_factor"]),
        rr_min=float(KNOBS["rr_min"]),
        rr_max=float(KNOBS["rr_max"]),
        stop_loss_pct=None,
        take_profit_pct=None,
        commission_per_lot=7.0,
        risk_include_commission=bool(KNOBS["risk_include_commission"]),
        commission_at_entry=bool(KNOBS["commission_at_entry"]),
        force_flat_friday=bool(KNOBS["force_flat_friday"]),
        force_flat_daily=bool(KNOBS["force_flat_daily"]),
        force_flat_utc_hour=19,
        force_flat_friday_from_hour=19,
        winrate_min_trades=20,
        spread_pips=0.0,
        slippage_pips=0.0,
    )
    t0 = time.perf_counter()
    engine = MultiMarketEngine(strategy, config, max_positions=60)
    engine._progress_every = 25
    result = engine.run(cut, timeframe="1h", master_symbol=master, same_bar_exit=False)
    elapsed = time.perf_counter() - t0

    trades = list(result.trades)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "friday": friday.date().isoformat(),
        "capital": float(args.capital),
        "warmup_days": int(args.warmup_days),
        "ghost_prime_bars": GHOST_PRIME_BARS,
        "master": master,
        "master_bars": len(master_bars),
        "first_bar": str(master_bars[0].timestamp),
        "last_bar": str(master_bars[-1].timestamp),
        "knobs": KNOBS,
        "summary": {
            "account": float(result.final_equity),
            "net_pnl": float(result.final_equity) - float(args.capital),
            "return_pct": float(result.total_return_pct),
            "trades": int(result.total_trades),
            "wins": int(result.winning_trades),
            "losses": int(result.losing_trades),
            "win_rate": float(result.win_rate),
            "profit_factor": float(result.profit_factor),
            "max_drawdown_pct": float(result.max_drawdown_pct),
            "avg_planned_rr": float(result.avg_planned_rr),
            "exit_reasons": dict(Counter(t.exit_reason for t in trades)),
        },
        "elapsed_sec": elapsed,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"report_{friday.date()}.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    s = report["summary"]
    print(
        f"Friday {friday.date()}  account=${s['account']:,.2f}  "
        f"trades={s['trades']} WR={s['win_rate']*100:.1f}%  "
        f"PF={s['profit_factor']:.2f} MDD={s['max_drawdown_pct']*100:.1f}%  "
        f"→ {path}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
