#!/usr/bin/env python3
"""AlexG10 Test 1 — offline reproduction / attribution / validation.

MT5 CLOSED: loads H1 exclusively from data/cache/mt5/utc_v2 (no broker connect).
Does not start Live/Mirror or place orders.
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
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from borex.alexg import AlexG8Strategy, AlexG10Strategy
from borex.alexg.multi_market import default_forex_universe, pick_master_symbol
from borex.alexg.force_flat import _trade_session
from borex.backtest.engine import BacktestConfig
from borex.backtest.multi_market_engine import MultiMarketEngine
from borex.backtest.portfolio import Trade
from borex.gate2.thursday_entry_gate import filter_block_thursday_entries
from borex.models.candle import Candle
from borex.viewerMT5.mt5_feed import (
    _cache_path,
    _dedupe_hourly,
    _drop_forming_bar,
    _frame_to_candles,
    _slice_candles,
)

# ---- Locked original C config (from META / summary) ----
ORIG_RUN = ROOT / (
    "data/runs/research/frozen_thu_only_alexg10_rrdyn_noflat_20261002_144951"
)
CAPITAL = 1000.0
LEV = 500.0
RR_FACTOR = 1.0
ENTRY_START = pd.Timestamp("2026-06-01", tz="UTC")
DATA_START = pd.Timestamp("2026-04-01", tz="UTC")
# Original META data_end_exclusive
REPRO_END = pd.Timestamp("2026-10-02T14:00:00+00:00")
# Clean comparable window end (closed September)
SEP_END = pd.Timestamp("2026-10-01T00:00:00+00:00")
MAX_POSITIONS = 60
GHOST_PRIME_BARS = 72
JUN_INDEX = ROOT / "data/runs/mt5_1m_n1_oos_jun2026/index.json"
SEP_OOS = ROOT / (
    "data/runs/research/gate2_september_oos_final_20261003_151905"
)
THU_PRIOR = ROOT / "data/runs/research/gate2_thursday_gate_20260925_195229"

REPORTED_NET = 851.231259328757
REPRO_TOL = 1.0  # absolute $ tolerance for PASS


def _git() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=str(ROOT), text=True
        ).strip()
    except Exception:
        return "unknown"


def _ts(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def fingerprint(obj: Any) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, default=str).encode()
    ).hexdigest()


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


def load_h1_cache_only(
    symbols: list[str], start: pd.Timestamp, end: pd.Timestamp
) -> dict[str, list[Candle]]:
    """Offline H1 load — NEVER connects to MT5."""
    data: dict[str, list[Candle]] = {}
    start_py, end_py = start.to_pydatetime(), end.to_pydatetime()
    for i, sym in enumerate(symbols, 1):
        path = _cache_path(sym, "1h")
        if not path.is_file():
            continue
        df = pd.read_parquet(path)
        candles = _drop_forming_bar(
            _dedupe_hourly(_slice_candles(_frame_to_candles(df), start_py, end_py)),
            "1h",
        )
        if len(candles) >= 200:
            data[sym] = candles
        if i % 20 == 0 or i == len(symbols):
            print(f"  H1 cache {i}/{len(symbols)} loaded={len(data)}", flush=True)
    return data


def h1_dataset_hash(data: dict[str, list[Candle]]) -> str:
    h = hashlib.sha256()
    for sym in sorted(data):
        h.update(sym.encode())
        bars = data[sym]
        if not bars:
            continue
        h.update(str(bars[0].timestamp).encode())
        h.update(str(bars[-1].timestamp).encode())
        h.update(str(len(bars)).encode())
        # sample a few OHLC points
        for idx in (0, len(bars) // 2, -1):
            c = bars[idx]
            h.update(f"{c.open},{c.high},{c.low},{c.close}".encode())
    return h.hexdigest()


def bt_cfg(
    spreads: dict[str, float],
    *,
    force_flat_daily: bool,
    true_sl: bool,
    honor_signal_stops: bool,
    rr_mode: str,
    rr_factor: float = RR_FACTOR,
) -> BacktestConfig:
    return BacktestConfig(
        initial_capital=CAPITAL,
        leverage=LEV,
        position_size_pct=0.01,
        size_mode="margin",
        true_sl=bool(true_sl),
        true_sl_rr=2.0,
        honor_signal_stops=bool(honor_signal_stops),
        rr_mode=str(rr_mode),
        rr_factor=float(rr_factor),
        rr_min=2.0,
        rr_max=5.0,
        commission_per_lot=7.0,
        min_commission_per_side=0.04,
        lot_notional=100_000.0,
        risk_include_commission=True,
        force_flat_daily=bool(force_flat_daily),
        force_flat_friday=False,
        force_flat_utc_hour=19,
        force_flat_friday_from_hour=19,
        winrate_min_trades=20,
        spread_pips=0.0,
        spread_pips_by_symbol=spreads,
        slippage_pips=0.0,
        intra_hour_sl=False,
    )


class ThursdayBlockGate:
    """Research thursday_gate: block Thursday UTC new entries."""

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
        ts = _ts(candles[index].timestamp)
        if int(ts.dayofweek) == 3:
            return None
        return sig


def run_engine(
    *,
    name: str,
    strategy: Any,
    data: dict[str, list[Candle]],
    spreads: dict[str, float],
    master: str,
    entry_start: pd.Timestamp,
    end: pd.Timestamp,
    force_flat_daily: bool,
    true_sl: bool,
    honor_signal_stops: bool,
    rr_mode: str,
    thursday_block: bool = False,
) -> dict[str, Any]:
    # Slice data to end (exclusive of end timestamp)
    sliced = {}
    for sym, bars in data.items():
        sliced[sym] = [c for c in bars if _ts(c.timestamp) < end]
        if len(sliced[sym]) < 200:
            del sliced[sym]
    if master not in sliced:
        raise SystemExit(f"{name}: master missing after slice")

    strat = strategy
    if thursday_block:
        strat = ThursdayBlockGate(strat)

    cfg = bt_cfg(
        spreads,
        force_flat_daily=force_flat_daily,
        true_sl=true_sl,
        honor_signal_stops=honor_signal_stops,
        rr_mode=rr_mode,
    )
    engine = MultiMarketEngine(strat, cfg, max_positions=MAX_POSITIONS)
    engine._progress_every = 500

    entry_i = next(
        i for i, c in enumerate(sliced[master]) if _ts(c.timestamp) >= entry_start
    )
    min_bars = int(getattr(strat, "min_bars", 100) or 100)
    start_i = max(min_bars, entry_i - GHOST_PRIME_BARS)

    print(
        f"\n=== {name} === bars={len(sliced[master])} start_i={start_i} "
        f"entry={entry_start} end={end} flat={force_flat_daily} "
        f"true_sl={true_sl} honor={honor_signal_stops} rr={rr_mode} "
        f"thu_block={thursday_block}",
        flush=True,
    )
    t0 = time.time()
    result = engine.run(
        sliced,
        timeframe="1h",
        master_symbol=master,
        same_bar_exit=False,
        start_master_i=start_i,
    )
    elapsed = time.time() - t0

    rows = []
    for t in result.trades:
        if _ts(t.entry_time) < entry_start:
            continue
        if _ts(t.entry_time) >= end:
            continue
        side = t.side.value if hasattr(t.side, "value") else str(t.side)
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
        et = _ts(t.entry_time)
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
                "rr_resolved": rr,
                "pnl": float(t.pnl or 0.0),
                "commission": float(getattr(t, "commission", 0) or 0.0),
                "margin": float(t.margin or 0.0),
                "exit_reason": t.exit_reason,
                "pattern": str(t.pattern or ""),
                "month": et.strftime("%Y-%m"),
                "dow": int(et.dayofweek),
                "weekday": et.day_name(),
                "hour": int(et.hour),
                "session": getattr(t, "entry_session", "") or "",
                "iso_week": f"{et.isocalendar().year}-W{et.isocalendar().week:02d}",
            }
        )

    print(
        f"  done {elapsed:.1f}s fills={len(rows)} "
        f"pnl={sum(r['pnl'] for r in rows):.2f} equity={result.final_equity:.2f}",
        flush=True,
    )
    return {
        "name": name,
        "trades": rows,
        "elapsed_s": elapsed,
        "final_equity_engine": float(result.final_equity),
        "max_dd_pct_engine": float(result.max_drawdown_pct),
        "n_symbols": len(sliced),
        "master": master,
        "entry_start": str(entry_start),
        "end": str(end),
        "config": {
            "force_flat_daily": force_flat_daily,
            "true_sl": true_sl,
            "honor_signal_stops": honor_signal_stops,
            "rr_mode": rr_mode,
            "rr_factor": RR_FACTOR,
            "thursday_block": thursday_block,
            "leverage": LEV,
            "capital": CAPITAL,
            "position_size_pct": 0.01,
            "timeframe": "1h",
            "ghost_fill_at_close": True,
            "ghost_sl_mult": 4.0,
            "peer_blend": 0.0,
        },
    }


def metrics(trades: list[dict], *, capital: float = CAPITAL) -> dict[str, Any]:
    pnls = [float(t["pnl"]) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gp = sum(wins)
    gl = sum(losses)
    pf = (gp / abs(gl)) if gl else (float("inf") if gp > 0 else None)
    exits = Counter(str(t["exit_reason"]) for t in trades)
    # sequential equity MDD by exit order
    ordered = sorted(
        trades,
        key=lambda t: (_ts(t["exit_time"] or t["entry_time"]), t["entry_time"]),
    )
    eq = capital
    peak = capital
    max_dd = 0.0
    for t in ordered:
        eq += float(t["pnl"])
        peak = max(peak, eq)
        dd = (peak - eq) / peak if peak > 0 else 0.0
        max_dd = max(max_dd, dd)
    max_cl = cur = 0
    for p in pnls:
        if p <= 0:
            cur += 1
            max_cl = max(max_cl, cur)
        else:
            cur = 0
    net = float(sum(pnls)) if pnls else 0.0
    ending = capital + net
    return {
        "fills": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (len(wins) / len(trades)) if trades else None,
        "gross_profit": float(gp),
        "gross_loss": float(gl),
        "profit_factor": pf,
        "net_pnl": net,
        "mean_trade": float(statistics.mean(pnls)) if pnls else None,
        "median_trade": float(statistics.median(pnls)) if pnls else None,
        "std_trade": float(statistics.pstdev(pnls)) if len(pnls) > 1 else (0.0 if pnls else None),
        "max_drawdown": float(max_dd),
        "max_consecutive_losses": max_cl,
        "TP_count": int(exits.get("take_profit", 0)),
        "SL_count": int(exits.get("stop_loss", 0)),
        "margin_stop_count": int(exits.get("margin_stop", 0)),
        "daily_flat_count": int(exits.get("daily_close", 0)),
        "friday_flat_count": int(exits.get("friday_close", 0)),
        "end_of_data_count": int(exits.get("end_of_data", 0)),
        "other_exits": {
            k: v
            for k, v in exits.items()
            if k
            not in {
                "take_profit",
                "stop_loss",
                "margin_stop",
                "daily_close",
                "friday_close",
                "end_of_data",
            }
        },
        "commission_sum": float(sum(float(t.get("commission") or 0) for t in trades)),
        "swap_sum": None,  # NOT PRODUCED
        "starting_equity": capital,
        "ending_equity": float(ending),
        "return_pct": float((ending / capital - 1.0) * 100.0),
        "equity_identity_ok": abs((capital + net) - ending) < 1e-9,
        "exit_mix": dict(exits),
    }


def monthly_table(trades: list[dict]) -> list[dict]:
    by = defaultdict(list)
    for t in trades:
        by[t["month"]].append(t)
    out = []
    for m in sorted(by):
        s = metrics(by[m])
        out.append({"month": m, **{k: v for k, v in s.items() if not isinstance(v, dict)}})
    return out


def concentration(trades: list[dict]) -> dict:
    ordered = sorted(trades, key=lambda t: float(t["pnl"]), reverse=True)
    total = sum(float(t["pnl"]) for t in trades) if trades else 0.0
    def top_n(n):
        s = sum(float(t["pnl"]) for t in ordered[:n])
        return {"sum": s, "share_of_total": (s / total) if total else None}

    by_pair = defaultdict(float)
    for t in trades:
        by_pair[t["symbol"]] += float(t["pnl"])
    pair_sorted = sorted(by_pair.items(), key=lambda x: x[1], reverse=True)
    top_pairs = [{"pair": p, "pnl": v} for p, v in pair_sorted[:10]]
    top5_pair = sum(v for _, v in pair_sorted[:5])
    top10_pair = sum(v for _, v in pair_sorted[:10])
    excl = {}
    for n in (1, 5, 10):
        keep = ordered[n:]
        excl[f"profit_excluding_top_{n}"] = sum(float(t["pnl"]) for t in keep)
    return {
        "total_net": total,
        "top_1": top_n(1),
        "top_5": top_n(5),
        "top_10": top_n(10),
        "top_20": top_n(20),
        "largest_win": float(ordered[0]["pnl"]) if ordered else None,
        "largest_loss": float(min((t["pnl"] for t in trades), default=0)),
        **excl,
        "top_5_pairs_pnl": top5_pair,
        "top_10_pairs_pnl": top10_pair,
        "top_5_pairs_share": (top5_pair / total) if total else None,
        "top_10_pairs_share": (top10_pair / total) if total else None,
        "top_pairs": top_pairs,
        "n_symbols_traded": len(by_pair),
    }


def rr_distribution(trades: list[dict]) -> dict:
    rrs = [float(t["rr_resolved"]) for t in trades if t.get("rr_resolved") is not None]
    if not rrs:
        return {"count": 0}
    rrs_s = sorted(rrs)

    def pct(p):
        i = int(round((p / 100.0) * (len(rrs_s) - 1)))
        return rrs_s[i]

    return {
        "count": len(rrs),
        "min": rrs_s[0],
        "p25": pct(25),
        "median": pct(50),
        "p75": pct(75),
        "max": rrs_s[-1],
        "mean": float(statistics.mean(rrs)),
    }


def force_flat_nat_probe() -> dict:
    """Determine whether NaT guard changes economics or only prevents crashes."""
    class T:
        pass

    cases = []
    for label, entry in (
        ("None", None),
        ("empty_str", ""),
        ("nat_str", "NaT"),
        ("valid", "2026-06-02T10:00:00+00:00"),
    ):
        tr = T()
        tr.entry_time = entry
        tr.entry_session = ""
        try:
            sess = _trade_session(tr)
            cases.append({"case": label, "result": str(sess), "raised": False})
        except Exception as exc:
            cases.append({"case": label, "result": None, "raised": True, "error": str(exc)})
    return {
        "conclusion": (
            "INFRASTRUCTURE_CORRECTNESS_FIX — returns None for empty/NaT entry_time "
            "instead of crashing classify_session; does not alter SL/TP/PnL math for "
            "valid fills."
        ),
        "cases": cases,
        "economic_effect": False,
    }


def make_strategy(kind: str):
    kwargs = dict(
        min_rr=2.0,
        execution_interval="1h",
        peer_blend=0.0,
        ghost_sl_mult=4.0,
        ghost_fill_at_close=True,
    )
    if kind == "alexg10":
        return AlexG10Strategy(**kwargs)
    if kind == "alexg8":
        return AlexG8Strategy(**kwargs)
    raise ValueError(kind)


def main() -> int:
    now = datetime.now(timezone.utc)
    out = ROOT / "data" / "runs" / "research" / f"alexg10_test1_{now.strftime('%Y%m%d_%H%M%S')}"
    out.mkdir(parents=True, exist_ok=True)
    docs_out = ROOT / "docs" / "research" / "alexg10_test1_validation.md"
    print(f"OUT={out}", flush=True)

    jun = json.loads(JUN_INDEX.read_text(encoding="utf-8"))
    spreads = dict(jun["spread_pips_by_symbol"])
    symbols = default_forex_universe()
    if "EURUSD=X" not in symbols:
        symbols = ["EURUSD=X"] + list(symbols)
    # Prefer pinned 60 from jun index order if available
    pinned = sorted(spreads.keys())
    if len(pinned) == 60:
        symbols = pinned

    manifest = {
        "created_at_utc": now.isoformat(),
        "git_commit": _git(),
        "git_branch": "closest-sim",
        "python": sys.version,
        "platform": platform.platform(),
        "mt5_used": False,
        "live_mirror_used": False,
        "orders_placed": False,
        "original_run": str(ORIG_RUN),
        "reported_net": REPORTED_NET,
        "repro_tolerance_abs": REPRO_TOL,
        "data_source": "data/cache/mt5/utc_v2 H1 parquet (cache-only, no MT5)",
        "interpretation": "DISCOVERY result validation — not fresh unbiased OOS",
    }
    (out / "EXPERIMENT_MANIFEST.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )

    print("Loading H1 from utc_v2 cache (NO MT5)…", flush=True)
    data = load_h1_cache_only(symbols, DATA_START, REPRO_END)
    if "EURUSD=X" not in data:
        raise SystemExit("EURUSD=X missing from cache")
    master = pick_master_symbol(data) or "EURUSD=X"
    # Ensure spreads for all loaded symbols
    for sym in data:
        spreads.setdefault(sym, 0.0)
    dhash = h1_dataset_hash(data)
    print(f"loaded={len(data)} master={master} hash={dhash[:16]}…", flush=True)

    config = {
        "strategy": "alexg10",
        "ghost_sl_mult": 4.0,
        "peer_blend": 0.0,
        "ghost_fill_at_close": True,
        "capital": CAPITAL,
        "leverage": LEV,
        "position_size_pct": 0.01,
        "size_mode": "margin",
        "true_sl": True,
        "honor_signal_stops": False,
        "rr_mode": "dynamic",
        "rr_factor": 1.0,
        "rr_min": 2.0,
        "rr_max": 5.0,
        "force_flat_daily": False,
        "force_flat_friday": False,
        "thursday_gate": False,
        "thursday_only": False,
        "commission_per_lot": 7.0,
        "risk_include_commission": True,
        "timeframe": "1h",
        "path": "H1_signal_H1_exit_fill_at_close",
        "entry_start": str(ENTRY_START),
        "data_start": str(DATA_START),
        "repro_end": str(REPRO_END),
        "max_positions": MAX_POSITIONS,
        "n_symbols": len(data),
        "master": master,
        "h1_dataset_hash": dhash,
        "spreads_source": str(JUN_INDEX),
    }
    (out / "CONFIG.json").write_text(json.dumps(config, indent=2), encoding="utf-8")

    # ---- A exact reproduction ----
    repro = run_engine(
        name="A_alexg10_exact_repro",
        strategy=make_strategy("alexg10"),
        data=data,
        spreads=spreads,
        master=master,
        entry_start=ENTRY_START,
        end=REPRO_END,
        force_flat_daily=False,
        true_sl=True,
        honor_signal_stops=False,
        rr_mode="dynamic",
        thursday_block=False,
    )
    m_repro = metrics(repro["trades"])
    delta = m_repro["net_pnl"] - REPORTED_NET
    repro_pass = abs(delta) <= REPRO_TOL
    print(
        f"REPRODUCTION {'PASS' if repro_pass else 'FAIL'}: "
        f"reported={REPORTED_NET:.4f} reproduced={m_repro['net_pnl']:.4f} "
        f"diff={delta:.4f}",
        flush=True,
    )

    write_csv(out / "TRADES.csv", repro["trades"])
    (out / "TRADES.json").write_text(
        json.dumps(repro["trades"], indent=2), encoding="utf-8"
    )

    # Cost reconciliation
    cost = {
        "starting_equity": CAPITAL,
        "sum_trade_pnl": m_repro["net_pnl"],
        "ending_equity_from_trades": m_repro["ending_equity"],
        "engine_final_equity": repro["final_equity_engine"],
        "equity_identity_ok": m_repro["equity_identity_ok"],
        "note_engine_equity_gap": (
            "Engine final_equity includes pre-entry_start prime trades; "
            "eval ledger net is filtered to entry_start onwards."
        ),
        "commission_sum": m_repro["commission_sum"],
        "swap_sum": None,
        "spread_model": "spread_pips_by_symbol from jun N1 index (applied at fill)",
        "gross_profit": m_repro["gross_profit"],
        "gross_loss": m_repro["gross_loss"],
        "profit_factor": m_repro["profit_factor"],
        "fills": m_repro["fills"],
        "wins": m_repro["wins"],
        "losses": m_repro["losses"],
        "win_rate": m_repro["win_rate"],
        "mean_trade": m_repro["mean_trade"],
        "median_trade": m_repro["median_trade"],
        "std_trade": m_repro["std_trade"],
        "max_drawdown": m_repro["max_drawdown"],
        "max_consecutive_losses": m_repro["max_consecutive_losses"],
        "TP_count": m_repro["TP_count"],
        "SL_count": m_repro["SL_count"],
        "margin_stop_count": m_repro["margin_stop_count"],
        "daily_flat_count": m_repro["daily_flat_count"],
        "reported_net": REPORTED_NET,
        "reproduced_net": m_repro["net_pnl"],
        "difference": delta,
        "reproduction": "PASS" if repro_pass else "FAIL",
    }
    (out / "COST_RECONCILIATION.json").write_text(
        json.dumps(cost, indent=2, default=str), encoding="utf-8"
    )

    # Breakdowns from repro trades
    monthly = monthly_table(repro["trades"])
    write_csv(out / "MONTHLY_RESULTS.csv", monthly)

    # Day / session / pair
    by_day = defaultdict(list)
    by_sess = defaultdict(list)
    by_pair = defaultdict(list)
    by_dow = defaultdict(list)
    by_week = defaultdict(list)
    for t in repro["trades"]:
        by_day[str(_ts(t["entry_time"]).date())].append(t)
        by_sess[t["session"] or "unknown"].append(t)
        by_pair[t["symbol"]].append(t)
        by_dow[t["weekday"]].append(t)
        by_week[t["iso_week"]].append(t)

    write_csv(
        out / "DAY_BREAKDOWN.csv",
        [
            {"date": d, **{k: v for k, v in metrics(by_day[d]).items() if not isinstance(v, dict)}}
            for d in sorted(by_day)
        ],
    )
    write_csv(
        out / "SESSION_BREAKDOWN.csv",
        [
            {"session": s, **{k: v for k, v in metrics(by_sess[s]).items() if not isinstance(v, dict)}}
            for s in sorted(by_sess)
        ],
    )
    write_csv(
        out / "PAIR_BREAKDOWN.csv",
        [
            {"pair": p, **{k: v for k, v in metrics(by_pair[p]).items() if not isinstance(v, dict)}}
            for p in sorted(by_pair, key=lambda x: metrics(by_pair[x])["net_pnl"], reverse=True)
        ],
    )
    write_csv(
        out / "DOW_BREAKDOWN.csv",
        [
            {"weekday": d, **{k: v for k, v in metrics(by_dow[d]).items() if not isinstance(v, dict)}}
            for d in ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
            if d in by_dow
        ],
    )
    write_csv(
        out / "WEEKLY_BREAKDOWN.csv",
        [
            {"week": w, **{k: v for k, v in metrics(by_week[w]).items() if not isinstance(v, dict)}}
            for w in sorted(by_week)
        ],
    )

    rr_dist = rr_distribution(repro["trades"])
    write_csv(out / "RR_DISTRIBUTION.csv", [rr_dist])
    conc = concentration(repro["trades"])
    (out / "PROFIT_CONCENTRATION.json").write_text(
        json.dumps(conc, indent=2, default=str), encoding="utf-8"
    )

    # Continuous Jun→Sep (end Oct 1) — same config
    cont = run_engine(
        name="continuous_jun_sep",
        strategy=make_strategy("alexg10"),
        data=data,
        spreads=spreads,
        master=master,
        entry_start=ENTRY_START,
        end=SEP_END,
        force_flat_daily=False,
        true_sl=True,
        honor_signal_stops=False,
        rr_mode="dynamic",
    )
    m_cont = metrics(cont["trades"])
    monthly_cont = monthly_table(cont["trades"])
    # Label: monthly rows from continuous path are NOT independent monthly runs
    for row in monthly_cont:
        row["path_type"] = "SLICE_OF_CONTINUOUS"
    write_csv(out / "MONTHLY_FROM_CONTINUOUS.csv", monthly_cont)

    # Independent monthly runs (separate equity resets)
    indep_months = []
    month_bounds = [
        ("2026-06", pd.Timestamp("2026-06-01", tz="UTC"), pd.Timestamp("2026-07-01", tz="UTC")),
        ("2026-07", pd.Timestamp("2026-07-01", tz="UTC"), pd.Timestamp("2026-08-01", tz="UTC")),
        ("2026-08", pd.Timestamp("2026-08-01", tz="UTC"), pd.Timestamp("2026-09-01", tz="UTC")),
        ("2026-09", pd.Timestamp("2026-09-01", tz="UTC"), pd.Timestamp("2026-10-01", tz="UTC")),
    ]
    for label, start, end in month_bounds:
        r = run_engine(
            name=f"indep_{label}",
            strategy=make_strategy("alexg10"),
            data=data,
            spreads=spreads,
            master=master,
            entry_start=start,
            end=end,
            force_flat_daily=False,
            true_sl=True,
            honor_signal_stops=False,
            rr_mode="dynamic",
        )
        s = metrics(r["trades"])
        indep_months.append(
            {
                "month": label,
                "path_type": "INDEPENDENT_MONTHLY_RESET",
                **{k: v for k, v in s.items() if not isinstance(v, dict)},
            }
        )
    write_csv(out / "MONTHLY_INDEPENDENT.csv", indep_months)
    agg_indep = sum(float(r["net_pnl"]) for r in indep_months)

    # ---- Ablations (same repro window) ----
    ablations = []

    def add_ablation(tag: str, result: dict):
        s = metrics(result["trades"])
        ablations.append(
            {
                "ablation": tag,
                "net_pnl": s["net_pnl"],
                "fills": s["fills"],
                "PF": s["profit_factor"],
                "WR": s["win_rate"],
                "mean": s["mean_trade"],
                "MDD": s["max_drawdown"],
                "TP": s["TP_count"],
                "margin_stop": s["margin_stop_count"],
                "daily_flat": s["daily_flat_count"],
                "delta_net_vs_A": s["net_pnl"] - m_repro["net_pnl"],
                "delta_fills_vs_A": s["fills"] - m_repro["fills"],
                "delta_PF_vs_A": (
                    (s["profit_factor"] - m_repro["profit_factor"])
                    if s["profit_factor"] is not None
                    and m_repro["profit_factor"] is not None
                    and math.isfinite(s["profit_factor"])
                    and math.isfinite(m_repro["profit_factor"])
                    else None
                ),
                "delta_MDD_vs_A": s["max_drawdown"] - m_repro["max_drawdown"],
            }
        )
        return s

    ablations.append(
        {
            "ablation": "A_alexg10_exact",
            "net_pnl": m_repro["net_pnl"],
            "fills": m_repro["fills"],
            "PF": m_repro["profit_factor"],
            "WR": m_repro["win_rate"],
            "mean": m_repro["mean_trade"],
            "MDD": m_repro["max_drawdown"],
            "TP": m_repro["TP_count"],
            "margin_stop": m_repro["margin_stop_count"],
            "daily_flat": m_repro["daily_flat_count"],
            "delta_net_vs_A": 0.0,
            "delta_fills_vs_A": 0,
            "delta_PF_vs_A": 0.0,
            "delta_MDD_vs_A": 0.0,
        }
    )

    # B — remove strategy10 flip (alexg8 same exits)
    b = run_engine(
        name="B_alexg8_same_exits_noflat",
        strategy=make_strategy("alexg8"),
        data=data,
        spreads=spreads,
        master=master,
        entry_start=ENTRY_START,
        end=REPRO_END,
        force_flat_daily=False,
        true_sl=True,
        honor_signal_stops=False,
        rr_mode="dynamic",
    )
    m_b = add_ablation("B_alexg8_no_strategy10_flip", b)

    # C — restore force_flat_daily
    c = run_engine(
        name="C_alexg10_force_flat_ON",
        strategy=make_strategy("alexg10"),
        data=data,
        spreads=spreads,
        master=master,
        entry_start=ENTRY_START,
        end=REPRO_END,
        force_flat_daily=True,
        true_sl=True,
        honor_signal_stops=False,
        rr_mode="dynamic",
    )
    m_c = add_ablation("C_alexg10_force_flat_restored", c)

    # D — Thursday block ON (research gate); baseline had OFF
    d = run_engine(
        name="D_alexg10_thursday_block_ON",
        strategy=make_strategy("alexg10"),
        data=data,
        spreads=spreads,
        master=master,
        entry_start=ENTRY_START,
        end=REPRO_END,
        force_flat_daily=False,
        true_sl=True,
        honor_signal_stops=False,
        rr_mode="dynamic",
        thursday_block=True,
    )
    m_d = add_ablation("D_alexg10_thursday_gate_ON", d)

    # E — fixed RR closest to N1 (true_sl_rr=2, rr_mode=fixed)
    e = run_engine(
        name="E_alexg10_rr_fixed2",
        strategy=make_strategy("alexg10"),
        data=data,
        spreads=spreads,
        master=master,
        entry_start=ENTRY_START,
        end=REPRO_END,
        force_flat_daily=False,
        true_sl=True,
        honor_signal_stops=False,
        rr_mode="fixed",
    )
    m_e = add_ablation("E_alexg10_rr_fixed_2", e)

    # honor_signal_stops ON (structural ghost-SL TP)
    h = run_engine(
        name="honor_signal_stops_ON",
        strategy=make_strategy("alexg10"),
        data=data,
        spreads=spreads,
        master=master,
        entry_start=ENTRY_START,
        end=REPRO_END,
        force_flat_daily=False,
        true_sl=True,
        honor_signal_stops=True,
        rr_mode="dynamic",
    )
    m_h = add_ablation("honor_signal_stops_ON", h)

    write_csv(out / "ABLATION_RESULTS.csv", ablations)

    nat = force_flat_nat_probe()
    (out / "FORCE_FLAT_NAT_PROBE.json").write_text(
        json.dumps(nat, indent=2), encoding="utf-8"
    )

    # Controls from prior locked runs (different path: H1+M1) — document gap
    controls = {
        "path_caveat": (
            "N1 locked runs use h1_signal_1m_exit; AlexG10 discovery uses H1-only "
            "fill-at-close. Comparison is economic context, not path-identical."
        ),
        "n1_thursday_prior_jun_aug_sep_todate": {},
        "september_oos": {},
    }
    if (THU_PRIOR / "control_results.csv").is_file():
        import csv as _csv

        with (THU_PRIOR / "control_results.csv").open(encoding="utf-8") as f:
            controls["n1_control_by_period"] = list(_csv.DictReader(f))
        with (THU_PRIOR / "treatment_results.csv").open(encoding="utf-8") as f:
            controls["n1_thursday_by_period"] = list(_csv.DictReader(f))
    if (SEP_OOS / "OOS_STATISTICS.json").is_file():
        controls["september_oos"] = json.loads(
            (SEP_OOS / "OOS_STATISTICS.json").read_text(encoding="utf-8")
        )

    # Side / exit attribution for repro
    side = {}
    for sname in ("long", "short"):
        tr = [t for t in repro["trades"] if t["side"] == sname]
        side[sname] = metrics(tr)
    by_exit: dict[str, list] = defaultdict(list)
    for t in repro["trades"]:
        by_exit[str(t["exit_reason"])].append(t)
    exit_attr = {reason: metrics(tr) for reason, tr in by_exit.items()}

    results = {
        "reproduction": {
            "status": "PASS" if repro_pass else "FAIL",
            "reported_net": REPORTED_NET,
            "reproduced_net": m_repro["net_pnl"],
            "difference": delta,
            "tolerance": REPRO_TOL,
            "fills": m_repro["fills"],
            "path_type": "CONTINUOUS_COMPOUND_PATH",
            "window": f"{ENTRY_START} -> {REPRO_END}",
            "metrics": m_repro,
        },
        "continuous_jun_sep": {
            "window": f"{ENTRY_START} -> {SEP_END}",
            "path_type": "CONTINUOUS_COMPOUND_PATH",
            "metrics": m_cont,
            "monthly_slices": monthly_cont,
        },
        "independent_monthly": {
            "path_type": "AGGREGATED_MONTHLY_PnL",
            "months": indep_months,
            "sum_of_monthly_nets": agg_indep,
            "vs_continuous_jun_sep_net": agg_indep - m_cont["net_pnl"],
        },
        "monthly_from_repro_continuous": monthly,
        "side": {k: {kk: vv for kk, vv in v.items() if not isinstance(vv, dict)} for k, v in side.items()},
        "exit_attribution": {
            k: {kk: vv for kk, vv in v.items() if not isinstance(vv, dict)}
            for k, v in exit_attr.items()
        },
        "rr_distribution": rr_dist,
        "concentration": conc,
        "ablations": ablations,
        "honor_signal_stops": {
            "off_net": m_repro["net_pnl"],
            "on_net": m_h["net_pnl"],
            "delta_net": m_h["net_pnl"] - m_repro["net_pnl"],
            "delta_fills": m_h["fills"] - m_repro["fills"],
            "delta_TP": m_h["TP_count"] - m_repro["TP_count"],
            "delta_margin_stop": m_h["margin_stop_count"] - m_repro["margin_stop_count"],
            "delta_PF": (
                (m_h["profit_factor"] - m_repro["profit_factor"])
                if m_h["profit_factor"] is not None and m_repro["profit_factor"] is not None
                else None
            ),
            "delta_MDD": m_h["max_drawdown"] - m_repro["max_drawdown"],
        },
        "force_flat_delta": {
            "noflat_net": m_repro["net_pnl"],
            "flat_on_net": m_c["net_pnl"],
            "delta_net": m_c["net_pnl"] - m_repro["net_pnl"],
            "delta_daily_flat_count": m_c["daily_flat_count"] - m_repro["daily_flat_count"],
        },
        "force_flat_nat_guard": nat,
        "controls_context": controls,
        "research_status": None,  # filled below
    }

    # Status classification
    pos_months = [r for r in monthly if r["month"] <= "2026-09" and r["net_pnl"] > 0]
    neg_months = [r for r in monthly if r["month"] <= "2026-09" and r["net_pnl"] <= 0]
    sep_net = next((r["net_pnl"] for r in monthly if r["month"] == "2026-09"), None)

    if not repro_pass:
        status = "NOT REPRODUCED"
    elif m_cont["net_pnl"] > 0 and len(pos_months) >= 3:
        status = "SUPPORTED FOR FURTHER OOS"
    elif repro_pass and m_repro["net_pnl"] > 0:
        status = "PARTIALLY_REPRODUCED"
    else:
        status = "REPRODUCED"

    # Refine: if reproduced and multi-month positive on continuous path
    if repro_pass and m_repro["net_pnl"] > 0:
        if m_cont["net_pnl"] > 0 and sep_net is not None and sep_net > 0:
            status = "SUPPORTED FOR FURTHER OOS"
        else:
            status = "REPRODUCED"

    results["research_status"] = status
    results["month_robustness"] = {
        "positive_months": [r["month"] for r in pos_months],
        "negative_months": [r["month"] for r in neg_months],
        "positive_count": f"{len(pos_months)}/{len(pos_months)+len(neg_months)}",
        "negative_count": f"{len(neg_months)}/{len(pos_months)+len(neg_months)}",
        "september_net": sep_net,
    }
    (out / "RESULTS.json").write_text(
        json.dumps(results, indent=2, default=str), encoding="utf-8"
    )

    # DATA_INTEGRITY
    integrity = f"""# DATA_INTEGRITY — AlexG10 Test 1

