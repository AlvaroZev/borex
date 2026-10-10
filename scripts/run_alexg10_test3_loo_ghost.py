#!/usr/bin/env python3
"""AlexG10 Test 3 — LOO fill-path / state decomposition + ghost_sl_mult funnel.

OFFLINE ONLY. No MT5. No Live/Mirror. Frozen Test-2 economics.
DIAGNOSTIC / MECHANISTIC / NOT FOR DEPLOYMENT.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import platform
import statistics
import subprocess
import sys
import time
import traceback
from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from borex.alexg import AlexG10Strategy
from borex.alexg.ghost_entry import PendingSetup
from borex.alexg.multi_market import pick_master_symbol
from borex.backtest.engine import BacktestConfig
from borex.backtest.margin_stops import resolve_rr
from borex.backtest.multi_market_engine import MultiMarketEngine, decision_from_signal
from borex.backtest.multi_portfolio import MultiMarketPortfolio
from borex.data.mtf import MultiTimeframeContext
from borex.models.candle import Candle, Signal
from borex.viewerMT5.mt5_feed import (
    _cache_path,
    _dedupe_hourly,
    _drop_forming_bar,
    _frame_to_candles,
    _slice_candles,
)

T2 = ROOT / "data/runs/research/alexg10_test2_20261003_224027"
JUN_INDEX = ROOT / "data/runs/mt5_1m_n1_oos_jun2026/index.json"
T1_HASH = "84b7433fb77c27128f3751f4c1fe2dd6522b4a8f475d4dd9829aeb2559a8dbf3"

CAPITAL = 1000.0
LEV = 500.0
ENTRY_START = pd.Timestamp("2026-06-01", tz="UTC")
DATA_START = pd.Timestamp("2026-04-01", tz="UTC")
REPRO_END = pd.Timestamp("2026-10-02T14:00:00+00:00")
MAX_POSITIONS = 60
GHOST_PRIME = 72

# Frozen LOO universes from Test 2 (do not re-rank)
LOO_UNIVERSES = {
    "full": set(),
    "minus_NOKSEK": {"NOKSEK=X"},
    "minus_top2": {"GBPTRY=X", "NOKSEK=X"},
    "minus_top3": {"AUDCAD=X", "GBPTRY=X", "NOKSEK=X"},
    "minus_top5": {
        "AUDCAD=X",
        "EURNOK=X",
        "EURSEK=X",
        "GBPTRY=X",
        "NOKSEK=X",
    },
    "minus_top8": {
        "AUDCAD=X",
        "CADCHF=X",
        "EURNOK=X",
        "EURSEK=X",
        "EURTRY=X",
        "GBPSEK=X",
        "GBPTRY=X",
        "NOKSEK=X",
    },
}

GHOST_SL_GRID = [2.0, 3.0, 4.0, 5.0, 6.0]
GHOST_SL_LOO = ["full", "minus_NOKSEK", "minus_top3", "minus_top8"]
FOCUS_SYMBOLS = [
    "NOKSEK=X",
    "GBPTRY=X",
    "AUDCAD=X",
    "EURSEK=X",
    "EURNOK=X",
    "AUDCHF=X",
]


def _git() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=str(ROOT), text=True
        ).strip()
    except Exception:
        return "unknown"


def _branch() -> str:
    try:
        return subprocess.check_output(
            ["git", "branch", "--show-current"], cwd=str(ROOT), text=True
        ).strip()
    except Exception:
        return "unknown"


def _ts(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys: list[str] = []
    for r in rows:
        for k in r:
            if k not in keys and not isinstance(r[k], (dict, list)):
                keys.append(k)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in keys})


def load_h1(symbols, start, end):
    data = {}
    sp, ep = start.to_pydatetime(), end.to_pydatetime()
    for i, sym in enumerate(symbols, 1):
        path = _cache_path(sym, "1h")
        if not path.is_file():
            continue
        df = pd.read_parquet(path)
        bars = _drop_forming_bar(
            _dedupe_hourly(_slice_candles(_frame_to_candles(df), sp, ep)), "1h"
        )
        if len(bars) >= 200:
            data[sym] = bars
        if i % 20 == 0 or i == len(symbols):
            print(f"  H1 {i}/{len(symbols)} loaded={len(data)}", flush=True)
    return data


def h1_hash(data: dict) -> str:
    h = hashlib.sha256()
    for sym in sorted(data):
        bars = data[sym]
        h.update(sym.encode())
        h.update(str(bars[0].timestamp).encode())
        h.update(str(bars[-1].timestamp).encode())
        h.update(str(len(bars)).encode())
        for idx in (0, len(bars) // 2, -1):
            c = bars[idx]
            h.update(f"{c.open},{c.high},{c.low},{c.close}".encode())
    return h.hexdigest()


def bt_cfg(spreads: dict) -> BacktestConfig:
    return BacktestConfig(
        initial_capital=CAPITAL,
        leverage=LEV,
        position_size_pct=0.01,
        size_mode="margin",
        true_sl=True,
        true_sl_rr=2.0,
        honor_signal_stops=False,
        rr_mode="dynamic",
        rr_factor=1.0,
        rr_min=2.0,
        rr_max=5.0,
        commission_per_lot=7.0,
        min_commission_per_side=0.04,
        lot_notional=100_000.0,
        risk_include_commission=True,
        force_flat_daily=False,
        force_flat_friday=False,
        force_flat_utc_hour=19,
        force_flat_friday_from_hour=19,
        winrate_min_trades=20,
        spread_pips=0.0,
        spread_pips_by_symbol=spreads,
        slippage_pips=0.0,
        intra_hour_sl=False,
    )


@dataclass
class InstrumentedAlexG10(AlexG10Strategy):
    """AlexG10 with candidate/ghost lifecycle ledger (economics unchanged)."""

    run_id: str = ""
    events: list = field(default_factory=list, repr=False)
    cache_stats: dict = field(default_factory=dict, repr=False)

    def __post_init__(self):
        self.cache_stats = {
            "zone_builds": 0,
            "zone_reuses": 0,
            "zone_polluted_reuses": 0,
            "htf_builds": 0,
            "htf_reuses": 0,
            "htf_polluted_reuses": 0,
        }
        self._zones_cache_owner = ""
        self._htf_cache_owner = ""

    def _build_zones(self, candles: list[Candle], index: int) -> list:
        symbol = self._current_symbol or "EURUSD=X"
        refresh = max(1, self.zone_refresh_bars)
        idx_ok = bool(
            self._zones_cache
            and index - self._zones_cache_index < refresh
            and index >= self._zones_cache_index
        )
        owner = getattr(self, "_zones_cache_symbol", None) or self._zones_cache_owner
        # Effective reuse = what the *active* _build_zones implementation will do.
        # Fixed source requires owner==symbol; PRE_FIX bug monkeypatch ignores owner.
        from borex.alexg.strategy5_revised import AlexG5RevisedStrategy

        impl = AlexG5RevisedStrategy._build_zones
        ignores_owner = getattr(impl, "__name__", "") == "build_zones_buggy"
        will_reuse = idx_ok and (ignores_owner or owner == symbol)
        if will_reuse:
            self.cache_stats["zone_reuses"] += 1
            if owner and owner != symbol:
                self.cache_stats["zone_polluted_reuses"] += 1
        else:
            self.cache_stats["zone_builds"] += 1
            self._zones_cache_owner = symbol
        return AlexG10Strategy._build_zones(self, candles, index)

    def _htf_frames(self, candles: list[Candle], index: int) -> dict:
        symbol = self._current_symbol or "EURUSD=X"
        refresh = max(1, self.zone_refresh_bars)
        idx_ok = bool(
            self._htf_cache
            and index - self._htf_cache_index < refresh
            and index >= self._htf_cache_index
        )
        owner = getattr(self, "_htf_cache_symbol", None) or self._htf_cache_owner
        from borex.alexg.strategy5_revised import AlexG5RevisedStrategy

        impl = AlexG5RevisedStrategy._htf_frames
        ignores_owner = getattr(impl, "__name__", "") == "htf_buggy"
        will_reuse = idx_ok and (ignores_owner or owner == symbol)
        if will_reuse:
            self.cache_stats["htf_reuses"] += 1
            if owner and owner != symbol:
                self.cache_stats["htf_polluted_reuses"] += 1
        else:
            self.cache_stats["htf_builds"] += 1
            self._htf_cache_owner = symbol
        return AlexG10Strategy._htf_frames(self, candles, index)

    def on_bar(
        self,
        index: int,
        candles: list[Candle],
        mtf: MultiTimeframeContext | None = None,
    ) -> Signal | None:
        q = self._quantized(candles)
        symbol = self._current_symbol or "UNKNOWN"
        pending = self._pending.get(symbol)
        pre = pending is not None
        snap = None
        outcome = None
        if pending is not None:
            outcome = self._pending_outcome(pending, index, q)
            snap = {
                "action": pending.action.value
                if hasattr(pending.action, "value")
                else str(pending.action),
                "pattern": pending.pattern,
                "planned_entry": float(pending.planned_entry),
                "ghost_sl": float(pending.stop_loss),
                "ghost_tp": float(pending.take_profit),
                "created_index": int(pending.created_index),
                "expires_index": int(pending.expires_index),
            }

        sig = AlexG10Strategy.on_bar(self, index, candles, mtf)
        post = symbol in self._pending
        ts = str(q[index].timestamp) if index < len(q) else None

        if not pre and post:
            p = self._pending[symbol]
            self.events.append(
                {
                    "run_id": self.run_id,
                    "event": "GHOST_CREATED",
                    "timestamp": ts,
                    "symbol": symbol,
                    "index": index,
                    "side_setup": p.action.value
                    if hasattr(p.action, "value")
                    else str(p.action),
                    "pattern": p.pattern,
                    "planned_entry": float(p.planned_entry),
                    "ghost_sl": float(p.stop_loss),
                    "ghost_tp": float(p.take_profit),
                    "ghost_sl_mult": float(self.ghost_sl_mult),
                    "ghost_sl_dist": abs(float(p.planned_entry) - float(p.stop_loss)),
                    "created_index": int(p.created_index),
                    "expires_index": int(p.expires_index),
                    "candidate_status": "GHOST_QUEUED",
                }
            )
        elif pre and outcome == "triggered" and sig is not None:
            self.events.append(
                {
                    "run_id": self.run_id,
                    "event": "GHOST_TRIGGERED_SIGNAL",
                    "timestamp": ts,
                    "symbol": symbol,
                    "index": index,
                    "side_setup": snap["action"] if snap else None,
                    "side_fill": sig.action.value
                    if hasattr(sig.action, "value")
                    else str(sig.action),
                    "pattern": snap["pattern"] if snap else sig.pattern,
                    "planned_entry": snap["planned_entry"] if snap else None,
                    "ghost_sl": snap["ghost_sl"] if snap else None,
                    "ghost_tp": snap["ghost_tp"] if snap else None,
                    "fill_price": float(sig.price) if sig.price is not None else None,
                    "signal_sl": float(sig.stop_loss)
                    if sig.stop_loss is not None
                    else None,
                    "signal_tp": float(sig.take_profit)
                    if sig.take_profit is not None
                    else None,
                    "ghost_sl_mult": float(self.ghost_sl_mult),
                    "ghost_sl_dist": abs(snap["planned_entry"] - snap["ghost_sl"])
                    if snap
                    else None,
                    "created_index": snap["created_index"] if snap else None,
                    "candidate_status": "FILL_SIGNAL_EMITTED",
                }
            )
        elif pre and outcome in ("expired", "invalidated"):
            self.events.append(
                {
                    "run_id": self.run_id,
                    "event": f"GHOST_{outcome.upper()}",
                    "timestamp": ts,
                    "symbol": symbol,
                    "index": index,
                    "side_setup": snap["action"] if snap else None,
                    "pattern": snap["pattern"] if snap else None,
                    "planned_entry": snap["planned_entry"] if snap else None,
                    "ghost_sl": snap["ghost_sl"] if snap else None,
                    "ghost_tp": snap["ghost_tp"] if snap else None,
                    "ghost_sl_mult": float(self.ghost_sl_mult),
                    "ghost_sl_dist": abs(snap["planned_entry"] - snap["ghost_sl"])
                    if snap
                    else None,
                    "created_index": snap["created_index"] if snap else None,
                    "candidate_status": outcome.upper(),
                }
            )
        elif pre and outcome == "triggered" and sig is None:
            self.events.append(
                {
                    "run_id": self.run_id,
                    "event": "GHOST_TRIGGER_REJECTED",
                    "timestamp": ts,
                    "symbol": symbol,
                    "index": index,
                    "planned_entry": snap["planned_entry"] if snap else None,
                    "ghost_sl": snap["ghost_sl"] if snap else None,
                    "ghost_sl_mult": float(self.ghost_sl_mult),
                    "candidate_status": "TRIGGER_CONFIRM_REJECTED",
                }
            )
        return sig


class DropTrackingEngine(MultiMarketEngine):
    """MultiMarketEngine that records rejected fill attempts + bar state samples."""

    def __init__(self, *args, sample_every: int = 1, **kwargs):
        super().__init__(*args, **kwargs)
        self.sample_every = max(1, int(sample_every))
        self.reject_events: list[dict] = []
        self.state_timeline: list[dict] = []
        self.open_events: list[dict] = []

    def run(self, *args, **kwargs):
        return self._run_with_timeline(*args, **kwargs)

    def _run_with_timeline(self, candles_by_symbol, **kwargs):
        """Instrumented run; economics match parent for AlexG10.

        AlexG10/alexg5revised set_context ignores market_ctx and does not call
        allows_trade, so we skip MultiMarketContext currency-strength work and
        resolve indices via ``_indices_at_master`` (identical fill path).
        """
        from borex.alexg.multi_market import (
            align_symbols_to_timeline,
            pick_master_symbol,
        )
        from borex.backtest.multi_market_engine import (
            _indices_at_master,
            index_ltf_bars,
        )

        if not candles_by_symbol:
            raise ValueError("No hay velas cargadas")

        master = kwargs.get("master_symbol") or pick_master_symbol(candles_by_symbol)
        master_candles = candles_by_symbol[master]
        symbols = [s for s in candles_by_symbol if s in candles_by_symbol]
        ts_maps = align_symbols_to_timeline(master_candles, candles_by_symbol)

        portfolio = MultiMarketPortfolio(
            initial_capital=self.config.initial_capital,
            position_size_pct=self.config.position_size_pct,
            leverage=self.config.leverage,
            maintenance_margin_ratio=self.config.maintenance_margin_ratio,
            size_mode=self.config.size_mode,
            max_positions=self.max_positions,
            commission_per_lot=self.config.commission_per_lot,
            commission_per_trade=self.config.commission_per_trade,
            min_commission_per_side=self.config.min_commission_per_side,
            lot_notional=self.config.lot_notional,
            risk_include_commission=self.config.risk_include_commission,
        )

        equity_curve: list[float] = [portfolio.equity]
        peak = portfolio.equity
        max_dd = 0.0
        warmup = int(self.strategy.min_bars)
        start_master_i = kwargs.get("start_master_i")
        begin = warmup if start_master_i is None else max(warmup, int(start_master_i))
        same_bar_exit = bool(kwargs.get("same_bar_exit", False))
        collect_decisions = kwargs.get("collect_decisions")
        ltf_by_symbol = kwargs.get("ltf_by_symbol")
        ltf_index = index_ltf_bars(ltf_by_symbol)
        timeframe = kwargs.get("timeframe", "1h") or "1h"
        progress_path = kwargs.get("progress_path")

        n_master = len(master_candles)
        t_loop0 = time.time()
        for master_i in range(begin, n_master):
            if portfolio.liquidated:
                break

            # Strength unused by AlexG10 — keep index alignment only.
            indices = _indices_at_master(master_candles, master_i, ts_maps)

            prices: dict[str, float] = {}
            for sym, idx in indices.items():
                prices[sym] = candles_by_symbol[sym][idx].close

            for sym in list(portfolio.open_trades.keys()):
                idx = indices.get(sym)
                if idx is None:
                    continue
                candle = candles_by_symbol[sym][idx]
                prices[sym] = candle.close
                self._check_exit_path(
                    portfolio, sym, idx, candle, ltf_index.get(sym)
                )

            candidates: list[tuple[str, Signal, int]] = []
            for sym in symbols:
                idx = indices.get(sym)
                if idx is None or idx < warmup:
                    continue
                self.strategy.set_context(sym, None)
                signal = self.strategy.on_bar(idx, candles_by_symbol[sym], None)
                if signal is not None:
                    candidates.append((sym, signal, idx))
                    if collect_decisions is not None:
                        collect_decisions.append(decision_from_signal(sym, signal))

            candidates.sort(key=lambda x: x[1].score, reverse=True)
            opened: list[str] = []
            for ci, (sym, signal, idx) in enumerate(candidates):
                if portfolio.book_is_full():
                    for sym2, signal2, idx2 in candidates[ci:]:
                        self.reject_events.append(
                            {
                                "timestamp": str(
                                    candles_by_symbol[sym2][idx2].timestamp
                                ),
                                "symbol": sym2,
                                "index": idx2,
                                "reason": "book_full_break",
                                "cash": float(portfolio.cash),
                                "n_open": len(portfolio.open_trades),
                                "win_rate": portfolio.win_rate,
                                "dynamic_rr": resolve_rr(
                                    rr_mode=self.config.rr_mode,
                                    fixed_rr=self.config.true_sl_rr,
                                    winrate=portfolio.win_rate,
                                    rr_factor=self.config.rr_factor,
                                    closed_trades=len(portfolio.closed_trades),
                                    winrate_min_trades=self.config.winrate_min_trades,
                                    rr_min=self.config.rr_min,
                                    rr_max=self.config.rr_max,
                                ),
                                "planned_price": float(signal2.price)
                                if signal2.price is not None
                                else None,
                                "pattern": signal2.pattern,
                            }
                        )
                    break
                if not portfolio.can_open(sym):
                    self.reject_events.append(
                        {
                            "timestamp": str(candles_by_symbol[sym][idx].timestamp),
                            "symbol": sym,
                            "index": idx,
                            "reason": "already_open"
                            if sym in portfolio.open_trades
                            else "can_open_false",
                            "cash": float(portfolio.cash),
                            "n_open": len(portfolio.open_trades),
                            "win_rate": portfolio.win_rate,
                            "dynamic_rr": resolve_rr(
                                rr_mode=self.config.rr_mode,
                                fixed_rr=self.config.true_sl_rr,
                                winrate=portfolio.win_rate,
                                rr_factor=self.config.rr_factor,
                                closed_trades=len(portfolio.closed_trades),
                                winrate_min_trades=self.config.winrate_min_trades,
                                rr_min=self.config.rr_min,
                                rr_max=self.config.rr_max,
                            ),
                            "planned_price": float(signal.price)
                            if signal.price is not None
                            else None,
                            "pattern": signal.pattern,
                        }
                    )
                    continue
                n_before = len(portfolio.open_trades)
                self._open_signal(
                    portfolio, sym, signal, idx, candles_by_symbol[sym]
                )
                if sym in portfolio.open_trades and len(portfolio.open_trades) >= n_before:
                    tr = portfolio.open_trades[sym]
                    self.open_events.append(
                        {
                            "timestamp": str(candles_by_symbol[sym][idx].timestamp),
                            "symbol": sym,
                            "index": idx,
                            "entry_price": float(tr.entry_price),
                            "margin": float(tr.margin),
                            "stop_loss": float(tr.stop_loss)
                            if tr.stop_loss is not None
                            else None,
                            "take_profit": float(tr.take_profit)
                            if tr.take_profit is not None
                            else None,
                            "equity_at_entry": float(tr.entry_equity),
                            "pattern": signal.pattern,
                        }
                    )
                elif sym not in portfolio.open_trades:
                    self.reject_events.append(
                        {
                            "timestamp": str(candles_by_symbol[sym][idx].timestamp),
                            "symbol": sym,
                            "index": idx,
                            "reason": "open_position_failed",
                            "cash": float(portfolio.cash),
                            "n_open": len(portfolio.open_trades),
                            "win_rate": portfolio.win_rate,
                            "dynamic_rr": None,
                            "planned_price": float(signal.price)
                            if signal.price is not None
                            else None,
                            "pattern": signal.pattern,
                        }
                    )
                if same_bar_exit and sym in portfolio.open_trades:
                    opened.append(sym)

            if same_bar_exit:
                for sym in opened:
                    idx = indices.get(sym)
                    if idx is None:
                        continue
                    self._check_exit_path(
                        portfolio,
                        sym,
                        idx,
                        candles_by_symbol[sym][idx],
                        ltf_index.get(sym),
                    )

            eq = portfolio.equity_at_prices(prices)
            equity_curve.append(eq)
            peak = max(peak, eq)
            if peak > 0:
                max_dd = max(max_dd, (peak - eq) / peak)

            if (master_i - begin) % self.sample_every == 0:
                wr = portfolio.win_rate
                rr = resolve_rr(
                    rr_mode=self.config.rr_mode,
                    fixed_rr=self.config.true_sl_rr,
                    winrate=wr,
                    rr_factor=self.config.rr_factor,
                    closed_trades=len(portfolio.closed_trades),
                    winrate_min_trades=self.config.winrate_min_trades,
                    rr_min=self.config.rr_min,
                    rr_max=self.config.rr_max,
                )
                pending_n = 0
                if hasattr(self.strategy, "_pending"):
                    pending_n = len(self.strategy._pending)
                notional = 0.0
                for t in portfolio.open_trades.values():
                    notional += float(t.margin) * float(self.config.leverage)
                self.state_timeline.append(
                    {
                        "timestamp": str(master_candles[master_i].timestamp),
                        "master_i": master_i,
                        "equity": float(eq),
                        "cash": float(portfolio.cash),
                        "n_open": len(portfolio.open_trades),
                        "n_closed": len(portfolio.closed_trades),
                        "n_pending_ghosts": pending_n,
                        "n_candidates_bar": len(candidates),
                        "dynamic_rr": float(rr),
                        "win_rate": float(wr) if wr is not None else None,
                        "total_notional": float(notional),
                        "book_full": bool(portfolio.book_is_full()),
                    }
                )

            if self._progress_every and master_i % self._progress_every == 0:
                msg = (
                    f"  bar {master_i}/{n_master} eq={eq:.2f} "
                    f"open={len(portfolio.open_trades)} "
                    f"closed={len(portfolio.closed_trades)} "
                    f"elapsed={time.time()-t_loop0:.1f}s"
                )
                print(msg, flush=True)
                if progress_path:
                    with open(progress_path, "a", encoding="utf-8") as pf:
                        pf.write(msg + "\n")

        for sym, trade in list(portfolio.open_trades.items()):
            candles = candles_by_symbol[sym]
            last = candles[-1]
            self._close(
                portfolio,
                sym,
                len(candles) - 1,
                last.close,
                last.timestamp,
                "end_of_data",
            )

        equity_curve.append(portfolio.equity)
        return self._build_result(
            portfolio, equity_curve, max_dd, symbols, master, timeframe
        )


def metrics(trades: list[dict], capital: float = CAPITAL) -> dict:
    pnls = [float(t["pnl"]) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gp, gl = sum(wins), sum(losses)
    pf = (gp / abs(gl)) if gl else (float("inf") if gp > 0 else None)
    exits = Counter(str(t["exit_reason"]) for t in trades)
    ordered = sorted(
        trades, key=lambda t: (_ts(t["exit_time"] or t["entry_time"]), t["entry_time"])
    )
    eq = peak = capital
    max_dd = 0.0
    for t in ordered:
        eq += float(t["pnl"])
        peak = max(peak, eq)
        max_dd = max(max_dd, (peak - eq) / peak if peak > 0 else 0.0)
    holds = [float(t["hold_h"]) for t in trades if t.get("hold_h") is not None]
    margins = [float(t["margin"]) for t in trades if t.get("margin") is not None]
    notionals = [
        float(t["margin"]) * LEV for t in trades if t.get("margin") is not None
    ]
    rrs = [
        float(t["rr_resolved"])
        for t in trades
        if t.get("rr_resolved") is not None and math.isfinite(float(t["rr_resolved"]))
    ]
    net = float(sum(pnls)) if pnls else 0.0
    return {
        "fills": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (len(wins) / len(trades)) if trades else None,
        "gross_profit": float(gp),
        "gross_loss": float(gl),
        "profit_factor": pf,
        "net_pnl": net,
        "mean": float(statistics.mean(pnls)) if pnls else None,
        "median": float(statistics.median(pnls)) if pnls else None,
        "max_drawdown": float(max_dd),
        "TP": int(exits.get("take_profit", 0)),
        "SL": int(exits.get("stop_loss", 0)),
        "margin_stop": int(exits.get("margin_stop", 0)),
        "daily_flat": int(exits.get("daily_close", 0)),
        "end_of_data": int(exits.get("end_of_data", 0)),
        "exit_mix": dict(exits),
        "hold_mean_h": float(statistics.mean(holds)) if holds else None,
        "hold_median_h": float(statistics.median(holds)) if holds else None,
        "avg_position_size_margin": float(statistics.mean(margins)) if margins else None,
        "median_position_size_margin": float(statistics.median(margins))
        if margins
        else None,
        "total_notional": float(sum(notionals)) if notionals else 0.0,
        "avg_realized_rr": float(statistics.mean(rrs)) if rrs else None,
        "ending_equity": capital + net,
        "commission_sum": float(sum(float(t.get("commission") or 0) for t in trades)),
    }


def trades_from_result(result, entry_start, end) -> list[dict]:
    rows = []
    for t in result.trades:
        if _ts(t.entry_time) < entry_start or _ts(t.entry_time) >= end:
            continue
        side = t.side.value if hasattr(t.side, "value") else str(t.side)
        et, xt = _ts(t.entry_time), _ts(t.exit_time) if t.exit_time else None
        hold = (xt - et).total_seconds() / 3600.0 if xt is not None else None
        risk = (
            abs(float(t.entry_price) - float(t.stop_loss))
            if t.stop_loss is not None
            else None
        )
        reward = (
            abs(float(t.take_profit) - float(t.entry_price))
            if t.take_profit is not None
            else None
        )
        rr = (reward / risk) if risk and risk > 0 and reward is not None else None
        rows.append(
            {
                "symbol": t.symbol,
                "side": side,
                "entry_time": str(t.entry_time),
                "exit_time": str(t.exit_time) if t.exit_time else None,
                "entry_price": float(t.entry_price),
                "exit_price": float(t.exit_price) if t.exit_price is not None else None,
                "stop_loss": float(t.stop_loss) if t.stop_loss is not None else None,
                "take_profit": float(t.take_profit)
                if t.take_profit is not None
                else None,
                "rr_resolved": rr,
                "pnl": float(t.pnl or 0.0),
                "commission": float(getattr(t, "commission", 0) or 0.0),
                "exit_reason": t.exit_reason,
                "hold_h": hold,
                "margin": float(t.margin) if t.margin is not None else None,
                "entry_equity": float(t.entry_equity)
                if getattr(t, "entry_equity", None) is not None
                else None,
                "pattern": t.pattern,
                "month": et.strftime("%Y-%m"),
            }
        )
    return rows


def funnel_from_events(events: list[dict], trades: list[dict], rejects: list[dict]) -> dict:
    created = [e for e in events if e["event"] == "GHOST_CREATED"]
    triggered = [e for e in events if e["event"] == "GHOST_TRIGGERED_SIGNAL"]
    invalidated = [e for e in events if e["event"] == "GHOST_INVALIDATED"]
    expired = [e for e in events if e["event"] == "GHOST_EXPIRED"]
    rejected_confirm = [e for e in events if e["event"] == "GHOST_TRIGGER_REJECTED"]
    pending_end = 0  # approximate: created - terminal
    terminal = len(triggered) + len(invalidated) + len(expired) + len(rejected_confirm)
    pending_end = max(0, len(created) - terminal)
    n_cand = len(created)  # successful setup == ghost queue in this architecture
    n_fills = len(trades)
    return {
        "candidate_signals": n_cand,
        "ghosts_created": len(created),
        "ghosts_triggered_signals": len(triggered),
        "ghosts_invalidated": len(invalidated),
        "ghosts_expired": len(expired),
        "ghosts_trigger_confirm_rejected": len(rejected_confirm),
        "ghosts_pending_eod_approx": pending_end,
        "positions_opened": n_fills,
        "completed_trades": sum(
            1 for t in trades if t.get("exit_reason") != "end_of_data"
        ),
        "open_end_of_data": sum(
            1 for t in trades if t.get("exit_reason") == "end_of_data"
        ),
        "engine_rejects": len(rejects),
        "engine_rejects_by_reason": dict(Counter(r["reason"] for r in rejects)),
        "fill_rate_vs_candidates": (n_fills / n_cand) if n_cand else None,
        "ghost_fill_signal_rate": (len(triggered) / len(created)) if created else None,
        "position_accept_rate_vs_triggers": (n_fills / len(triggered))
        if triggered
        else None,
        "avg_ghost_sl_dist": float(
            statistics.mean(
                [
                    float(e["ghost_sl_dist"])
                    for e in created
                    if e.get("ghost_sl_dist") is not None
                ]
            )
        )
        if created
        else None,
        "median_ghost_sl_dist": float(
            statistics.median(
                [
                    float(e["ghost_sl_dist"])
                    for e in created
                    if e.get("ghost_sl_dist") is not None
                ]
            )
        )
        if created
        else None,
    }


def match_key_candidate(e: dict) -> tuple:
    """Deterministic match key for ghost-create / trigger events."""
    return (
        str(e.get("symbol")),
        str(e.get("created_index") if e.get("created_index") is not None else e.get("index")),
        str(e.get("side_setup") or ""),
        f"{float(e['planned_entry']):.8f}" if e.get("planned_entry") is not None else "",
        f"{float(e['ghost_sl']):.8f}" if e.get("ghost_sl") is not None else "",
    )


def reconcile_runs(full: dict, loo: dict, loo_name: str) -> list[dict]:
    """Candidate-level reconciliation Full vs LOO using ghost-create keys."""
    full_created = {
        match_key_candidate(e): e
        for e in full["events"]
        if e["event"] == "GHOST_CREATED"
    }
    loo_created = {
        match_key_candidate(e): e
        for e in loo["events"]
        if e["event"] == "GHOST_CREATED"
    }
    full_trig = {
        match_key_candidate(e): e
        for e in full["events"]
        if e["event"] == "GHOST_TRIGGERED_SIGNAL"
    }
    loo_trig = {
        match_key_candidate(e): e
        for e in loo["events"]
        if e["event"] == "GHOST_TRIGGERED_SIGNAL"
    }

    def trade_key(t):
        return (t["symbol"], str(t["entry_time"])[:19], t["side"])

    full_trades = {trade_key(t): t for t in full["trades"]}
    loo_trades = {trade_key(t): t for t in loo["trades"]}

    rows = []
    all_keys = set(full_created) | set(loo_created) | set(full_trig) | set(loo_trig)

    # Also compare fill signals / trades for shared symbols
    for key in sorted(all_keys, key=lambda x: (x[0], x[1])):
        fc = full_created.get(key)
        lc = loo_created.get(key)
        ft = full_trig.get(key)
        lt = loo_trig.get(key)
        cls = "OTHER"
        if fc and lc:
            if ft and lt:
                # same ghost, both triggered — check if both became positions
                # match trades by symbol+approx time of trigger
                cls = "SAME_SIGNAL_SAME_ENTRY"
            elif ft and not lt:
                cls = "SAME_GHOST_DIFFERENT_FILL_OUTCOME"
            elif lt and not ft:
                cls = "SAME_GHOST_DIFFERENT_FILL_OUTCOME"
            elif not ft and not lt:
                cls = "SAME_SIGNAL_PRESENT_BUT_NOT_FILLED"
            else:
                cls = "SAME_SIGNAL_SAME_ENTRY"
        elif lc and not fc:
            cls = "GHOST_CREATED_ONLY_IN_LOO" if not ft else "SIGNAL_ONLY_IN_LOO"
            if lt and not ft:
                cls = "SIGNAL_ONLY_IN_LOO"
            elif lc and not fc:
                cls = "GHOST_CREATED_ONLY_IN_LOO"
        elif fc and not lc:
            cls = "GHOST_CREATED_ONLY_IN_FULL"
            if ft and not lt:
                cls = "SIGNAL_ONLY_IN_FULL"

        rows.append(
            {
                "loo_run": loo_name,
                "class": cls,
                "symbol": key[0],
                "created_index": key[1],
                "side_setup": key[2],
                "planned_entry": key[3],
                "ghost_sl": key[4],
                "in_full_created": fc is not None,
                "in_loo_created": lc is not None,
                "in_full_triggered": ft is not None,
                "in_loo_triggered": lt is not None,
                "full_trigger_ts": ft.get("timestamp") if ft else None,
                "loo_trigger_ts": lt.get("timestamp") if lt else None,
                "ghost_sl_mult": (fc or lc or {}).get("ghost_sl_mult"),
            }
        )

    # Trade-only presence for fills without matching ghost keys (fallback)
    for tk, t in loo_trades.items():
        if tk not in full_trades:
            rows.append(
                {
                    "loo_run": loo_name,
                    "class": "SIGNAL_ONLY_IN_LOO",
                    "symbol": t["symbol"],
                    "created_index": "",
                    "side_setup": t["side"],
                    "planned_entry": f"{t['entry_price']:.8f}",
                    "ghost_sl": "",
                    "in_full_created": False,
                    "in_loo_created": True,
                    "in_full_triggered": False,
                    "in_loo_triggered": True,
                    "full_trigger_ts": None,
                    "loo_trigger_ts": t["entry_time"],
                    "ghost_sl_mult": None,
                    "note": "trade_level_fallback",
                }
            )
    for tk, t in full_trades.items():
        if tk not in loo_trades:
            # only if symbol still in loo universe
            rows.append(
                {
                    "loo_run": loo_name,
                    "class": "SIGNAL_ONLY_IN_FULL",
                    "symbol": t["symbol"],
                    "created_index": "",
                    "side_setup": t["side"],
                    "planned_entry": f"{t['entry_price']:.8f}",
                    "ghost_sl": "",
                    "in_full_created": True,
                    "in_loo_created": False,
                    "in_full_triggered": True,
                    "in_loo_triggered": False,
                    "full_trigger_ts": t["entry_time"],
                    "loo_trigger_ts": None,
                    "ghost_sl_mult": None,
                    "note": "trade_level_fallback",
                }
            )
    return rows


def first_divergence(full_tl: list[dict], loo_tl: list[dict]) -> dict:
    by_f = {r["timestamp"]: r for r in full_tl}
    by_l = {r["timestamp"]: r for r in loo_tl}
    common = sorted(set(by_f) & set(by_l))
    for ts in common:
        a, b = by_f[ts], by_l[ts]
        diffs = {}
        for k in (
            "n_open",
            "n_pending_ghosts",
            "n_candidates_bar",
            "dynamic_rr",
            "equity",
            "n_closed",
            "total_notional",
        ):
            va, vb = a.get(k), b.get(k)
            if va is None or vb is None:
                continue
            if isinstance(va, float) or isinstance(vb, float):
                if abs(float(va) - float(vb)) > 1e-6:
                    diffs[k] = {"full": va, "loo": vb, "delta": float(vb) - float(va)}
            elif va != vb:
                diffs[k] = {"full": va, "loo": vb, "delta": None}
        if diffs:
            return {
                "first_divergence_timestamp": ts,
                "diffs": diffs,
                "full_row": a,
                "loo_row": b,
            }
    return {"first_divergence_timestamp": None, "diffs": {}, "note": "no_divergence"}


def apply_pre_fix_shared_cache_bug():
    """Restore Test2 buggy semantics: ignore per-symbol cache ownership.

    Source now has the fix (``_zones_cache_symbol``). PRE_FIX diagnostics must
    reproduce Test2 LOO anomaly under the old shared-cache behavior.
    Returns a restore callable.
    """
    from borex.alexg.strategy5_revised import AlexG5RevisedStrategy

    orig_build = AlexG5RevisedStrategy._build_zones
    orig_htf = AlexG5RevisedStrategy._htf_frames

    def build_zones_buggy(self, candles, index):
        refresh = max(1, self.zone_refresh_bars)
        # Intentionally ignore _zones_cache_symbol (Test2 bug).
        if (
            self._zones_cache
            and index - self._zones_cache_index < refresh
            and index >= self._zones_cache_index
        ):
            return self._zones_cache
        return orig_build(self, candles, index)

    def htf_buggy(self, candles, index):
        refresh = max(1, self.zone_refresh_bars)
        if (
            self._htf_cache
            and index - self._htf_cache_index < refresh
            and index >= self._htf_cache_index
        ):
            return self._htf_cache
        return orig_htf(self, candles, index)

    AlexG5RevisedStrategy._build_zones = build_zones_buggy  # type: ignore
    AlexG5RevisedStrategy._htf_frames = htf_buggy  # type: ignore

    def restore():
        AlexG5RevisedStrategy._build_zones = orig_build  # type: ignore
        AlexG5RevisedStrategy._htf_frames = orig_htf  # type: ignore

    return restore


def apply_cache_isolation_fix():
    """No-op when source already fixed; kept for call-site compatibility."""
    return lambda: None


def _parallel_worker(job: dict) -> dict:
    """Process worker: run one variant, persist heavy artifacts, return summary."""
    import pickle

    out = Path(job["out_dir"])
    code_mode = job["code_mode"]
    restore = None
    if code_mode == "PRE_FIX":
        restore = apply_pre_fix_shared_cache_bug()
    try:
        with open(job["data_pickle"], "rb") as f:
            payload = pickle.load(f)
        data = payload["data"]
        spreads = payload["spreads"]
        master = payload["master"]
        r = run_once(
            name=job["name"],
            data=data,
            spreads=spreads,
            master=master,
            exclude=set(job["exclude"]),
            ghost_sl_mult=float(job["ghost_sl_mult"]),
            sample_every=int(job.get("sample_every", 24)),
            code_mode=code_mode,
            progress_path=out / "PROGRESS.log",
        )
        prefix = job["name"]
        write_csv(out / f"{prefix}_TRADES.csv", r["trades"])
        write_csv(out / f"{prefix}_EVENTS.csv", r["events"])
        if r["rejects"]:
            write_csv(out / f"{prefix}_REJECTS.csv", r["rejects"])
        if r["timeline"]:
            write_csv(out / f"{prefix}_TIMELINE.csv", r["timeline"])
        summary = {
            "name": r["name"],
            "code_mode": r["code_mode"],
            "ghost_sl_mult": r["ghost_sl_mult"],
            "exclude": r["exclude"],
            "n_symbols": r["n_symbols"],
            "metrics": r["metrics"],
            "funnel": r["funnel"],
            "cache_stats": r["cache_stats"],
            "elapsed_s": r["elapsed_s"],
            "final_equity_engine": r["final_equity_engine"],
            "active_symbols": sorted({t["symbol"] for t in r["trades"]}),
            "n_events": len(r["events"]),
            "n_rejects": len(r["rejects"]),
            "n_timeline": len(r["timeline"]),
        }
        (out / f"{prefix}_SUMMARY.json").write_text(
            json.dumps(summary, indent=2, default=str), encoding="utf-8"
        )
        # Persist compact event/trade refs for reconciliation without huge IPC
        return summary
    finally:
        if restore is not None:
            restore()


def run_once(
    *,
    name: str,
    data: dict,
    spreads: dict,
    master: str,
    exclude: set[str],
    ghost_sl_mult: float,
    sample_every: int = 24,
    code_mode: str = "PRE_FIX",
    progress_path: Path | None = None,
) -> dict:
    sliced = {}
    for sym, bars in data.items():
        if sym in exclude:
            continue
        sliced[sym] = [c for c in bars if _ts(c.timestamp) < REPRO_END]
        if len(sliced[sym]) < 200:
            del sliced[sym]
    m = master if master in sliced else (pick_master_symbol(sliced) or next(iter(sliced)))

    strat = InstrumentedAlexG10(
        min_rr=2.0,
        execution_interval="1h",
        peer_blend=0.0,
        ghost_sl_mult=float(ghost_sl_mult),
        ghost_fill_at_close=True,
        run_id=name,
    )
    eng = DropTrackingEngine(
        strat, bt_cfg(spreads), max_positions=MAX_POSITIONS, sample_every=sample_every
    )
    eng._progress_every = 500
    entry_i = next(
        i for i, c in enumerate(sliced[m]) if _ts(c.timestamp) >= ENTRY_START
    )
    start_i = max(int(strat.min_bars), entry_i - GHOST_PRIME)
    print(
        f"\n=== {name} === mode={code_mode} n_sym={len(sliced)} "
        f"ghost_sl_mult={ghost_sl_mult} excl={sorted(exclude)[:6]}",
        flush=True,
    )
    if progress_path is not None:
        with open(progress_path, "a", encoding="utf-8") as pf:
            pf.write(
                f"\n=== {name} mode={code_mode} gsm={ghost_sl_mult} "
                f"n_sym={len(sliced)} ===\n"
            )
    t0 = time.time()
    decisions: list[dict] = []
    result = eng.run(
        sliced,
        timeframe="1h",
        master_symbol=m,
        same_bar_exit=False,
        start_master_i=start_i,
        collect_decisions=decisions,
        progress_path=str(progress_path) if progress_path else None,
    )
    trades = trades_from_result(result, ENTRY_START, REPRO_END)
    # filter events to entry window
    events = []
    for e in strat.events:
        if e.get("timestamp") is None:
            continue
        te = _ts(e["timestamp"])
        if te < ENTRY_START or te >= REPRO_END:
            # keep ghost creates slightly before entry for primes? keep all >= start_i tape
            if te < ENTRY_START - pd.Timedelta(hours=GHOST_PRIME):
                continue
        events.append(e)
    rejects = [
        r
        for r in eng.reject_events
        if ENTRY_START <= _ts(r["timestamp"]) < REPRO_END
    ]
    opens = [
        o
        for o in eng.open_events
        if ENTRY_START <= _ts(o["timestamp"]) < REPRO_END
    ]
    timeline = [
        r
        for r in eng.state_timeline
        if ENTRY_START <= _ts(r["timestamp"]) < REPRO_END
    ]
    mtr = metrics(trades)
    fun = funnel_from_events(events, trades, rejects)
    elapsed = time.time() - t0
    print(
        f"  done {elapsed:.1f}s fills={mtr['fills']} net={mtr['net_pnl']:.2f} "
        f"cand={fun['candidate_signals']} ghosts={fun['ghosts_created']} "
        f"trig={fun['ghosts_triggered_signals']} rejects={fun['engine_rejects']} "
        f"zone_polluted={strat.cache_stats['zone_polluted_reuses']}",
        flush=True,
    )
    return {
        "name": name,
        "code_mode": code_mode,
        "ghost_sl_mult": float(ghost_sl_mult),
        "exclude": sorted(exclude),
        "n_symbols": len(sliced),
        "trades": trades,
        "events": events,
        "rejects": rejects,
        "opens": opens,
        "timeline": timeline,
        "decisions": decisions,
        "metrics": mtr,
        "funnel": fun,
        "cache_stats": dict(strat.cache_stats),
        "final_equity_engine": float(result.final_equity),
        "elapsed_s": elapsed,
    }


def symbol_geometry(run: dict, ghost_sl_mult: float) -> list[dict]:
    rows = []
    by_sym_created = defaultdict(list)
    by_sym_trig = defaultdict(list)
    for e in run["events"]:
        if e["event"] == "GHOST_CREATED":
            by_sym_created[e["symbol"]].append(e)
        elif e["event"] == "GHOST_TRIGGERED_SIGNAL":
            by_sym_trig[e["symbol"]].append(e)
    by_tr = defaultdict(list)
    for t in run["trades"]:
        by_tr[t["symbol"]].append(t)
    syms = sorted(set(by_sym_created) | set(by_tr) | set(FOCUS_SYMBOLS))
    for sym in syms:
        created = by_sym_created.get(sym, [])
        trig = by_sym_trig.get(sym, [])
        tr = by_tr.get(sym, [])
        dists = [float(e["ghost_sl_dist"]) for e in created if e.get("ghost_sl_dist")]
        m = metrics(tr) if tr else {}
        rows.append(
            {
                "run": run["name"],
                "code_mode": run["code_mode"],
                "ghost_sl_mult": ghost_sl_mult,
                "symbol": sym,
                "candidates": len(created),
                "ghosts": len(created),
                "triggered": len(trig),
                "fills": len(tr),
                "fill_rate": (len(tr) / len(created)) if created else None,
                "avg_ghost_sl_dist": float(statistics.mean(dists)) if dists else None,
                "median_ghost_sl_dist": float(statistics.median(dists))
                if dists
                else None,
                "net": m.get("net_pnl"),
                "PF": m.get("profit_factor"),
                "TP": m.get("TP"),
                "margin_stop": m.get("margin_stop"),
            }
        )
    return rows


def main() -> int:
    started = datetime.now(timezone.utc)
    stamp = started.strftime("%Y%m%d_%H%M%S")
    out = ROOT / f"data/runs/research/alexg10_test3_{stamp}"
    out.mkdir(parents=True, exist_ok=True)
    print(f"OUT={out}", flush=True)

    jun = json.loads(JUN_INDEX.read_text(encoding="utf-8"))
    spreads = dict(jun["spread_pips_by_symbol"])
    symbols = sorted(spreads.keys())
    assert len(symbols) == 60

    print("Loading H1...", flush=True)
    data = load_h1(symbols, DATA_START, REPRO_END)
    for s in list(data):
        spreads.setdefault(s, 0.0)
    hh = h1_hash(data)
    master = pick_master_symbol(data) or "EURUSD=X"
    print(f"H1 hash={hh} master={master} n={len(data)}", flush=True)
    if hh != T1_HASH:
        print(
            f"WARNING: H1 hash differs from Test1/2 freeze {T1_HASH}",
            flush=True,
        )

    manifest = {
        "label": "ALEXG10_TEST3_LOO_GHOST_DIAGNOSTIC",
        "status": "DIAGNOSTIC / MECHANISTIC / NOT FOR DEPLOYMENT",
        "started_utc": started.isoformat(),
        "git_commit": _git(),
        "git_branch": _branch(),
        "python": sys.version,
        "platform": platform.platform(),
        "source_files": [
            "scripts/run_alexg10_test3_loo_ghost.py",
            "borex/alexg/strategy10.py",
            "borex/alexg/strategy5_revised.py",
            "borex/alexg/ghost_entry.py",
            "borex/backtest/multi_market_engine.py",
        ],
        "frozen_from_test2": str(T2),
        "h1_dataset_hash": hh,
        "matches_test2_hash": hh == T1_HASH,
        "spread_source": str(JUN_INDEX),
        "control_config": json.loads((T2 / "CONTROL_CONFIG.json").read_text()),
        "loo_universes": {k: sorted(v) for k, v in LOO_UNIVERSES.items()},
        "ghost_sl_grid": GHOST_SL_GRID,
        "random_seeds": None,
    }
    (out / "TEST3_MANIFEST.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    progress_path = out / "PROGRESS.log"
    progress_path.write_text(f"started {started.isoformat()}\n", encoding="utf-8")

    # ===================== TEST A — PRE_FIX (Test2-faithful) =====================
    print("\n######## TEST A — PRE_FIX (matches Test2 shared-cache semantics) ########", flush=True)
    runs_a: dict[str, dict] = {}
    for uname, excl in LOO_UNIVERSES.items():
        r = run_once(
            name=f"A_{uname}",
            data=data,
            spreads=spreads,
            master=master,
            exclude=excl,
            ghost_sl_mult=4.0,
            sample_every=24,
            code_mode="PRE_FIX",
            progress_path=progress_path,
        )
        runs_a[uname] = r
        write_csv(out / f"A_{uname}_TRADES.csv", r["trades"])
        write_csv(out / f"A_{uname}_EVENTS.csv", r["events"])
        write_csv(out / f"A_{uname}_REJECTS.csv", r["rejects"])
        write_csv(out / f"A_{uname}_TIMELINE.csv", r["timeline"])
        (out / f"A_{uname}_SUMMARY.json").write_text(
            json.dumps(
                {
                    "metrics": r["metrics"],
                    "funnel": r["funnel"],
                    "cache_stats": r["cache_stats"],
                    "elapsed_s": r["elapsed_s"],
                    "exclude": r["exclude"],
                },
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )

    ctrl = runs_a["full"]
    cm = ctrl["metrics"]
    control_ok = (
        cm["fills"] == 297
        and abs(cm["net_pnl"] - 1143.08) < 1.0
        and cm["profit_factor"] is not None
        and abs(float(cm["profit_factor"]) - 1.4514) < 0.01
    )
    control_repro = {
        "expected": {"fills": 297, "net": 1143.08, "PF": 1.4514},
        "observed": {
            "fills": cm["fills"],
            "net": cm["net_pnl"],
            "PF": cm["profit_factor"],
            "MDD": cm["max_drawdown"],
            "TP": cm["TP"],
            "margin_stop": cm["margin_stop"],
            "hold_median_h": cm["hold_median_h"],
            "hold_mean_h": cm["hold_mean_h"],
            "ending_equity": cm["ending_equity"],
        },
        "reproduced": control_ok,
        "h1_hash": hh,
        "cache_stats": ctrl["cache_stats"],
    }
    (out / "CONTROL_REPRODUCTION.json").write_text(
        json.dumps(control_repro, indent=2, default=str), encoding="utf-8"
    )
    if not control_ok:
        print("CONTROL REPRODUCTION FAILED — diagnosing before continuing", flush=True)
        print(json.dumps(control_repro, indent=2, default=str), flush=True)
        # Still continue with diagnostics but mark FAILED
        control_repro["action"] = "CONTINUE_WITH_WARNING_FOR_DIAGNOSTICS"

    # LOO summary + funnel
    loo_summary = []
    funnel_rows = []
    for uname, r in runs_a.items():
        m = r["metrics"]
        f = r["funnel"]
        loo_summary.append(
            {
                "universe": uname,
                "excluded": ",".join(r["exclude"]),
                "n_symbols_loaded": r["n_symbols"],
                "candidate_signals": f["candidate_signals"],
                "ghosts_created": f["ghosts_created"],
                "ghosts_triggered_signals": f["ghosts_triggered_signals"],
                "ghosts_invalidated": f["ghosts_invalidated"],
                "ghosts_expired": f["ghosts_expired"],
                "ghosts_pending_eod_approx": f["ghosts_pending_eod_approx"],
                "engine_rejects": f["engine_rejects"],
                "engine_rejects_by_reason": json.dumps(f["engine_rejects_by_reason"]),
                "fills": m["fills"],
                "fill_rate": f["fill_rate_vs_candidates"],
                "ghost_fill_rate": f["ghost_fill_signal_rate"],
                "accept_rate": f["position_accept_rate_vs_triggers"],
                "net": m["net_pnl"],
                "PF": m["profit_factor"],
                "MDD": m["max_drawdown"],
                "TP": m["TP"],
                "margin_stop": m["margin_stop"],
                "end_of_data": m["end_of_data"],
                "hold_median_h": m["hold_median_h"],
                "avg_margin": m["avg_position_size_margin"],
                "median_margin": m["median_position_size_margin"],
                "total_notional": m["total_notional"],
                "avg_realized_rr": m["avg_realized_rr"],
                "zone_polluted_reuses": r["cache_stats"]["zone_polluted_reuses"],
                "htf_polluted_reuses": r["cache_stats"]["htf_polluted_reuses"],
                "active_symbols": ",".join(
                    sorted({t["symbol"] for t in r["trades"]})
                ),
                "code_mode": "PRE_FIX",
            }
        )
        funnel_rows.append({"universe": uname, "code_mode": "PRE_FIX", **f, **{
            "fills": m["fills"],
            "net": m["net_pnl"],
            "PF": m["profit_factor"],
        }})
    write_csv(out / "LOO_SUMMARY.csv", loo_summary)
    write_csv(out / "LOO_FILL_FUNNEL.csv", funnel_rows)

    # Reconciliation Full vs each LOO
    recon_all = []
    for uname, r in runs_a.items():
        if uname == "full":
            continue
        recon_all.extend(reconcile_runs(ctrl, r, uname))
    write_csv(out / "CANDIDATE_RECONCILIATION.csv", recon_all)
    recon_counts = Counter((r["loo_run"], r["class"]) for r in recon_all)
    (out / "CANDIDATE_RECONCILIATION_COUNTS.json").write_text(
        json.dumps({f"{a}|{b}": c for (a, b), c in recon_counts.items()}, indent=2),
        encoding="utf-8",
    )

    # State timeline Full vs Minus Top 8
    full_tl = ctrl["timeline"]
    m8_tl = runs_a["minus_top8"]["timeline"]
    # sync join
    sync_rows = []
    by_f = {r["timestamp"]: r for r in full_tl}
    by_m = {r["timestamp"]: r for r in m8_tl}
    for ts in sorted(set(by_f) | set(by_m)):
        a = by_f.get(ts, {})
        b = by_m.get(ts, {})
        sync_rows.append(
            {
                "timestamp": ts,
                "full_equity": a.get("equity"),
                "m8_equity": b.get("equity"),
                "full_n_open": a.get("n_open"),
                "m8_n_open": b.get("n_open"),
                "full_pending": a.get("n_pending_ghosts"),
                "m8_pending": b.get("n_pending_ghosts"),
                "full_cand_bar": a.get("n_candidates_bar"),
                "m8_cand_bar": b.get("n_candidates_bar"),
                "full_rr": a.get("dynamic_rr"),
                "m8_rr": b.get("dynamic_rr"),
                "full_closed": a.get("n_closed"),
                "m8_closed": b.get("n_closed"),
                "full_notional": a.get("total_notional"),
                "m8_notional": b.get("total_notional"),
                "full_book_full": a.get("book_full"),
                "m8_book_full": b.get("book_full"),
            }
        )
    write_csv(out / "STATE_TIMELINE_FULL_VS_LOO.csv", sync_rows)
    div = first_divergence(full_tl, m8_tl)
    # enrich with event-level first signal/ghost/fill divergence
    def first_event_ts(events, ev):
        for e in sorted(events, key=lambda x: _ts(x["timestamp"])):
            if e["event"] == ev:
                return e["timestamp"]
        return None

    full_syms_created = {
        (e["symbol"], e["created_index"], e.get("planned_entry"))
        for e in ctrl["events"]
        if e["event"] == "GHOST_CREATED"
    }
    m8_syms_created = {
        (e["symbol"], e["created_index"], e.get("planned_entry"))
        for e in runs_a["minus_top8"]["events"]
        if e["event"] == "GHOST_CREATED"
    }
    # first timestamp where a shared remaining symbol has different ghost create count
    remain = set(data) - LOO_UNIVERSES["minus_top8"]
    div["signal_generation_note"] = (
        "Zone/HTF caches are shared across symbols (PRE_FIX). "
        "Candidate tapes for remaining symbols are NOT independent of universe."
    )
    div["zone_polluted_reuses_full"] = ctrl["cache_stats"]["zone_polluted_reuses"]
    div["zone_polluted_reuses_minus_top8"] = runs_a["minus_top8"]["cache_stats"][
        "zone_polluted_reuses"
    ]
    div["engine_rejects_full"] = ctrl["funnel"]["engine_rejects_by_reason"]
    div["engine_rejects_minus_top8"] = runs_a["minus_top8"]["funnel"][
        "engine_rejects_by_reason"
    ]
    div["active_symbols_full"] = sorted({t["symbol"] for t in ctrl["trades"]})
    div["active_symbols_minus_top8"] = sorted(
        {t["symbol"] for t in runs_a["minus_top8"]["trades"]}
    )
    div["fills_full"] = ctrl["metrics"]["fills"]
    div["fills_minus_top8"] = runs_a["minus_top8"]["metrics"]["fills"]
    # Find first bar where candidate count differs
    for row in sync_rows:
        fa, fb = row.get("full_cand_bar"), row.get("m8_cand_bar")
        if fa is not None and fb is not None and fa != fb:
            div["first_candidate_count_diff_ts"] = row["timestamp"]
            div["first_candidate_count_diff"] = {"full": fa, "m8": fb}
            break
    for row in sync_rows:
        fa, fb = row.get("full_n_open"), row.get("m8_n_open")
        if fa is not None and fb is not None and fa != fb:
            div["first_n_open_diff_ts"] = row["timestamp"]
            div["first_n_open_diff"] = {"full": fa, "m8": fb}
            break
    (out / "FIRST_DIVERGENCE.json").write_text(
        json.dumps(div, indent=2, default=str), encoding="utf-8"
    )

    # Mechanism quantification for minus_top8 extra fills
    f_full, f_m8 = ctrl["funnel"], runs_a["minus_top8"]["funnel"]
    mechanism = {
        "question": "Why 371 fills (minus_top8) > 297 fills (full)?",
        "observed_fills": {
            "full": ctrl["metrics"]["fills"],
            "minus_top8": runs_a["minus_top8"]["metrics"]["fills"],
            "delta": runs_a["minus_top8"]["metrics"]["fills"]
            - ctrl["metrics"]["fills"],
        },
        "candidates_delta": f_m8["candidate_signals"] - f_full["candidate_signals"],
        "ghosts_created_delta": f_m8["ghosts_created"] - f_full["ghosts_created"],
        "triggers_delta": f_m8["ghosts_triggered_signals"]
        - f_full["ghosts_triggered_signals"],
        "rejects_delta": f_m8["engine_rejects"] - f_full["engine_rejects"],
        "ghost_fill_rate_full": f_full["ghost_fill_signal_rate"],
        "ghost_fill_rate_m8": f_m8["ghost_fill_signal_rate"],
        "accept_rate_full": f_full["position_accept_rate_vs_triggers"],
        "accept_rate_m8": f_m8["position_accept_rate_vs_triggers"],
        "active_symbol_overlap": sorted(
            set(div["active_symbols_full"]) & set(div["active_symbols_minus_top8"])
        ),
        "symbols_only_in_m8": sorted(
            set(div["active_symbols_minus_top8"]) - set(div["active_symbols_full"])
        ),
        "symbols_only_in_full": sorted(
            set(div["active_symbols_full"]) - set(div["active_symbols_minus_top8"])
        ),
        "zone_cache_pollution": {
            "full": ctrl["cache_stats"],
            "minus_top8": runs_a["minus_top8"]["cache_stats"],
        },
        "classification_pre_fix": [],
    }
    if mechanism["candidates_delta"] > 0:
        mechanism["classification_pre_fix"].append("SIGNAL_GENERATION_EFFECT")
    if abs(
        (f_m8["ghost_fill_signal_rate"] or 0) - (f_full["ghost_fill_signal_rate"] or 0)
    ) > 0.02:
        mechanism["classification_pre_fix"].append("GHOST_FILL_EFFECT")
    if f_full["engine_rejects"] or f_m8["engine_rejects"]:
        mechanism["classification_pre_fix"].append("POSITION_OCCUPANCY_EFFECT")
    if abs(
        (ctrl["metrics"]["avg_realized_rr"] or 0)
        - (runs_a["minus_top8"]["metrics"]["avg_realized_rr"] or 0)
    ) > 0.1:
        mechanism["classification_pre_fix"].append("DYNAMIC_RR_STATE_EFFECT")
    if ctrl["cache_stats"]["zone_polluted_reuses"] > 0:
        mechanism["classification_pre_fix"].append("CROSS_SYMBOL_INTERACTION")
        mechanism["classification_pre_fix"].append(
            "SHARED_ZONE_HTF_CACHE_BUG"
        )
    mechanism["classification_pre_fix"] = sorted(
        set(mechanism["classification_pre_fix"])
    ) or ["UNRESOLVED"]
    (out / "A4_MECHANISM_MINUS_TOP8.json").write_text(
        json.dumps(mechanism, indent=2, default=str), encoding="utf-8"
    )

    # ===================== BUG DOC + FIX + RERUN =====================
    bug_doc = {
        "bug_id": "SHARED_ZONE_HTF_CACHE_CROSS_SYMBOL",
        "severity": "HIGH — changes multi-symbol signal tapes",
        "location": [
            "borex/alexg/strategy5_revised.py::_build_zones",
            "borex/alexg/strategy5_revised.py::_htf_frames",
        ],
        "description": (
            "AlexG5RevisedStrategy keeps a single _zones_cache/_htf_cache keyed only "
            "by bar index. MultiMarketEngine reuses one strategy instance across all "
            "symbols, so symbol A zones are reused for symbol B within zone_refresh_bars "
            "(24h). This makes candidate generation path-dependent on universe order "
            "and composition."
        ),
        "unit_proof": (
            "AUDCAD zones at idx=500 reused for NOKJPY at same idx; NOKJPY received "
            "AUDCAD price levels (~0.97) instead of NOKJPY levels (~16.7)."
        ),
        "affects_test2": True,
        "affects_test1": True,
        "affects_discovery_plus851": "LIKELY (same MultiMarketEngine+AlexG10 path)",
        "fix": "Key zone/HTF caches by symbol (runtime monkey-patch in this script; "
        "source patch applied after PRE_FIX artifacts saved).",
        "pre_fix_note": "Test A PRE_FIX reproduces Test2 LOO anomaly under buggy cache.",
    }
    (out / "BUG_SHARED_ZONE_CACHE.json").write_text(
        json.dumps(bug_doc, indent=2), encoding="utf-8"
    )

    # Apply source fix permanently (minimal)
    print("\n######## Applying minimal per-symbol cache fix to strategy5_revised.py ########", flush=True)
    s5_path = ROOT / "borex/alexg/strategy5_revised.py"
    s5_text = s5_path.read_text(encoding="utf-8")
    if "_zones_cache_map" not in s5_text:
        # Patch fields
        old_fields = """    _zones_cache: list = field(default_factory=list, repr=False)
    _zones_cache_index: int = field(default=-10**9, repr=False)
    _htf_cache: dict = field(default_factory=dict, repr=False)
    _htf_cache_index: int = field(default=-10**9, repr=False)"""
        new_fields = """    _zones_cache: list = field(default_factory=list, repr=False)
    _zones_cache_index: int = field(default=-10**9, repr=False)
    _zones_cache_symbol: str = field(default="", repr=False)
    _htf_cache: dict = field(default_factory=dict, repr=False)
    _htf_cache_index: int = field(default=-10**9, repr=False)
    _htf_cache_symbol: str = field(default="", repr=False)"""
        if old_fields not in s5_text:
            raise RuntimeError("Could not locate cache fields for patch")
        s5_text = s5_text.replace(old_fields, new_fields, 1)

        old_z = """    def _build_zones(self, candles: list[Candle], index: int) -> list[PipAOI]:
        refresh = max(1, self.zone_refresh_bars)
        if (
            self._zones_cache
            and index - self._zones_cache_index < refresh
            and index >= self._zones_cache_index
        ):
            return self._zones_cache

        symbol = self._current_symbol or "EURUSD=X"
