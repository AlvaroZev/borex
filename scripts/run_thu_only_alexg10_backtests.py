#!/usr/bin/env python3
"""Three frozen-N1-family backtests through available data into October.

Variants
--------
A) alexg8 frozen N1 knobs, **Thursday-only** entries, 1% margin
B) same as A, **2%** margin per trade
C) **alexg10** — fill at ghost planned entry, TP = ghost SL (flipped), all weekdays, 1%

Notes
-----
* Frozen research ``thursday_gate`` *blocks* Thursday. Here A/B are the opposite:
  **only** Thursday UTC fills (user request).
* Data: MT5 H1 (utc_v2 cache + live refresh). No September firewall.
* H1 + ghost_fill_at_close (Live-like). Skips M1 for multi-month speed.
"""

from __future__ import annotations

import json
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

MAIN_ROOT = Path(r"C:\Users\azeva\OneDrive\Documentos\work\trading\borex-main")
LIVE_ROOT = Path(r"C:\Users\azeva\OneDrive\Documentos\work\trading\borex_live")
sys.path[:0] = [str(MAIN_ROOT), str(LIVE_ROOT)]

from borex.alexg import AlexG8Strategy, AlexG10Strategy
from borex.alexg.multi_market import default_forex_universe, pick_master_symbol
from borex.backtest.engine import BacktestConfig
from borex.backtest.multi_market_engine import MultiMarketEngine
from borex.models.candle import Candle
from borex.viewerMT5.mt5_feed import (
    fetch_mt5_candles,
    connect_mt5_client,
    read_spread_pips,
)

OUT = (
    MAIN_ROOT
    / "data"
    / "runs"
    / "research"
    / f"frozen_thu_only_alexg10_rrdyn_noflat_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
)
OUT.mkdir(parents=True, exist_ok=True)

CAPITAL = 1000.0
LEV = 500.0
RR_FACTOR = 1.0
FORCE_FLAT_DAILY = False
# Include history before eval so ghosts can prime.
# Jun→Oct keeps the post-firewall months the user asked for without a
# multi-hour 60-pair Jan→Oct walk.
ENTRY_START = pd.Timestamp("2026-06-01", tz="UTC")
DATA_START = pd.Timestamp("2026-04-01", tz="UTC")
MAX_POSITIONS = 60
GHOST_PRIME_BARS = 72


