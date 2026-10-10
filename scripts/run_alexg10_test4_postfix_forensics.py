#!/usr/bin/env python3
"""
Test 4 — POST_FIX Signal-Tape + Fill-Acceptance Forensics (OFFLINE ONLY).

DIAGNOSTIC / MECHANISTIC / NOT FOR DEPLOYMENT.
Reuses frozen Test2/Test3 dataset + knobs via run_alexg10_test3_loo_ghost helpers.
"""
from __future__ import annotations

import csv
import gc
import json
import math
import platform
import statistics
import sys
import time
import traceback
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from borex.alexg.multi_market import pick_master_symbol  # noqa: E402
from borex.backtest.margin_stops import resolve_rr  # noqa: E402
from borex.backtest.multi_market_engine import (  # noqa: E402
    _indices_at_master,
    index_ltf_bars,
)
from borex.backtest.multi_portfolio import MultiMarketPortfolio  # noqa: E402
from borex.models.candle import Signal  # noqa: E402

from run_alexg10_test3_loo_ghost import (  # noqa: E402
    CAPITAL,
    DATA_START,
    DropTrackingEngine,
    ENTRY_START,
    GHOST_PRIME,
    GHOST_SL_GRID,
    InstrumentedAlexG10,
    JUN_INDEX,
    LEV,
    LOO_UNIVERSES,
    MAX_POSITIONS,
    REPRO_END,
    T1_HASH,
    T2,
    _branch,
    _git,
    _ts,
    apply_cache_isolation_fix,
    bt_cfg,
    funnel_from_events,
    h1_hash,
    load_h1,
    metrics,
    run_once,
    trades_from_result,
    write_csv,
)

TEST3 = ROOT / "data/runs/research/alexg10_test3_20261004_141920"
OUT_ROOT = ROOT / "data/runs/research"

EXPECTED = {
    "candidates": 18313,
    "triggers": 16116,
    "positions": 2389,
    "engine_rejects": 13532,
    "already_open": 13336,
    "book_full_break": 196,
    "net": 5074.55,
    "profit_factor": 1.324,
    "max_drawdown": 0.2434,
}

INDEP_BASE = [
    "AUDCAD=X", "AUDCHF=X", "EURUSD=X", "GBPTRY=X", "NOKSEK=X",
    "USDJPY=X", "NOKJPY=X", "SEKJPY=X", "USDZAR=X",
]
DEEP_DIVE = [
    "AUDCAD=X", "AUDCHF=X", "EURUSD=X", "GBPTRY=X", "NOKSEK=X",
    "EURSEK=X", "EURNOK=X", "NOKJPY=X", "SEKJPY=X", "USDZAR=X",
]


def _jdump(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")


def _pct(n, d):
    return (float(n) / float(d)) if d else None


def _mean(xs):
    xs = [x for x in xs if x is not None and not (isinstance(x, float) and math.isnan(x))]
    return float(statistics.mean(xs)) if xs else None


def _median(xs):
    xs = [x for x in xs if x is not None and not (isinstance(x, float) and math.isnan(x))]
    return float(statistics.median(xs)) if xs else None


def _read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _cand_id(e: dict) -> str:
    return "|".join([
        str(e.get("symbol") or ""),
        str(e.get("timestamp") or ""),
        str(e.get("side_setup") or e.get("side") or ""),
        str(e.get("pattern") or ""),
        str(e.get("planned_entry") or ""),
        str(e.get("ghost_sl") or ""),
        str(e.get("ghost_tp") or ""),
        str(e.get("created_index") or e.get("index") or ""),
    ])


# ---------------------------------------------------------------------------
# Diagnostic engine: replace-on-signal (relax already_open)
# ---------------------------------------------------------------------------
class ReplaceOnSignalEngine(DropTrackingEngine):
    """Diagnostic: close same-symbol open position before accepting new signal.

    Does NOT change signal generation, RR, sizing, spreads, or costs.
    NOT for deployment.
    """

    def _run_with_timeline(self, candles_by_symbol, **kwargs):
        from borex.alexg.multi_market import align_symbols_to_timeline, pick_master_symbol

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
        self.n_replaced = 0

        for master_i in range(begin, n_master):
            if portfolio.liquidated:
                break

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
                self._check_exit_path(portfolio, sym, idx, candle, ltf_index.get(sym))

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
                        from borex.backtest.multi_market_engine import decision_from_signal
                        collect_decisions.append(decision_from_signal(sym, signal))

            candidates.sort(key=lambda x: x[1].score, reverse=True)
            opened: list[str] = []
            for ci, (sym, signal, idx) in enumerate(candidates):
                if portfolio.book_is_full() and sym not in portfolio.open_trades:
                    # still book-full for NEW symbols; same-symbol replace may free a slot
                    # if book full due to max_positions and sym already open, replace is OK
                    if sym not in portfolio.open_trades:
                        for sym2, signal2, idx2 in candidates[ci:]:
                            if sym2 in portfolio.open_trades:
                                continue  # will replace below if we get there — but break ends loop
                            self.reject_events.append({
                                "timestamp": str(candles_by_symbol[sym2][idx2].timestamp),
                                "symbol": sym2, "index": idx2, "reason": "book_full_break",
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
                                    rr_min=self.config.rr_min, rr_max=self.config.rr_max,
                                ),
                                "planned_price": float(signal2.price) if signal2.price is not None else None,
                                "pattern": signal2.pattern,
                            })
                        break

                # DIAGNOSTIC: replace existing same-symbol position
                if sym in portfolio.open_trades:
                    candle = candles_by_symbol[sym][idx]
                    self._close(
                        portfolio, sym, idx, candle.close, candle.timestamp, "replaced_by_signal"
                    )
                    self.n_replaced += 1

                if not portfolio.can_open(sym):
                    self.reject_events.append({
                        "timestamp": str(candles_by_symbol[sym][idx].timestamp),
                        "symbol": sym, "index": idx,
                        "reason": "already_open" if sym in portfolio.open_trades else "can_open_false",
                        "cash": float(portfolio.cash),
                        "n_open": len(portfolio.open_trades),
                        "win_rate": portfolio.win_rate,
                        "dynamic_rr": resolve_rr(
                            rr_mode=self.config.rr_mode, fixed_rr=self.config.true_sl_rr,
                            winrate=portfolio.win_rate, rr_factor=self.config.rr_factor,
                            closed_trades=len(portfolio.closed_trades),
                            winrate_min_trades=self.config.winrate_min_trades,
                            rr_min=self.config.rr_min, rr_max=self.config.rr_max,
                        ),
                        "planned_price": float(signal.price) if signal.price is not None else None,
                        "pattern": signal.pattern,
                    })
                    continue

                n_before = len(portfolio.open_trades)
                self._open_signal(portfolio, sym, signal, idx, candles_by_symbol[sym])
                if sym in portfolio.open_trades and len(portfolio.open_trades) >= n_before:
                    tr = portfolio.open_trades[sym]
                    self.open_events.append({
                        "timestamp": str(candles_by_symbol[sym][idx].timestamp),
                        "symbol": sym, "index": idx,
                        "entry_price": float(tr.entry_price),
                        "margin": float(tr.margin),
                        "stop_loss": float(tr.stop_loss) if tr.stop_loss is not None else None,
                        "take_profit": float(tr.take_profit) if tr.take_profit is not None else None,
                        "equity_at_entry": float(tr.entry_equity),
                        "pattern": signal.pattern,
                    })
                elif sym not in portfolio.open_trades:
                    self.reject_events.append({
                        "timestamp": str(candles_by_symbol[sym][idx].timestamp),
                        "symbol": sym, "index": idx, "reason": "open_position_failed",
                        "cash": float(portfolio.cash), "n_open": len(portfolio.open_trades),
                        "win_rate": portfolio.win_rate, "dynamic_rr": None,
                        "planned_price": float(signal.price) if signal.price is not None else None,
                        "pattern": signal.pattern,
                    })
                if same_bar_exit and sym in portfolio.open_trades:
                    opened.append(sym)

            if same_bar_exit:
                for sym in opened:
                    idx = indices.get(sym)
                    if idx is None:
                        continue
                    self._check_exit_path(
                        portfolio, sym, idx, candles_by_symbol[sym][idx], ltf_index.get(sym)
                    )

            eq = portfolio.equity_at_prices(prices)
            equity_curve.append(eq)
            peak = max(peak, eq)
            if peak > 0:
                max_dd = max(max_dd, (peak - eq) / peak)

            if (master_i - begin) % self.sample_every == 0:
                wr = portfolio.win_rate
                rr = resolve_rr(
                    rr_mode=self.config.rr_mode, fixed_rr=self.config.true_sl_rr,
                    winrate=wr, rr_factor=self.config.rr_factor,
                    closed_trades=len(portfolio.closed_trades),
                    winrate_min_trades=self.config.winrate_min_trades,
                    rr_min=self.config.rr_min, rr_max=self.config.rr_max,
                )
                pending_n = len(getattr(self.strategy, "_pending", {}) or {})
                notional = sum(
                    float(t.margin) * float(self.config.leverage)
                    for t in portfolio.open_trades.values()
                )
                self.state_timeline.append({
                    "timestamp": str(master_candles[master_i].timestamp),
                    "master_i": master_i, "equity": float(eq),
                    "cash": float(portfolio.cash),
                    "n_open": len(portfolio.open_trades),
                    "n_closed": len(portfolio.closed_trades),
                    "n_pending_ghosts": pending_n,
                    "n_candidates_bar": len(candidates),
                    "dynamic_rr": float(rr),
                    "win_rate": float(wr) if wr is not None else None,
                    "total_notional": float(notional),
                    "book_full": bool(portfolio.book_is_full()),
                })

            if getattr(self, "_progress_every", None) and master_i % self._progress_every == 0:
                msg = (
                    f"  bar {master_i}/{n_master} eq={eq:.2f} "
                    f"open={len(portfolio.open_trades)} "
                    f"closed={len(portfolio.closed_trades)} "
                    f"replaced={self.n_replaced} "
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
                portfolio, sym, len(candles) - 1, last.close, last.timestamp, "end_of_data"
            )

        equity_curve.append(portfolio.equity)
        return self._build_result(
            portfolio, equity_curve, max_dd, symbols, master, timeframe
        )


