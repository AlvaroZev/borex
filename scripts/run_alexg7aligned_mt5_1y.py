#!/usr/bin/env python3
"""One-year alexg7aligned/alexg8 MT5 H1 backtest with live-matching params.

Pre-quantizes OHLC once per symbol (same as strategy.ohlc_quantize_pips /
peer_blend=0) so MultiMarketEngine does not rebuild every bar.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
LIVE = ROOT.parent / "borex_live"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(LIVE))
load_dotenv(LIVE / ".env")

def _pip_size(symbol: str) -> float:
    sym = symbol.upper().replace("=X", "")
    return 0.01 if "JPY" in sym else 0.0001


def prequantize(candles, symbol: str, pips: float):
    from borex.models.candle import Candle

    step = pips * _pip_size(symbol)
    if step <= 0:
        return candles

    def snap(x: float) -> float:
        return round(x / step) * step

    out = []
    for c in candles:
        o, h, l, cl = snap(c.open), snap(c.high), snap(c.low), snap(c.close)
        hi = max(h, o, cl)
        lo = min(l, o, cl)
        out.append(
            Candle(
                timestamp=c.timestamp,
                open=o,
                high=hi,
                low=lo,
                close=cl,
                volume=c.volume,
            )
        )
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--strategy",
        choices=("alexg7aligned", "alexg8"),
        default="alexg7aligned",
    )
    args = parser.parse_args()

    from borex.alexg import AlexG7AlignedStrategy, AlexG8Strategy
    from borex.alexg.multi_market import pick_master_symbol
    from borex.backtest.engine import BacktestConfig
    from borex.backtest.multi_market_engine import MultiMarketEngine
    from borex.viewerMT5.mt5_feed import (
        connect_mt5_client,
        fetch_mt5_candles,
        list_tradeable_yahoo_symbols,
    )

    capital = 889.69
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=365)
    print(f"Loading MT5 H1 {start.date()} → {end.date()}", flush=True)

    client = connect_mt5_client()
    raw: dict = {}
    coverage: dict = {}
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
                coverage[sym] = {
                    "bars": len(bars),
                    "first": str(bars[0].timestamp),
                    "last": str(bars[-1].timestamp),
                }
            if i % 15 == 0 or i == len(symbols):
                print(f"  loaded {len(raw)}/{i}", flush=True)
    finally:
        client.disconnect()

    strategy_cls = AlexG8Strategy if args.strategy == "alexg8" else AlexG7AlignedStrategy
    strategy = strategy_cls(
        min_rr=3.0,
        execution_interval="1h",
        peer_blend=0.0,
    )
    # Pre-apply the same OHLC quantize the strategy would do on every bar.
    data = {
        sym: prequantize(bars, sym, strategy.ohlc_quantize_pips)
        for sym, bars in raw.items()
    }
    strategy.ohlc_quantize_pips = 0.0  # already applied; skip per-bar rebuild

    config = BacktestConfig(
        initial_capital=capital,
        leverage=5000.0,
        position_size_pct=0.01,
        size_mode="margin",
        true_sl=True,
        true_sl_rr=3.0,
        rr_mode="dynamic",
        rr_factor=1.88,
        stop_loss_pct=None,
        take_profit_pct=None,
        commission_per_lot=7.0,
        risk_include_commission=True,
        winrate_min_trades=20,
        spread_pips=0.0,
        slippage_pips=0.0,
    )
    master = pick_master_symbol(data)
    print(
        f"Running {args.strategy} | pairs={len(data)} master={master} "
        f"bars={len(data[master])}",
        flush=True,
    )
    result = MultiMarketEngine(strategy, config, max_positions=60).run(
        data,
        timeframe="1h",
        master_symbol=master,
        same_bar_exit=False,
    )

    monthly: dict[str, dict] = defaultdict(lambda: {"pnl": 0.0, "trades": 0, "wins": 0})
    by_symbol: dict[str, dict] = defaultdict(lambda: {"pnl": 0.0, "trades": 0, "wins": 0})
    for t in result.trades:
        ts = pd.Timestamp(t.exit_time or t.entry_time)
        key = ts.strftime("%Y-%m")
        monthly[key]["pnl"] += float(t.pnl)
        monthly[key]["trades"] += 1
        monthly[key]["wins"] += int(t.pnl > 0)
        s = by_symbol[t.symbol]
        s["pnl"] += float(t.pnl)
        s["trades"] += 1
        s["wins"] += int(t.pnl > 0)

    # downsample equity for chart (~120 pts)
    eq = list(result.equity_curve or [])
    if len(eq) > 120:
        step = max(1, len(eq) // 120)
        eq_chart = [{"i": i, "equity": float(eq[i])} for i in range(0, len(eq), step)]
        if eq_chart[-1]["i"] != len(eq) - 1:
            eq_chart.append({"i": len(eq) - 1, "equity": float(eq[-1])})
    else:
        eq_chart = [{"i": i, "equity": float(v)} for i, v in enumerate(eq)]

    report = {
        "generated_at": end.isoformat(),
        "window": {
            "requested_start": start.isoformat(),
            "requested_end": end.isoformat(),
            "first_master_bar": coverage[master]["first"],
            "last_master_bar": coverage[master]["last"],
        },
        "params": {
            "strategy": args.strategy,
            "feed": "MT5 H1",
            "capital": capital,
            "leverage": 5000.0,
            "position_size_pct": 0.01,
            "min_rr": 3.0,
            "rr_mode": "dynamic",
            "rr_factor": 1.88,
            "winrate_min_trades": 20,
            "max_positions": 60,
            "same_bar_exit": False,
            "commission_per_lot": 7.0,
            "risk_include_commission": True,
            "spread_pips": 0.0,
            "slippage_pips": 0.0,
            "peer_blend": 0.0,
            "session": "all" if args.strategy == "alexg8" else "overlap",
        },
        "coverage": {
            "pairs": len(data),
            "full_year_pairs": sum(v["bars"] >= 6000 for v in coverage.values()),
            "limited_pairs": sum(v["bars"] < 6000 for v in coverage.values()),
            "master": master,
            "master_bars": len(data[master]),
        },
        "summary": {
            "initial_equity": capital,
            "final_equity": result.final_equity,
            "net_pnl": result.final_equity - capital,
            "return_pct": result.total_return_pct,
            "max_drawdown_pct": result.max_drawdown_pct,
            "trades": result.total_trades,
            "wins": result.winning_trades,
            "losses": result.losing_trades,
            "win_rate": result.win_rate,
            "profit_factor": result.profit_factor,
            "avg_win": result.avg_win,
            "avg_loss": result.avg_loss,
            "total_commission": result.total_commission,
            "avg_planned_rr": result.avg_planned_rr,
        },
        "monthly": dict(sorted(monthly.items())),
        "exit_reasons": dict(Counter(t.exit_reason for t in result.trades)),
        "sides": dict(Counter(t.side.value for t in result.trades)),
        "top_symbols": sorted(
            ({"symbol": k, **v} for k, v in by_symbol.items()),
            key=lambda x: x["pnl"],
            reverse=True,
        )[:10],
        "bottom_symbols": sorted(
            ({"symbol": k, **v} for k, v in by_symbol.items()),
            key=lambda x: x["pnl"],
        )[:10],
        "equity_curve": eq_chart,
    }

    out_dir = ROOT / "data" / "runs" / f"{args.strategy}_mt5_1y_liveparams"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "report.json"
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Wrote {out_path}", flush=True)
    s = report["summary"]
    print(
        f"RETURN {s['return_pct']*100:+.2f}% | final ${s['final_equity']:.2f} | "
        f"trades={s['trades']} WR={s['win_rate']*100:.1f}% MDD={s['max_drawdown_pct']*100:.1f}% "
        f"PF={s['profit_factor']:.2f}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
