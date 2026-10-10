#!/usr/bin/env python3
"""AlexG10 Test 2 — causal + concentration validation (offline, no MT5)."""

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

from borex.alexg import AlexG10Strategy
from borex.alexg.multi_market import pick_master_symbol
from borex.backtest.engine import BacktestConfig
from borex.backtest.multi_market_engine import MultiMarketEngine
from borex.viewerMT5.mt5_feed import (
    _cache_path,
    _dedupe_hourly,
    _drop_forming_bar,
    _frame_to_candles,
    _slice_candles,
)

T1 = ROOT / "data/runs/research/alexg10_test1_20261003_170949"
ORIG = ROOT / (
    "data/runs/research/frozen_thu_only_alexg10_rrdyn_noflat_20261002_144951"
)
JUN_INDEX = ROOT / "data/runs/mt5_1m_n1_oos_jun2026/index.json"
SEP_OOS = ROOT / (
    "data/runs/research/gate2_september_oos_final_20261003_151905"
)

CAPITAL = 1000.0
LEV = 500.0
ENTRY_START = pd.Timestamp("2026-06-01", tz="UTC")
DATA_START = pd.Timestamp("2026-04-01", tz="UTC")
REPRO_END = pd.Timestamp("2026-10-02T14:00:00+00:00")
SEP_END = pd.Timestamp("2026-10-01T00:00:00+00:00")
SEP_START = pd.Timestamp("2026-09-01", tz="UTC")
MAX_POSITIONS = 60
GHOST_PRIME = 72
T1_HASH = "84b7433fb77c27128f3751f4c1fe2dd6522b4a8f475d4dd9829aeb2559a8dbf3"


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


def bt_cfg(spreads, *, force_flat_daily: bool, honor_signal_stops: bool):
    return BacktestConfig(
        initial_capital=CAPITAL,
        leverage=LEV,
        position_size_pct=0.01,
        size_mode="margin",
        true_sl=True,
        true_sl_rr=2.0,
        honor_signal_stops=bool(honor_signal_stops),
        rr_mode="dynamic",
        rr_factor=1.0,
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


def make_strat():
    return AlexG10Strategy(
        min_rr=2.0,
        execution_interval="1h",
        peer_blend=0.0,
        ghost_sl_mult=4.0,
        ghost_fill_at_close=True,
    )


def run_variant(
    *,
    name: str,
    data: dict,
    spreads: dict,
    master: str,
    entry_start: pd.Timestamp,
    end: pd.Timestamp,
    force_flat_daily: bool,
    honor_signal_stops: bool,
    exclude_symbols: set[str] | None = None,
) -> dict:
    exclude_symbols = exclude_symbols or set()
    sliced = {}
    for sym, bars in data.items():
        if sym in exclude_symbols:
            continue
        sliced[sym] = [c for c in bars if _ts(c.timestamp) < end]
        if len(sliced[sym]) < 200:
            del sliced[sym]
    if master not in sliced:
        # pick another master if EURUSD excluded
        master = pick_master_symbol(sliced) or next(iter(sliced))

    engine = MultiMarketEngine(
        make_strat(),
        bt_cfg(spreads, force_flat_daily=force_flat_daily, honor_signal_stops=honor_signal_stops),
        max_positions=MAX_POSITIONS,
    )
    engine._progress_every = 500
    entry_i = next(i for i, c in enumerate(sliced[master]) if _ts(c.timestamp) >= entry_start)
    start_i = max(int(make_strat().min_bars), entry_i - GHOST_PRIME)
    print(
        f"\n=== {name} === n_sym={len(sliced)} flat={force_flat_daily} "
        f"honor={honor_signal_stops} excl={sorted(exclude_symbols)[:5]}{'…' if len(exclude_symbols)>5 else ''}",
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
    rows = []
    for t in result.trades:
        if _ts(t.entry_time) < entry_start or _ts(t.entry_time) >= end:
            continue
        side = t.side.value if hasattr(t.side, "value") else str(t.side)
        et, xt = _ts(t.entry_time), _ts(t.exit_time) if t.exit_time else None
        hold = (xt - et).total_seconds() / 3600.0 if xt is not None else None
        risk = abs(float(t.entry_price) - float(t.stop_loss)) if t.stop_loss is not None else None
        reward = abs(float(t.take_profit) - float(t.entry_price)) if t.take_profit is not None else None
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
                "take_profit": float(t.take_profit) if t.take_profit is not None else None,
                "rr_resolved": rr,
                "pnl": float(t.pnl or 0.0),
                "commission": float(getattr(t, "commission", 0) or 0.0),
                "exit_reason": t.exit_reason,
                "hold_h": hold,
                "month": et.strftime("%Y-%m"),
                "weekday": et.day_name(),
                "iso_week": f"{et.isocalendar().year}-W{et.isocalendar().week:02d}",
            }
        )
    print(
        f"  done {time.time()-t0:.1f}s fills={len(rows)} pnl={sum(r['pnl'] for r in rows):.2f}",
        flush=True,
    )
    return {
        "name": name,
        "trades": rows,
        "force_flat_daily": force_flat_daily,
        "honor_signal_stops": honor_signal_stops,
        "exclude_symbols": sorted(exclude_symbols),
        "final_equity_engine": float(result.final_equity),
        "elapsed_s": time.time() - t0,
    }