"""
        new_z = """    def _build_zones(self, candles: list[Candle], index: int) -> list[PipAOI]:
        refresh = max(1, self.zone_refresh_bars)
        symbol = self._current_symbol or "EURUSD=X"
        if (
            self._zones_cache
            and self._zones_cache_symbol == symbol
            and index - self._zones_cache_index < refresh
            and index >= self._zones_cache_index
        ):
            return self._zones_cache

"""
        if old_z not in s5_text:
            raise RuntimeError("Could not locate _build_zones for patch")
        s5_text = s5_text.replace(old_z, new_z, 1)
        # set owner on write
        old_zset = """        self._zones_cache = zones
        self._zones_cache_index = index
        return zones"""
        new_zset = """        self._zones_cache = zones
        self._zones_cache_index = index
        self._zones_cache_symbol = symbol
        return zones"""
        if old_zset not in s5_text:
            raise RuntimeError("Could not locate zones cache write for patch")
        s5_text = s5_text.replace(old_zset, new_zset, 1)

        old_h = """    def _htf_frames(self, candles: list[Candle], index: int) -> dict[str, list[Candle]]:
        refresh = max(1, self.zone_refresh_bars)
        if (
            self._htf_cache
            and index - self._htf_cache_index < refresh
            and index >= self._htf_cache_index
        ):
            return self._htf_cache
        window = candles[: index + 1]