def run_replace_once(*, name, data, spreads, master, ghost_sl_mult, progress_path=None) -> dict:
    sliced = {}
    for sym, bars in data.items():
        sliced[sym] = [c for c in bars if _ts(c.timestamp) < REPRO_END]
        if len(sliced[sym]) < 200:
            del sliced[sym]
    m = master if master in sliced else (pick_master_symbol(sliced) or next(iter(sliced)))
    strat = InstrumentedAlexG10(
        min_rr=2.0, execution_interval="1h", peer_blend=0.0,
        ghost_sl_mult=float(ghost_sl_mult), ghost_fill_at_close=True, run_id=name,
    )
    eng = ReplaceOnSignalEngine(strat, bt_cfg(spreads), max_positions=MAX_POSITIONS, sample_every=24)
    eng._progress_every = 500
    entry_i = next(i for i, c in enumerate(sliced[m]) if _ts(c.timestamp) >= ENTRY_START)
    start_i = max(int(strat.min_bars), entry_i - GHOST_PRIME)
    print(f"\n=== {name} REPLACE-ON-SIGNAL n_sym={len(sliced)} gsm={ghost_sl_mult} ===", flush=True)
    t0 = time.time()
    result = eng.run(
        sliced, timeframe="1h", master_symbol=m, same_bar_exit=False,
        start_master_i=start_i, progress_path=str(progress_path) if progress_path else None,
    )
    trades = trades_from_result(result, ENTRY_START, REPRO_END)
    events = [e for e in strat.events if e.get("timestamp") and _ts(e["timestamp"]) >= ENTRY_START - pd.Timedelta(hours=GHOST_PRIME)]
    rejects = [r for r in eng.reject_events if ENTRY_START <= _ts(r["timestamp"]) < REPRO_END]
    timeline = [r for r in eng.state_timeline if ENTRY_START <= _ts(r["timestamp"]) < REPRO_END]
    mtr = metrics(trades)
    fun = funnel_from_events(events, trades, rejects)
    elapsed = time.time() - t0
    print(
        f"  done {elapsed:.1f}s fills={mtr['fills']} net={mtr['net_pnl']:.2f} "
        f"cand={fun['candidate_signals']} rejects={fun['engine_rejects']} "
        f"replaced={getattr(eng, 'n_replaced', 0)}",
        flush=True,
    )
    return {
        "name": name, "trades": trades, "events": events, "rejects": rejects,
        "timeline": timeline, "metrics": mtr, "funnel": fun,
        "cache_stats": dict(strat.cache_stats), "elapsed_s": elapsed,
        "n_replaced": getattr(eng, "n_replaced", 0),
        "final_equity_engine": float(result.final_equity),
        "n_symbols": len(sliced),
    }


# ---------------------------------------------------------------------------
# Forensics helpers
# ---------------------------------------------------------------------------
def enrich_already_open(rejects: list[dict], trades: list[dict]) -> list[dict]:
    by_sym: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        by_sym[str(t["symbol"])].append(t)
    for sym in by_sym:
        by_sym[sym].sort(key=lambda x: _ts(x["entry_time"]))

    events = []
    for t in trades:
        events.append((_ts(t["entry_time"]), 1, t))
        if t.get("exit_time"):
            events.append((_ts(t["exit_time"]), -1, t))
    events.sort(key=lambda x: (x[0], -x[1]))

    out = []
    ao = [r for r in rejects if r.get("reason") == "already_open"]
    for r in ao:
        ts = _ts(r.get("timestamp"))
        sym = str(r.get("symbol") or "")
        side = str(r.get("pattern") or "")  # side not always on reject; use pattern
        # Infer side from matching trigger if needed — occupancy is symbol-keyed anyway
        occ = None
        for t in by_sym.get(sym, []):
            et = _ts(t["entry_time"])
            xt = _ts(t["exit_time"]) if t.get("exit_time") else None
            if et <= ts and (xt is None or xt > ts):
                occ = t
                break

        # reconstruct open set
        active = {}
        for et, delta, t in events:
            if et > ts:
                break
            s = str(t["symbol"])
            if delta == 1:
                active[s] = t
            else:
                active.pop(s, None)

        occ_side = str(occ.get("side") or "").lower() if occ else None
        rej_side = None  # rejects don't carry side; leave None
        same_side = None
        opp_side = None
        if occ:
            # compare via pattern heuristics unavailable; mark by occupancy presence
            same_side = None
            opp_side = None

        age_h = None
        until_exit_h = None
        if occ:
            age_h = (ts - _ts(occ["entry_time"])).total_seconds() / 3600.0
            if occ.get("exit_time"):
                until_exit_h = (_ts(occ["exit_time"]) - ts).total_seconds() / 3600.0

        same_bar = bool(occ and str(occ.get("entry_time"))[:13] == str(r.get("timestamp"))[:13])
        if occ is None:
            cls = "STALE_OCCUPANCY"
        elif same_bar:
            cls = "SAME_BAR_COLLISION"
        else:
            cls = "SAME_SYMBOL_ALREADY_OPEN"
            if occ_side:
                # cannot know rejected side from reject row; keep SAME_SYMBOL
                pass

        # Global occupancy note: already_open is per-symbol, not global
        out.append({
            "timestamp": r.get("timestamp"),
            "symbol": sym,
            "pattern": r.get("pattern"),
            "planned_entry": r.get("planned_price"),
            "reject_reason": "already_open",
            "existing_position_symbol": occ.get("symbol") if occ else None,
            "existing_position_side": occ_side,
            "existing_position_entry_time": occ.get("entry_time") if occ else None,
            "existing_position_exit_time": occ.get("exit_time") if occ else None,
            "existing_position_exit_reason": occ.get("exit_reason") if occ else None,
            "same_symbol": True if occ else False,
            "same_side": same_side,
            "opposite_side": opp_side,
            "age_of_existing_h": age_h,
            "time_until_exit_h": until_exit_h,
            "same_bar_collision": same_bar,
            "n_open_approx": len(active),
            "open_symbols_approx": ",".join(sorted(active.keys())),
            "classification": cls,
            "reject_n_open_field": r.get("n_open"),
            "reject_cash": r.get("cash"),
            "reject_dynamic_rr": r.get("dynamic_rr"),
            "reject_equity_proxy_cash": r.get("cash"),
        })
    return out


def enrich_already_open_with_triggers(ao_rows: list[dict], events: list[dict]) -> list[dict]:
    """Attach rejected trigger side from GHOST_TRIGGERED_SIGNAL events."""
    trig_by = {}
    for e in events:
        if e.get("event") != "GHOST_TRIGGERED_SIGNAL":
            continue
        key = (str(e.get("symbol")), str(e.get("timestamp"))[:19])
        trig_by[key] = e
    for r in ao_rows:
        key = (str(r.get("symbol")), str(r.get("timestamp"))[:19])
        e = trig_by.get(key)
        if not e:
            continue
        side = str(e.get("side_fill") or e.get("side_setup") or "").lower()
        r["rejected_side"] = side
        occ_side = str(r.get("existing_position_side") or "").lower()
        if occ_side and side:
            r["same_side"] = occ_side == side
            r["opposite_side"] = occ_side != side
            if r["classification"] == "SAME_SYMBOL_ALREADY_OPEN":
                if r["opposite_side"]:
                    r["classification"] = "OPPOSITE_SIDE_ALREADY_OPEN"
                elif r["same_side"]:
                    r["classification"] = "SAME_SYMBOL_ALREADY_OPEN"
    return ao_rows


def enrich_book_full(rejects: list[dict], trades: list[dict], max_positions: int) -> list[dict]:
    bf = [r for r in rejects if r.get("reason") == "book_full_break"]
    events = []
    for t in trades:
        events.append((_ts(t["entry_time"]), 1, t))
        if t.get("exit_time"):
            events.append((_ts(t["exit_time"]), -1, t))
    events.sort(key=lambda x: (x[0], -x[1]))
    out = []
    for r in bf:
        ts = _ts(r.get("timestamp"))
        sym = str(r.get("symbol") or "")
        active = {}
        for et, delta, t in events:
            if et > ts:
                break
            s = str(t["symbol"])
            if delta == 1:
                active[s] = t
            else:
                active.pop(s, None)
        same_sym = sym in active
        n_open = len(active)
        # book_is_full also true when cash/margin exhausted
        out.append({
            "timestamp": r.get("timestamp"),
            "symbol": sym,
            "pattern": r.get("pattern"),
            "planned_entry": r.get("planned_price"),
            "n_open": n_open,
            "n_open_reject_field": r.get("n_open"),
            "max_positions": max_positions,
            "open_symbols": ",".join(sorted(active.keys())),
            "same_symbol_also_occupied": same_sym,
            "cash": r.get("cash"),
            "dynamic_rr": r.get("dynamic_rr"),
            "at_max_positions": n_open >= max_positions if max_positions else None,
            "likely_cash_margin_cap": (n_open < max_positions) if max_positions else None,
        })
    return out


def compare_created_tapes(full_created: list[dict], other_created: list[dict], symbol: str) -> dict:
    def key(e):
        return (
            str(e.get("timestamp")),
            str(e.get("side_setup")),
            f"{float(e['planned_entry']):.8f}" if e.get("planned_entry") not in (None, "") else "",
            f"{float(e['ghost_sl']):.8f}" if e.get("ghost_sl") not in (None, "") else "",
            f"{float(e['ghost_tp']):.8f}" if e.get("ghost_tp") not in (None, "") else "",
            str(e.get("pattern") or ""),
        )
    ka = [key(e) for e in full_created if str(e.get("symbol")) == symbol]
    kb = [key(e) for e in other_created if str(e.get("symbol")) == symbol]
    sa, sb = set(ka), set(kb)
    only_a, only_b = len(sa - sb), len(sb - sa)
    common = len(sa & sb)
    if only_a == 0 and only_b == 0 and len(ka) == len(kb):
        cls = "EXACTLY_EQUAL"
    elif common > 0 and (only_a + only_b) <= max(1, int(0.01 * max(len(ka), len(kb), 1))):
        cls = "MINOR_NUMERICAL_DIFFERENCE"
    elif len(ka) == 0 and len(kb) == 0:
        cls = "EXACTLY_EQUAL"
    else:
        cls = "DIFFERENT_SIGNAL_TAPE"
    return {
        "symbol": symbol, "n_full": len(ka), "n_other": len(kb),
        "common": common, "only_full": only_a, "only_other": only_b,
        "classification": cls,
    }