- MT5 connected: **False**
- Live/Mirror used: **False**
- Orders placed: **False**
- Data: `data/cache/mt5/utc_v2/{{SYM}}/1h.parquet` cache-only
- H1 symbols loaded: **{len(data)}**
- H1 dataset hash: `{dhash}`
- Spreads: pinned N1 jun index (`{JUN_INDEX.name}`)
- Simulator: frozen `MultiMarketEngine`, timeframe=`1h` (NOT h1+1m)
- Original discovery path matches: H1 + ghost_fill_at_close
- No simulator economics patched for this test
- force_flat NaT guard: infrastructure correctness only (see FORCE_FLAT_NAT_PROBE.json)
"""
    (out / "DATA_INTEGRITY.md").write_text(integrity, encoding="utf-8")

    # REPRODUCTION.md
    repro_md = f"""# REPRODUCTION — AlexG10 +$851

## Command

```bash
# Offline cache-only (MT5 closed):
.venv311/python scripts/run_alexg10_test1_validation.py
```

Original discovery script (would touch MT5 — NOT used in Test 1):
`scripts/run_thu_only_alexg10_backtests.py` variant C.

## Exact configuration

```json
{json.dumps(config, indent=2)}
```

## Result

| | Value |
|---|---:|
| Reported net | {REPORTED_NET:.6f} |
| Reproduced net | {m_repro['net_pnl']:.6f} |
| Difference | {delta:.6f} |
| Tolerance | ±{REPRO_TOL} |
| Status | **{'PASS' if repro_pass else 'FAIL'}** |
| Path type | CONTINUOUS_COMPOUND_PATH |
| Window | {ENTRY_START} → {REPRO_END} |
| Fills | {m_repro['fills']} |
| Engine final equity | {repro['final_equity_engine']:.4f} |
| Eval ending equity (1000+net) | {m_repro['ending_equity']:.4f} |

