#!/usr/bin/env python3
"""Replay fixed AlexG entries with alternate SL/TP parameters."""

from __future__ import annotations

import itertools
from dataclasses import dataclass

from borex.alexg.risk import apply_sl_multiplier, structure_take_profit
from borex.alexg.strategy import AlexGMethodStrategy
from borex.backtest.engine import BacktestConfig, BacktestEngine
from borex.data import build_full_mtf_context, load_market_data
from borex.models.candle import Candle, SignalAction


@dataclass(frozen=True)
class FixedEntry:
    entry_index: int
    entry_price: float
    stop_loss: float
    side: str
    pattern: str
    original_exit: str
    original_pnl_pct: float


def collect_entries(
    symbol: str = "GBPUSD=X",
    period: str = "730d",
    interval: str = "1h",
) -> tuple[list[Candle], list[FixedEntry]]:
    candles = load_market_data(symbol, period, interval, cache_mode="only")
    mtf = build_full_mtf_context(candles, interval, symbol, period, cache_mode="only")
    strategy = AlexGMethodStrategy(min_rr=3.0, max_tp_pct=0.01)
    config = BacktestConfig(
        initial_capital=10_000,
        leverage=1.0,
        stop_loss_pct=None,
        take_profit_pct=None,
    )
    result = BacktestEngine(strategy, config).run(
        candles, symbol=symbol, timeframe=interval, mtf=mtf
    )
    entries = [
        FixedEntry(
            entry_index=t.entry_index,
            entry_price=t.entry_price,
            stop_loss=t.stop_loss or 0.0,
            side=t.side.value,
            pattern=t.pattern,
            original_exit=t.exit_reason,
            original_pnl_pct=t.pnl_pct,
        )
        for t in result.trades
    ]
    return candles, entries


def simulate_exit(
    candles: list[Candle],
    entry: FixedEntry,
    min_rr: float,
    max_tp_pct: float | None,
    sl_mult: float = 1.0,
) -> tuple[str, float]:
    action = SignalAction.BUY if entry.side == "long" else SignalAction.SELL
    entry_px = entry.entry_price
    if entry.stop_loss <= 0:
        return "invalid", 0.0

    sl = apply_sl_multiplier(entry_px, entry.stop_loss, action, sl_mult)

    tp = structure_take_profit(
        candles,
        entry.entry_index - 1,
        action,
        entry_px,
        sl,
        min_rr,
        swings=None,
        max_tp_pct=max_tp_pct,
    )

    if entry.side == "long" and tp <= entry_px:
        return "invalid", 0.0
    if entry.side == "short" and tp >= entry_px:
        return "invalid", 0.0

    for i in range(entry.entry_index + 1, len(candles)):
        c = candles[i]
        if entry.side == "long":
            if c.low <= sl:
                pnl_pct = (sl - entry_px) / entry_px
                return "stop_loss", pnl_pct
            if c.high >= tp:
                pnl_pct = (tp - entry_px) / entry_px
                return "take_profit", pnl_pct
        else:
            if c.high >= sl:
                pnl_pct = (entry_px - sl) / entry_px
                return "stop_loss", pnl_pct
            if c.low <= tp:
                pnl_pct = (entry_px - tp) / entry_px
                return "take_profit", pnl_pct

    last = candles[-1].close
    if entry.side == "long":
        pnl_pct = (last - entry_px) / entry_px
    else:
        pnl_pct = (entry_px - last) / entry_px
    return "end_of_data", pnl_pct


def run_grid(candles: list[Candle], entries: list[FixedEntry]) -> list[dict]:
    min_rrs = [1.5, 2.0, 2.5, 3.0, 4.0, 5.0]
    max_tps: list[float | None] = [0.005, 0.007, 0.01, 0.015, 0.02, None]
    sl_mults = [0.75, 1.0, 1.25, 1.5]

    rows: list[dict] = []
    for min_rr, max_tp, sl_mult in itertools.product(min_rrs, max_tps, sl_mults):
        equity = 10_000.0
        peak = equity
        max_dd = 0.0
        wins = losses = 0
        skipped = 0
        exit_counts: dict[str, int] = {}

        for e in entries:
            reason, pnl_pct = simulate_exit(candles, e, min_rr, max_tp, sl_mult)
            if reason == "invalid":
                skipped += 1
                continue
            exit_counts[reason] = exit_counts.get(reason, 0) + 1
            pnl = equity * pnl_pct
            equity += pnl
            if pnl > 0:
                wins += 1
            else:
                losses += 1
            peak = max(peak, equity)
            if peak > 0:
                max_dd = max(max_dd, (peak - equity) / peak)

        n = wins + losses
        rows.append(
            {
                "min_rr": min_rr,
                "max_tp_pct": max_tp if max_tp is not None else -1,
                "sl_mult": sl_mult,
                "return_pct": (equity - 10_000) / 10_000 * 100,
                "final_equity": equity,
                "trades": n,
                "skipped": skipped,
                "wins": wins,
                "losses": losses,
                "win_rate": wins / n * 100 if n else 0,
                "max_dd_pct": max_dd * 100,
                "tp_hits": exit_counts.get("take_profit", 0),
                "sl_hits": exit_counts.get("stop_loss", 0),
                "eod": exit_counts.get("end_of_data", 0),
            }
        )
    return rows