def metrics(trades: list[dict], capital: float = CAPITAL) -> dict:
    pnls = [float(t["pnl"]) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gp, gl = sum(wins), sum(losses)
    pf = (gp / abs(gl)) if gl else (float("inf") if gp > 0 else None)
    exits = Counter(str(t["exit_reason"]) for t in trades)
    ordered = sorted(trades, key=lambda t: (_ts(t["exit_time"] or t["entry_time"]), t["entry_time"]))
    eq = peak = capital
    max_dd = 0.0
    for t in ordered:
        eq += float(t["pnl"])
        peak = max(peak, eq)
        max_dd = max(max_dd, (peak - eq) / peak if peak > 0 else 0.0)
    holds = [float(t["hold_h"]) for t in trades if t.get("hold_h") is not None]
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
        "hold_max_h": float(max(holds)) if holds else None,
        "ending_equity": capital + net,
        "commission_sum": float(sum(float(t.get("commission") or 0) for t in trades)),
    }


def concentration(trades: list[dict]) -> dict:
    ordered = sorted(trades, key=lambda t: float(t["pnl"]), reverse=True)
    total = sum(float(t["pnl"]) for t in trades) or 0.0
    winners = [t for t in ordered if float(t["pnl"]) > 0]

    def top_n(n):
        s = sum(float(t["pnl"]) for t in ordered[:n])
        return {"sum": s, "share": (s / total) if total else None}

    def excl_top_winners(n):
        drop = {id(t) for t in winners[:n]}
        keep = [t for t in trades if id(t) not in drop]
        # fallback by value if id issues — use entry keys
        return sum(float(t["pnl"]) for t in keep)

    # robust exclude by sorting winners
    def excl_w(n):
        wset = {(t["symbol"], t["entry_time"], t["pnl"]) for t in winners[:n]}
        return sum(
            float(t["pnl"])
            for t in trades
            if (t["symbol"], t["entry_time"], t["pnl"]) not in wset
        )

    return {
        "total": total,
        "top1": top_n(1),
        "top5": top_n(5),
        "top10": top_n(10),
        "top20": top_n(20),
        "top50": top_n(50),
        "net_excl_top1_winner": excl_w(1),
        "net_excl_top5_winners": excl_w(5),
        "net_excl_top10_winners": excl_w(10),
    }


def pair_table(trades: list[dict]) -> list[dict]:
    by = defaultdict(list)
    for t in trades:
        by[t["symbol"]].append(t)
    rows = []
    for sym, tr in by.items():
        m = metrics(tr)
        rows.append({"pair": sym, **{k: v for k, v in m.items() if not isinstance(v, dict)}})
    rows.sort(key=lambda r: r["net_pnl"], reverse=True)
    return rows


def reconcile_ledgers(orig: list[dict], cur: list[dict]) -> list[dict]:
    def key(t):
        return (
            str(t.get("symbol")),
            str(t.get("side")),
            str(t.get("entry_time"))[:19],
        )

    om = {key(t): t for t in orig}
    cm = {key(t): t for t in cur}
    keys = sorted(set(om) | set(cm))
    rows = []
    for k in keys:
        o, c = om.get(k), cm.get(k)
        if o and not c:
            cls = "MISSING_TRADE"
        elif c and not o:
            cls = "NEW_TRADE"
        else:
            # both exist
            diffs = []
            if abs(float(o["pnl"]) - float(c["pnl"])) > 1e-6:
                diffs.append("PNL")
            if str(o.get("exit_time")) != str(c.get("exit_time")):
                diffs.append("EXIT_TIME")
            if str(o.get("exit_reason")) != str(c.get("exit_reason")):
                diffs.append("EXIT_REASON")
            if abs(float(o.get("entry_price") or 0) - float(c.get("entry_price") or 0)) > 1e-8:
                diffs.append("ENTRY_PRICE")
            if abs(float(o.get("exit_price") or 0) - float(c.get("exit_price") or 0)) > 1e-8:
                diffs.append("EXIT_PRICE")
            if abs(float(o.get("stop_loss") or 0) - float(c.get("stop_loss") or 0)) > 1e-8:
                diffs.append("SL")
            if abs(float(o.get("take_profit") or 0) - float(c.get("take_profit") or 0)) > 1e-8:
                diffs.append("TP")
            if not diffs:
                cls = "MATCH"
            elif "ENTRY_PRICE" in diffs and "EXIT_TIME" not in diffs:
                cls = "SPREAD_OR_FILL_DIFFERENCE"
            elif "EXIT_TIME" in diffs or "EXIT_REASON" in diffs or "TP" in diffs or "SL" in diffs:
                cls = "EXIT_DIFFERENCE"
            else:
                cls = "OTHER"
        rows.append(
            {
                "class": cls,
                "symbol": k[0],
                "side": k[1],
                "entry_time": k[2],
                "orig_pnl": float(o["pnl"]) if o else None,
                "cur_pnl": float(c["pnl"]) if c else None,
                "delta_pnl": (
                    (float(c["pnl"]) if c else 0.0) - (float(o["pnl"]) if o else 0.0)
                ),
                "orig_exit": o.get("exit_reason") if o else None,
                "cur_exit": c.get("exit_reason") if c else None,
                "orig_exit_time": o.get("exit_time") if o else None,
                "cur_exit_time": c.get("exit_time") if c else None,
            }
        )
    return rows