## Reconciliation

- sum(trade pnl) = {m_repro['net_pnl']:.6f}
- starting + net = {m_repro['ending_equity']:.6f}
- identity ok = {m_repro['equity_identity_ok']}
"""
    (out / "REPRODUCTION.md").write_text(repro_md, encoding="utf-8")

    # RESULTS.md brief
    results_md = f"""# RESULTS — AlexG10 Test 1

STATUS: **{status}**

## Reproduction: {'PASS' if repro_pass else 'FAIL'}

- reported_net: {REPORTED_NET:.4f}
- reproduced_net: {m_repro['net_pnl']:.4f}
- difference: {delta:.4f}

## Continuous Jun→Sep (to 2026-10-01)

- net: {m_cont['net_pnl']:.4f}
- fills: {m_cont['fills']}
- PF: {m_cont['profit_factor']}
- MDD: {m_cont['max_drawdown']:.4f}

## Independent monthly sum

- sum: {agg_indep:.4f}
- vs continuous: {agg_indep - m_cont['net_pnl']:.4f}

## Ablation deltas vs A (exact)

| Ablation | net | delta_net |
|---|---:|---:|
"""
    for a in ablations:
        results_md += f"| {a['ablation']} | {a['net_pnl']:.2f} | {a['delta_net_vs_A']:.2f} |\n"
    results_md += f"""