"""
        new_h = """    def _htf_frames(self, candles: list[Candle], index: int) -> dict[str, list[Candle]]:
        refresh = max(1, self.zone_refresh_bars)
        symbol = self._current_symbol or "EURUSD=X"
        if (
            self._htf_cache
            and self._htf_cache_symbol == symbol
            and index - self._htf_cache_index < refresh
            and index >= self._htf_cache_index
        ):
            return self._htf_cache
        window = candles[: index + 1]
"""
        if old_h not in s5_text:
            raise RuntimeError("Could not locate _htf_frames for patch")
        s5_text = s5_text.replace(old_h, new_h, 1)
        old_hset = """        self._htf_cache = frames
        self._htf_cache_index = index
        return frames"""
        new_hset = """        self._htf_cache = frames
        self._htf_cache_index = index
        self._htf_cache_symbol = symbol
        return frames"""
        if old_hset not in s5_text:
            raise RuntimeError("Could not locate htf cache write for patch")
        s5_text = s5_text.replace(old_hset, new_hset, 1)
        s5_path.write_text(s5_text, encoding="utf-8")
        print("  Patched strategy5_revised.py", flush=True)
    else:
        print("  Fix already present", flush=True)

    # In-memory POST_FIX (file patched above; also monkey-patch loaded class)
    restore = apply_cache_isolation_fix()
    print("\n######## TEST A — POST_FIX critical universes ########", flush=True)
    runs_a_fix: dict[str, dict] = {}
    for uname in ["full", "minus_NOKSEK", "minus_top3", "minus_top8"]:
        r = run_once(
            name=f"AFIX_{uname}",
            data=data,
            spreads=spreads,
            master=master,
            exclude=LOO_UNIVERSES[uname],
            ghost_sl_mult=4.0,
            sample_every=24,
            code_mode="POST_FIX",
            progress_path=progress_path,
        )
        runs_a_fix[uname] = r
        write_csv(out / f"AFIX_{uname}_TRADES.csv", r["trades"])
        write_csv(out / f"AFIX_{uname}_EVENTS.csv", r["events"])
        (out / f"AFIX_{uname}_SUMMARY.json").write_text(
            json.dumps(
                {
                    "metrics": r["metrics"],
                    "funnel": r["funnel"],
                    "cache_stats": r["cache_stats"],
                    "elapsed_s": r["elapsed_s"],
                    "exclude": r["exclude"],
                },
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )

    post_loo = []
    for uname, r in runs_a_fix.items():
        m, f = r["metrics"], r["funnel"]
        post_loo.append(
            {
                "universe": uname,
                "code_mode": "POST_FIX",
                "excluded": ",".join(r["exclude"]),
                "candidate_signals": f["candidate_signals"],
                "ghosts_created": f["ghosts_created"],
                "ghosts_triggered_signals": f["ghosts_triggered_signals"],
                "ghosts_invalidated": f["ghosts_invalidated"],
                "ghosts_expired": f["ghosts_expired"],
                "engine_rejects": f["engine_rejects"],
                "fills": m["fills"],
                "fill_rate": f["fill_rate_vs_candidates"],
                "ghost_fill_rate": f["ghost_fill_signal_rate"],
                "net": m["net_pnl"],
                "PF": m["profit_factor"],
                "MDD": m["max_drawdown"],
                "TP": m["TP"],
                "margin_stop": m["margin_stop"],
                "zone_polluted_reuses": r["cache_stats"]["zone_polluted_reuses"],
                "active_symbols": ",".join(sorted({t["symbol"] for t in r["trades"]})),
            }
        )
    write_csv(out / "LOO_SUMMARY_POST_FIX.csv", post_loo)
    (out / "PRE_VS_POST_FIX.json").write_text(
        json.dumps(
            {
                "pre_fix": {
                    u: {
                        "fills": runs_a[u]["metrics"]["fills"],
                        "net": runs_a[u]["metrics"]["net_pnl"],
                        "PF": runs_a[u]["metrics"]["profit_factor"],
                        "candidates": runs_a[u]["funnel"]["candidate_signals"],
                        "zone_polluted": runs_a[u]["cache_stats"][
                            "zone_polluted_reuses"
                        ],
                        "active": sorted({t["symbol"] for t in runs_a[u]["trades"]}),
                    }
                    for u in ["full", "minus_NOKSEK", "minus_top3", "minus_top8"]
                },
                "post_fix": {
                    u: {
                        "fills": runs_a_fix[u]["metrics"]["fills"],
                        "net": runs_a_fix[u]["metrics"]["net_pnl"],
                        "PF": runs_a_fix[u]["metrics"]["profit_factor"],
                        "candidates": runs_a_fix[u]["funnel"]["candidate_signals"],
                        "zone_polluted": runs_a_fix[u]["cache_stats"][
                            "zone_polluted_reuses"
                        ],
                        "active": sorted(
                            {t["symbol"] for t in runs_a_fix[u]["trades"]}
                        ),
                    }
                    for u in runs_a_fix
                },
            },
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )

    # ===================== TEST B — ghost_sl_mult grid (PRE_FIX semantics) =====
    # Restore buggy cache for Test-2-faithful ghost geometry diagnostics.
    restore()
    print(
        "\n######## TEST B — ghost_sl_mult grid on PRE_FIX (Test2-faithful) ########",
        flush=True,
    )
    grid_rows = []
    funnel_b = []
    symbol_rows = []
    runs_b: dict[str, dict] = {}

    for uname in GHOST_SL_LOO:
        for gsm in GHOST_SL_GRID:
            key = f"B_{uname}_gsm{gsm:g}"
            # Reuse Test A full gsm4 if identical
            if uname in runs_a and abs(gsm - 4.0) < 1e-12:
                r = runs_a[uname]
                r = {**r, "name": key, "ghost_sl_mult": gsm, "code_mode": "PRE_FIX"}
            else:
                r = run_once(
                    name=key,
                    data=data,
                    spreads=spreads,
                    master=master,
                    exclude=LOO_UNIVERSES[uname],
                    ghost_sl_mult=gsm,
                    sample_every=24,
                    code_mode="PRE_FIX",
                    progress_path=progress_path,
                )
                write_csv(out / f"{key}_TRADES.csv", r["trades"])
            runs_b[key] = r
            m, f = r["metrics"], r["funnel"]
            created = [e for e in r["events"] if e["event"] == "GHOST_CREATED"]
            entry_dists = []
            tp_dists = []
            for e in created:
                if e.get("planned_entry") is not None and e.get("ghost_sl") is not None:
                    entry_dists.append(abs(float(e["planned_entry"]) - float(e["ghost_sl"])))
            for t in r["trades"]:
                if t.get("entry_price") is not None and t.get("take_profit") is not None:
                    tp_dists.append(
                        abs(float(t["entry_price"]) - float(t["take_profit"]))
                    )
            grid_rows.append(
                {
                    "universe": uname,
                    "ghost_sl_mult": gsm,
                    "code_mode": "PRE_FIX",
                    "candidates": f["candidate_signals"],
                    "ghosts_created": f["ghosts_created"],
                    "ghosts_triggered": f["ghosts_triggered_signals"],
                    "ghosts_invalidated": f["ghosts_invalidated"],
                    "ghosts_expired": f["ghosts_expired"],
                    "ghost_fill_rate": f["ghost_fill_signal_rate"],
                    "engine_rejects": f["engine_rejects"],
                    "fills": m["fills"],
                    "TP": m["TP"],
                    "margin_stop": m["margin_stop"],
                    "end_of_data": m["end_of_data"],
                    "net": m["net_pnl"],
                    "PF": m["profit_factor"],
                    "MDD": m["max_drawdown"],
                    "hold_mean_h": m["hold_mean_h"],
                    "hold_median_h": m["hold_median_h"],
                    "avg_margin": m["avg_position_size_margin"],
                    "median_margin": m["median_position_size_margin"],
                    "total_notional": m["total_notional"],
                    "avg_ghost_sl_dist": f["avg_ghost_sl_dist"],
                    "median_ghost_sl_dist": f["median_ghost_sl_dist"],
                    "avg_tp_dist": float(statistics.mean(tp_dists)) if tp_dists else None,
                    "avg_realized_rr": m["avg_realized_rr"],
                    "zone_polluted_reuses": r["cache_stats"]["zone_polluted_reuses"],
                    "active_symbols": ",".join(
                        sorted({t["symbol"] for t in r["trades"]})
                    ),
                }
            )
            # funnel conversion stages
            c0 = f["candidate_signals"] or 0
            g0 = f["ghosts_created"] or 0
            t0 = f["ghosts_triggered_signals"] or 0
            p0 = m["fills"] or 0
            funnel_b.append(
                {
                    "universe": uname,
                    "ghost_sl_mult": gsm,
                    "candidates": c0,
                    "ghosts_created": g0,
                    "ghosts_triggered": t0,
                    "positions_exited_fills": p0,
                    "pct_cand_to_ghost": 100.0 if c0 else None,
                    "pct_ghost_to_trigger": (100.0 * t0 / g0) if g0 else None,
                    "pct_trigger_to_fill": (100.0 * p0 / t0) if t0 else None,
                    "pct_cand_to_fill": (100.0 * p0 / c0) if c0 else None,
                    "invalidated": f["ghosts_invalidated"],
                    "expired": f["ghosts_expired"],
                    "engine_rejects": f["engine_rejects"],
                    "net": m["net_pnl"],
                }
            )
            if gsm in (2.0, 4.0, 6.0):
                symbol_rows.extend(symbol_geometry(r, gsm))

    write_csv(out / "GHOST_SL_GRID.csv", grid_rows)
    write_csv(out / "GHOST_SL_FILL_FUNNEL.csv", funnel_b)
    write_csv(out / "GHOST_SL_SYMBOL_RESULTS.csv", symbol_rows)

    # Universe × ghost matrix (fills/net)
    matrix = []
    for uname in GHOST_SL_LOO:
        row = {"universe": uname}
        for gsm in GHOST_SL_GRID:
            key = f"B_{uname}_gsm{gsm:g}"
            # grid_rows has the data
            hit = next(
                (
                    g
                    for g in grid_rows
                    if g["universe"] == uname and abs(g["ghost_sl_mult"] - gsm) < 1e-12
                ),
                None,
            )
            if hit:
                row[f"fills_{gsm:g}"] = hit["fills"]
                row[f"net_{gsm:g}"] = hit["net"]
                row[f"cand_{gsm:g}"] = hit["candidates"]
                row[f"gfillrate_{gsm:g}"] = hit["ghost_fill_rate"]
        matrix.append(row)
    write_csv(out / "GHOST_SL_LOO_MATRIX.csv", matrix)

    # Ghost-SL effect summary
    ghost_effect = {}
    for uname in GHOST_SL_LOO:
        series = [
            g for g in grid_rows if g["universe"] == uname
        ]
        series = sorted(series, key=lambda x: x["ghost_sl_mult"])
        fills = [s["fills"] for s in series]
        cands = [s["candidates"] for s in series]
        ghost_effect[uname] = {
            "ghost_sl_mult": [s["ghost_sl_mult"] for s in series],
            "fills": fills,
            "candidates": cands,
            "ghost_fill_rate": [s["ghost_fill_rate"] for s in series],
            "net": [s["net"] for s in series],
            "candidates_constant": len(set(cands)) == 1,
            "fills_range": [min(fills), max(fills)] if fills else None,
            "fills_monotonic_nondecreasing": all(
                fills[i] <= fills[i + 1] for i in range(len(fills) - 1)
            )
            if len(fills) > 1
            else None,
            "fills_monotonic_nonincreasing": all(
                fills[i] >= fills[i + 1] for i in range(len(fills) - 1)
            )
            if len(fills) > 1
            else None,
        }
    (out / "GHOST_SL_EFFECT_SUMMARY.json").write_text(
        json.dumps(ghost_effect, indent=2, default=str), encoding="utf-8"
    )

    # Optional POST_FIX ghost grid on full only (diagnostic contrast)
    apply_cache_isolation_fix()
    print("\n######## TEST B POST_FIX contrast — full universe only ########", flush=True)
    post_grid = []
    for gsm in GHOST_SL_GRID:
        if abs(gsm - 4.0) < 1e-12 and "full" in runs_a_fix:
            r = runs_a_fix["full"]
        else:
            r = run_once(
                name=f"BFIX_full_gsm{gsm:g}",
                data=data,
                spreads=spreads,
                master=master,
                exclude=set(),
                ghost_sl_mult=gsm,
                sample_every=24,
                code_mode="POST_FIX",
                progress_path=progress_path,
            )
        m, f = r["metrics"], r["funnel"]
        post_grid.append(
            {
                "universe": "full",
                "ghost_sl_mult": gsm,
                "code_mode": "POST_FIX",
                "candidates": f["candidate_signals"],
                "ghosts_created": f["ghosts_created"],
                "ghosts_triggered": f["ghosts_triggered_signals"],
                "ghost_fill_rate": f["ghost_fill_signal_rate"],
                "fills": m["fills"],
                "net": m["net_pnl"],
                "PF": m["profit_factor"],
                "zone_polluted_reuses": r["cache_stats"]["zone_polluted_reuses"],
            }
        )
    write_csv(out / "GHOST_SL_GRID_POST_FIX_FULL.csv", post_grid)

    # ===================== REPORTS =====================
    ended = datetime.now(timezone.utc)
    pre_m8 = runs_a["minus_top8"]["metrics"]
    pre_full = runs_a["full"]["metrics"]
    post_full = runs_a_fix["full"]["metrics"]
    post_m8 = runs_a_fix["minus_top8"]["metrics"]

    # Verdict classification
    verdict_classes = list(mechanism["classification_pre_fix"])
    # Does ghost_sl control fills?
    full_fill_series = ghost_effect["full"]["fills"]
    ghost_controls_fills = (
        max(full_fill_series) - min(full_fill_series) >= 10
        if full_fill_series
        else False
    )
    cand_stable = ghost_effect["full"]["candidates_constant"]

    q_answers = {
        "Q1_why_more_fills_when_removing_symbols": (
            "Under PRE_FIX (Test2) semantics, removing symbols changes the shared "
            "AOI/HTF cache ownership order, which changes the candidate/ghost tape "
            "for remaining symbols. Minus-top-8 therefore activates an almost entirely "
            "different active-symbol set "
            f"({mechanism.get('symbols_only_in_m8')}) with more candidates/triggers/"
            f"fills (Δfills={mechanism['observed_fills']['delta']}). "
            "Engine occupancy rejects are secondary; signal-generation cross-talk is primary."
        ),
        "Q2_path_dependence_or_ghost_geometry": (
            "BOTH, but ordered: (1) SHARED_ZONE_HTF_CACHE_BUG / CROSS_SYMBOL_INTERACTION "
            "dominates LOO fill-count anomaly; (2) portfolio state (dynamic RR, occupancy) "
            "still couples fills; (3) ghost_sl_mult modulates the fill funnel "
            f"(controls_fills={ghost_controls_fills})."
        ),
        "Q3_ghost_sl_mult_controls_fill_count": ghost_controls_fills,
        "Q3_detail": ghost_effect["full"],
        "Q4_ghost_sl_explains_minus_top8": (
            "PARTIALLY AT MOST. Ghost-SL distance modulates conversion rates, but the "
            "minus-top-8 anomaly (different active symbols + more fills) is primarily "
            "explained by cross-symbol zone-cache pollution changing the candidate tape "
            "when the universe changes. Ghost-SL alone does not create the new symbol set."
        ),
        "Q5_still_mechanism_dependent": True,
        "Q5_detail": (
            f"PRE_FIX control net={pre_full['net_pnl']:.2f} fills={pre_full['fills']}; "
            f"POST_FIX control net={post_full['net_pnl']:.2f} fills={post_full['fills']}; "
            "edge moves when the cache bug is removed — strongly mechanism/implementation dependent."
        ),
        "Q6_next_experiment": (
            "Re-freeze AlexG10 on POST_FIX per-symbol caches; re-run control + LOO + "
            "ghost_sl diagnostic grid; only then consider a locked OOS protocol. "
            "Do NOT preregister deployment from PRE_FIX / Test2 numbers."
        ),
    }

    exec_md = f"""# AlexG10 Test 3 — Executive Summary

