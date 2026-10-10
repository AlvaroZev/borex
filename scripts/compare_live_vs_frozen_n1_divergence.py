#!/usr/bin/env python3
"""Comparative replay: frozen N1 (1h+1m) vs Live-like (H1, ghost_fill_at_close).

Produces structured divergence facts for the last 3h window.
"""

from __future__ import annotations

import json
import sys
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
from borex.models.candle import Candle
from borex_mirror.engine import _ThursdayEntryGate

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
WARMUP_H1 = 500
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
    return sym.replace("=X", "").replace("=x", "")


def load_mt5(symbols: list[str]):
    import MetaTrader5 as mt5
    from borex.data.mt5_time import measure_mt5_server_offset_seconds

    assert mt5.initialize(timeout=90_000)
    offset = int(measure_mt5_server_offset_seconds(mt5) or 0)
    h1, m1, spreads = {}, {}, {}
    for sym in symbols:
        mt5_sym = yahoo_to_mt5(sym)
        info = mt5.symbol_info(mt5_sym)
        if info is None:
            mt5.symbol_select(mt5_sym, True)
            info = mt5.symbol_info(mt5_sym)
        if info is None:
            continue
        mt5.symbol_select(mt5_sym, True)
        point = float(info.point or 0.0)
        dig = int(info.digits or 5)
        pip = point * 10 if dig in (3, 5) else (point if dig in (2, 4) else point * 10)
        spreads[sym] = (float(info.spread or 0) * point / pip) if pip > 0 else 0.0
        rates_h = mt5.copy_rates_from_pos(mt5_sym, mt5.TIMEFRAME_H1, 0, WARMUP_H1 + HOURS + 5)
        if rates_h is None or len(rates_h) < 120:
            continue
        bars = rates_to_candles(rates_h, offset_seconds=offset)
        now = pd.Timestamp.now(tz="UTC")
        if bars and _ts(bars[-1].timestamp).floor("h") == now.floor("h"):
            bars = bars[:-1]
        if len(bars) < 120:
            continue
        h1[sym] = bars
        rates_m = mt5.copy_rates_from_pos(
            mt5_sym, mt5.TIMEFRAME_M1, 0, (HOURS + 6) * 60 + 30
        )
        if rates_m is not None and len(rates_m) > 10:
            mins = rates_to_candles(rates_m, offset_seconds=offset)
            if mins and _ts(mins[-1].timestamp).floor("min") == now.floor("min"):
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
        "exit_time": str(t.exit_time) if t.exit_time is not None else None,
        "exit_price": t.exit_price,
        "exit_reason": t.exit_reason,
        "pnl": float(t.pnl or 0.0),
        "status": "open" if t.is_open else "closed",
        "pattern": str(t.pattern or "")[:180],
    }


def pending_rows(strategy) -> list[dict[str, Any]]:
    rows = []
    pending = getattr(strategy, "_pending", None) or {}
    for sym, p in pending.items():
        rows.append(
            {
                "symbol": sym,
                "action": str(getattr(p, "action", "")),
                "planned_entry": float(getattr(p, "planned_entry", 0) or 0),
                "stop_loss": float(getattr(p, "stop_loss", 0) or 0),
                "take_profit": float(getattr(p, "take_profit", 0) or 0),
                "created_index": getattr(p, "created_index", None),
                "pattern": str(getattr(p, "pattern", ""))[:160],
            }
        )
    return rows


def run_variant(
    name: str,
    h1,
    m1,
    spreads,
    master: str,
    entry_start: pd.Timestamp,
    end: pd.Timestamp,
    *,
    timeframe: str,
    ghost_fill_at_close: bool,
    thursday_gate: bool,
) -> dict[str, Any]:
    data = {s: [c for c in bars if _ts(c.timestamp) < end] for s, bars in h1.items()}
    ltf = {s: [c for c in bars if _ts(c.timestamp) < end] for s, bars in m1.items()}
    entry_i = next(i for i, c in enumerate(data[master]) if _ts(c.timestamp) >= entry_start)
    prime_i = max(int(AlexG8Strategy().min_bars), entry_i - 72)

    strategy = AlexG8Strategy(
        min_rr=2.0,
        execution_interval="1h",
        peer_blend=0.0,
        ghost_sl_mult=4.0,
        ghost_fill_at_close=ghost_fill_at_close,
    )
    if thursday_gate:
        strategy = _ThursdayEntryGate(strategy)
    engine = MultiMarketEngine(strategy, bt_cfg(spreads), max_positions=0)
    engine._progress_every = 100_000
    kwargs = dict(
        timeframe=timeframe,
        master_symbol=master,
        same_bar_exit=False,
        start_master_i=prime_i,
    )
    if timeframe == "1h+1m":
        kwargs["ltf_by_symbol"] = ltf
    result = engine.run(data, **kwargs)
    trades = [trade_row(t) for t in result.trades]
    window = [t for t in trades if entry_start <= _ts(t["entry_time"]) < end]
    pending = pending_rows(engine.strategy)
    return {
        "name": name,
        "timeframe": timeframe,
        "ghost_fill_at_close": ghost_fill_at_close,
        "thursday_gate": thursday_gate,
        "prime_i": prime_i,
        "entry_i": entry_i,
        "n_trades_all": len(trades),
        "n_opened_window": len(window),
        "n_open_end": sum(1 for t in trades if t["status"] == "open"),
        "n_pending": len(pending),
        "window_trades": window,
        "all_trades": trades,
        "pending": pending,
        "ending_equity": float(result.final_equity),
        "mdd_pct": float(result.max_drawdown_pct),
        "strategy_attrs": {
            "ghost_sl_mult": float(
                getattr(getattr(engine.strategy, "_inner", engine.strategy), "ghost_sl_mult", -1)
            ),
            "peer_blend": float(
                getattr(getattr(engine.strategy, "_inner", engine.strategy), "peer_blend", -1)
            ),
            "ghost_fill_at_close": bool(
                getattr(
                    getattr(engine.strategy, "_inner", engine.strategy),
                    "ghost_fill_at_close",
                    False,
                )
            ),
        },
    }