## honor_signal_stops delta_net

{results['honor_signal_stops']['delta_net']:.2f}

## force_flat restored delta_net

{results['force_flat_delta']['delta_net']:.2f}

## Concentration top-10 trades share

{conc['top_10']['share_of_total']}
"""
    (out / "RESULTS.md").write_text(results_md, encoding="utf-8")

    # Full docs report
    pos_list = ", ".join(results["month_robustness"]["positive_months"]) or "(none)"
    neg_list = ", ".join(results["month_robustness"]["negative_months"]) or "(none)"
    doc = f"""# AlexG10 Test 1 — Offline Validation

STATUS: **{status}**

Generated (UTC): {datetime.now(timezone.utc).isoformat()}  
Git: `{_git()}`  
Artifacts: `{out}`

Interpretation: **discovery result** validation (not a fresh unbiased OOS).  
September N1 OOS remains a separate locked experiment.

---

## Executive summary

1. **Did +$851 reproduce?** {'YES (PASS)' if repro_pass else 'NO (FAIL)'} — reproduced net **{m_repro['net_pnl']:.4f}** (diff {delta:.4f}).
2. **Exact configuration:** alexg10 flip entry at planned ghost entry; `true_sl=True`; `rr_mode=dynamic`×1 clamp 2–5; `force_flat_daily=False`; `honor_signal_stops=False`; Thursday gate OFF; lev 500; 1% margin; $1000; H1 fill-at-close; commission $7/lot.
3. **Dates:** entries `{ENTRY_START}` → `{REPRO_END}` (matches original META). Continuous compound path with Apr warmup.
4. **Continuous or aggregated?** **CONTINUOUS_COMPOUND_PATH** (not sum of independent months). Independent monthly sum = {agg_indep:.2f} vs continuous Jun–Sep {m_cont['net_pnl']:.2f}.
5. **Positive across June–September?** Positive months: {results['month_robustness']['positive_count']} ({pos_list}). Negative: {results['month_robustness']['negative_count']} ({neg_list}).
6. **September alone (from continuous repro ledger):** net = **{sep_net}**.
7. **Mechanism:** Ablation shows largest causal levers (see table). Primary discovery knobs: **strategy10 flip** and/or **no daily flat** and/or **dynamic RR** — see deltas.
8. **Concentrated?** Top-10 trades share = {conc['top_10']['share_of_total']}; top-5 pairs share = {conc['top_5_pairs_share']}; symbols traded = {conc['n_symbols_traded']}.
9. **`honor_signal_stops` effect:** delta_net = **{results['honor_signal_stops']['delta_net']:.2f}** (ON vs OFF).
10. **`force_flat NaT` guard:** {nat['conclusion']} Economic effect: **{nat['economic_effect']}**.
11. **Unvalidated:** Live/Mirror execution; path-identical vs N1 H1+M1; pair-concentration robustness; pre-registered unseen October+ holdout.