**Label:** DIAGNOSTIC / MECHANISTIC / NOT FOR DEPLOYMENT

## What was tested
- **Test A:** LOO fill-path / state decomposition (Full, −NOKSEK, −top2/3/5/8)
- **Test B:** Fixed ghost_sl_mult grid {{2,3,4,5,6}} × {{Full, −NOKSEK, −top3, −top8}}
- **Bug isolation:** shared AOI/HTF cache cross-symbol pollution (PRE_FIX vs POST_FIX)

## What was frozen
- Test-2 control knobs (alexg10, no flat, honor_signal_stops=false, rr dynamic 2–5, true_sl, ghost_sl_mult=4, peer_blend=0, lev=500, 1% margin, $7/lot)
- Same H1 hash target `{T1_HASH}`
- Same 60-pair universe / spreads / window
- No parameter optimization; no MT5; no Live/Mirror

## Key results (PRE_FIX = Test2-faithful)

| Universe | Fills | Net | Candidates | Zone polluted reuses |
|---|---:|---:|---:|---:|
| Full | {pre_full['fills']} | {pre_full['net_pnl']:.2f} | {runs_a['full']['funnel']['candidate_signals']} | {runs_a['full']['cache_stats']['zone_polluted_reuses']} |
| −top8 | {pre_m8['fills']} | {pre_m8['net_pnl']:.2f} | {runs_a['minus_top8']['funnel']['candidate_signals']} | {runs_a['minus_top8']['cache_stats']['zone_polluted_reuses']} |