class ThursdayOnlyEntryGate:
    """Drop NEW entry signals unless the bar is Thursday UTC (weekday=3)."""

    def __init__(self, inner: Any) -> None:
        object.__setattr__(self, "_inner", inner)

    def __getattr__(self, name: str) -> Any:
        if name == "_inner":
            raise AttributeError(name)
        return getattr(object.__getattribute__(self, "_inner"), name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "_inner":
            object.__setattr__(self, name, value)
            return
        setattr(object.__getattribute__(self, "_inner"), name, value)

    def on_bar(self, index: int, candles: list[Candle], portfolio: Any):
        inner = object.__getattribute__(self, "_inner")
        sig = inner.on_bar(index, candles, portfolio)
        if sig is None:
            return None
        ts = pd.Timestamp(candles[index].timestamp)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        else:
            ts = ts.tz_convert("UTC")
        if int(ts.dayofweek) != 3:
            return None
        return sig


def _ts(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def bt_cfg(
    spreads: dict[str, float],
    *,
    position_size_pct: float,
    true_sl: bool,
    honor_signal_stops: bool = False,
    rr_mode: str = "dynamic",
) -> BacktestConfig:
    return BacktestConfig(
        initial_capital=CAPITAL,
        leverage=LEV,
        position_size_pct=float(position_size_pct),
        size_mode="margin",
        true_sl=bool(true_sl),
        true_sl_rr=2.0,
        honor_signal_stops=bool(honor_signal_stops),
        rr_mode=str(rr_mode),
        rr_factor=float(RR_FACTOR),
        rr_min=2.0,
        rr_max=5.0,
        commission_per_lot=7.0,
        min_commission_per_side=0.04,
        lot_notional=100_000.0,
        risk_include_commission=True,
        force_flat_daily=bool(FORCE_FLAT_DAILY),
        force_flat_friday=False,
        force_flat_utc_hour=19,
        force_flat_friday_from_hour=19,
        winrate_min_trades=20,
        spread_pips=0.0,
        spread_pips_by_symbol=spreads,
        slippage_pips=0.0,
        intra_hour_sl=False,
    )


def load_h1_universe(symbols: list[str], start: pd.Timestamp, end: pd.Timestamp):
    client = connect_mt5_client()
    data: dict[str, list[Candle]] = {}
    spreads: dict[str, float] = {}
    try:
        total = len(symbols)
        for i, sym in enumerate(symbols, 1):
            try:
                bars = fetch_mt5_candles(
                    sym,
                    "1h",
                    start.to_pydatetime(),
                    end.to_pydatetime(),
                    client=client,
                    use_cache=True,
                    write_cache=True,
                )
                if len(bars) < 200:
                    print(f"  [{i}/{total}] skip {sym}: only {len(bars)} bars")
                    continue
                data[sym] = bars
                try:
                    spreads[sym] = float(read_spread_pips(client, sym) or 0.0)
                except Exception:
                    spreads[sym] = 0.0
                if i % 10 == 0 or i == total:
                    print(f"  [{i}/{total}] loaded {len(data)} symbols…")
            except Exception as exc:
                print(f"  [{i}/{total}] skip {sym}: {exc}")
    finally:
        try:
            client.disconnect()
        except Exception:
            pass
    return data, spreads


def summarize(trades: list[dict[str, Any]], *, capital: float) -> dict[str, Any]:
    if not trades:
        return {
            "n": 0,
            "wins": 0,
            "losses": 0,
            "wr": None,
            "pnl": 0.0,
            "ret_pct": 0.0,
            "avg_pnl": None,
            "by_exit": {},
        }
    pnls = [float(t["pnl"]) for t in trades]
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p <= 0)
    by_exit: dict[str, int] = {}
    for t in trades:
        r = str(t.get("exit_reason") or "?")
        by_exit[r] = by_exit.get(r, 0) + 1
    total = sum(pnls)
    return {
        "n": len(trades),
        "wins": wins,
        "losses": losses,
        "wr": wins / len(trades) if trades else None,
        "pnl": total,
        "ret_pct": (total / capital) * 100.0,
        "avg_pnl": total / len(trades),
        "by_exit": by_exit,
    }


def run_variant(
    *,
    name: str,
    strategy: Any,
    spreads: dict[str, float],
    data: dict[str, list[Candle]],
    master: str,
    entry_start: pd.Timestamp,
    position_size_pct: float,
    true_sl: bool,
    thursday_only: bool,
    honor_signal_stops: bool = False,
) -> dict[str, Any]:
    t0 = time.time()
    if thursday_only:
        strategy = ThursdayOnlyEntryGate(strategy)

    engine = MultiMarketEngine(
        strategy,
        bt_cfg(
            spreads,
            position_size_pct=position_size_pct,
            true_sl=true_sl,
            honor_signal_stops=honor_signal_stops,
        ),
        max_positions=MAX_POSITIONS,
    )
    engine._progress_every = 200

    entry_i = next(
        i
        for i, c in enumerate(data[master])
        if _ts(c.timestamp) >= entry_start
    )
    # min_bars on wrapper → inner
    min_bars = int(getattr(strategy, "min_bars", 100) or 100)
    start_i = max(min_bars, entry_i - GHOST_PRIME_BARS)

    print(
        f"\n=== {name} === start_i={start_i} entry_i={entry_i} "
        f"bars={len(data[master])} thu_only={thursday_only} "
        f"risk={position_size_pct:.0%} true_sl={true_sl} "
        f"rr=dynamic*{RR_FACTOR:g} flat_daily={FORCE_FLAT_DAILY}",
        flush=True,
    )
    result = engine.run(
        data,
        timeframe="1h",
        master_symbol=master,
        same_bar_exit=False,
        start_master_i=start_i,
    )
    print(f"  engine done in {time.time()-t0:.1f}s", flush=True)

    rows = []
    for t in result.trades:
        if _ts(t.entry_time) < entry_start:
            continue
        side = t.side.value if hasattr(t.side, "value") else str(t.side)
        rows.append(
            {
                "symbol": t.symbol,
                "side": side,
                "entry_time": str(t.entry_time),
                "exit_time": str(t.exit_time) if t.exit_time is not None else None,
                "entry_price": float(t.entry_price),
                "exit_price": float(t.exit_price) if t.exit_price is not None else None,
                "stop_loss": float(t.stop_loss) if t.stop_loss is not None else None,
                "take_profit": float(t.take_profit) if t.take_profit is not None else None,
                "pnl": float(t.pnl or 0.0),
                "commission": float(getattr(t, "commission", 0) or 0.0),
                "margin": float(t.margin or 0.0),
                "exit_reason": t.exit_reason,
                "pattern": str(t.pattern or ""),
                "dow": int(_ts(t.entry_time).dayofweek),
            }
        )

    summary = summarize(rows, capital=CAPITAL)
    summary.update(
        {
            "name": name,
            "elapsed_s": round(time.time() - t0, 1),
            "final_equity": float(result.final_equity),
            "max_dd_pct": float(result.max_drawdown_pct),
            "thursday_only": thursday_only,
            "position_size_pct": position_size_pct,
            "true_sl": true_sl,
            "rr_mode": "dynamic",
            "rr_factor": float(RR_FACTOR),
            "force_flat_daily": bool(FORCE_FLAT_DAILY),
            "leverage": float(LEV),
            "n_open_end": sum(1 for t in result.trades if t.is_open),
        }
    )

    (OUT / f"{name}_trades.json").write_text(
        json.dumps(rows, indent=2), encoding="utf-8"
    )
    (OUT / f"{name}_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    print(
        f"  n={summary['n']} wr={summary['wr']} pnl={summary['pnl']:.2f} "
        f"ret={summary['ret_pct']:.1f}% dd={summary['max_dd_pct']:.1f}% "
        f"equity={summary['final_equity']:.2f} ({summary['elapsed_s']}s)"
    )
    return {"summary": summary, "trades": rows}


def main() -> int:
    now = pd.Timestamp.now(tz="UTC")
    end = now.floor("h")  # exclusive of forming hour
    print(f"OUT={OUT}")
    print(f"DATA [{DATA_START} -> {end})  ENTRY_START={ENTRY_START}")

    symbols = default_forex_universe()
    if "EURUSD=X" not in symbols:
        symbols = ["EURUSD=X"] + list(symbols)

    print(f"Loading H1 for {len(symbols)} symbols (cache+MT5 refresh)…")
    data, spreads = load_h1_universe(symbols, DATA_START, end)
    if "EURUSD=X" not in data:
        print("ERROR: EURUSD=X missing")
        return 1
    master = pick_master_symbol(data) or "EURUSD=X"
    last = _ts(data[master][-1].timestamp)
    print(f"master={master} symbols={len(data)} last_bar={last}")

    meta = {
        "out": str(OUT),
        "data_start": str(DATA_START),
        "entry_start": str(ENTRY_START),
        "data_end_exclusive": str(end),
        "last_master_bar": str(last),
        "n_symbols": len(data),
        "master": master,
        "capital": CAPITAL,
        "leverage": LEV,
        "rr_factor": RR_FACTOR,
        "force_flat_daily": FORCE_FLAT_DAILY,
        "note": (
            f"All variants: rr_mode=dynamic, rr_factor={RR_FACTOR}, rr_min=2, rr_max=5, "
            f"true_sl=True, leverage={LEV:g}, force_flat_daily={FORCE_FLAT_DAILY}. "
            "A/B = Thursday-ONLY fills. C = alexg10 flipped entry at planned "
            "ghost entry; exits use true_sl + dynamic RR."
        ),
    }
    (OUT / "META.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    results = []

    # A) Thursday only, 1%, dynamic RR, no daily flat
    try:
        results.append(
            run_variant(
                name="A_thu_only_1pct_rrdyn_noflat",
                strategy=AlexG8Strategy(
                    min_rr=2.0,
                    execution_interval="1h",
                    peer_blend=0.0,
                    ghost_sl_mult=4.0,
                    ghost_fill_at_close=True,
                ),
                spreads=spreads,
                data=data,
                master=master,
                entry_start=ENTRY_START,
                position_size_pct=0.01,
                true_sl=True,
                thursday_only=True,
            )
        )
    except Exception:
        traceback.print_exc()
        (OUT / "A_ERROR.txt").write_text(traceback.format_exc(), encoding="utf-8")

    # B) Thursday only, 2%
    try:
        results.append(
            run_variant(
                name="B_thu_only_2pct_rrdyn_noflat",
                strategy=AlexG8Strategy(
                    min_rr=2.0,
                    execution_interval="1h",
                    peer_blend=0.0,
                    ghost_sl_mult=4.0,
                    ghost_fill_at_close=True,
                ),
                spreads=spreads,
                data=data,
                master=master,
                entry_start=ENTRY_START,
                position_size_pct=0.02,
                true_sl=True,
                thursday_only=True,
            )
        )
    except Exception:
        traceback.print_exc()
        (OUT / "B_ERROR.txt").write_text(traceback.format_exc(), encoding="utf-8")

    # C) alexg10
    try:
        results.append(
            run_variant(
                name="C_alexg10_entry_rrdyn_noflat",
                strategy=AlexG10Strategy(
                    min_rr=2.0,
                    execution_interval="1h",
                    peer_blend=0.0,
                    ghost_sl_mult=4.0,
                    ghost_fill_at_close=True,
                ),
                spreads=spreads,
                data=data,
                master=master,
                entry_start=ENTRY_START,
                position_size_pct=0.01,
                true_sl=True,
                thursday_only=False,
                honor_signal_stops=False,
            )
        )
    except Exception:
        traceback.print_exc()
        (OUT / "C_ERROR.txt").write_text(traceback.format_exc(), encoding="utf-8")

    summaries = [r["summary"] for r in results]
    (OUT / "ALL_SUMMARIES.json").write_text(
        json.dumps(summaries, indent=2), encoding="utf-8"
    )

    lines = [
        "# Thursday-only + alexg10 (RR dynamic x1, no daily flat, lev 500)",
        "",
        f"- Out: `{OUT}`",
        f"- Eval entries: `{ENTRY_START}` -> last closed H1 `{last}`",
        f"- Universe: {len(data)} FX pairs, H1, ghost_fill_at_close, lev={LEV:g}, capital=${CAPITAL:g}",
        f"- **RR: dynamic** (factor={RR_FACTOR:g}, clamp 2..5), true_sl=True",
        f"- **force_flat_daily={FORCE_FLAT_DAILY}**",
        "",
        "## Results",
        "",
        "| Variant | n | WR | PnL | Ret% | MaxDD% | Equity |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for s in summaries:
        wr = f"{100*s['wr']:.1f}%" if s.get("wr") is not None else "-"
        lines.append(
            f"| {s['name']} | {s['n']} | {wr} | {s['pnl']:.2f} | "
            f"{s['ret_pct']:.1f} | {s['max_dd_pct']:.1f} | {s['final_equity']:.2f} |"
        )
    lines += [
        "",
        "## Definitions",
        "",
        f"- **A / B**: alexg8 + dynamic RR x{RR_FACTOR:g}, lev {LEV:g}, "
        f"no daily flat. Thursday-only fills. Risk 1% vs 2%.",
        f"- **C (alexg10)**: Flipped entry at planned ghost entry; "
        f"true_sl + dynamic RR x{RR_FACTOR:g}; no daily flat.",
        "",
    ]
    report = "\n".join(lines)
    (OUT / "REPORT.md").write_text(report, encoding="utf-8")
    print("\n" + report)
    print(f"\nDone -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