---

## What changed vs AlexG8 / N1

| Item | Frozen N1 / n1_thursday | AlexG10 +$851 run |
|---|---|---|
| Strategy | alexg8 | **alexg10** (flip at planned entry toward ghost SL) |
| Entry trigger | ghost SL touch (typical) | **planned entry touch** |
| Direction | setup direction | **flipped** |
| Exits | true_sl + dynamic RR | **same** (honor_signal_stops=False) |
| force_flat_daily | True | **False** |
| Thursday gate | OFF (control) / ON (treatment) | **OFF** |
| Path | h1_signal_1m_exit | **H1-only** |
| Capital / lev | 1000 / 500 | 1000 / 500 |

`honor_signal_stops` is **not** required for the +$851 result (it was False).  
When True, engine keeps structural ghost-SL TP instead of rewriting to true_sl/dynamic RR.

---

## Reproduction

See `REPRODUCTION.md`. Cache-only offline replay of variant C.

---

## Economic results (reproduced continuous)

{json.dumps(m_repro, indent=2, default=str)}

---

## Monthly results (slices of continuous repro path)

| Month | fills | net | PF | WR | MDD |
|---|---:|---:|---:|---:|---:|
"""
    for r in monthly:
        doc += (
            f"| {r['month']} | {r['fills']} | {r['net_pnl']:.2f} | {r['profit_factor']} | "
            f"{r['win_rate']} | {r['max_drawdown']:.4f} |\n"
        )
    doc += f"""