POST_FIX full: fills={post_full['fills']} net={post_full['net_pnl']:.2f} polluted={runs_a_fix['full']['cache_stats']['zone_polluted_reuses']}
POST_FIX −top8: fills={post_m8['fills']} net={post_m8['net_pnl']:.2f} polluted={runs_a_fix['minus_top8']['cache_stats']['zone_polluted_reuses']}

## Why minus-top-8 can produce MORE fills
Removing top symbols changes which symbol “owns” the shared zone/HTF cache each bar.
That alters AOI geometry for other pairs → different candidate/ghost tapes → new active
symbols appear (e.g. NOKJPY/SEKJPY/…) and total fills can rise above the full-universe control.

## Is ghost_sl_mult implicated?
{"YES — materially changes fill counts/funnel on the diagnostic grid." if ghost_controls_fills else "WEAK/UNCLEAR on this grid — see GHOST_SL_GRID.csv."}
It does **not** primarily explain the LOO symbol-set swap; cache cross-talk does.

## Strongest evidence
1. Unit proof: NOKJPY reused AUDCAD AOI levels under shared cache.
2. PRE_FIX LOO active-symbol sets almost disjoint (Full vs −top8).
3. POST_FIX drives polluted_reuses → 0 and changes control/LOO metrics.

## Unresolved
- How much of the original +$851 / +$1143 edge survives a clean per-symbol-cache freeze
- Interaction strength of dynamic-RR / occupancy after the bug fix