def main() -> int:
    now = pd.Timestamp.now(tz="UTC")
    entry_start = (now - pd.Timedelta(hours=HOURS)).floor("h")
    end = now.floor("h")
    symbols = default_forex_universe()
    if "EURUSD=X" not in symbols:
        symbols = ["EURUSD=X"] + list(symbols)
    h1, m1, spreads, offset = load_mt5(symbols)
    master = pick_master_symbol(h1)
    last_master = _ts(h1[master][-1].timestamp)
    end = min(end, last_master + pd.Timedelta(hours=1))
    print("window", entry_start, end, "offset", offset, "master", master)

    variants = [
        run_variant(
            "FROZEN_N1_1h1m",
            h1,
            m1,
            spreads,
            master,
            entry_start,
            end,
            timeframe="1h+1m",
            ghost_fill_at_close=False,  # classic ghost fill at SL on touch (offline N1 lab)
            thursday_gate=True,
        ),
        run_variant(
            "FROZEN_N1_1h1m_fill_at_close",
            h1,
            m1,
            spreads,
            master,
            entry_start,
            end,
            timeframe="1h+1m",
            ghost_fill_at_close=True,  # alexg8 default / live geometry
            thursday_gate=True,
        ),
        run_variant(
            "LIVE_LIKE_H1_fill_at_close",
            h1,
            m1,
            spreads,
            master,
            entry_start,
            end,
            timeframe="1h",
            ghost_fill_at_close=True,
            thursday_gate=True,
        ),
        run_variant(
            "LIVE_LIKE_H1_fill_at_close_NO_THU_GATE",
            h1,
            m1,
            spreads,
            master,
            entry_start,
            end,
            timeframe="1h",
            ghost_fill_at_close=True,
            thursday_gate=False,
        ),
    ]

    # Live snapshot bits
    live = {}
    live_path = OUT / "LIVE_DASHBOARD_SNAPSHOT.json"
    if live_path.exists():
        snap = json.loads(live_path.read_text(encoding="utf-8"))
        d = snap.get("dashboard") or {}
        live = {
            "status": (snap.get("status") or {}).get("status"),
            "ghost_entry": (snap.get("status") or {}).get("ghost_entry"),
            "entry_mode": d.get("entry_mode") or (snap.get("status") or {}).get("entry_mode"),
            "open_trades": len(d.get("open_trades") or []),
            "pending_ghosts": len(d.get("pending_ghosts") or []),
            "pending_orders_mt5": len(d.get("pending_orders_mt5") or []),
            "theory_open": len((d.get("theory") or {}).get("open_trades") or []),
            "theory_closed": len((d.get("theory") or {}).get("closed_trades") or []),
            "closed_trades_db": len(d.get("closed_trades") or []),
            "live_candle_count": d.get("live_candle_count"),
        }

    mt5_state = {}
    if (OUT / "MT5_STATE.json").exists():
        mt5_state = json.loads((OUT / "MT5_STATE.json").read_text(encoding="utf-8"))

    comp = {
        "generated_at_utc": str(now),
        "window": {
            "entry_start": str(entry_start),
            "end": str(end),
            "mt5_server_offset_seconds": offset,
            "master": master,
            "pairs_h1": len(h1),
            "pairs_m1": len(m1),
        },
        "live": live,
        "mt5": {
            "login": mt5_state.get("login"),
            "balance": mt5_state.get("balance"),
            "n_positions": mt5_state.get("n_positions"),
            "n_orders": mt5_state.get("n_orders"),
            "deals_24h": mt5_state.get("deals_24h"),
        },
        "variants": [
            {
                k: v
                for k, v in var.items()
                if k not in ("all_trades",)  # keep window+pending; dump all separately
            }
            | {"all_trades": var["all_trades"]}
            for var in variants
        ],
    }
    (OUT / "DIVERGENCE_COMPARISON.json").write_text(
        json.dumps(comp, indent=2, default=str), encoding="utf-8"
    )

    # Compact table
    print("\nVARIANT COMPARISON")
    for v in variants:
        print(
            f"{v['name']}: window_opens={v['n_opened_window']} "
            f"open_end={v['n_open_end']} pending={v['n_pending']} "
            f"all={v['n_trades_all']} fill_at_close={v['ghost_fill_at_close']} tf={v['timeframe']}"
        )
        for p in v["pending"]:
            print(f"  PENDING {p['symbol']} {p['action']} entry={p['planned_entry']} sl={p['stop_loss']}")
        for t in v["window_trades"]:
            print(f"  TRADE {t}")

    print("LIVE", live)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