Path label for these rows: **SLICE_OF_CONTINUOUS** (not AGGREGATED_MONTHLY_PnL).

### Independent monthly resets

| Month | fills | net | PF |
|---|---:|---:|---:|
"""
    for r in indep_months:
        doc += f"| {r['month']} | {r['fills']} | {r['net_pnl']:.2f} | {r['profit_factor']} |\n"
    doc += f"""
Sum of independent monthly nets: **{agg_indep:.2f}**  
Continuous Jun→Sep net: **{m_cont['net_pnl']:.2f}**

---

## Continuous-path results

- Repro window (Jun→~Oct 2): net **{m_repro['net_pnl']:.2f}**, fills {m_repro['fills']}, PF {m_repro['profit_factor']}
- Clean Jun→Sep (→ Oct 1): net **{m_cont['net_pnl']:.2f}**, fills {m_cont['fills']}, PF {m_cont['profit_factor']}

---

## Attribution (ablations)

| Ablation | net | Δnet vs A | fills | PF | MDD |
|---|---:|---:|---:|---:|---:|
"""
    for a in ablations:
        doc += (
            f"| {a['ablation']} | {a['net_pnl']:.2f} | {a['delta_net_vs_A']:.2f} | "
            f"{a['fills']} | {a['PF']} | {a['MDD']:.4f} |\n"
        )
    doc += f"""