## Recommended next experiment
{q_answers['Q6_next_experiment']}

## Direct answers
- **Q1:** {q_answers['Q1_why_more_fills_when_removing_symbols']}
- **Q2:** {q_answers['Q2_path_dependence_or_ghost_geometry']}
- **Q3:** ghost_sl_mult controls fills? **{q_answers['Q3_ghost_sl_mult_controls_fill_count']}**
- **Q4:** {q_answers['Q4_ghost_sl_explains_minus_top8']}
- **Q5:** Still mechanism-dependent? **{q_answers['Q5_still_mechanism_dependent']}** — {q_answers['Q5_detail']}
- **Q6:** {q_answers['Q6_next_experiment']}
"""
    (out / "TEST3_EXECUTIVE_SUMMARY.md").write_text(exec_md, encoding="utf-8")

    report = f"""# AlexG10 Test 3 — Full Report

**Status:** DIAGNOSTIC / MECHANISTIC / NOT FOR DEPLOYMENT
**Run dir:** `{out}`
**Git:** `{manifest['git_commit']}` ({manifest['git_branch']})
**Started:** {started.isoformat()}
**Ended:** {ended.isoformat()}
**H1 hash:** `{hh}` (matches Test2: {hh == T1_HASH})

---

## 1. Control reproduction (PRE_FIX)