def print_trade_breakdown(candles: list[Candle], entries: list[FixedEntry]) -> None:
    print("\n=== ORIGINAL 37 TRADES ===")
    sl = sum(1 for e in entries if "stop" in e.original_exit)
    tp = sum(1 for e in entries if e.original_exit == "take_profit")
    opp = sum(1 for e in entries if e.original_exit == "opposite_signal")
    eod = sum(1 for e in entries if e.original_exit == "end_of_data")
    print(f"SL: {sl} | TP: {tp} | opposite: {opp} | EOD: {eod}")
    print(f"Avg loss when SL: {sum(e.original_pnl_pct for e in entries if 'stop' in e.original_exit) / max(sl,1)*100:.3f}%")
    print(f"Avg win when TP: {sum(e.original_pnl_pct for e in entries if e.original_exit == 'take_profit') / max(tp,1)*100:.3f}%")

    # Per-trade: would TP at 0.5% have saved losers?
    print("\n=== LOSERS: max favorable excursion (MFE) ===")
    for i, e in enumerate(entries, 1):
        if e.original_pnl_pct >= 0:
            continue
        mfe = 0.0
        mae = 0.0
        for c in candles[e.entry_index + 1 :]:
            if e.side == "long":
                mfe = max(mfe, (c.high - e.entry_price) / e.entry_price)
                mae = min(mae, (c.low - e.entry_price) / e.entry_price)
            else:
                mfe = max(mfe, (e.entry_price - c.low) / e.entry_price)
                mae = min(mae, (e.entry_price - c.high) / e.entry_price)
        print(
            f"  #{i:2d} {e.side:5s} entry={e.entry_price:.5f} "
            f"MFE={mfe*100:+.2f}% MAE={mae*100:+.2f}% exit={e.original_exit}"
        )


def main() -> None:
    candles, entries = collect_entries()
    print(f"Collected {len(entries)} trades from GBPUSD 730d 1h")
    print_trade_breakdown(candles, entries)

    rows = run_grid(candles, entries)
    rows.sort(key=lambda r: (r["return_pct"], -r["max_dd_pct"]), reverse=True)

    print("\n=== TOP 15 PARAMETER SETS (SL/TP only, same entries) ===")
    print(
        f"{'RR':>4} {'maxTP%':>7} {'SLx':>5} {'Return%':>8} {'WR%':>6} "
        f"{'MaxDD%':>7} {'TP':>3} {'SL':>3} {'EOD':>3}"
    )
    for r in rows[:15]:
        tp_label = "none" if r["max_tp_pct"] < 0 else f"{r['max_tp_pct']*100:.1f}"
        print(
            f"{r['min_rr']:4.1f} {tp_label:>7} {r['sl_mult']:5.2f} "
            f"{r['return_pct']:8.2f} {r['win_rate']:6.1f} "
            f"{r['max_dd_pct']:7.2f} {r['tp_hits']:3d} {r['sl_hits']:3d} {r['eod']:3d}"
        )

    # Best balanced: return / dd
    for r in rows:
        r["score"] = r["return_pct"] - 0.5 * r["max_dd_pct"]
    rows.sort(key=lambda r: r["score"], reverse=True)
    best = rows[0]
    print("\n=== RECOMMENDED (return - 0.5*DD) ===")
    tp_label = "sin tope" if best["max_tp_pct"] < 0 else f"{best['max_tp_pct']*100:.1f}%"
    print(
        f"--min-rr {best['min_rr']} --max-tp-pct {best['max_tp_pct'] if best['max_tp_pct']>=0 else 'omitir'} "
        f"(SL estructural x{best['sl_mult']:.2f})"
    )
    print(
        f"Return: {best['return_pct']:.2f}% | WR: {best['win_rate']:.1f}% | "
        f"MaxDD: {best['max_dd_pct']:.2f}% | TP:{best['tp_hits']} SL:{best['sl_hits']}"
    )

    # Leverage projection for top 3
    print("\n=== LEVERAGE PROJECTION (top 3 configs) ===")
    for r in rows[:3]:
        for lev in [1, 5, 10]:
            ret = r["return_pct"] * lev
            tp_l = "none" if r["max_tp_pct"] < 0 else f"{r['max_tp_pct']*100:.1f}%"
            print(
                f"RR={r['min_rr']} TP={tp_l} SLx={r['sl_mult']} "
                f"lev={lev}x -> ~{ret:.1f}%"
            )


if __name__ == "__main__":
    main()