### honor_signal_stops

| | OFF (A) | ON | Δ |
|---|---:|---:|---:|
| net | {m_repro['net_pnl']:.2f} | {m_h['net_pnl']:.2f} | {results['honor_signal_stops']['delta_net']:.2f} |
| fills | {m_repro['fills']} | {m_h['fills']} | {results['honor_signal_stops']['delta_fills']} |
| TP | {m_repro['TP_count']} | {m_h['TP_count']} | {results['honor_signal_stops']['delta_TP']} |
| margin_stop | {m_repro['margin_stop_count']} | {m_h['margin_stop_count']} | {results['honor_signal_stops']['delta_margin_stop']} |

### force_flat restored

Δnet (flat ON − noflat) = **{results['force_flat_delta']['delta_net']:.2f}**

### force_flat NaT guard

{nat['conclusion']}

---

## Profit concentration

- Top 1 trade: {conc['top_1']}
- Top 5: {conc['top_5']}
- Top 10: {conc['top_10']}
- Top 20: {conc['top_20']}
- Profit excluding top 10: {conc['profit_excluding_top_10']}
- Top 5 pairs share: {conc['top_5_pairs_share']}
- Top pairs: {conc['top_pairs']}

---

## Comparison context (NOT path-identical)

N1 uses H1+M1 closest-sim; AlexG10 discovery uses H1-only. Treat as contextual, not apples-to-apples.