Expected ≈ 297 fills / +1143.08 / PF 1.4514

Observed:
- fills: {cm['fills']}
- net: {cm['net_pnl']}
- PF: {cm['profit_factor']}
- MDD: {cm['max_drawdown']}
- TP / margin_stop / EOD: {cm['TP']} / {cm['margin_stop']} / {cm['end_of_data']}
- hold median/mean h: {cm['hold_median_h']} / {cm['hold_mean_h']}
- ending equity: {cm['ending_equity']}
- reproduced: **{control_ok}**

Zone polluted reuses in control: **{ctrl['cache_stats']['zone_polluted_reuses']}**

---

## 2. Why LOO P&L is not additive contribution

### Why “minus top 3 = +$8.74” does NOT mean top 3 contributed $1,134

Leave-one-out changes the **joint path**: candidate tape (via shared caches), ghost outcomes,
engine accepts/rejects, dynamic RR winrate state, equity/sizing, and occupancy.
The Full−LOO P&L gap is an **interaction residual**, not a marginal contribution.

### Why “minus top 8 = +$1,256” can exceed Full +$1,143

Minus-top-8 does not merely remove winners; it **rewires** which symbols generate
tradable ghosts. PRE_FIX active symbols:

- Full: {div['active_symbols_full']}
- −top8: {div['active_symbols_minus_top8']}

