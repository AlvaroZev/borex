"""Backtest the 8792 live window with the same alexg8 knobs.

Entries from 2026-08-27 01:00 UTC (theory epoch). Capital $1000.
same_bar_exit=False, daily flatten ON, Friday flatten OFF.
"""
from __future__ import annotations

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

OUT = ROOT / "data" / "runs" / "mirror_2d_8792"
ENTRY_START = pd.Timestamp("2026-08-27 01:00:00", tz="UTC")
WARMUP_DAYS = 400
GHOST_PRIME_BARS = 72
CAPITAL = 1000.0
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
    intra_hour_sl=False,
)


def _ts(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    if t.tzinfo is None:
        return t.tz_localize("UTC")
    return t.tz_convert("UTC")


def load_mt5(start: datetime, end: datetime) -> tuple[dict, dict[str, float]]:
    from borex.viewerMT5.mt5_feed import (
        _dedupe_hourly,
        _drop_forming_bar,
        fetch_mt5_candles,
        list_tradeable_yahoo_symbols,
        read_spread_pips,
    )
    from borex_live.mt5.client import Mt5Client
    import os

    print(f"Loading MT5 H1 {start.date()} → {end.date()}", flush=True)
    # Attach to the already-open mirror terminal (recent H1). Do not login.
    path = os.environ.get("MIRROR_MT5_PATH") or os.environ.get("MT5_PATH", "")
    client = Mt5Client(path=path)
    client.connect()
    off = int(getattr(client, "server_offset_seconds", 0) or 0)
    print(f"MT5 server offset {off}s ({off / 3600:.1f}h)", flush=True)
    raw: dict = {}
    spreads: dict[str, float] = {}
    tail_start = end - timedelta(days=21)
    try:
        symbols = list_tradeable_yahoo_symbols(client=client)
        for i, sym in enumerate(symbols, 1):
            hist = fetch_mt5_candles(
                sym,
                "1h",
                start,
                end,
                client=client,
                use_cache=True,
                write_cache=True,
            )
            bars = list(hist)
            try:
                tail = fetch_mt5_candles(
                    sym,
                    "1h",
                    tail_start,
                    end,
                    client=client,
                    use_cache=False,
                    write_cache=True,
                )
                bars = _dedupe_hourly(bars + list(tail))
            except Exception as exc:
                print(f"  tail skip {sym}: {exc}", flush=True)
                bars = _dedupe_hourly(bars)
            bars = _drop_forming_bar(bars, "1h")
            if len(bars) >= 120:
                raw[sym] = bars
                spreads[sym] = read_spread_pips(client, sym)
            if i % 15 == 0 or i == len(symbols):
                last = bars[-1].timestamp if bars else "?"
                print(f"  loaded {len(raw)}/{i} last={last}", flush=True)
    finally:
        # Do not shutdown — another process owns this terminal.
        client._connected = False
        client._mt5 = None
    minutes = {
        pd.Timestamp(c.timestamp).minute
        for bars in raw.values()
        for c in bars[-48:]
    }
    print(f"Pairs {len(raw)} H1 minutes={sorted(minutes)}", flush=True)
    if minutes - {0}:
        raise SystemExit(f"H1 grid is not on the hour: {sorted(minutes)}")
    if spreads:
        vals = sorted(spreads.values())
        mid = vals[len(vals) // 2]
        print(
            f"Spreads n={len(spreads)} median={mid:.2f} pip "
            f"min={vals[0]:.2f} max={vals[-1]:.2f}",
            flush=True,
        )
    return raw, spreads


def main() -> int:
    # Live 8792 last processed Friday 23:46 UTC (FX closed). Do not replay
    # Saturday phantom bars the broker never traded.
    live_end = datetime(2026, 8, 28, 23, 59, tzinfo=timezone.utc)
    end = min(datetime.now(timezone.utc), live_end)
    start = end - timedelta(days=WARMUP_DAYS)
    data, spreads = load_mt5(start, end)
    if not data:
        raise SystemExit("no MT5 bars")

    from borex.alexg import AlexG8Strategy
    from borex.alexg.multi_market import pick_master_symbol
    from borex.backtest.engine import BacktestConfig
    from borex.backtest.multi_market_engine import MultiMarketEngine

    master = pick_master_symbol(data)
    master_bars = data[master]
    entry_i = next(
        (i for i, c in enumerate(master_bars) if _ts(c.timestamp) >= ENTRY_START),
        None,
    )
    if entry_i is None:
        raise SystemExit(f"no master bar on/after {ENTRY_START}")

    strategy = AlexG8Strategy(
        min_rr=float(KNOBS["true_sl_rr"]),
        execution_interval="1h",
        peer_blend=0.0,
    )
    prime_i = max(int(strategy.min_bars), entry_i - GHOST_PRIME_BARS)
    print(
        f"Window {ENTRY_START} → {master_bars[-1].timestamp} | "
        f"engine from master {prime_i} (ghost replay) "
        f"report from {entry_i}/{len(master_bars)} warmup={strategy.min_bars}",
        flush=True,
    )

    config = BacktestConfig(
        initial_capital=CAPITAL,
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
        min_commission_per_side=0.04,
        lot_notional=100_000.0,
        risk_include_commission=bool(KNOBS["risk_include_commission"]),
        commission_at_entry=bool(KNOBS["commission_at_entry"]),
        force_flat_friday=bool(KNOBS["force_flat_friday"]),
        force_flat_daily=bool(KNOBS["force_flat_daily"]),
        force_flat_utc_hour=19,
        force_flat_friday_from_hour=19,
        winrate_min_trades=20,
        spread_pips=0.0,
        spread_pips_by_symbol=spreads,
        slippage_pips=0.0,
        intra_hour_sl=bool(KNOBS["intra_hour_sl"]),
    )
    t0 = time.perf_counter()
    engine = MultiMarketEngine(strategy, config, max_positions=60)
    engine._progress_every = 25
    result = engine.run(
        data,
        timeframe="1h",
        master_symbol=master,
        same_bar_exit=False,
        start_master_i=prime_i,
    )
    elapsed = time.perf_counter() - t0

    window = [t for t in result.trades if _ts(t.entry_time) >= ENTRY_START]
    trades = []
    for t in window:
        trades.append(
            {
                "symbol": t.symbol,
                "side": t.side.value if hasattr(t.side, "value") else str(t.side),
                "entry_time": str(t.entry_time),
                "exit_time": str(t.exit_time),
                "entry": float(t.entry_price),
                "exit": float(t.exit_price or 0),
                "sl": float(t.stop_loss or 0),
                "tp": float(t.take_profit or 0),
                "pnl": float(t.pnl),
                "margin": float(t.margin or 0),
                "rr": float(t.score or 0),
                "exit_reason": t.exit_reason,
                "pattern": str(t.pattern or ""),
            }
        )

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "capital": CAPITAL,
        "entry_start": str(ENTRY_START),
        "first_bar": str(master_bars[0].timestamp),
        "last_bar": str(master_bars[-1].timestamp),
        "master": master,
        "knobs": KNOBS,
        "spread_pips_by_symbol": {k: round(v, 4) for k, v in sorted(spreads.items())},
        "same_bar_exit": False,
        "ghost_replay_bars": entry_i - prime_i,
        "warmup_trades": int(result.total_trades) - len(window),
        "summary": {
            "account": CAPITAL + sum(float(t.pnl) for t in window),
            "net_pnl": sum(float(t.pnl) for t in window),
            "return_pct": (sum(float(t.pnl) for t in window) / CAPITAL) if CAPITAL else 0.0,
            "trades": len(window),
            "wins": sum(1 for t in window if t.pnl > 0),
            "losses": sum(1 for t in window if t.pnl <= 0),
            "win_rate": (sum(1 for t in window if t.pnl > 0) / len(window)) if window else 0.0,
            "profit_factor": (
                (sum(t.pnl for t in window if t.pnl > 0) / abs(sum(t.pnl for t in window if t.pnl <= 0)))
                if any(t.pnl <= 0 for t in window) and sum(t.pnl for t in window if t.pnl <= 0) != 0
                else 0.0
            ),
            "max_drawdown_pct": float(result.max_drawdown_pct),
            "avg_planned_rr": float(result.avg_planned_rr),
            "exit_reasons": dict(Counter(t.exit_reason for t in window)),
            "full_run_equity": float(result.final_equity),
        },
        "trades": trades,
        "elapsed_sec": elapsed,
    }
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "backtest.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    s = report["summary"]
    print(
        f"BT account=${s['account']:,.2f} trades={s['trades']} "
        f"WR={s['win_rate']*100:.1f}% PF={s['profit_factor']:.2f} "
        f"MDD={s['max_drawdown_pct']*100:.1f}% last={master_bars[-1].timestamp} → {path}",
        flush=True,
    )
    broker = OUT / "compare_books.json"
    if broker.is_file():
        prev = json.loads(broker.read_text(encoding="utf-8"))
        off = prev.get("official") or {}
        print(
            f"Broker equity={off.get('equity')} net={off.get('net_profit')} "
            f"trades={off.get('total_trades')} WR={off.get('profit_trades')}",
            flush=True,
        )
        print(
            f"Theory closed n={prev.get('theory_closed', {}).get('n')} "
            f"net={prev.get('theory_closed', {}).get('net')}",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