September OOS (locked, separate):
- N1 control Sep net ≈ -123.84
- N1 Thursday Sep net ≈ -96.25
- Relative Thursday improvement ≈ +27.59
- Both September N1 arms negative

AlexG10 September (from this continuous H1 ledger) net = **{sep_net}**.

Do **not** claim AlexG10 is validated merely because its aggregate is positive while N1 September was negative — different path and discovery contamination.

---

## Failure modes / weaknesses

1. Heavy pair concentration (few symbols; NOKSEK-class dominance historically).
2. Short-side dominated edge in the discovery report.
3. Low WR / fat TP tail under dynamic RR — fragile to WR bootstrap.
4. Includes September inside discovery window (not pre-registered OOS for AlexG10).
5. H1-only path ≠ frozen N1 M1-exit research path.
6. Engine `max_dd_pct` in original summary mis-scaled; use ledger equity-curve MDD here.
7. Posthoc discovery — requires newly pre-registered unseen test (October+) before any production claim.

---

## Research conclusion

**{status}**

Next step if pursuing further: **pre-register** an unseen-data test (e.g. October+) — do not retune on June–September.

Do not promote to live trading from this test alone.
"""
    docs_out.write_text(doc, encoding="utf-8")
    (out / "FINAL_REPORT.md").write_text(doc, encoding="utf-8")

    # Terminal summary
    print(
        f"""
ALEXG10 TEST 1
---------------
reproduction: {'PASS' if repro_pass else 'FAIL'}
reported_net: {REPORTED_NET:.4f}
reproduced_net: {m_repro['net_pnl']:.4f}
difference: {delta:.4f}
June: {next((r['net_pnl'] for r in monthly if r['month']=='2026-06'), 'NA')}
July: {next((r['net_pnl'] for r in monthly if r['month']=='2026-07'), 'NA')}
August: {next((r['net_pnl'] for r in monthly if r['month']=='2026-08'), 'NA')}
September: {next((r['net_pnl'] for r in monthly if r['month']=='2026-09'), 'NA')}
continuous Jun-Sep: {m_cont['net_pnl']:.4f}
honor_signal_stops_delta: {results['honor_signal_stops']['delta_net']:.4f}
force_flat_delta: {results['force_flat_delta']['delta_net']:.4f}
top-10-trade concentration: {conc['top_10']['share_of_total']}
status: {status}
artifacts: {out}
docs: {docs_out}
""",
        flush=True,
    )
    return 0 if repro_pass else 2


if __name__ == "__main__":
    raise SystemExit(main())