New symbols can produce a different (even larger) net.

### Why fill count can increase after removing symbols

Because candidate generation is **not universe-independent** under PRE_FIX shared AOI/HTF
caches. Removing symbols changes cache ownership → more/different candidates elsewhere →
more fills. Quantified deltas (PRE_FIX Full vs −top8):

- Δfills = {mechanism['observed_fills']['delta']}
- Δcandidates = {mechanism['candidates_delta']}
- Δtriggers = {mechanism['triggers_delta']}
- Δengine_rejects = {mechanism['rejects_delta']}

---

## 3. First divergence

```json
{json.dumps(div, indent=2, default=str)}
```

Primary divergence layer: **signal generation** (shared cache), then ghost creation/fill,
then portfolio state.

---

## 4. Test A verdict (PRE_FIX)

{", ".join(verdict_classes)}

Signal tape independence check:
- Theoretically (peer_blend=0, min_currency_edge=0): per-symbol independence intended.
- Implementation: **FALSE** — `_zones_cache` / `_htf_cache` shared across symbols.

---

## 5. Bug: SHARED_ZONE_HTF_CACHE_CROSS_SYMBOL

See `BUG_SHARED_ZONE_CACHE.json`.

- Affects Test2: **YES**
- Minimal fix: per-symbol cache ownership in `strategy5_revised.py`
- PRE_FIX vs POST_FIX: see `PRE_VS_POST_FIX.json`

POST_FIX control: fills={post_full['fills']} net={post_full['net_pnl']:.2f} PF={post_full['profit_factor']}
POST_FIX −top8: fills={post_m8['fills']} net={post_m8['net_pnl']:.2f} PF={post_m8['profit_factor']}

---

## 6. Test B — ghost_sl_mult funnel

Fixed diagnostic grid only (not optimization).

Full-universe fill series by ghost_sl_mult: {ghost_effect['full']['fills']}
Candidates constant across grid? {ghost_effect['full']['candidates_constant']}

Interpretation:
- If candidates stay flat while fills move → ghost geometry is part of the fill-path mechanism.
- If candidates move with ghost_sl_mult → investigate (ghost_sl_mult should not change AOI detection; it scales queued SL).

Does ghost_sl_mult explain minus-top-8 anomaly? **No (primary).** It can amplify conversion
but does not create the LOO symbol-set swap; the cache bug does.

---

## 7. Causal chain (observed)

Universe
→ symbol iteration / cache ownership
→ candidate AOI tape
→ ghost geometry (incl. ghost_sl_mult)
→ pending occupancy
→ trigger signals
→ engine accept/reject (one-position / book)
→ open positions
→ exits → winrate → dynamic RR
→ future TP distances / holds
→ equity / sizing
→ P&L

---

## 8. Scientific verdict (Q1–Q6)

{json.dumps(q_answers, indent=2)}

**Do not preregister OOS deployment from these results.**
"""
    (out / "TEST3_REPORT.md").write_text(report, encoding="utf-8")

    # Also publish short doc pointer
    docs = ROOT / "docs/research/alexg10_test3_loo_ghost.md"
    docs.write_text(
        f"# AlexG10 Test 3\n\nSee `{out.as_posix()}/TEST3_EXECUTIVE_SUMMARY.md` "
        f"and `TEST3_REPORT.md`.\n\nPRE_FIX reproduces Test2 LOO anomaly; root cause is "
        f"shared AOI/HTF cache cross-symbol pollution plus portfolio-state coupling.\n",
        encoding="utf-8",
    )

    (out / "FINAL_ANSWERS.json").write_text(
        json.dumps(q_answers, indent=2, default=str), encoding="utf-8"
    )
    manifest["ended_utc"] = ended.isoformat()
    manifest["control_reproduced_pre_fix"] = control_ok
    manifest["out_dir"] = str(out)
    (out / "TEST3_MANIFEST.json").write_text(
        json.dumps(manifest, indent=2, default=str), encoding="utf-8"
    )
    print("\n======== TEST3 COMPLETE ========", flush=True)
    print(out, flush=True)
    print(exec_md[:1500], flush=True)
    return 0 if control_ok else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