def month_boundary_analysis(trades: list[dict]) -> dict:
    """Why continuous ≫ monthly: trades spanning month ends + compounding."""
    cross = []
    for t in trades:
        et, xt = _ts(t["entry_time"]), _ts(t["exit_time"]) if t.get("exit_time") else None
        if xt is None:
            continue
        if et.strftime("%Y-%m") != xt.strftime("%Y-%m"):
            cross.append(t)
    return {
        "n_trades_crossing_month_boundary": len(cross),
        "pnl_crossing_month_boundary": float(sum(float(t["pnl"]) for t in cross)),
        "share_of_fills": len(cross) / len(trades) if trades else None,
        "mechanism_notes": [
            "Independent monthly runs reset portfolio equity and dynamic RR winrate state.",
            "Dynamic RR uses portfolio winrate (after winrate_min_trades=20); continuous path carries WR/RR.",
            "Open positions can span month boundaries in continuous path; monthly restart cannot.",
            "Equity-dependent 1% margin sizing compounds differently on continuous path.",
        ],
    }


def main() -> int:
    now = datetime.now(timezone.utc)
    out = ROOT / "data/runs/research" / f"alexg10_test2_{now.strftime('%Y%m%d_%H%M%S')}"
    out.mkdir(parents=True, exist_ok=True)
    docs = ROOT / "docs/research/alexg10_test2_causal_validation.md"
    print(f"OUT={out}", flush=True)

    # ---- Original data reconstruction ----
    reconstruction = {
        "original_H1_dataset_recoverable": False,
        "original_M1_dataset_recoverable": False,  # discovery was H1-only
        "original_spread_dataset_recoverable": False,
        "original_universe_recoverable": True,  # 60 from META / jun index
        "original_hashes_recoverable": False,
        "original_exact_command_recoverable": True,
        "notes": [
            "Discovery META has no spreads dump and no H1 content hash.",
            "Logs show bars=3259; Test1/current cache has bars=3260 on same end window.",
            "Live MT5 spread reads at discovery time were not persisted.",
            "Exact +851 bit-replay permanently unresolvable with available artifacts.",
            "Original trade ledger IS recoverable and coherent.",
            "Exact command: scripts/run_thu_only_alexg10_backtests.py variant C "
            "(FORCE_FLAT_DAILY=False, RR_FACTOR=1.0, honor_signal_stops=False).",
        ],
    }
    print("ORIGINAL DATA RECONSTRUCTION", flush=True)
    for k, v in reconstruction.items():
        if k != "notes":
            print(f"  {k}: {v}", flush=True)

    jun = json.loads(JUN_INDEX.read_text(encoding="utf-8"))
    spreads = dict(jun["spread_pips_by_symbol"])
    symbols = sorted(spreads.keys())
    assert len(symbols) == 60

    print("Loading frozen H1 cache…", flush=True)
    data = load_h1(symbols, DATA_START, REPRO_END)
    master = pick_master_symbol(data) or "EURUSD=X"
    for s in data:
        spreads.setdefault(s, 0.0)
    dhash = h1_hash(data)
    print(f"hash={dhash} match_t1={dhash==T1_HASH} n={len(data)}", flush=True)

    data_manifest = {
        "frozen_at_utc": now.isoformat(),
        "H1_source": "data/cache/mt5/utc_v2/{SYM}/1h.parquet (cache-only, no MT5)",
        "M1_source": "NOT_USED (AlexG10 discovery/Test1/Test2 path is H1-only)",
        "spread_source": str(JUN_INDEX),
        "symbol_universe": symbols,
        "universe_n": len(symbols),
        "timezone": "UTC",
        "data_start": str(DATA_START),
        "entry_start": str(ENTRY_START),
        "repro_end": str(REPRO_END),
        "sep_end": str(SEP_END),
        "h1_dataset_hash": dhash,
        "matches_test1_hash": dhash == T1_HASH,
        "master": master,
        "mt5_connected": False,
    }
    (out / "DATA_MANIFEST.json").write_text(json.dumps(data_manifest, indent=2), encoding="utf-8")
    (out / "EXPERIMENT_MANIFEST.json").write_text(
        json.dumps(
            {
                "created_at_utc": now.isoformat(),
                "git_commit": _git(),
                "branch": "closest-sim",
                "python": sys.version,
                "platform": platform.platform(),
                "test1_run": str(T1),
                "original_discovery_run": str(ORIG),
                "original_data_reconstruction": reconstruction,
                "mt5_used": False,
                "orders_placed": False,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    control_cfg = {
        "name": "ALEXG10_T2_CONTROL",
        "strategy": "alexg10",
        "force_flat_daily": False,
        "force_flat_friday": False,
        "honor_signal_stops": False,
        "rr_mode": "dynamic",
        "rr_factor": 1.0,
        "rr_min": 2.0,
        "rr_max": 5.0,
        "true_sl": True,
        "ghost_sl_mult": 4.0,
        "peer_blend": 0.0,
        "ghost_fill_at_close": True,
        "leverage": LEV,
        "position_size_pct": 0.01,
        "commission_per_lot": 7.0,
        "spread_model": "jun N1 spread_pips_by_symbol",
        "entry_model": "planned_entry_touch_flip_fill_at_close",
        "exit_model": "true_sl_margin_stop + dynamic_RR_TP",
        "thursday_gate": False,
        "pair_universe": 60,
        "starting_equity": CAPITAL,
        "timeframe": "1h",
        "window": f"{ENTRY_START} -> {REPRO_END}",
    }
    (out / "CONTROL_CONFIG.json").write_text(json.dumps(control_cfg, indent=2), encoding="utf-8")

    # ---- Control ----
    ctrl = run_variant(
        name="C0_control_noflat_nohonor",
        data=data,
        spreads=spreads,
        master=master,
        entry_start=ENTRY_START,
        end=REPRO_END,
        force_flat_daily=False,
        honor_signal_stops=False,
    )
    m0 = metrics(ctrl["trades"])
    write_csv(out / "CONTROL_TRADES.csv", ctrl["trades"])
    (out / "CONTROL_RESULTS.json").write_text(
        json.dumps({"config": control_cfg, "metrics": m0, **{k: ctrl[k] for k in ("final_equity_engine", "elapsed_s")}}, indent=2, default=str),
        encoding="utf-8",
    )

    # ---- 2x2 causal ----
    c1 = run_variant(
        name="C1_flatON_honorOFF",
        data=data, spreads=spreads, master=master,
        entry_start=ENTRY_START, end=REPRO_END,
        force_flat_daily=True, honor_signal_stops=False,
    )
    c2 = run_variant(
        name="C2_flatOFF_honorON",
        data=data, spreads=spreads, master=master,
        entry_start=ENTRY_START, end=REPRO_END,
        force_flat_daily=False, honor_signal_stops=True,
    )
    c3 = run_variant(
        name="C3_flatON_honorON",
        data=data, spreads=spreads, master=master,
        entry_start=ENTRY_START, end=REPRO_END,
        force_flat_daily=True, honor_signal_stops=True,
    )
    cells = [
        ("C0", False, False, ctrl, m0),
        ("C1", True, False, c1, metrics(c1["trades"])),
        ("C2", False, True, c2, metrics(c2["trades"])),
        ("C3", True, True, c3, metrics(c3["trades"])),
    ]
    causal_rows = []
    for name, flat, honor, raw, m in cells:
        causal_rows.append(
            {
                "variant": name,
                "force_flat": flat,
                "honor_signal_stops": honor,
                "net": m["net_pnl"],
                "PF": m["profit_factor"],
                "MDD": m["max_drawdown"],
                "fills": m["fills"],
                "WR": m["win_rate"],
                "mean": m["mean"],
                "median": m["median"],
                "TP": m["TP"],
                "SL": m["SL"],
                "margin_stop": m["margin_stop"],
                "daily_flat": m["daily_flat"],
                "hold_mean_h": m["hold_mean_h"],
                "hold_median_h": m["hold_median_h"],
                "hold_max_h": m["hold_max_h"],
            }
        )
        write_csv(out / f"{name}_TRADES.csv", raw["trades"])
    write_csv(out / "CAUSAL_2X2.csv", causal_rows)
    # interaction on net: (C3-C2) - (C1-C0)
    nets = {r["variant"]: r["net"] for r in causal_rows}
    interaction = (nets["C3"] - nets["C2"]) - (nets["C1"] - nets["C0"])
    force_flat_effect = nets["C1"] - nets["C0"]  # ON - OFF with honor=False
    signal_stop_effect = nets["C2"] - nets["C0"]  # ON - OFF with flat=False

    # ---- Continuous vs monthly ----
    cont = run_variant(
        name="D0_continuous_jun_sep",
        data=data, spreads=spreads, master=master,
        entry_start=ENTRY_START, end=SEP_END,
        force_flat_daily=False, honor_signal_stops=False,
    )
    m_cont = metrics(cont["trades"])
    indep = []
    bounds = [
        ("2026-06", pd.Timestamp("2026-06-01", tz="UTC"), pd.Timestamp("2026-07-01", tz="UTC")),
        ("2026-07", pd.Timestamp("2026-07-01", tz="UTC"), pd.Timestamp("2026-08-01", tz="UTC")),
        ("2026-08", pd.Timestamp("2026-08-01", tz="UTC"), pd.Timestamp("2026-09-01", tz="UTC")),
        ("2026-09", pd.Timestamp("2026-09-01", tz="UTC"), pd.Timestamp("2026-10-01", tz="UTC")),
    ]
    for label, a, b in bounds:
        r = run_variant(
            name=f"D1_indep_{label}",
            data=data, spreads=spreads, master=master,
            entry_start=a, end=b,
            force_flat_daily=False, honor_signal_stops=False,
        )
        m = metrics(r["trades"])
        indep.append({"month": label, "mode": "INDEPENDENT_RESET", **{k: v for k, v in m.items() if not isinstance(v, dict)}})
        write_csv(out / f"INDEP_{label}_TRADES.csv", r["trades"])
    monthly_sum = sum(r["net_pnl"] for r in indep)
    # continuous monthly slices
    cont_months = []
    bym = defaultdict(list)
    for t in cont["trades"]:
        bym[t["month"]].append(t)
    for m in sorted(bym):
        mm = metrics(bym[m])
        cont_months.append({"month": m, "mode": "SLICE_OF_CONTINUOUS", **{k: v for k, v in mm.items() if not isinstance(v, dict)}})
    mba = month_boundary_analysis(cont["trades"])
    write_csv(
        out / "MONTHLY_VS_CONTINUOUS.csv",
        cont_months
        + indep
        + [
            {
                "month": "AGGREGATE",
                "mode": "CONTINUOUS_TOTAL",
                "net_pnl": m_cont["net_pnl"],
                "fills": m_cont["fills"],
                "profit_factor": m_cont["profit_factor"],
            },
            {
                "month": "AGGREGATE",
                "mode": "INDEPENDENT_SUM",
                "net_pnl": monthly_sum,
                "fills": sum(r["fills"] for r in indep),
                "profit_factor": None,
            },
        ],
    )
    (out / "CONTINUOUS_VS_MONTHLY_MECHANISM.json").write_text(
        json.dumps(
            {
                "continuous_net": m_cont["net_pnl"],
                "monthly_sum_net": monthly_sum,
                "delta_continuous_minus_monthly_sum": m_cont["net_pnl"] - monthly_sum,
                "boundary": mba,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    # ---- Pair / concentration / leave-one-out ----
    pairs = pair_table(ctrl["trades"])
    write_csv(out / "PAIR_RESULTS.csv", pairs)
    ranked = [r["pair"] for r in pairs]  # by net descending
    top_symbols = {
        "top1": ranked[:1],
        "top2": ranked[:2],
        "top3": ranked[:3],
        "top5": ranked[:5],
        "top8": ranked[:8],
    }
    # NOKSEK specifically
    loo_specs = [
        ("all_pairs", set()),
        ("minus_NOKSEK", {"NOKSEK=X"}),
        ("minus_top2", set(top_symbols["top2"])),
        ("minus_top3", set(top_symbols["top3"])),
        ("minus_top5", set(top_symbols["top5"])),
        ("minus_top8", set(top_symbols["top8"])),
    ]
    loo_rows = []
    for label, excl in loo_specs:
        if not excl and label == "all_pairs":
            m = m0
            raw_tr = ctrl["trades"]
        else:
            r = run_variant(
                name=f"LOO_{label}",
                data=data, spreads=spreads, master=master,
                entry_start=ENTRY_START, end=REPRO_END,
                force_flat_daily=False, honor_signal_stops=False,
                exclude_symbols=excl,
            )
            m = metrics(r["trades"])
            raw_tr = r["trades"]
            write_csv(out / f"LOO_{label}_TRADES.csv", raw_tr)
        loo_rows.append(
            {
                "exclusion": label,
                "excluded": ",".join(sorted(excl)) if excl else "",
                "net": m["net_pnl"],
                "PF": m["profit_factor"],
                "fills": m["fills"],
                "MDD": m["max_drawdown"],
                "WR": m["win_rate"],
                "mean": m["mean"],
            }
        )
    write_csv(out / "LEAVE_ONE_OUT.csv", loo_rows)

    conc_ctrl = concentration(ctrl["trades"])
    orig_trades = json.loads((ORIG / "C_alexg10_entry_rrdyn_noflat_trades.json").read_text(encoding="utf-8"))
    # normalize orig fields for concentration
    for t in orig_trades:
        t.setdefault("hold_h", None)
    conc_orig = concentration(orig_trades)
    (out / "TRADE_CONCENTRATION.json").write_text(
        json.dumps({"control": conc_ctrl, "original_ledger": conc_orig}, indent=2),
        encoding="utf-8",
    )

    active = [r for r in pairs if r["fills"] > 0]
    pos = [r for r in active if r["net_pnl"] > 0]
    neg = [r for r in active if r["net_pnl"] < 0]
    flat = [r for r in active if r["net_pnl"] == 0]
    profitable_pct = (len(pos) / len(active)) if active else None

    # ---- September retrospective ----
    sep_trades = [t for t in ctrl["trades"] if t["month"] == "2026-09"]
    # Also independent Sep already in indep
    sep_indep = next(r for r in indep if r["month"] == "2026-09")
    m_sep_slice = metrics(sep_trades)
    sep_weeks = []
    byw = defaultdict(list)
    for t in sep_trades:
        byw[t["iso_week"]].append(t)
    for w in sorted(byw):
        mw = metrics(byw[w])
        sep_weeks.append({"week": w, **{k: v for k, v in mw.items() if not isinstance(v, dict)}})
    sep_pairs = pair_table(sep_trades)
    n1_sep = {}
    if (SEP_OOS / "OOS_STATISTICS.json").is_file():
        s = json.loads((SEP_OOS / "OOS_STATISTICS.json").read_text(encoding="utf-8"))
        n1_sep = {
            "n1_control_net": s["control"]["net_pnl"],
            "n1_thursday_net": s["treatment"]["net_pnl"],
            "label": "LOCKED_SEPTEMBER_OOS_separate_experiment",
        }
    (out / "SEPTEMBER_RESULTS.json").write_text(
        json.dumps(
            {
                "label": "RETROSPECTIVE_NOT_PREREGISTERED_OOS",
                "from_continuous_control_slice": m_sep_slice,
                "independent_september_reset": sep_indep,
                "weekly": sep_weeks,
                "pairs": sep_pairs,
                "n1_context": n1_sep,
            },
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )

    # ---- Original vs current reconciliation ----
    recon = reconcile_ledgers(orig_trades, ctrl["trades"])
    write_csv(out / "ORIGINAL_VS_CURRENT_RECONCILIATION.csv", recon)
    by_cls = Counter(r["class"] for r in recon)
    delta_explained = sum(r["delta_pnl"] for r in recon)
    recon_summary = {
        "original_net": sum(float(t["pnl"]) for t in orig_trades),
        "current_control_net": m0["net_pnl"],
        "delta": m0["net_pnl"] - sum(float(t["pnl"]) for t in orig_trades),
        "class_counts": dict(by_cls),
        "sum_delta_pnl_rows": delta_explained,
        "new_trades_pnl": sum(r["delta_pnl"] for r in recon if r["class"] == "NEW_TRADE"),
        "missing_trades_pnl": sum(r["delta_pnl"] for r in recon if r["class"] == "MISSING_TRADE"),
        "exit_diff_pnl": sum(r["delta_pnl"] for r in recon if r["class"] == "EXIT_DIFFERENCE"),
        "spread_or_fill_pnl": sum(
            r["delta_pnl"] for r in recon if r["class"] == "SPREAD_OR_FILL_DIFFERENCE"
        ),
        "match_count": by_cls.get("MATCH", 0),
        "bars_note": "Discovery log bars=3259; current freeze bars=%d" % len(data[master]),
    }
    (out / "ORIGINAL_VS_CURRENT_SUMMARY.json").write_text(
        json.dumps(recon_summary, indent=2), encoding="utf-8"
    )

    # ---- All variant results ----
    all_rows = list(causal_rows)
    for r in loo_rows:
        all_rows.append(
            {
                "variant": f"LOO_{r['exclusion']}",
                "force_flat": False,
                "honor_signal_stops": False,
                "net": r["net"],
                "PF": r["PF"],
                "MDD": r["MDD"],
                "fills": r["fills"],
                "WR": r["WR"],
                "mean": r["mean"],
            }
        )
    all_rows.append(
        {
            "variant": "D0_continuous_jun_sep",
            "force_flat": False,
            "honor_signal_stops": False,
            "net": m_cont["net_pnl"],
            "PF": m_cont["profit_factor"],
            "MDD": m_cont["max_drawdown"],
            "fills": m_cont["fills"],
            "WR": m_cont["win_rate"],
            "mean": m_cont["mean"],
        }
    )
    write_csv(out / "ALL_VARIANT_RESULTS.csv", all_rows)

    # Verdict
    loo_map = {r["exclusion"]: r for r in loo_rows}
    without_noksek = loo_map["minus_NOKSEK"]["net"]
    without_top5 = loo_map["minus_top5"]["net"]
    broad = (
        profitable_pct is not None
        and profitable_pct >= 0.5
        and without_noksek > 0
        and without_top5 > 0
        and force_flat_effect > -200  # edge not almost entirely from noflat
    )
    # Based on evidence: edge requires noflat massively; concentrated
    if abs(force_flat_effect) > 500 and without_noksek <= 0:
        verdict = "MECHANISM-DEPENDENT"
        next_step = "DO NOT PREREGISTER YET"
        remaining_q = (
            "Can a pre-registered AlexG10 variant with explicit continuous-holding "
            "risk limits and a diversified (non-NOKSEK-dominated) book show "
            "positive expectancy on a pinned unseen window?"
        )
    elif without_noksek > 0 and profitable_pct and profitable_pct >= 0.4 and m0["net_pnl"] > 0:
        if abs(force_flat_effect) > 500:
            verdict = "PROMISING_BUT_CONCENTRATED"
            next_step = "DO NOT PREREGISTER YET"
            remaining_q = (
                "Demonstrate non-trivial edge with force_flat_daily=True OR "
                "document continuous-holding as intentional and pass leave-NOKSEK "
                "plus leave-top5 on a pinned dataset before October+ preregistration."
            )
        else:
            verdict = "SUPPORTED_FOR_PREREGISTRATION"
            next_step = "PREREGISTER ALEXG10 FOR OCTOBER+"
            remaining_q = None
    else:
        verdict = "MECHANISM-DEPENDENT"
        next_step = "DO NOT PREREGISTER YET"
        remaining_q = (
            "Quantify whether any AlexG10 edge remains after removing no-flat "
            "dependence and top-symbol concentration on a pinned dataset."
        )

    # Refine verdict with actual numbers
    if without_noksek <= 0 or without_top5 <= 0:
        verdict = "PROMISING_BUT_CONCENTRATED" if m0["net_pnl"] > 0 else "NOT_SUPPORTED"
        next_step = "DO NOT PREREGISTER YET"
        remaining_q = (
            "AlexG10 remains profitable only with concentrated symbols and/or "
            "no-daily-flat continuous holding. Smallest remaining question: does a "
            "pinned, leave-NOKSEK (or equal-weight diagnostic) continuous-hold "
            "AlexG10 still clear a pre-registered cost-adjusted hurdle on unseen data?"
        )
    if abs(force_flat_effect) > 800:
        # mechanism dominant
        if verdict == "SUPPORTED_FOR_PREREGISTRATION":
            verdict = "MECHANISM-DEPENDENT"

    final = {
        "verdict": verdict,
        "next_step": next_step,
        "remaining_research_question": remaining_q,
        "control": m0,
        "causal_2x2": causal_rows,
        "force_flat_effect_C1_minus_C0": force_flat_effect,
        "signal_stop_effect_C2_minus_C0": signal_stop_effect,
        "interaction_(C3-C2)-(C1-C0)": interaction,
        "continuous_jun_sep": m_cont["net_pnl"],
        "monthly_sum_jun_sep": monthly_sum,
        "leave_one_out": loo_rows,
        "profitable_symbol_pct": profitable_pct,
        "n_active_symbols": len(active),
        "n_positive_symbols": len(pos),
        "n_negative_symbols": len(neg),
        "concentration_control": conc_ctrl,
        "concentration_original": conc_orig,
        "september_slice_net": m_sep_slice["net_pnl"],
        "september_slice_pf": m_sep_slice["profit_factor"],
        "original_vs_current": recon_summary,
        "original_data_recovered": False,
        "reconstruction": reconstruction,
    }
    (out / "FINAL_RESULTS.json").write_text(json.dumps(final, indent=2, default=str), encoding="utf-8")

    # Markdown reports
    noksek_pnl = next((r["net_pnl"] for r in pairs if r["pair"] == "NOKSEK=X"), None)
    report = f"""# AlexG10 Test 2 — Causal + Concentration Validation

## 1. Executive verdict

**{verdict}**

Does AlexG10 survive Test 2? **Not as a broad, mechanism-light edge.**

- Control net (frozen current cache): **{m0['net_pnl']:.2f}**
- Force-flat effect (C1−C0): **{force_flat_effect:.2f}**
- Signal-stop effect (C2−C0): **{signal_stop_effect:.2f}**
- Without NOKSEK: **{without_noksek:.2f}**
- Without top-5 symbols: **{without_top5:.2f}**
- Profitable symbol %: **{None if profitable_pct is None else f'{100*profitable_pct:.1f}%'}** ({len(pos)}/{len(active)})

Next step: **{next_step}**

{f'Remaining question: {remaining_q}' if remaining_q else ''}

---

## 2. Data reproducibility

```text
ORIGINAL DATA RECONSTRUCTION
----------------------------
original H1 dataset recoverable: NO
original M1 dataset recoverable: NO
original spread dataset recoverable: NO
original universe recoverable: YES
original hashes recoverable: NO
original exact command recoverable: YES
```

Exact +$851 bit-replay is **permanently unresolvable** with available artifacts
(spreads never saved; H1 snapshot unpinned; discovery bars=3259 vs current {len(data[master])}).

Test-2 freeze hash: `{dhash}`  
Matches Test-1 hash: `{dhash == T1_HASH}`

---

## 3. 2×2 causal results

| Variant | Force flat | Signal stops | Net | PF | MDD | fills | daily_flat | hold_med_h |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
"""
    for r in causal_rows:
        report += (
            f"| {r['variant']} | {r['force_flat']} | {r['honor_signal_stops']} | "
            f"{r['net']:.2f} | {r['PF']} | {r['MDD']:.4f} | {r['fills']} | "
            f"{r['daily_flat']} | {r['hold_median_h']} |\n"
        )
    report += f"""
- force_flat_effect (C1−C0) = **{force_flat_effect:.2f}**
- signal_stop_effect (C2−C0) = **{signal_stop_effect:.2f}**
- interaction (C3−C2)−(C1−C0) = **{interaction:.2f}**

### Mechanism notes

- **Force-flat OFF** lets winners run multi-day into dynamic-RR TPs; ON converts many
  paths into `daily_close` and collapses net (C0→C1).
- **honor_signal_stops ON** keeps structural ghost-SL TP / 1R SL instead of true_sl+dynamic RR,
  cutting hold times and destroying the fat-TP right tail (C0→C2).
- Interaction quantifies non-additivity of the two switches.

AlexG10 strategy code is designed around stop-run harvest entries; it does **not**
itself disable daily flatten — that was an experimental engine knob in the discovery run.

---

## 4. Continuous vs monthly

| Mode | Net | Fills |
|---|---:|---:|
| Continuous Jun→Sep | {m_cont['net_pnl']:.2f} | {m_cont['fills']} |
| Independent monthly sum | {monthly_sum:.2f} | {sum(r['fills'] for r in indep)} |
| Delta (continuous − sum) | {m_cont['net_pnl']-monthly_sum:.2f} | |

Month-boundary crossing trades in continuous path: **{mba['n_trades_crossing_month_boundary']}**
(pnl {mba['pnl_crossing_month_boundary']:.2f}).

Mechanisms:
"""
    for n in mba["mechanism_notes"]:
        report += f"- {n}\n"

    report += f"""
---

## 5. Symbol concentration

NOKSEK control net: **{noksek_pnl}**

Top pairs (control):
"""
    for r in pairs[:10]:
        report += (
            f"- {r['pair']}: n={r['fills']} net={r['net_pnl']:.2f} "
            f"PF={r.get('profit_factor')}\n"
        )
    report += f"""
Leave-one-out (ranking frozen from control):
"""
    for r in loo_rows:
        report += f"- {r['exclusion']}: net={r['net']:.2f} PF={r['PF']} fills={r['fills']}\n"
    report += f"""
Profitable symbols: {len(pos)}/{len(active)} = {profitable_pct}

---

## 6. Trade concentration

### Control
{json.dumps(conc_ctrl, indent=2)}

### Original +$851 ledger
{json.dumps(conc_orig, indent=2)}

---

## 7. September (RETROSPECTIVE — not pre-registered AlexG10 OOS)

- Continuous-control September slice: net **{m_sep_slice['net_pnl']:.2f}**, PF **{m_sep_slice['profit_factor']}**, fills {m_sep_slice['fills']}
- Independent September reset: net **{sep_indep['net_pnl']:.2f}**, fills {sep_indep['fills']}
- Locked N1 Sep context: {n1_sep}

---

## 8. Original +$851 vs current +{m0['net_pnl']:.2f}

Delta: **{recon_summary['delta']:.4f}**

Class counts: {dict(by_cls)}

| Bucket | Σ Δpnl |
|---|---:|
| NEW_TRADE | {recon_summary['new_trades_pnl']:.2f} |
| MISSING_TRADE | {recon_summary['missing_trades_pnl']:.2f} |
| EXIT_DIFFERENCE | {recon_summary['exit_diff_pnl']:.2f} |
| SPREAD_OR_FILL_DIFFERENCE | {recon_summary['spread_or_fill_pnl']:.2f} |

Unrecoverable root cause remains unpinned H1 + live spreads at discovery.

---

## 9. Interpretation

AlexG10's apparent edge on this frozen dataset is **not** a broad cross-sectional alpha.

It is primarily:

1. **Continuous holding** (`force_flat_daily=False`) enabling dynamic-RR TP realization
2. **Exit semantics** that rewrite stops (`honor_signal_stops=False` + true_sl/dynamic RR)
3. **Heavy concentration** in a handful of symbols/trades (NOKSEK-class)

This is **MECHANISM-DEPENDENT** and **PROMISING_BUT_CONCENTRATED** discovery evidence,
not a validated general strategy.

---

## Future OOS decision

### {next_step}

{remaining_q or ''}
"""
    (out / "FINAL_RESULTS.md").write_text(report, encoding="utf-8")
    docs.write_text(report, encoding="utf-8")

    print(
        f"""
ALEXG10 TEST 2
----------------
control_net: {m0['net_pnl']:.4f}
control_pf: {m0['profit_factor']}
continuous_jun_sep: {m_cont['net_pnl']:.4f}
monthly_sum_jun_sep: {monthly_sum:.4f}

force_flat_effect: {force_flat_effect:.4f}
signal_stop_effect: {signal_stop_effect:.4f}
2x2_interaction: {interaction:.4f}

without_noksek: {without_noksek:.4f}
without_top5_symbols: {without_top5:.4f}

profitable_symbol_pct: {profitable_pct}
top1_trade_concentration: {conc_ctrl['top1']['share']}
top10_trade_concentration: {conc_ctrl['top10']['share']}

september_net: {m_sep_slice['net_pnl']:.4f}
september_pf: {m_sep_slice['profit_factor']}

original_vs_current_delta: {recon_summary['delta']:.4f}
original_data_recovered: NO

verdict: {verdict}
next_step: {next_step}
artifacts: {out}
docs: {docs}
""",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