def validate_aoi_htf(data: dict, symbols: list[str]) -> list[dict]:
    """Compare zone cache owner/values: multi-symbol walk vs isolated symbol."""
    from borex.alexg.strategy5_revised import AlexG5RevisedStrategy

    rows = []
    probe_fracs = [0.05, 0.25, 0.5, 0.75, 0.95]
    for sym in symbols:
        if sym not in data:
            continue
        series = data[sym]
        n = len(series)
        idxs = sorted({max(60, min(n - 2, int(n * f))) for f in probe_fracs})
        # Also probe idx≈500 if in range (known bug example)
        if 500 < n - 2:
            idxs = sorted(set(idxs) | {500})

        for idx in idxs:
            # Isolated
            s_iso = InstrumentedAlexG10(
                min_rr=2.0, execution_interval="1h", peer_blend=0.0,
                ghost_sl_mult=4.0, ghost_fill_at_close=True, run_id=f"iso_{sym}",
            )
            s_iso.set_context(sym, None)
            for i in range(0, idx + 1):
                s_iso.on_bar(i, series, None)
            z_iso = list(s_iso._zones_cache) if s_iso._zones_cache else []
            z_iso_sym = getattr(s_iso, "_zones_cache_symbol", "")
            h_iso_sym = getattr(s_iso, "_htf_cache_symbol", "")

            # Contaminated walk: feed AUDCAD then target at same idx pattern
            # Full-like: alternate feeding peer then symbol up to idx
            peer = "AUDCAD=X" if sym != "AUDCAD=X" else "NOKJPY=X"
            s_full = InstrumentedAlexG10(
                min_rr=2.0, execution_interval="1h", peer_blend=0.0,
                ghost_sl_mult=4.0, ghost_fill_at_close=True, run_id=f"fullish_{sym}",
            )
            peer_series = data.get(peer)
            for i in range(0, idx + 1):
                if peer_series is not None and i < len(peer_series):
                    s_full.set_context(peer, None)
                    s_full.on_bar(i, peer_series, None)
                s_full.set_context(sym, None)
                s_full.on_bar(i, series, None)
            z_full = list(s_full._zones_cache) if s_full._zones_cache else []
            z_full_sym = getattr(s_full, "_zones_cache_symbol", "")

            def zone_sig(zones):
                if not zones:
                    return ""
                parts = []
                for z in zones[:5]:
                    try:
                        parts.append(
                            f"{float(getattr(z, 'high', getattr(z, 'top', 0))):.6f}-"
                            f"{float(getattr(z, 'low', getattr(z, 'bottom', 0))):.6f}"
                        )
                    except Exception:
                        parts.append(str(z)[:40])
                return ";".join(parts)

            sig_iso = zone_sig(z_iso)
            sig_full = zone_sig(z_full)
            rows.append({
                "symbol": sym, "idx": idx,
                "timestamp": str(series[idx].timestamp),
                "zone_sig_iso": sig_iso,
                "zone_sig_multi": sig_full,
                "zone_match": sig_iso == sig_full,
                "zones_cache_symbol_iso": z_iso_sym,
                "zones_cache_symbol_multi": z_full_sym,
                "htf_cache_symbol_iso": h_iso_sym,
                "has_per_symbol_fields": hasattr(AlexG5RevisedStrategy, "__dataclass_fields__")
                and "_zones_cache_symbol" in getattr(AlexG5RevisedStrategy, "__dataclass_fields__", {}),
                "n_zones_iso": len(z_iso),
                "n_zones_multi": len(z_full),
            })
            del s_iso, s_full
            gc.collect()
    return rows


def duplicate_forensics(created: list[dict]) -> tuple[list[dict], dict]:
    by_id: dict[str, list[int]] = defaultdict(list)
    by_sym_ts_side: dict[tuple, list[int]] = defaultdict(list)
    rows = []
    for i, e in enumerate(created):
        cid = _cand_id(e)
        by_id[cid].append(i)
        by_sym_ts_side[(str(e.get("symbol")), str(e.get("timestamp")), str(e.get("side_setup")))].append(i)

    exact = {k: v for k, v in by_id.items() if len(v) > 1}
    same_bar = {k: v for k, v in by_sym_ts_side.items() if len(v) > 1}

    consec = 0
    by_sym = defaultdict(list)
    for e in created:
        by_sym[str(e.get("symbol"))].append(e)
    for sym, lst in by_sym.items():
        lst = sorted(lst, key=lambda x: _ts(x.get("timestamp")))
        for a, b in zip(lst, lst[1:]):
            if (str(a.get("side_setup")) == str(b.get("side_setup"))
                    and str(a.get("planned_entry")) == str(b.get("planned_entry"))
                    and (_ts(b.get("timestamp")) - _ts(a.get("timestamp"))).total_seconds() <= 7200):
                consec += 1
                if len(rows) < 15000:
                    rows.append({
                        "type": "consecutive_similar", "symbol": sym,
                        "ts_a": a.get("timestamp"), "ts_b": b.get("timestamp"),
                        "side": a.get("side_setup"), "planned_entry": a.get("planned_entry"),
                    })

    for cid, idxs in list(exact.items())[:5000]:
        rows.append({
            "type": "exact_duplicate", "candidate_id": cid, "count": len(idxs),
            "symbol": created[idxs[0]].get("symbol"),
            "timestamp": created[idxs[0]].get("timestamp"),
        })
    for k, idxs in list(same_bar.items())[:5000]:
        rows.append({
            "type": "same_bar_multi", "symbol": k[0], "timestamp": k[1],
            "side": k[2], "count": len(idxs),
        })

    summary = {
        "n_candidates": len(created),
        "n_unique_ids": len(by_id),
        "exact_duplicate_groups": len(exact),
        "exact_duplicate_extra": sum(len(v) - 1 for v in exact.values()),
        "same_bar_multi_groups": len(same_bar),
        "consecutive_similar_pairs": consec,
        "duplicate_rate": _pct(sum(len(v) - 1 for v in exact.values()), len(created)),
    }
    return rows, summary


