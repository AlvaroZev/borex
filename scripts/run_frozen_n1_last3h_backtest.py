#!/usr/bin/env python3
"""Frozen n1_thursday backtest for the last N hours (MT5 live feed).

Reports:
  - fills that opened in the window (+ PnL if closed)
  - still-open positions at end of window
  - pending ghost setups still waiting
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

LIVE_ROOT = Path(r"C:\Users\azeva\OneDrive\Documentos\work\trading\borex_live")
MAIN_ROOT = Path(r"C:\Users\azeva\OneDrive\Documentos\work\trading\borex-main")
sys.path[:0] = [str(MAIN_ROOT), str(LIVE_ROOT)]

from borex.alexg import AlexG8Strategy
from borex.alexg.multi_market import default_forex_universe, pick_master_symbol
from borex.backtest.engine import BacktestConfig
from borex.backtest.multi_market_engine import MultiMarketEngine
from borex.gate2.thursday_entry_gate import filter_block_thursday_entries
from borex.models.candle import Candle

OUT = (
    MAIN_ROOT
    / "data"
    / "runs"
    / "research"
    / "live_mirror_dashboards"
    / "frozen_n1_last3h_backtest"
)
OUT.mkdir(parents=True, exist_ok=True)

HOURS = 3
WARMUP_H1 = 500  # enough for strategy min_bars + ghost prime
M1_LOOKBACK_HOURS = HOURS + 6
CAPITAL = 1000.0
LEV = 500.0


def _ts(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    if t.tzinfo is None:
        return t.tz_localize("UTC")
    return t.tz_convert("UTC")


def rates_to_candles(rates, *, offset_seconds: int) -> list[Candle]:
    from borex.data.mt5_time import mt5_unix_to_utc

    out: list[Candle] = []
    for r in rates:
        ts = mt5_unix_to_utc(int(r["time"]), offset_seconds)
        out.append(
            Candle(
                timestamp=pd.Timestamp(ts),
                open=float(r["open"]),
                high=float(r["high"]),
                low=float(r["low"]),
                close=float(r["close"]),
                volume=float(r["tick_volume"]),
            )
        )
    return out


def yahoo_to_mt5(sym: str) -> str:
    s = sym.replace("=X", "").replace("=x", "")
    return s


def load_mt5_universe(symbols: list[str], h1_count: int, m1_count: int):
    import MetaTrader5 as mt5
    from borex.data.mt5_time import measure_mt5_server_offset_seconds

    ok = mt5.initialize(timeout=90_000)
    if not ok:
        raise RuntimeError(f"MT5 init failed {mt5.last_error()}")
    acc = mt5.account_info()
    print(
        "MT5",
        None if not acc else (acc.login, acc.leverage, acc.balance, acc.equity),
    )
    offset = int(measure_mt5_server_offset_seconds(mt5) or 0)
    print(f"server_offset_seconds={offset} ({offset/3600:.1f}h)")

    h1: dict[str, list[Candle]] = {}
    m1: dict[str, list[Candle]] = {}
    spreads: dict[str, float] = {}
    for sym in symbols:
        mt5_sym = yahoo_to_mt5(sym)
        info = mt5.symbol_info(mt5_sym)
        if info is None:
            if not mt5.symbol_select(mt5_sym, True):
                continue
            info = mt5.symbol_info(mt5_sym)
        if info is None:
            continue
        mt5.symbol_select(mt5_sym, True)
        point = float(info.point or 0.0)
        dig = int(info.digits or 5)
        pip = point * 10 if dig in (3, 5) else (point if dig in (2, 4) else point * 10)
        spr_pts = float(info.spread or 0.0)
        spreads[sym] = (spr_pts * point / pip) if pip > 0 else 0.0

        rates_h = mt5.copy_rates_from_pos(mt5_sym, mt5.TIMEFRAME_H1, 0, h1_count)
        if rates_h is None or len(rates_h) < 120:
            continue
        bars = rates_to_candles(rates_h, offset_seconds=offset)
        if bars:
            last = bars[-1].timestamp
            now = pd.Timestamp.now(tz="UTC")
            if pd.Timestamp(last).tz_convert("UTC").floor("h") == now.floor("h"):
                bars = bars[:-1]
        if len(bars) < 120:
            continue
        h1[sym] = bars

        rates_m = mt5.copy_rates_from_pos(mt5_sym, mt5.TIMEFRAME_M1, 0, m1_count)
        if rates_m is not None and len(rates_m) > 10:
            mins = rates_to_candles(rates_m, offset_seconds=offset)
            now = pd.Timestamp.now(tz="UTC")
            if mins and pd.Timestamp(mins[-1].timestamp).tz_convert("UTC").floor("min") == now.floor("min"):
                mins = mins[:-1]
            m1[sym] = mins

    mt5.shutdown()
    return h1, m1, spreads, offset


def bt_cfg(spreads: dict[str, float]) -> BacktestConfig:
    return BacktestConfig(
        initial_capital=CAPITAL,
        leverage=LEV,
        position_size_pct=0.01,
        size_mode="margin",
        true_sl=True,
        true_sl_rr=2.0,
        rr_mode="dynamic",
        rr_factor=1.0,
        rr_min=2.0,
        rr_max=5.0,
        commission_per_lot=7.0,
        min_commission_per_side=0.04,
        lot_notional=100_000.0,
        risk_include_commission=True,
        force_flat_daily=True,
        force_flat_friday=False,
        force_flat_utc_hour=19,
        force_flat_friday_from_hour=19,
        winrate_min_trades=20,
        spread_pips=0.0,
        spread_pips_by_symbol=spreads,
        slippage_pips=0.0,
        intra_hour_sl=False,
    )


def trade_row(t) -> dict[str, Any]:
    side = t.side.value if hasattr(t.side, "value") else str(t.side)
    return {
        "symbol": t.symbol,
        "side": side,
        "entry_time": str(t.entry_time),
        "entry_price": float(t.entry_price),
        "stop_loss": t.stop_loss,
        "take_profit": t.take_profit,
        "exit_time": str(t.exit_time) if t.exit_time is not None else None,
        "exit_price": t.exit_price,
        "exit_reason": t.exit_reason,
        "pnl": float(t.pnl or 0.0),
        "commission": float(getattr(t, "commission", 0) or 0.0),
        "margin": float(t.margin or 0.0),
        "rr": float(t.score or 0.0),
        "status": "open" if t.is_open else "closed",
        "pattern": str(t.pattern or ""),
    }


def pending_from_strategy(strategy) -> list[dict[str, Any]]:
    rows = []
    pending = getattr(strategy, "_pending", None) or {}
    # alexg ghost pending structures vary; also check _pending_retest
    if isinstance(pending, dict):
        for sym, p in pending.items():
            rows.append(
                {
                    "symbol": sym,
                    "kind": "pending",
                    "raw": repr(p)[:500],
                }
            )
    retest = getattr(strategy, "_pending_retest", None) or {}
    if isinstance(retest, dict):
        for sym, p in retest.items():
            rows.append(
                {
                    "symbol": sym,
                    "kind": "pending_retest",
                    "raw": repr(p)[:500],
                }
            )
    # GhostEntryManager style
    ghosts = getattr(strategy, "ghosts", None) or getattr(strategy, "_ghosts", None)
    if isinstance(ghosts, dict):
        for sym, g in ghosts.items():
            rows.append({"symbol": sym, "kind": "ghost", "raw": repr(g)[:500]})
    elif isinstance(ghosts, list):
        for g in ghosts:
            rows.append(
                {
                    "symbol": getattr(g, "symbol", "?"),
                    "kind": "ghost",
                    "raw": repr(g)[:500],
                }
            )
    return rows


def main() -> int:
    now = pd.Timestamp.now(tz="UTC")
    entry_start = (now - pd.Timedelta(hours=HOURS)).floor("h")
    end = now.floor("h")  # exclusive of forming hour; last closed H1
    print(f"Window entry_start={entry_start} end={end} (closed H1 only) now={now}")

    symbols = default_forex_universe()
    if "EURUSD=X" not in symbols:
        symbols = ["EURUSD=X"] + list(symbols)

    h1, m1, spreads, offset = load_mt5_universe(
        symbols,
        h1_count=WARMUP_H1 + HOURS + 5,
        m1_count=M1_LOOKBACK_HOURS * 60 + 30,
    )
    master = pick_master_symbol(h1)
    print(f"pairs_h1={len(h1)} pairs_m1={len(m1)} master={master}")
    if master not in h1:
        raise SystemExit("no master bars")

    # Align end to last closed master bar + 1h
    last_master = _ts(h1[master][-1].timestamp)
    end = min(end, last_master + pd.Timedelta(hours=1))
    print(f"adjusted end={end} last_master={last_master} offset={offset}")

    # Truncate series to end
    for sym in list(h1):
        h1[sym] = [c for c in h1[sym] if _ts(c.timestamp) < end]
    for sym in list(m1):
        m1[sym] = [c for c in m1[sym] if _ts(c.timestamp) < end]

    entry_i = next(
        (i for i, c in enumerate(h1[master]) if _ts(c.timestamp) >= entry_start),
        None,
    )
    if entry_i is None:
        raise SystemExit("entry_start beyond available H1")
    prime_i = max(int(AlexG8Strategy().min_bars), entry_i - 72)
    print(f"entry_i={entry_i} prime_i={prime_i} master_bars={len(h1[master])}")

    strategy = AlexG8Strategy(
        min_rr=2.0,
        execution_interval="1h",
        peer_blend=0.0,
        ghost_sl_mult=4.0,
    )
    engine = MultiMarketEngine(strategy, bt_cfg(spreads), max_positions=0)
    engine._progress_every = 10_000

    # Free-run (no prebuilt tape) then apply Thursday filter on resulting trades'
    # entry times for reporting — but for true n1_thursday, filter intents.
    # MultiMarketEngine without decisions uses strategy.on_bar; Thursday gate
    # must wrap strategy.
    from borex_mirror.engine import _ThursdayEntryGate

    strategy_g = _ThursdayEntryGate(strategy)
    engine = MultiMarketEngine(strategy_g, bt_cfg(spreads), max_positions=0)
    engine._progress_every = 10_000

    result = engine.run(
        h1,
        timeframe="1h+1m",
        master_symbol=master,
        same_bar_exit=False,
        start_master_i=prime_i,
        ltf_by_symbol=m1,
    )

    all_trades = [trade_row(t) for t in result.trades]
    # Window attribution: entry in [entry_start, end)
    window = [
        t
        for t in all_trades
        if entry_start <= _ts(t["entry_time"]) < end
    ]
    opened = window
    still_open = [t for t in window if t["status"] == "open"]
    closed = [t for t in window if t["status"] == "closed"]

    # Also opens before window still open at end (carry)
    carry_open = [
        t
        for t in all_trades
        if t["status"] == "open" and _ts(t["entry_time"]) < entry_start
    ]

    pending = pending_from_strategy(engine.strategy)

    net = sum(float(t["pnl"]) for t in closed)
    summary = {
        "frozen": {
            "candidate": "n1_thursday",
            "strategy": "alexg8",
            "ghost_sl_mult": 4.0,
            "peer_blend": 0.0,
            "leverage": 500,
            "rr_factor": 1.0,
            "rr_min": 2.0,
            "rr_max": 5.0,
            "force_flat_daily": True,
            "thursday_gate": True,
            "timeframe": "1h+1m",
            "capital": CAPITAL,
        },
        "window": {
            "entry_start": str(entry_start),
            "end": str(end),
            "hours": HOURS,
            "master": master,
            "pairs_h1": len(h1),
            "pairs_m1": len(m1),
            "mt5_server_offset_seconds": offset,
        },
        "counts": {
            "opened_in_window": len(opened),
            "closed_in_window": len(closed),
            "still_open_from_window": len(still_open),
            "carry_open_from_before": len(carry_open),
            "pending_ghosts": len(pending),
            "all_engine_trades": len(all_trades),
        },
        "pnl": {
            "closed_net": net,
            "open_unrealized_marked_zero_here": 0.0,
            "mdd_pct": float(result.max_drawdown_pct),
            "ending_equity": float(result.final_equity),
        },
    }

    (OUT / "SUMMARY.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (OUT / "OPENED_IN_WINDOW.json").write_text(
        json.dumps(opened, indent=2, default=str), encoding="utf-8"
    )
    (OUT / "STILL_OPEN.json").write_text(
        json.dumps(still_open + carry_open, indent=2, default=str), encoding="utf-8"
    )
    (OUT / "PENDING.json").write_text(
        json.dumps(pending, indent=2, default=str), encoding="utf-8"
    )
    (OUT / "ALL_TRADES.json").write_text(
        json.dumps(all_trades, indent=2, default=str), encoding="utf-8"
    )

    lines = [
        "# Frozen N1 Thursday — last 3h backtest",
        "",
        f"- Window: `{entry_start}` → `{end}` UTC (closed H1)",
        f"- Pairs: H1={len(h1)} M1={len(m1)} master={master}",
        f"- Opened in window: **{len(opened)}**",
        f"- Closed in window: **{len(closed)}** (net PnL **{net:.2f}**)",
        f"- Still open (from window): **{len(still_open)}**",
        f"- Carry open (entered before window): **{len(carry_open)}**",
        f"- Pending ghosts at end: **{len(pending)}**",
        "",
        "## Opened in window",
        "",
        "| Symbol | Side | Entry | Exit | PnL | Reason | Status |",
        "|---|---|---|---|---|---|---|",
    ]
    for t in sorted(opened, key=lambda x: x["entry_time"]):
        lines.append(
            f"| {t['symbol']} | {t['side']} | {t['entry_time']} | {t['exit_time'] or ''} | "
            f"{t['pnl']:.2f} | {t['exit_reason'] or ''} | {t['status']} |"
        )
    if not opened:
        lines.append("| — | — | — | — | — | — | none |")

    lines += ["", "## Still open at end", ""]
    for t in still_open + carry_open:
        lines.append(
            f"- `{t['symbol']}` {t['side']} entry={t['entry_time']} @ {t['entry_price']} "
            f"SL={t['stop_loss']} TP={t['take_profit']}"
        )
    if not still_open and not carry_open:
        lines.append("- none")

    lines += ["", "## Pending", ""]
    if pending:
        for p in pending:
            lines.append(f"- `{p['symbol']}` ({p['kind']}): `{p['raw'][:200]}`")
    else:
        lines.append("- none (or strategy does not expose pending state after run)")

    (OUT / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    print("Wrote", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