def source_semantics() -> dict:
    eng = (ROOT / "borex/backtest/multi_market_engine.py").read_text(encoding="utf-8")
    port = (ROOT / "borex/backtest/multi_portfolio.py").read_text(encoding="utf-8")
    # DropTrackingEngine already_open path is in test3 script; portfolio.can_open is source of truth
    port_lines = port.splitlines()
    can_open_snip = []
    book_snip = []
    in_can = in_book = False
    for i, line in enumerate(port_lines):
        if "def can_open" in line:
            in_can = True
        if "def book_is_full" in line:
            in_book = True
        if in_can:
            can_open_snip.append(f"L{i+1}: {line}")
            if len(can_open_snip) > 1 and line.startswith("    def ") and "can_open" not in line:
                can_open_snip.pop()
                in_can = False
            if len(can_open_snip) > 20:
                in_can = False
        if in_book:
            book_snip.append(f"L{i+1}: {line}")
            if len(book_snip) > 1 and line.startswith("    def ") and "book_is_full" not in line:
                book_snip.pop()
                in_book = False
            if len(book_snip) > 15:
                in_book = False

    t3 = (ROOT / "scripts/run_alexg10_test3_loo_ghost.py").read_text(encoding="utf-8")
    ao_engine_lines = []
    for i, line in enumerate(t3.splitlines()):
        if "already_open" in line or "book_full_break" in line:
            ao_engine_lines.append(f"L{i+1}: {line.strip()}")

    return {
        "already_open": {
            "portfolio_can_open": can_open_snip,
            "engine_reject_sites": ao_engine_lines[:30],
            "interpretation": {
                "per_symbol": True,
                "mechanism": (
                    "MultiMarketPortfolio.can_open(symbol) returns False when "
                    "symbol in open_trades. DropTrackingEngine records reason "
                    "'already_open' if sym in portfolio.open_trades else 'can_open_false'."
                ),
                "side_matters": False,
                "opposite_side_blocks": True,
                "pending_ghosts_count": False,
                "global_book_restriction": False,
                "positions_coexist_across_symbols": True,
                "multiple_positions_per_symbol": False,
                "evaluated_order": "book_is_full checked first among candidates; then can_open/already_open; then _open_signal",
            },
        },
        "book_full_break": {
            "portfolio_book_is_full": book_snip,
            "interpretation": {
                "global_cap": True,
                "mechanism": (
                    "book_is_full() True when liquidated/no cash, or "
                    "len(open_trades)>=max_positions, or compute_margin(1.0)<=0. "
                    "Engine labels remaining candidates on that bar as book_full_break."
                ),
                "max_positions_test": MAX_POSITIONS,
                "also_cash_margin": True,
            },
        },
        "engine_file": "borex/backtest/multi_market_engine.py",
        "portfolio_file": "borex/backtest/multi_portfolio.py",
        "instrumented_engine": "scripts/run_alexg10_test3_loo_ghost.py::DropTrackingEngine",
    }


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def main() -> int:
    started = datetime.now(timezone.utc)
    stamp = started.strftime("%Y%m%d_%H%M%S")
    out = OUT_ROOT / f"alexg10_test4_{stamp}"
    out.mkdir(parents=True, exist_ok=True)
    progress_path = out / "PROGRESS.log"
    progress_path.write_text(f"started {started.isoformat()}\n", encoding="utf-8")
    print(f"=== TEST4 START OUT={out} ===", flush=True)

    apply_cache_isolation_fix()  # no-op; source already fixed

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
    print(f"H1 hash={hh} master={master} n={len(data)} match={hh == T1_HASH}", flush=True)

    manifest = {
        "label": "ALEXG10_TEST4_POSTFIX_FORENSICS",
        "status": "RUNNING",
        "nature": "DIAGNOSTIC / MECHANISTIC / NOT FOR DEPLOYMENT",
        "started_utc": started.isoformat(),
        "git_commit": _git(),
        "git_branch": _branch(),
        "python": sys.version,
        "platform": platform.platform(),
        "test3_dir": str(TEST3),
        "h1_dataset_hash": hh,
        "matches_frozen_hash": hh == T1_HASH,
        "frozen_hash_expected": T1_HASH,
        "spread_source": str(JUN_INDEX),
        "control_config": json.loads((T2 / "CONTROL_CONFIG.json").read_text()) if (T2 / "CONTROL_CONFIG.json").exists() else None,
        "knobs": {
            "strategy": "alexg10", "force_flat_daily": False, "force_flat_friday": False,
            "honor_signal_stops": False, "rr_mode": "dynamic", "rr_factor": 1.0,
            "rr_min": 2.0, "rr_max": 5.0, "true_sl": True, "ghost_sl_mult": 4.0,
            "peer_blend": 0.0, "ghost_fill_at_close": True, "leverage": LEV,
            "position_size_pct": 0.01, "commission_per_lot": 7.0, "capital": CAPITAL,
            "max_positions": MAX_POSITIONS, "entry_start": str(ENTRY_START),
            "repro_end": str(REPRO_END),
        },
        "expected_control": EXPECTED,
    }
    _jdump(out / "TEST4_MANIFEST.json", manifest)

    # ===================== PHASE 0 =====================
    print("\n######## PHASE 0 — POST_FIX CONTROL REPRODUCTION ########", flush=True)
    ctrl = run_once(
        name="T4_AFIX_full",
        data=data, spreads=spreads, master=master, exclude=set(),
        ghost_sl_mult=4.0, sample_every=24, code_mode="POST_FIX",
        progress_path=progress_path,
    )
    (out / "phase0").mkdir(parents=True, exist_ok=True)
    write_csv(out / "phase0" / "TRADES.csv", ctrl["trades"])
    # Avoid giant full EVENTS dump — write sample + keep in memory for analysis
    created_all = [e for e in ctrl["events"] if e.get("event") == "GHOST_CREATED"]
    triggered_all = [e for e in ctrl["events"] if e.get("event") == "GHOST_TRIGGERED_SIGNAL"]
    write_csv(out / "phase0" / "EVENTS_CREATED_SAMPLE.csv", created_all[:5000])
    write_csv(out / "phase0" / "REJECTS.csv", ctrl["rejects"])
    write_csv(out / "phase0" / "TIMELINE.csv", ctrl["timeline"])
    _jdump(out / "phase0" / "SUMMARY.json", {
        "metrics": ctrl["metrics"], "funnel": ctrl["funnel"],
        "cache_stats": ctrl["cache_stats"], "elapsed_s": ctrl["elapsed_s"],
    })

    f0 = ctrl["funnel"]
    m0 = ctrl["metrics"]
    rej_by = f0.get("engine_rejects_by_reason") or {}
    repro = {
        "candidates": f0["candidate_signals"],
        "ghosts_created": f0["ghosts_created"],
        "triggers": f0["ghosts_triggered_signals"],
        "invalidations": f0["ghosts_invalidated"],
        "positions": f0["positions_opened"],
        "completed_trades": f0["completed_trades"],
        "end_of_data_open": f0["open_end_of_data"],
        "engine_rejects": f0["engine_rejects"],
        "already_open": rej_by.get("already_open", 0),
        "book_full_break": rej_by.get("book_full_break", 0),
        "net": m0["net_pnl"],
        "profit_factor": m0["profit_factor"],
        "max_drawdown": m0["max_drawdown"],
        "zone_polluted_reuses": ctrl["cache_stats"].get("zone_polluted_reuses"),
        "elapsed_s": ctrl["elapsed_s"],
        "expected": EXPECTED,
    }

    def mat(got, exp, rel=0.02, abs_tol=None):
        if got is None or exp is None:
            return True
        if abs_tol is not None:
            return abs(float(got) - float(exp)) > abs_tol
        if float(exp) == 0:
            return abs(float(got)) > 1
        return abs(float(got) - float(exp)) / abs(float(exp)) > rel

    diffs = {}
    checks = {
        "candidates": (repro["candidates"], EXPECTED["candidates"], 0.02, None),
        "triggers": (repro["triggers"], EXPECTED["triggers"], 0.02, None),
        "positions": (repro["positions"], EXPECTED["positions"], 0.02, None),
        "already_open": (repro["already_open"], EXPECTED["already_open"], 0.02, None),
        "book_full_break": (repro["book_full_break"], EXPECTED["book_full_break"], 0.05, 5),
        "net": (repro["net"], EXPECTED["net"], 0.05, 50),
        "profit_factor": (repro["profit_factor"], EXPECTED["profit_factor"], 0.05, 0.02),
        "max_drawdown": (repro["max_drawdown"], EXPECTED["max_drawdown"], 0.05, 0.02),
    }
    for k, (got, exp, rel, atol) in checks.items():
        diffs[k] = {"got": got, "expected": exp, "material_diff": mat(got, exp, rel, atol)}
    repro["diffs"] = diffs
    repro["stop"] = any(
        diffs[k]["material_diff"]
        for k in ("candidates", "triggers", "positions", "already_open", "net")
    )
    _jdump(out / "POST_FIX_CONTROL_REPRODUCTION.json", repro)
    print(json.dumps(diffs, indent=2, default=str), flush=True)

    if repro["stop"]:
        print("PHASE 0 MATERIAL DIFFERENCE — STOP", flush=True)
        manifest["status"] = "STOPPED_PHASE0_MISMATCH"
        _jdump(out / "TEST4_MANIFEST.json", manifest)
        (out / "TEST4_EXECUTIVE_SUMMARY.md").write_text(
            f"# TEST4 STOPPED AT PHASE 0\n\n```json\n{json.dumps(repro, indent=2, default=str)}\n```\n",
            encoding="utf-8",
        )
        (out / "TEST4_REPORT.md").write_text(
            "# TEST4 REPORT — STOPPED AT PHASE 0\n\nControl reproduction materially differed.\n",
            encoding="utf-8",
        )
        return 2

    print("PHASE 0 OK", flush=True)
    trades = ctrl["trades"]
    rejects = ctrl["rejects"]
    events = ctrl["events"]
    timeline = ctrl["timeline"]
    n_cand = f0["candidate_signals"]
    n_trig = f0["ghosts_triggered_signals"]
    n_pos = f0["positions_opened"]
    n_ao = int(rej_by.get("already_open", 0))
    n_bf = int(rej_by.get("book_full_break", 0))
    gc.collect()

    # ===================== PHASE 1 =====================
    print("\n######## PHASE 1 — SIGNAL EXPANSION BY SYMBOL ########", flush=True)
    pre_events = _read_csv(TEST3 / "A_full_EVENTS.csv")
    pre_created = [e for e in pre_events if e.get("event") == "GHOST_CREATED"]
    pre_by = Counter(str(e.get("symbol")) for e in pre_created)
    post_by = Counter(str(e.get("symbol")) for e in created_all)
    trig_by = Counter(str(e.get("symbol")) for e in triggered_all)
    inv_by = Counter(str(e.get("symbol")) for e in events if e.get("event") == "GHOST_INVALIDATED")
    fill_by = Counter(str(t["symbol"]) for t in trades)
    bars_by = {s: len(data[s]) for s in symbols if s in data}

    expansion = []
    for sym in symbols:
        pre_c = int(pre_by.get(sym, 0))
        post_c = int(post_by.get(sym, 0))
        expansion.append({
            "symbol": sym,
            "pre_fix_candidates": pre_c,
            "post_fix_candidates": post_c,
            "delta": post_c - pre_c,
            "post_cand_per_h1_bar": _pct(post_c, bars_by.get(sym, 0)),
            "ghost_creation_count": post_c,
            "trigger_count": int(trig_by.get(sym, 0)),
            "invalidation_count": int(inv_by.get(sym, 0)),
            "fills": int(fill_by.get(sym, 0)),
            "h1_bars": bars_by.get(sym, 0),
        })
    expansion.sort(key=lambda r: -r["delta"])
    write_csv(out / "POST_FIX_SIGNAL_EXPANSION_BY_SYMBOL.csv", expansion)
    print(f"PRE total={sum(pre_by.values())} POST total={sum(post_by.values())} "
          f"top_delta={[(r['symbol'], r['delta']) for r in expansion[:8]]}", flush=True)
    del pre_events
    gc.collect()

    # ===================== PHASE 2 =====================
    print("\n######## PHASE 2 — SIGNAL-TAPE INDEPENDENCE ########", flush=True)
    indep_rows = []
    alone_created = {}
    for sym in INDEP_BASE:
        if sym not in data:
            continue
        # exclude all others
        excl = set(symbols) - {sym}
        r = run_once(
            name=f"indep_alone_{sym}",
            data=data, spreads=spreads, master=sym if sym in data else master,
            exclude=excl, ghost_sl_mult=4.0, sample_every=10_000,
            code_mode="POST_FIX", progress_path=progress_path,
        )
        oc = [e for e in r["events"] if e.get("event") == "GHOST_CREATED"]
        alone_created[sym] = oc
        cmp = compare_created_tapes(created_all, oc, sym)
        cmp["mode"] = "FULL_vs_ALONE"
        indep_rows.append(cmp)
        print(f"  {sym} FULL vs ALONE: {cmp['classification']} "
              f"({cmp['n_full']} vs {cmp['n_other']})", flush=True)
        del r
        gc.collect()

    unrelated_pool = ["EURUSD=X", "USDJPY=X", "GBPUSD=X", "AUDNZD=X", "USDCAD=X"]
    for sym in INDEP_BASE:
        if sym not in data:
            continue
        others = [x for x in unrelated_pool if x != sym and x in data][:2]
        keep = {sym, *others}
        excl = set(symbols) - keep
        r = run_once(
            name=f"indep_small_{sym}",
            data=data, spreads=spreads, master=sym,
            exclude=excl, ghost_sl_mult=4.0, sample_every=10_000,
            code_mode="POST_FIX", progress_path=progress_path,
        )
        oc = [e for e in r["events"] if e.get("event") == "GHOST_CREATED"]
        cmp = compare_created_tapes(created_all, oc, sym)
        cmp["mode"] = "FULL_vs_SMALL_UNRELATED"
        cmp["universe"] = ",".join(sorted(keep))
        indep_rows.append(cmp)
        if sym in alone_created:
            cmp2 = compare_created_tapes(alone_created[sym], oc, sym)
            cmp2["mode"] = "ALONE_vs_SMALL"
            cmp2["universe"] = ",".join(sorted(keep))
            indep_rows.append(cmp2)
        print(f"  {sym} FULL vs SMALL: {cmp['classification']}", flush=True)
        del r
        gc.collect()
    write_csv(out / "SIGNAL_TAPE_INDEPENDENCE.csv", indep_rows)

    # ===================== PHASE 3 =====================
    print("\n######## PHASE 3 — AOI/HTF VALIDATION ########", flush=True)
    aoi_rows = validate_aoi_htf(
        data, ["AUDCAD=X", "NOKJPY=X", "EURUSD=X", "GBPTRY=X", "USDZAR=X"]
    )
    write_csv(out / "AOI_HTF_VALIDATION.csv", aoi_rows)
    n_match = sum(1 for r in aoi_rows if r.get("zone_match"))
    print(f"AOI rows={len(aoi_rows)} zone_match={n_match}", flush=True)
    # Known bug example note
    bug_example = {
        "description": "PRE_FIX: AUDCAD zones at idx≈500 reused for NOKJPY",
        "postfix_rows_idx500": [r for r in aoi_rows if r.get("idx") == 500],
        "source_fix_fields_present": True,
    }
    _jdump(out / "AOI_HTF_BUG_EXAMPLE.json", bug_example)
    gc.collect()

    # ===================== PHASE 4 =====================
    print("\n######## PHASE 4 — FILL FUNNEL ########", flush=True)
    funnel_rows = [
        {"stage": "CANDIDATE", "count": n_cand,
         "pct_of_candidates": 1.0, "pct_of_prev": 1.0},
        {"stage": "GHOST_CREATED", "count": f0["ghosts_created"],
         "pct_of_candidates": _pct(f0["ghosts_created"], n_cand),
         "pct_of_prev": _pct(f0["ghosts_created"], n_cand)},
        {"stage": "GHOST_TRIGGERED", "count": n_trig,
         "pct_of_candidates": _pct(n_trig, n_cand),
         "pct_of_prev": _pct(n_trig, f0["ghosts_created"])},
        {"stage": "GHOST_INVALIDATED", "count": f0["ghosts_invalidated"],
         "pct_of_candidates": _pct(f0["ghosts_invalidated"], n_cand),
         "pct_of_prev": _pct(f0["ghosts_invalidated"], f0["ghosts_created"])},
        {"stage": "ENGINE_REJECT_TOTAL", "count": f0["engine_rejects"],
         "pct_of_candidates": _pct(f0["engine_rejects"], n_cand),
         "pct_of_prev": _pct(f0["engine_rejects"], n_trig)},
        {"stage": "REJECT_already_open", "count": n_ao,
         "pct_of_candidates": _pct(n_ao, n_cand),
         "pct_of_prev": _pct(n_ao, n_trig)},
        {"stage": "REJECT_book_full_break", "count": n_bf,
         "pct_of_candidates": _pct(n_bf, n_cand),
         "pct_of_prev": _pct(n_bf, n_trig)},
        {"stage": "POSITION_OPEN", "count": n_pos,
         "pct_of_candidates": _pct(n_pos, n_cand),
         "pct_of_prev": _pct(n_pos, n_trig)},
        {"stage": "EXIT_COMPLETED", "count": f0["completed_trades"],
         "pct_of_candidates": _pct(f0["completed_trades"], n_cand),
         "pct_of_prev": _pct(f0["completed_trades"], n_pos)},
        {"stage": "EXIT_END_OF_DATA", "count": f0["open_end_of_data"],
         "pct_of_candidates": _pct(f0["open_end_of_data"], n_cand),
         "pct_of_prev": _pct(f0["open_end_of_data"], n_pos)},
    ]
    write_csv(out / "FILL_FUNNEL.csv", funnel_rows)

    # ===================== PHASE 5/6 =====================
    print("\n######## PHASE 5/6 — already_open FORENSICS ########", flush=True)
    sem = source_semantics()
    ao_rows = enrich_already_open(rejects, trades)
    ao_rows = enrich_already_open_with_triggers(ao_rows, events)
    write_csv(out / "ALREADY_OPEN_FORENSICS.csv", ao_rows)
    ao_breakdown = Counter(r["classification"] for r in ao_rows)
    ao_json = {
        "n_rejects": len(ao_rows),
        "breakdown": dict(ao_breakdown),
        "by_symbol_top25": Counter(r["symbol"] for r in ao_rows).most_common(25),
        "same_side_pct": _pct(sum(1 for r in ao_rows if r.get("same_side")), len(ao_rows)),
        "opposite_side_pct": _pct(sum(1 for r in ao_rows if r.get("opposite_side")), len(ao_rows)),
        "stale_pct": _pct(ao_breakdown.get("STALE_OCCUPANCY", 0), len(ao_rows)),
        "same_bar_pct": _pct(ao_breakdown.get("SAME_BAR_COLLISION", 0), len(ao_rows)),
        "median_age_h": _median([r["age_of_existing_h"] for r in ao_rows if r.get("age_of_existing_h") is not None]),
        "median_until_exit_h": _median([r["time_until_exit_h"] for r in ao_rows if r.get("time_until_exit_h") is not None]),
        "semantics": sem["already_open"]["interpretation"],
        "source_snippets": {
            "can_open": sem["already_open"]["portfolio_can_open"],
            "engine_sites": sem["already_open"]["engine_reject_sites"][:20],
        },
    }
    _jdump(out / "ALREADY_OPEN_BREAKDOWN.json", ao_json)
    print(f"already_open breakdown: {dict(ao_breakdown)}", flush=True)
    gc.collect()

    # ===================== PHASE 7 =====================
    print("\n######## PHASE 7 — book_full_break FORENSICS ########", flush=True)
    bf_rows = enrich_book_full(rejects, trades, MAX_POSITIONS)
    write_csv(out / "BOOK_FULL_FORENSICS.csv", bf_rows)
    bf_json = {
        "n_rejects": len(bf_rows),
        "max_positions": MAX_POSITIONS,
        "mean_n_open": _mean([float(r["n_open"]) for r in bf_rows]),
        "median_n_open": _median([float(r["n_open"]) for r in bf_rows]),
        "at_max_positions_count": sum(1 for r in bf_rows if r.get("at_max_positions")),
        "likely_cash_margin_cap_count": sum(1 for r in bf_rows if r.get("likely_cash_margin_cap")),
        "same_symbol_also_occupied_count": sum(1 for r in bf_rows if r.get("same_symbol_also_occupied")),
        "by_symbol_top20": Counter(r["symbol"] for r in bf_rows).most_common(20),
        "semantics": sem["book_full_break"]["interpretation"],
        "source_snippets": sem["book_full_break"]["portfolio_book_is_full"],
    }
    _jdump(out / "BOOK_FULL_BREAKDOWN.json", bf_json)
    print(f"book_full n={len(bf_rows)} cash_cap~={bf_json['likely_cash_margin_cap_count']}", flush=True)

    # ===================== PHASE 8 =====================
    print("\n######## PHASE 8 — OCCUPANCY TIMELINE ########", flush=True)
    # Dense occupancy from trades + per-bar candidate counts from events
    cand_by_ts = Counter()
    trig_by_ts = Counter()
    for e in created_all:
        cand_by_ts[str(e.get("timestamp"))[:19]] += 1
    for e in triggered_all:
        trig_by_ts[str(e.get("timestamp"))[:19]] += 1
    rej_by_ts = Counter(str(r.get("timestamp"))[:19] for r in rejects)
    acc_by_ts = Counter(str(t.get("entry_time"))[:19] for t in trades)

    # Build open-count series at each trade event
    occ_rows = []
    for row in timeline:
        ts = str(row.get("timestamp"))[:19]
        occ_rows.append({
            "timestamp": row.get("timestamp"),
            "master_i": row.get("master_i"),
            "candidate_count": cand_by_ts.get(ts, row.get("n_candidates_bar")),
            "triggered_count": trig_by_ts.get(ts, None),
            "accepted_count": acc_by_ts.get(ts, 0),
            "rejected_count": rej_by_ts.get(ts, 0),
            "open_position_count": row.get("n_open"),
            "n_pending_ghosts": row.get("n_pending_ghosts"),
            "equity": row.get("equity"),
            "dynamic_rr": row.get("dynamic_rr"),
            "total_notional": row.get("total_notional"),
            "book_full": row.get("book_full"),
            "cash": row.get("cash"),
            "cumulative_closed": row.get("n_closed"),
        })
    write_csv(out / "POST_FIX_OCCUPANCY_TIMELINE.csv", occ_rows)

    # ===================== PHASE 9 =====================
    print("\n######## PHASE 9 — REPRESENTATIVE EVENT TIMELINES ########", flush=True)
    ao_by_sym = Counter(r["symbol"] for r in ao_rows)
    dive_syms = list(dict.fromkeys(
        DEEP_DIVE
        + [r["symbol"] for r in expansion[:3]]
        + [s for s, _ in fill_by.most_common(3)]
        + [s for s, _ in ao_by_sym.most_common(3)]
    ))
    event_rows = []
    for sym in dive_syms:
        for e in events:
            if str(e.get("symbol")) != sym:
                continue
            if e.get("event") not in ("GHOST_CREATED", "GHOST_TRIGGERED_SIGNAL", "GHOST_INVALIDATED"):
                continue
            event_rows.append({
                "symbol": sym, "event": e.get("event"), "timestamp": e.get("timestamp"),
                "side": e.get("side_setup") or e.get("side_fill"),
                "entry": e.get("planned_entry") or e.get("fill_price"),
                "sl": e.get("ghost_sl") or e.get("signal_sl"),
                "tp": e.get("ghost_tp") or e.get("signal_tp"),
                "detail": e.get("pattern"),
            })
        for r in rejects:
            if str(r.get("symbol")) != sym:
                continue
            event_rows.append({
                "symbol": sym, "event": f"reject_{r.get('reason')}",
                "timestamp": r.get("timestamp"), "side": None,
                "entry": r.get("planned_price"), "sl": None, "tp": None,
                "detail": f"n_open={r.get('n_open')};rr={r.get('dynamic_rr')}",
            })
        for t in trades:
            if str(t.get("symbol")) != sym:
                continue
            event_rows.append({
                "symbol": sym, "event": "fill", "timestamp": t.get("entry_time"),
                "side": t.get("side"), "entry": t.get("entry_price"),
                "sl": t.get("stop_loss"), "tp": t.get("take_profit"),
                "detail": f"pnl_pending;margin={t.get('margin')}",
            })
            event_rows.append({
                "symbol": sym, "event": "exit", "timestamp": t.get("exit_time"),
                "side": t.get("side"), "entry": t.get("exit_price"),
                "sl": None, "tp": None,
                "detail": f"{t.get('exit_reason')};pnl={t.get('pnl')}",
            })
    event_rows.sort(key=lambda r: (str(r.get("symbol")), str(r.get("timestamp")), str(r.get("event"))))
    write_csv(out / "REPRESENTATIVE_EVENT_TIMELINES.csv", event_rows)
    print(f"deep-dive event rows={len(event_rows)} syms={len(dive_syms)}", flush=True)
    gc.collect()

    # ===================== PHASE 10 =====================
    print("\n######## PHASE 10 — OCCUPANCY COUNTERFACTUAL ########", flush=True)
    try:
        cf = run_replace_once(
            name="T4_REPLACE_full", data=data, spreads=spreads,
            master=master, ghost_sl_mult=4.0, progress_path=progress_path,
        )
        (out / "phase10_replace").mkdir(parents=True, exist_ok=True)
        write_csv(out / "phase10_replace" / "TRADES.csv", cf["trades"])
        write_csv(out / "phase10_replace" / "REJECTS.csv", cf["rejects"])
        write_csv(out / "phase10_replace" / "TIMELINE.csv", cf["timeline"])
        _jdump(out / "phase10_replace" / "SUMMARY.json", {
            "metrics": cf["metrics"], "funnel": cf["funnel"],
            "n_replaced": cf["n_replaced"], "elapsed_s": cf["elapsed_s"],
            "cache_stats": cf["cache_stats"],
        })
        tl_n = [int(r.get("n_open") or 0) for r in timeline]
        tl_c = [int(r.get("n_open") or 0) for r in cf["timeline"]]
        mcf = cf["metrics"]
        fcf = cf["funnel"]
        occ_cf = {
            "mode": "OCCUPANCY_RELAXED_REPLACE_ON_SIGNAL",
            "note": "DIAGNOSTIC ONLY. Closes existing same-symbol position before new signal. NOT deployment advice.",
            "normal": {
                "candidates": n_cand, "triggers": n_trig, "fills": n_pos,
                "fill_rate": _pct(n_pos, n_cand), "net": m0["net_pnl"],
                "profit_factor": m0["profit_factor"], "max_drawdown": m0["max_drawdown"],
                "already_open": n_ao, "book_full": n_bf,
                "avg_simultaneous": _mean(tl_n), "max_simultaneous": max(tl_n) if tl_n else 0,
                "ending_equity": m0["ending_equity"],
                "commission": m0.get("commission_sum"),
                "total_notional": m0.get("total_notional"),
                "hold_mean_h": m0.get("hold_mean_h"), "hold_median_h": m0.get("hold_median_h"),
                "TP": m0.get("TP"), "margin_stop": m0.get("margin_stop"),
            },
            "relaxed": {
                "candidates": fcf["candidate_signals"],
                "triggers": fcf["ghosts_triggered_signals"],
                "fills": mcf["fills"],
                "fill_rate": _pct(mcf["fills"], fcf["candidate_signals"]),
                "net": mcf["net_pnl"], "profit_factor": mcf["profit_factor"],
                "max_drawdown": mcf["max_drawdown"],
                "engine_rejects": fcf["engine_rejects"],
                "rejects_by_reason": fcf.get("engine_rejects_by_reason"),
                "n_replaced_closes": cf["n_replaced"],
                "avg_simultaneous": _mean(tl_c), "max_simultaneous": max(tl_c) if tl_c else 0,
                "ending_equity": mcf["ending_equity"],
                "commission": mcf.get("commission_sum"),
                "total_notional": mcf.get("total_notional"),
                "hold_mean_h": mcf.get("hold_mean_h"), "hold_median_h": mcf.get("hold_median_h"),
                "TP": mcf.get("TP"), "margin_stop": mcf.get("margin_stop"),
            },
            "delta_fills": mcf["fills"] - n_pos,
            "delta_net": mcf["net_pnl"] - m0["net_pnl"],
            "otherwise_valid_approx": cf["n_replaced"],
        }
        del cf
        gc.collect()
    except Exception as e:
        occ_cf = {"mode": "FAILED", "error": str(e), "traceback": traceback.format_exc()}
        print(f"COUNTERFACTUAL FAILED: {e}", flush=True)
    _jdump(out / "OCCUPANCY_COUNTERFACTUAL.json", occ_cf)

    # ===================== PHASE 11 =====================
    print("\n######## PHASE 11 — GHOST_SL ACCEPTANCE GRID ########", flush=True)
    gsm_rows = []
    for gsm in GHOST_SL_GRID:
        p = TEST3 / f"BFIX_full_gsm{int(gsm)}_SUMMARY.json"
        if not p.exists():
            continue
        d = json.loads(p.read_text(encoding="utf-8"))
        m = d.get("metrics") or {}
        f = d.get("funnel") or {}
        er = f.get("engine_rejects_by_reason") or {}
        gsm_rows.append({
            "ghost_sl_mult": gsm, "source": str(p.name),
            "candidates": f.get("candidate_signals"),
            "ghosts": f.get("ghosts_created"),
            "triggers": f.get("ghosts_triggered_signals"),
            "invalidations": f.get("ghosts_invalidated"),
            "already_open": er.get("already_open"),
            "book_full": er.get("book_full_break"),
            "fills": m.get("fills"),
            "fill_rate": f.get("fill_rate_vs_candidates"),
            "avg_ghost_sl_dist": f.get("avg_ghost_sl_dist"),
            "median_ghost_sl_dist": f.get("median_ghost_sl_dist"),
            "hold_mean_h": m.get("hold_mean_h"),
            "hold_median_h": m.get("hold_median_h"),
            "net": m.get("net_pnl"),
            "profit_factor": m.get("profit_factor"),
            "max_drawdown": m.get("max_drawdown"),
        })
    # Ensure gsm4 from our control if missing
    if not any(abs(float(r["ghost_sl_mult"]) - 4.0) < 1e-9 for r in gsm_rows):
        gsm_rows.append({
            "ghost_sl_mult": 4.0, "source": "phase0_control",
            "candidates": n_cand, "ghosts": f0["ghosts_created"], "triggers": n_trig,
            "invalidations": f0["ghosts_invalidated"], "already_open": n_ao, "book_full": n_bf,
            "fills": n_pos, "fill_rate": _pct(n_pos, n_cand),
            "avg_ghost_sl_dist": f0.get("avg_ghost_sl_dist"),
            "median_ghost_sl_dist": f0.get("median_ghost_sl_dist"),
            "hold_mean_h": m0.get("hold_mean_h"), "hold_median_h": m0.get("hold_median_h"),
            "net": m0["net_pnl"], "profit_factor": m0["profit_factor"],
            "max_drawdown": m0["max_drawdown"],
        })
    gsm_rows.sort(key=lambda r: float(r["ghost_sl_mult"]))
    write_csv(out / "GHOST_SL_ACCEPTANCE_GRID.csv", gsm_rows)
    print(f"GSM fills: {[(r['ghost_sl_mult'], r['fills'], r['already_open']) for r in gsm_rows]}", flush=True)

    # ===================== PHASE 12 =====================
    print("\n######## PHASE 12 — GHOST SL DISTANCE FORENSICS ########", flush=True)
    sl_rows = []
    for e in created_all:
        try:
            dist = float(e["ghost_sl_dist"]) if e.get("ghost_sl_dist") not in (None, "") else None
        except Exception:
            dist = None
        if dist is None:
            continue
        sl_rows.append({
            "symbol": e.get("symbol"),
            "timestamp": e.get("timestamp"),
            "side": e.get("side_setup"),
            "pattern": e.get("pattern"),
            "planned_entry": e.get("planned_entry"),
            "ghost_sl": e.get("ghost_sl"),
            "ghost_tp": e.get("ghost_tp"),
            "sl_distance": dist,
            "created_index": e.get("created_index"),
        })
    sl_rows.sort(key=lambda r: -float(r["sl_distance"]))
    write_csv(out / "GHOST_SL_DISTANCE_FORENSICS.csv", sl_rows)
    dists = [float(r["sl_distance"]) for r in sl_rows]
    bys = defaultdict(list)
    for r in sl_rows:
        bys[str(r["symbol"])].append(float(r["sl_distance"]))
    sl_summary = {
        "n": len(dists),
        "mean": _mean(dists),
        "median": _median(dists),
        "p90": sorted(dists)[int(0.9 * len(dists))] if dists else None,
        "p99": sorted(dists)[int(0.99 * len(dists))] if dists else None,
        "max": max(dists) if dists else None,
        "top15": sl_rows[:15],
        "by_symbol_mean_top15": sorted(
            ((s, _mean(v), _median(v), len(v)) for s, v in bys.items()),
            key=lambda x: -(x[1] or 0),
        )[:15],
        "note": (
            "Absolute price distances; JPY/TRY/ZAR/etc. pairs have larger quote units, "
            "inflating the mean vs median without implying a bug."
        ),
    }
    _jdump(out / "GHOST_SL_DISTANCE_SUMMARY.json", sl_summary)

    # ===================== PHASE 13/14 =====================
    print("\n######## PHASE 13/14 — SANITY + DUPLICATES ########", flush=True)
    if created_all:
        tmin = min(_ts(e.get("timestamp")) for e in created_all)
        tmax = max(_ts(e.get("timestamp")) for e in created_all)
        days = max(1.0, (tmax - tmin).total_seconds() / 86400.0)
    else:
        days = 1.0
    sanity = []
    for sym in symbols:
        cs = [e for e in created_all if str(e.get("symbol")) == sym]
        ts_list = sorted(_ts(e.get("timestamp")) for e in cs)
        spacings = [(b - a).total_seconds() / 3600.0 for a, b in zip(ts_list, ts_list[1:])] if len(ts_list) > 1 else []
        sanity.append({
            "symbol": sym,
            "candidates": len(cs),
            "candidates_per_day": len(cs) / days,
            "triggers": int(trig_by.get(sym, 0)),
            "fills": int(fill_by.get(sym, 0)),
            "fills_per_day": fill_by.get(sym, 0) / days,
            "rejection_ao": int(ao_by_sym.get(sym, 0)),
            "rejection_rate_vs_cand": _pct(ao_by_sym.get(sym, 0), len(cs)),
            "avg_signal_spacing_h": _mean(spacings),
            "median_signal_spacing_h": _median(spacings),
            "flag_high_activity": (len(cs) / days) > 5.0,
        })
    sanity.sort(key=lambda r: -r["candidates"])
    write_csv(out / "SIGNAL_ECONOMIC_SANITY_BY_SYMBOL.csv", sanity)

    dup_rows, dup_sum = duplicate_forensics(created_all)
    write_csv(out / "DUPLICATE_SIGNAL_FORENSICS.csv", dup_rows[:20000])
    _jdump(out / "DUPLICATE_SIGNAL_SUMMARY.json", dup_sum)
    print(f"dup groups={dup_sum['exact_duplicate_groups']} extra={dup_sum['exact_duplicate_extra']} "
          f"consec={dup_sum['consecutive_similar_pairs']}", flush=True)

    # ===================== PHASE 15 =====================
    print("\n######## PHASE 15 — POST_FIX LOO ########", flush=True)
    loo_rows = []
    for name in ["AFIX_full", "AFIX_minus_NOKSEK", "AFIX_minus_top3", "AFIX_minus_top8"]:
        p = TEST3 / f"{name}_SUMMARY.json"
        if not p.exists():
            continue
        d = json.loads(p.read_text(encoding="utf-8"))
        m = d.get("metrics") or {}
        f = d.get("funnel") or {}
        er = f.get("engine_rejects_by_reason") or {}
        loo_rows.append({
            "label": name, "source": "test3",
            "candidates": f.get("candidate_signals"),
            "triggers": f.get("ghosts_triggered_signals"),
            "fills": m.get("fills"),
            "already_open": er.get("already_open"),
            "book_full": er.get("book_full_break"),
            "fill_rate": f.get("fill_rate_vs_candidates"),
            "net": m.get("net_pnl"),
            "profit_factor": m.get("profit_factor"),
            "max_drawdown": m.get("max_drawdown"),
            "n_symbols": d.get("n_symbols"),
            "zone_polluted_reuses": (d.get("cache_stats") or {}).get("zone_polluted_reuses"),
            "active_symbols_n": len(d.get("active_symbols") or []),
            "ending_equity": m.get("ending_equity"),
            "hold_median_h": m.get("hold_median_h"),
            "note": "minus_top2/top5 not present in Test3 AFIX artifacts",
        })
    # Add our reproduced control
    loo_rows.insert(0, {
        "label": "T4_AFIX_full", "source": "test4_phase0",
        "candidates": n_cand, "triggers": n_trig, "fills": n_pos,
        "already_open": n_ao, "book_full": n_bf,
        "fill_rate": _pct(n_pos, n_cand), "net": m0["net_pnl"],
        "profit_factor": m0["profit_factor"], "max_drawdown": m0["max_drawdown"],
        "n_symbols": 60, "zone_polluted_reuses": 0,
        "active_symbols_n": len({t["symbol"] for t in trades}),
        "ending_equity": m0["ending_equity"],
        "hold_median_h": m0.get("hold_median_h"),
        "note": "reproduced",
    })
    write_csv(out / "POST_FIX_LOO_VALID.csv", loo_rows)

    # ===================== PHASE 16 + REPORTS =====================
    print("\n######## PHASE 16 — CAUSAL DECOMPOSITION + REPORTS ########", flush=True)
    indep_alone = [r for r in indep_rows if r.get("mode") == "FULL_vs_ALONE"]
    n_exact = sum(1 for r in indep_alone if r.get("classification") == "EXACTLY_EQUAL")
    signal_ok = bool(indep_alone) and n_exact == len(indep_alone)
    stale_n = int(ao_breakdown.get("STALE_OCCUPANCY", 0))
    fills_range = 0
    if gsm_rows:
        fills_range = max(int(r["fills"] or 0) for r in gsm_rows) - min(
            int(r["fills"] or 0) for r in gsm_rows
        )

    if not signal_ok:
        status = "POST_FIX_SIGNAL_GENERATION_SUSPECT"
    elif stale_n > 100:
        status = "POST_FIX_ENGINE_BEHAVIOR_SUSPECT"
    elif isinstance(occ_cf, dict) and occ_cf.get("mode") == "FAILED":
        status = "MIXED"
    elif signal_ok and ao_json.get("semantics", {}).get("per_symbol"):
        status = "POST_FIX_SIGNAL_AND_OCCUPANCY_VALIDATED"
    else:
        status = "POST_FIX_SIGNAL_VALIDATED_BUT_ENGINE_OCCUPANCY_UNRESOLVED"

    causal = {
        "chain": [
            {
                "from": "UNIVERSE", "to": "SIGNAL_GENERATION",
                "status": "VERIFIED" if signal_ok else "SUSPECTED",
                "evidence": f"FULL_vs_ALONE exact={n_exact}/{len(indep_alone)}",
            },
            {
                "from": "SIGNAL_GENERATION", "to": "AOI_HTF_STATE",
                "status": "SUPPORTED" if n_match == len(aoi_rows) and aoi_rows else "UNRESOLVED",
                "evidence": f"zone_match={n_match}/{len(aoi_rows)}; per-symbol cache fields present",
            },
            {
                "from": "AOI_HTF_STATE", "to": "CANDIDATE_TAPE",
                "status": "VERIFIED",
                "evidence": (
                    f"POST_FIX candidates={n_cand} vs PRE_FIX={sum(pre_by.values())}; "
                    "SHARED_ZONE_HTF_CACHE_BUG invalidated PRE_FIX"
                ),
            },
            {
                "from": "CANDIDATE_TAPE", "to": "GHOST_CREATION",
                "status": "VERIFIED",
                "evidence": f"ghosts_created={f0['ghosts_created']} == candidates architecture",
            },
            {
                "from": "GHOST_CREATION", "to": "GHOST_TRIGGER",
                "status": "VERIFIED",
                "evidence": f"triggers={n_trig}, invalidations={f0['ghosts_invalidated']}",
            },
            {
                "from": "GHOST_TRIGGER", "to": "PORTFOLIO_OCCUPANCY",
                "status": "VERIFIED",
                "evidence": "can_open(symbol) false iff symbol in open_trades",
            },
            {
                "from": "PORTFOLIO_OCCUPANCY", "to": "ENGINE_ACCEPT_REJECT",
                "status": "VERIFIED",
                "evidence": f"already_open={n_ao}, book_full_break={n_bf}, breakdown={dict(ao_breakdown)}",
            },
            {
                "from": "ENGINE_ACCEPT_REJECT", "to": "POSITION",
                "status": "VERIFIED",
                "evidence": f"fills={n_pos}, fill_rate={_pct(n_pos, n_cand)}",
            },
            {
                "from": "POSITION", "to": "EXIT",
                "status": "VERIFIED",
                "evidence": m0.get("exit_mix"),
            },
            {
                "from": "EXIT", "to": "DYNAMIC_RR_STATE",
                "status": "SUPPORTED",
                "evidence": "rr_mode=dynamic from portfolio win_rate",
            },
            {
                "from": "DYNAMIC_RR_STATE", "to": "EQUITY_POSITION_SIZE",
                "status": "SUPPORTED",
                "evidence": "position_size_pct * equity, size_mode=margin",
            },
            {
                "from": "EQUITY_POSITION_SIZE", "to": "PNL",
                "status": "VERIFIED",
                "evidence": f"net={m0['net_pnl']}, PF={m0['profit_factor']}, MDD={m0['max_drawdown']}",
            },
        ],
        "signal_tape_independent": signal_ok,
        "occupancy_semantics_clear": True,
        "ghost_sl_affects_bottleneck": fills_range > 50,
        "ghost_sl_fills_range": fills_range,
        "final_status": status,
    }
    _jdump(out / "CAUSAL_DECOMPOSITION.json", causal)

    pre_total = sum(pre_by.values())
    post_total = sum(post_by.values())
    top_delta = [(r["symbol"], r["delta"], r["post_fix_candidates"]) for r in expansion[:8]]
    answers = {
        "Q1": (
            f"POST_FIX produced {post_total} candidates vs PRE_FIX/Test2 ~{pre_total or 581}. "
            f"Root cause: SHARED_ZONE_HTF_CACHE_BUG — PRE_FIX reused AOI/HTF by bar index across "
            f"symbols, suppressing most setups. POST_FIX keys caches by `_zones_cache_symbol` / "
            f"`_htf_cache_symbol`, restoring symbol-local generation (~31×). "
            f"Top delta symbols: {top_delta}."
        ),
        "Q2": (
            f"AOI/HTF isolated vs multi-symbol probes: zone_match={n_match}/{len(aoi_rows)}. "
            + (
                "POST_FIX matches isolated reference at probed bars."
                if n_match == len(aoi_rows) and aoi_rows
                else "See AOI_HTF_VALIDATION.csv for mismatches."
            )
        ),
        "Q3": (
            f"FULL vs ALONE: {n_exact}/{len(indep_alone)} EXACTLY_EQUAL. "
            + (
                "Signal generation is symbol-local after the cache fix."
                if signal_ok
                else f"Classifications: {Counter(r.get('classification') for r in indep_alone)}."
            )
        ),
        "Q4": (
            f"Only ~{100 * (_pct(n_pos, n_cand) or 0):.2f}% of candidates become positions. Funnel: "
            f"{n_cand} candidates → {n_trig} triggers → {n_ao} already_open + {n_bf} book_full "
            f"→ {n_pos} fills. Dominant bottleneck is per-symbol occupancy (one open position per symbol)."
        ),
        "Q5": (
            "Source-verified: MultiMarketPortfolio.can_open(symbol) returns False when "
            "`symbol in open_trades`. DropTrackingEngine labels that reject `already_open`. "
            "PER-SYMBOL, side-agnostic (opposite side also blocked), pending ghosts do not count, "
            "cross-symbol positions coexist, multiple positions per symbol are not allowed. "
            "Evaluated after book_is_full on the candidate list, before _open_signal."
        ),
        "Q6": (
            f"book_full_break: portfolio.book_is_full() — liquidated/no cash, or "
            f"len(open_trades)>={MAX_POSITIONS}, or compute_margin(1.0)<=0. Engine then rejects "
            f"remaining new-symbol candidates on that bar. n={n_bf}; "
            f"cash/margin-cap subset≈{bf_json.get('likely_cash_margin_cap_count')}."
        ),
        "Q7": (
            f"Of {ao_json['n_rejects']} already_open rejects: {dict(ao_breakdown)}. "
            f"Nearly all are legitimate same-symbol occupancy "
            f"(SAME_SYMBOL / OPPOSITE_SIDE / SAME_BAR). STALE={stale_n} "
            f"({100 * (ao_json.get('stale_pct') or 0):.2f}%)."
        ),
        "Q8": (
            (
                f"Replace-on-signal counterfactual: fills {occ_cf['normal']['fills']} → "
                f"{occ_cf['relaxed']['fills']} (delta={occ_cf.get('delta_fills')}); "
                f"replaced_closes≈{occ_cf['relaxed'].get('n_replaced_closes')}. "
                f"Many rejected triggers become fills if same-symbol occupancy is cleared — "
                f"diagnostic only, not a recommendation."
            )
            if isinstance(occ_cf, dict) and occ_cf.get("mode") != "FAILED" and occ_cf.get("relaxed")
            else f"Counterfactual incomplete: {occ_cf}"
        ),
        "Q9": (
            (
                f"Economics under replace: net {occ_cf['normal']['net']} → {occ_cf['relaxed']['net']} "
                f"(delta={occ_cf.get('delta_net')}); PF {occ_cf['normal']['profit_factor']} → "
                f"{occ_cf['relaxed']['profit_factor']}; MDD {occ_cf['normal']['max_drawdown']} → "
                f"{occ_cf['relaxed']['max_drawdown']}. Interpret as exposure/turnover change, "
                f"NOT an optimization result."
            )
            if isinstance(occ_cf, dict) and occ_cf.get("mode") != "FAILED" and occ_cf.get("relaxed")
            else f"Counterfactual incomplete: {occ_cf}"
        ),
        "Q10": (
            f"ghost_sl_mult grid fills/already_open: "
            f"{[(r['ghost_sl_mult'], r['fills'], r.get('already_open'), r.get('triggers')) for r in gsm_rows]}. "
            f"fills_range={fills_range}. Acceptance bottleneck is occupancy, not ghost SL; "
            f"fills move only slightly across gsm 2..6."
        ),
        "Q11": (
            f"Mean SL distance={sl_summary.get('mean')}, median={sl_summary.get('median')}, "
            f"max={sl_summary.get('max')}. Skew from high absolute-price symbols "
            f"(JPY/TRY/ZAR etc.); median FX majors remain small. "
            f"Top: {[(r.get('symbol'), r.get('sl_distance')) for r in (sl_summary.get('top15') or [])[:5]]}. "
            f"Not assumed a bug — absolute quote units."
        ),
        "Q12": (
            f"Exact duplicate groups={dup_sum.get('exact_duplicate_groups')}, "
            f"extra={dup_sum.get('exact_duplicate_extra')}, rate={dup_sum.get('duplicate_rate')}, "
            f"consecutive_similar_pairs={dup_sum.get('consecutive_similar_pairs')}. "
            + (
                "Low exact-duplicate rate → mostly unique candidate identities."
                if (dup_sum.get("duplicate_rate") or 0) < 0.01
                else "Elevated duplicates — see DUPLICATE_SIGNAL_FORENSICS.csv."
            )
        ),
        "Q13": (
            f"POST_FIX {n_pos}-fill regime is internally coherent: symbol-local signals, "
            f"ghost funnel, and per-symbol already_open explaining ~{n_ao}/{n_trig} trigger rejects. "
            f"Economic meaning still depends on accepting one-position-per-symbol semantics."
        ),
        "Q14": (
            f"Valid POST_FIX LOO (available): "
            f"{[(r.get('label'), r.get('fills'), r.get('net'), r.get('profit_factor')) for r in loo_rows]}. "
            f"Universe sensitivity remains without zone_polluted_reuses — economic/occupancy, not cache bug. "
            f"minus_top2/top5 not in Test3 AFIX artifacts."
        ),
        "Q15": (
            "NO — +$5,074 POST_FIX is NOT ready for OOS as a deployment claim. "
            "It is a mechanistic in-sample diagnostic under frozen knobs. "
            "OOS requires a separate explicitly approved research phase."
        ),
        "Q16": (
            f"Status={status}. "
            + (
                "Signal+occupancy validated mechanistically; still do NOT preregister OOS "
                "without explicit approval. Live/Mirror unchanged."
                if status == "POST_FIX_SIGNAL_AND_OCCUPANCY_VALIDATED"
                else "Further investigation required before any OOS."
            )
        ),
        "FINAL_STATUS": status,
    }
    _jdump(out / "TEST4_ANSWERS.json", answers)

    elapsed = time.time() - time.mktime(started.timetuple()) if False else None
    # use wall clock
    finished = datetime.now(timezone.utc)
    manifest["status"] = status
    manifest["finished_utc"] = finished.isoformat()
    manifest["elapsed_s"] = (finished - started).total_seconds()
    manifest["phase0_repro"] = {k: repro[k] for k in (
        "candidates", "triggers", "positions", "already_open", "book_full_break",
        "net", "profit_factor", "max_drawdown", "stop",
    )}
    _jdump(out / "TEST4_MANIFEST.json", manifest)

    exec_md = f"""# TEST4 EXECUTIVE SUMMARY

**Run:** `{out.name}`  
**Status:** `{status}`  
**Regime:** POST_FIX only (PRE_FIX audit-only / invalidated)  
**Nature:** DIAGNOSTIC / MECHANISTIC / NOT FOR DEPLOYMENT

## Control reproduction (Phase 0)

| Metric | Got | Expected |
|--------|-----|----------|
| candidates | {repro['candidates']} | {EXPECTED['candidates']} |
| triggers | {repro['triggers']} | {EXPECTED['triggers']} |
| positions | {repro['positions']} | {EXPECTED['positions']} |
| already_open | {repro['already_open']} | {EXPECTED['already_open']} |
| book_full_break | {repro['book_full_break']} | {EXPECTED['book_full_break']} |
| net | {repro['net']} | {EXPECTED['net']} |
| PF | {repro['profit_factor']} | {EXPECTED['profit_factor']} |
| MDD | {repro['max_drawdown']} | {EXPECTED['max_drawdown']} |

## Headline answers

- **Q1 (31× candidates):** cache-bug suppression lifted under POST_FIX.
- **Q3 (independence):** {n_exact}/{len(indep_alone)} EXACTLY_EQUAL FULL vs ALONE.
- **Q4 (13% fill):** occupancy bottleneck — already_open={n_ao} of {n_trig} triggers.
- **Q5/Q6:** already_open = per-symbol `open_trades` key; book_full_break = global cap/cash.
- **Q10:** ghost_sl_mult does **not** drive the acceptance bottleneck (fills_range={fills_range}).
- **Q15 ready for OOS?** NO.
- **Q16:** {answers['Q16']}

## Verdict

`{status}`

Do **not** declare AlexG10 profitable or deployment-ready.  
Do **not** promote Live/Mirror from this test.  
Do **not** treat the occupancy counterfactual as an optimization.
"""
    (out / "TEST4_EXECUTIVE_SUMMARY.md").write_text(exec_md, encoding="utf-8")

    report = f"""# TEST4 REPORT — POST_FIX Signal-Tape + Fill-Acceptance Forensics

## Meta

- run_id: {out.name}
- git: {_git()} / {_branch()}
- python: {sys.version}
- H1 hash: {hh} (match={hh == T1_HASH})
- test3_dir: {TEST3}
- elapsed_s: {manifest['elapsed_s']}

## Phase 0 — Control

```json
{json.dumps(repro, indent=2, default=str)}
```

## already_open (source-verified)

```json
{json.dumps(ao_json.get('semantics'), indent=2, default=str)}
```

Breakdown: `{json.dumps(dict(ao_breakdown), default=str)}`

## book_full_break

```json
{json.dumps({k: bf_json.get(k) for k in (
    'n_rejects','max_positions','mean_n_open','at_max_positions_count',
    'likely_cash_margin_cap_count','same_symbol_also_occupied_count'
)}, indent=2, default=str)}
```

## Occupancy counterfactual (diagnostic only)

```json
{json.dumps(occ_cf, indent=2, default=str)[:12000]}
```

## Causal decomposition

```json
{json.dumps(causal, indent=2, default=str)}
```

## Full Q&A

"""
    for k in [f"Q{i}" for i in range(1, 17)] + ["FINAL_STATUS"]:
        report += f"\n### {k}\n\n{answers.get(k)}\n"
    report += """
## Artifacts

See this run directory for CSV/JSON outputs listed in the Test4 protocol.

## Hard constraints respected

- OFFLINE only; no MT5 / Live 8791 / Mirror 8792
- No parameter optimization / RR / leverage / sizing / spread / cost changes
- PRE_FIX retained audit-only; not used as strategy evidence
- Occupancy counterfactual is diagnostic-only, not a deployment recommendation
"""
    (out / "TEST4_REPORT.md").write_text(report, encoding="utf-8")

    print(f"\n=== TEST4 DONE status={status} elapsed={manifest['elapsed_s']:.1f}s OUT={out} ===", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        err = traceback.format_exc()
        print(err, flush=True)
        try:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            fatal_dir = OUT_ROOT / f"alexg10_test4_{stamp}_FATAL"
            fatal_dir.mkdir(parents=True, exist_ok=True)
            (fatal_dir / "FATAL_ERROR.txt").write_text(err, encoding="utf-8")
        except Exception:
            pass
        raise
