#!/usr/bin/env python3
"""Gate 2 strategy research campaign — daily theory↔broker + focused N1@5000 opt.

Offline / read-only. Does NOT:
- connect to Live (52958398)
- trade Mirror
- modify protected positions
- modify frozen simulator
- modify N1 strategy code

N1 control for this campaign: alexg8, ghost_sl_mult=4.0, lev=5000, rr=1.0,
force_flat_daily=true, both sides, Raw spreads, $7/lot, 60 pairs.
"""

from __future__ import annotations

import csv
import hashlib
import itertools
import json
import math
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

from borex.alexg import AlexG8Strategy
from borex.alexg.multi_market import pick_master_symbol
from borex.backtest.engine import BacktestConfig
from borex.backtest.multi_market_engine import MultiMarketEngine
from borex.gate2.daily_theory_broker import build_daily_dataset, write_daily_artifacts
from borex.viewerMT5.mt5_feed import (
    _cache_path,
    _dedupe_hourly,
    _drop_forming_bar,
    _frame_to_candles,
    _slice_candles,
)

CAPITAL = 1000.0
WARMUP_DAYS = 400
GHOST_PRIME_BARS = 72

JUN_INDEX = ROOT / "data/runs/mt5_1m_n1_oos_jun2026/index.json"
JUL_INDEX = ROOT / "data/runs/mt5_1m_n1_oos_jul2026/index.json"
AUG_INDEX = ROOT / "data/runs/mt5_1m_slmult_lev_split/index.json"
JUN_TAPE = ROOT / "data/runs/mt5_1m_n1_oos_jun2026/decisions_sl_4p0.json"
JUL_TAPE = ROOT / "data/runs/mt5_1m_n1_oos_jul2026/decisions_sl_4p0.json"
AUG_TAPE = ROOT / "data/runs/mt5_1m_slmult_lev_split/decisions_sl_4p0.json"
AUG_TAPE_DIR = ROOT / "data/runs/mt5_1m_slmult_lev_split"
CENSUS = (
    ROOT
    / "data/runs/gate2_fidelity_assessment/runs/g2a_20260924_130000/master_trade_census.json"
)

# Campaign N1 control (immutable reference). Historical OOS used lev=500;
# this campaign control matches live/demo account leverage=5000.
N1_CONTROL = {
    "strategy": "alexg8",
    "ghost_sl_mult": 4.0,
    "leverage": 5000.0,
    "rr_factor": 1.0,
    "rr_min": 2.0,
    "rr_max": 5.0,
    "true_sl_rr": 2.0,
    "rr_mode": "dynamic",
    "force_flat_daily": True,
    "force_flat_friday": False,
    "sides": "both",
    "commission_per_lot": 7.0,
    "path": "h1_signal_1m_exit",
}

# Focused search (not blind brute force)
LEVS = (500.0, 1000.0, 5000.0)
RRS = (0.6, 0.8, 1.0, 1.4)
SIDES = ("both", "long_only", "short_only")
# Extra ghost_sl tapes available for August only
AUG_GHOSTS = (3.2, 4.0, 4.8, 5.6)


def _git() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=str(ROOT), text=True
        ).strip()
    except Exception:
        return "unknown"


def _ts(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    if t.tzinfo is None:
        return t.tz_localize("UTC")
    return t.tz_convert("UTC")


def universe_hash(symbols: list[str]) -> str:
    return hashlib.sha256("\n".join(symbols).encode()).hexdigest()


def load_cache_candles(symbol: str, interval: str, start: datetime, end: datetime):
    path = _cache_path(symbol, interval)
    if not path.is_file():
        return []
    df = pd.read_parquet(path)
    candles = _frame_to_candles(df)
    if interval.strip().lower() in {"1h", "h1"}:
        candles = _dedupe_hourly(candles)
    candles = _slice_candles(candles, start, end)
    return _drop_forming_bar(candles, interval)


def remap_tape(tape: list[dict], data: dict) -> tuple[list[dict], dict]:
    maps = {}
    for sym, bars in data.items():
        maps[sym] = {int(_ts(c.timestamp).timestamp()): i for i, c in enumerate(bars)}
    out = []
    miss = 0
    for row in tape:
        sym = str(row.get("symbol") or "")
        tu = int(float(row["time_unix"]))
        idx = maps.get(sym, {}).get(tu)
        if idx is None:
            miss += 1
            continue
        r = dict(row)
        r["index"] = int(idx)
        out.append(r)
    return out, {"input": len(tape), "remapped": len(out), "missed": miss}


def filter_sides(tape: list[dict], sides: str) -> list[dict]:
    if sides == "both":
        return list(tape)
    if sides == "long_only":
        return [r for r in tape if str(r.get("action", "")).lower() in {"buy", "long"}]
    if sides == "short_only":
        return [r for r in tape if str(r.get("action", "")).lower() in {"sell", "short"}]
    raise ValueError(sides)


def _cfg(spreads: dict[str, float], *, leverage: float, rr_factor: float) -> BacktestConfig:
    return BacktestConfig(
        initial_capital=CAPITAL,
        leverage=float(leverage),
        position_size_pct=0.01,
        size_mode="margin",
        true_sl=True,
        true_sl_rr=float(N1_CONTROL["true_sl_rr"]),
        rr_mode=str(N1_CONTROL["rr_mode"]),
        rr_factor=float(rr_factor),
        rr_min=float(N1_CONTROL["rr_min"]),
        rr_max=float(N1_CONTROL["rr_max"]),
        stop_loss_pct=None,
        take_profit_pct=None,
        commission_per_lot=7.0,
        min_commission_per_side=0.04,
        lot_notional=100_000.0,
        risk_include_commission=True,
        commission_at_entry=False,
        force_flat_friday=bool(N1_CONTROL["force_flat_friday"]),
        force_flat_daily=bool(N1_CONTROL["force_flat_daily"]),
        force_flat_utc_hour=19,
        force_flat_friday_from_hour=19,
        winrate_min_trades=20,
        spread_pips=0.0,
        spread_pips_by_symbol=spreads,
        slippage_pips=0.0,
        intra_hour_sl=False,
    )


def _trade_row(t) -> dict:
    return {
        "symbol": t.symbol,
        "side": t.side.value if hasattr(t.side, "value") else str(t.side),
        "entry_time": str(t.entry_time),
        "exit_time": str(t.exit_time),
        "pnl": float(t.pnl),
        "exit_reason": t.exit_reason,
    }


def summarize(trades: list[dict], intents: int, mdd: float, entry_start, last_bar) -> dict:
    pnls = [float(t["pnl"]) for t in trades]
    wins = sum(1 for p in pnls if p > 0)
    days = max(1.0, (_ts(last_bar) - _ts(entry_start)).total_seconds() / 86400.0)
    sides = Counter(str(t["side"]).lower() for t in trades)
    exits = Counter(str(t["exit_reason"]) for t in trades)
    by_pair = Counter(t["symbol"] for t in trades)
    # daily pnl
    by_day: dict[str, float] = defaultdict(float)
    for t in trades:
        by_day[str(t["entry_time"])[:10]] += float(t["pnl"])
    day_pnls = list(by_day.values())
    # consecutive losses
    max_cl = cur = 0
    for p in pnls:
        if p <= 0:
            cur += 1
            max_cl = max(max_cl, cur)
        else:
            cur = 0
    gp = sum(p for p in pnls if p > 0)
    gl = sum(p for p in pnls if p <= 0)
    pf = (gp / abs(gl)) if gl else (float("inf") if gp > 0 else None)
    top_pair_share = (by_pair.most_common(1)[0][1] / len(trades)) if trades else None
    return {
        "intents": intents,
        "fills": len(trades),
        "trades_per_day": len(trades) / days,
        "WR": (wins / len(trades)) if trades else None,
        "PF": pf,
        "net_pnl": float(sum(pnls)) if pnls else 0.0,
        "mean_trade": float(statistics.mean(pnls)) if pnls else None,
        "median_trade": float(statistics.median(pnls)) if pnls else None,
        "MDD": float(mdd),
        "ending_equity": CAPITAL + (sum(pnls) if pnls else 0.0),
        "TP": int(exits.get("take_profit", 0)),
        "daily_close": int(exits.get("daily_close", 0)),
        "margin_stop": int(exits.get("margin_stop", 0)),
        "other_exits": {
            k: v
            for k, v in exits.items()
            if k not in {"take_profit", "daily_close", "margin_stop"}
        },
        "long_count": int(sides.get("long", 0) + sides.get("buy", 0)),
        "short_count": int(sides.get("short", 0) + sides.get("sell", 0)),
        "top_pair_share": top_pair_share,
        "worst_day_pnl": min(day_pnls) if day_pnls else None,
        "worst_trade": min(pnls) if pnls else None,
        "max_consecutive_losses": max_cl,
        "n_trading_days": len(by_day),
    }


def run_replay(
    *,
    data,
    m1,
    spreads,
    master,
    prime_i,
    entry_start,
    tape,
    leverage: float,
    rr_factor: float,
    ghost_sl_mult: float,
) -> dict:
    strategy = AlexG8Strategy(
        min_rr=float(N1_CONTROL["true_sl_rr"]),
        execution_interval="1h",
        peer_blend=0.0,
        ghost_sl_mult=float(ghost_sl_mult),
    )
    engine = MultiMarketEngine(
        strategy, _cfg(spreads, leverage=leverage, rr_factor=rr_factor), max_positions=0
    )
    engine._progress_every = 10_000
    t0 = time.perf_counter()
    result = engine.run(
        data,
        timeframe="1h+1m",
        master_symbol=master,
        same_bar_exit=False,
        start_master_i=prime_i,
        ltf_by_symbol=m1,
        decisions=tape,
    )
    elapsed = time.perf_counter() - t0
    window = [_trade_row(t) for t in result.trades if _ts(t.entry_time) >= entry_start]
    last_bar = data[master][-1].timestamp
    s = summarize(
        window, len(tape), float(result.max_drawdown_pct), entry_start, last_bar
    )
    s["elapsed_sec"] = elapsed
    return s


def load_period(
    *,
    name: str,
    index_path: Path,
    tape_path: Path | None,
    symbols: list[str],
    spreads: dict[str, float],
    entry_start: pd.Timestamp | None = None,
    end: pd.Timestamp | None = None,
    scan_if_no_tape: bool = False,
    ghost_sl_mult: float = 4.0,
) -> dict:
    idx = json.loads(index_path.read_text(encoding="utf-8")) if index_path.is_file() else {}
    if entry_start is None:
        entry_start = _ts(idx["eval_start"]).floor("h")
    if end is None:
        end = _ts(idx["last_bar"]) + pd.Timedelta(hours=1)
    h1_start = (end - pd.Timedelta(days=WARMUP_DAYS)).to_pydatetime()
    m1_start = (entry_start - pd.Timedelta(days=2)).to_pydatetime()
    end_py = end.to_pydatetime()

    print(f"[{name}] load cache …", flush=True)
    data: dict = {}
    m1: dict = {}
    for i, sym in enumerate(symbols, 1):
        bars = load_cache_candles(sym, "1h", h1_start, end_py)
        if len(bars) < 120:
            continue
        data[sym] = bars
        minutes = load_cache_candles(sym, "1m", m1_start, end_py)
        if minutes:
            m1[sym] = minutes
        if i % 20 == 0 or i == len(symbols):
            print(f"  {i}/{len(symbols)} H1={len(data)} M1={len(m1)}", flush=True)
    if len(data) < 50 or len(m1) < 50:
        raise SystemExit(f"{name}: insufficient pairs H1={len(data)} M1={len(m1)}")

    master = pick_master_symbol(data)
    master_bars = data[master]
    entry_i = next(
        (i for i, c in enumerate(master_bars) if _ts(c.timestamp) >= entry_start), None
    )
    if entry_i is None:
        raise SystemExit(f"{name}: no entry bar")
    warmup = int(AlexG8Strategy().min_bars)
    prime_i = max(warmup, entry_i - GHOST_PRIME_BARS)

    tape = None
    remap_meta = None
    if tape_path and tape_path.is_file():
        raw = json.loads(tape_path.read_text(encoding="utf-8"))
        tape, remap_meta = remap_tape(raw, data)
        print(f"[{name}] tape remap {remap_meta}", flush=True)
    elif scan_if_no_tape:
        print(f"[{name}] scanning alexg8 ghost={ghost_sl_mult} …", flush=True)
        strategy = AlexG8Strategy(
            min_rr=float(N1_CONTROL["true_sl_rr"]),
            execution_interval="1h",
            peer_blend=0.0,
            ghost_sl_mult=float(ghost_sl_mult),
        )
        engine = MultiMarketEngine(
            strategy,
            _cfg(spreads, leverage=5000.0, rr_factor=1.0),
            max_positions=0,
        )
        engine._progress_every = 500
        collected: list[dict] = []
        engine.run(
            data,
            timeframe="1h+1m",
            master_symbol=master,
            same_bar_exit=False,
            start_master_i=prime_i,
            ltf_by_symbol=m1,
            collect_decisions=collected,
        )
        tape = collected
        remap_meta = {"scanned": True, "n": len(tape)}
        print(f"[{name}] scanned intents={len(tape)}", flush=True)
    else:
        raise SystemExit(f"{name}: no tape")

    return {
        "name": name,
        "role": None,
        "entry_start": entry_start,
        "end": end,
        "data": data,
        "m1": m1,
        "spreads": spreads,
        "master": master,
        "prime_i": prime_i,
        "tape_base": tape,
        "remap_meta": remap_meta,
        "h1_pairs": len(data),
        "m1_pairs": len(m1),
    }


def cfg_id(ghost, lev, rr, sides) -> str:
    return f"g{str(ghost).replace('.', 'p')}_lev{int(lev)}_rr{str(rr).replace('.', 'p')}_{sides}"


def classify_vs_n1(cand: dict, n1: dict, periods: list[str]) -> str:
    """Neutral period-stability label using PF and mean_trade."""
    better = worse = 0
    for p in periods:
        c = cand["by_period"].get(p) or {}
        n = n1["by_period"].get(p) or {}
        if c.get("PF") is None or n.get("PF") is None:
            continue
        if c.get("mean_trade") is None or n.get("mean_trade") is None:
            continue
        c_ok = float(c["PF"]) >= float(n["PF"]) and float(c["mean_trade"]) >= float(
            n["mean_trade"]
        )
        n_ok = float(c["PF"]) <= float(n["PF"]) and float(c["mean_trade"]) <= float(
            n["mean_trade"]
        )
        if c_ok and not (
            math.isclose(float(c["PF"]), float(n["PF"]))
            and math.isclose(float(c["mean_trade"]), float(n["mean_trade"]))
        ):
            better += 1
        elif n_ok and not (
            math.isclose(float(c["PF"]), float(n["PF"]))
            and math.isclose(float(c["mean_trade"]), float(n["mean_trade"]))
        ):
            worse += 1
    if better >= 2 and worse == 0:
        return "improved_versus_N1_on_measured_sample"
    if worse >= 2 and better == 0:
        return "degraded"
    if better and worse:
        return "mixed"
    if better == 1 and worse == 0:
        return "unstable_across_periods"
    return "mixed"


def main() -> int:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out = ROOT / "data" / "runs" / "research" / f"gate2_strategy_optimization_{ts}"
    out.mkdir(parents=True, exist_ok=True)
    print(f"OUT {out}", flush=True)

    # ---- Part A: daily theory↔broker ----
    daily = build_daily_dataset(CENSUS)
    write_daily_artifacts(daily, out)
    print(
        f"Part A: {len(daily['trade_rows'])} trades, "
        f"{len(daily['daily_aggregates'])} days",
        flush=True,
    )

    jun_idx = json.loads(JUN_INDEX.read_text(encoding="utf-8"))
    symbols = sorted(jun_idx["spread_pips_by_symbol"].keys())
    spreads = dict(jun_idx["spread_pips_by_symbol"])  # Raw
    uhash = universe_hash(symbols)

    # Period roles
    periods_meta = {
        "june": "validation_OOS",
        "july": "validation_OOS",
        "august": "optimization_IS",
        "september_todate": "descriptive_in_progress",
    }

    # Load periods
    packed = {}
    packed["june"] = load_period(
        name="june", index_path=JUN_INDEX, tape_path=JUN_TAPE, symbols=symbols, spreads=spreads
    )
    packed["june"]["role"] = periods_meta["june"]
    packed["july"] = load_period(
        name="july", index_path=JUL_INDEX, tape_path=JUL_TAPE, symbols=symbols, spreads=spreads
    )
    packed["july"]["role"] = periods_meta["july"]
    packed["august"] = load_period(
        name="august",
        index_path=AUG_INDEX,
        tape_path=AUG_TAPE,
        symbols=symbols,
        spreads=spreads,
    )
    packed["august"]["role"] = periods_meta["august"]

    # September to-date from cache end
    m1_ends = []
    for sym in symbols:
        p = _cache_path(sym, "1m")
        if p.is_file():
            df = pd.read_parquet(p, columns=["timestamp"])
            m1_ends.append(pd.to_datetime(df["timestamp"], utc=True).max())
    sep_end = min(m1_ends) + pd.Timedelta(minutes=1) if m1_ends else _ts("2026-09-25")
    sep_start = _ts("2026-09-03T03:00:00+00:00")  # day-3 convention mid-month start
    packed["september_todate"] = load_period(
        name="september_todate",
        index_path=AUG_INDEX,  # unused for bounds
        tape_path=None,
        symbols=symbols,
        spreads=spreads,
        entry_start=sep_start,
        end=sep_end,
        scan_if_no_tape=True,
        ghost_sl_mult=4.0,
    )
    packed["september_todate"]["role"] = periods_meta["september_todate"]

    # Save Sep tape for reproducibility
    (out / "september_todate_decisions_sl_4p0.json").write_text(
        json.dumps(packed["september_todate"]["tape_base"], indent=2), encoding="utf-8"
    )

    # Build config list
    configs = []
    for lev, rr, sides in itertools.product(LEVS, RRS, SIDES):
        configs.append(
            {
                "id": cfg_id(4.0, lev, rr, sides),
                "ghost_sl_mult": 4.0,
                "leverage": lev,
                "rr_factor": rr,
                "sides": sides,
                "is_n1_control": (
                    lev == 5000.0 and rr == 1.0 and sides == "both"
                ),
                "scope": "all_periods",
            }
        )
    # August-only ghost variants at control lev/rr/sides
    for g in AUG_GHOSTS:
        if g == 4.0:
            continue
        configs.append(
            {
                "id": cfg_id(g, 5000.0, 1.0, "both"),
                "ghost_sl_mult": g,
                "leverage": 5000.0,
                "rr_factor": 1.0,
                "sides": "both",
                "is_n1_control": False,
                "scope": "august_only",
            }
        )

    # Deduplicate ids
    seen = set()
    uniq = []
    for c in configs:
        if c["id"] in seen:
            continue
        seen.add(c["id"])
        uniq.append(c)
    configs = uniq

    param_manifest = {
        "n1_control": N1_CONTROL,
        "search": {
            "leverages": list(LEVS),
            "rr_factors": list(RRS),
            "sides": list(SIDES),
            "ghost_sl_mult_all_periods": [4.0],
            "ghost_sl_mult_august_extra": [g for g in AUG_GHOSTS if g != 4.0],
            "rationale": (
                "ghost_sl_mult affects stop distance / entry geometry (signal-side); "
                "rr_factor scales TP (exec-side); leverage scales margin risk (exec); "
                "sides filter directional participation. Session/ablation pills left "
                "at alexg8 defaults (session=all) for this first campaign."
            ),
        },
        "periods": periods_meta,
        "n_configs": len(configs),
    }
    (out / "optimization_parameter_manifest.json").write_text(
        json.dumps(param_manifest, indent=2), encoding="utf-8"
    )

    results = []
    core_periods = ["june", "july", "august"]

    for ci, cfg in enumerate(configs, 1):
        print(
            f"=== [{ci}/{len(configs)}] {cfg['id']} scope={cfg['scope']} ===",
            flush=True,
        )
        by_period = {}
        period_list = (
            ["august"] if cfg["scope"] == "august_only" else list(packed.keys())
        )
        for pname in period_list:
            P = packed[pname]
            ghost = float(cfg["ghost_sl_mult"])
            # Load alternate August tape for non-4.0 ghost
            if pname == "august" and ghost != 4.0:
                tag = str(ghost).replace(".", "p")
                alt = AUG_TAPE_DIR / f"decisions_sl_{tag}.json"
                if not alt.is_file():
                    print(f"  skip {pname}: missing tape {alt.name}", flush=True)
                    continue
                raw = json.loads(alt.read_text(encoding="utf-8"))
                tape0, _ = remap_tape(raw, P["data"])
            else:
                if ghost != 4.0 and pname != "august":
                    continue
                tape0 = P["tape_base"]
            tape = filter_sides(tape0, cfg["sides"])
            s = run_replay(
                data=P["data"],
                m1=P["m1"],
                spreads=P["spreads"],
                master=P["master"],
                prime_i=P["prime_i"],
                entry_start=P["entry_start"],
                tape=tape,
                leverage=float(cfg["leverage"]),
                rr_factor=float(cfg["rr_factor"]),
                ghost_sl_mult=ghost,
            )
            by_period[pname] = s
            print(
                f"  {pname}: fills={s['fills']} PF={s['PF']} net={s['net_pnl']:.2f} "
                f"mean={s['mean_trade']} MDD={s['MDD']}",
                flush=True,
            )
        row = {**cfg, "by_period": by_period}
        # Aggregate across core periods present
        nets = [by_period[p]["net_pnl"] for p in core_periods if p in by_period]
        means = [
            by_period[p]["mean_trade"]
            for p in core_periods
            if p in by_period and by_period[p]["mean_trade"] is not None
        ]
        pfs = [
            by_period[p]["PF"]
            for p in core_periods
            if p in by_period and by_period[p]["PF"] is not None
        ]
        row["agg_core"] = {
            "periods_n": len(nets),
            "sum_net_pnl": float(sum(nets)) if nets else None,
            "mean_of_mean_trade": float(statistics.mean(means)) if means else None,
            "mean_of_PF": float(statistics.mean(pfs)) if pfs else None,
            "periods_PF_ge_1": sum(1 for x in pfs if x >= 1.0),
        }
        results.append(row)

    # N1 baseline extract
    n1 = next(r for r in results if r.get("is_n1_control"))
    for r in results:
        r["vs_n1_label"] = classify_vs_n1(r, n1, core_periods)
        r["delta_mean_trade_vs_n1"] = {}
        r["delta_pf_vs_n1"] = {}
        for p in core_periods:
            if p in r["by_period"] and p in n1["by_period"]:
                cm = r["by_period"][p].get("mean_trade")
                nm = n1["by_period"][p].get("mean_trade")
                cp = r["by_period"][p].get("PF")
                np_ = n1["by_period"][p].get("PF")
                r["delta_mean_trade_vs_n1"][p] = (
                    (cm - nm) if cm is not None and nm is not None else None
                )
                r["delta_pf_vs_n1"][p] = (
                    (cp - np_) if cp is not None and np_ is not None else None
                )

    # Shortlist: improved on measured sample, exclude control itself, prefer multi-period
    shortlist = [
        r
        for r in results
        if r["vs_n1_label"] == "improved_versus_N1_on_measured_sample"
        and not r.get("is_n1_control")
        and r.get("scope") == "all_periods"
    ]
    # Rank by mean_of_mean_trade then periods_PF_ge_1 (explicit metrics, not subjective score)
    shortlist.sort(
        key=lambda r: (
            r["agg_core"].get("periods_PF_ge_1") or 0,
            r["agg_core"].get("mean_of_mean_trade") or -1e9,
            r["agg_core"].get("mean_of_PF") or 0,
        ),
        reverse=True,
    )
    shortlist = shortlist[:5]

    # Also note best mixed / leverage findings
    lev_slice = [
        r
        for r in results
        if r["ghost_sl_mult"] == 4.0
        and r["rr_factor"] == 1.0
        and r["sides"] == "both"
        and r["scope"] == "all_periods"
    ]

    # Write artifacts
    (out / "n1_baseline.json").write_text(json.dumps(n1, indent=2), encoding="utf-8")
    (out / "all_configurations.json").write_text(
        json.dumps(results, indent=2), encoding="utf-8"
    )
    (out / "shortlisted_candidates.json").write_text(
        json.dumps(shortlist, indent=2), encoding="utf-8"
    )

    # Flat results CSV
    flat_rows = []
    for r in results:
        for p, s in r["by_period"].items():
            flat_rows.append(
                {
                    "config_id": r["id"],
                    "is_n1_control": r["is_n1_control"],
                    "ghost_sl_mult": r["ghost_sl_mult"],
                    "leverage": r["leverage"],
                    "rr_factor": r["rr_factor"],
                    "sides": r["sides"],
                    "scope": r["scope"],
                    "period": p,
                    "period_role": periods_meta.get(p),
                    "vs_n1_label": r["vs_n1_label"],
                    **{
                        k: s.get(k)
                        for k in (
                            "intents",
                            "fills",
                            "trades_per_day",
                            "WR",
                            "PF",
                            "net_pnl",
                            "mean_trade",
                            "median_trade",
                            "MDD",
                            "ending_equity",
                            "TP",
                            "daily_close",
                            "margin_stop",
                            "long_count",
                            "short_count",
                            "top_pair_share",
                            "worst_day_pnl",
                            "worst_trade",
                            "max_consecutive_losses",
                        )
                    },
                }
            )
    with (out / "results.csv").open("w", encoding="utf-8", newline="") as f:
        if flat_rows:
            w = csv.DictWriter(f, fieldnames=list(flat_rows[0].keys()))
            w.writeheader()
            w.writerows(flat_rows)

    comparison = {
        "n1_control_id": n1["id"],
        "n1_by_period": n1["by_period"],
        "shortlist_ids": [r["id"] for r in shortlist],
        "leverage_slice_at_rr1_both": [
            {
                "id": r["id"],
                "leverage": r["leverage"],
                "agg_core": r["agg_core"],
                "vs_n1_label": r["vs_n1_label"],
                "by_period": {
                    p: {
                        k: r["by_period"][p].get(k)
                        for k in ("fills", "PF", "net_pnl", "mean_trade", "MDD")
                    }
                    for p in r["by_period"]
                },
            }
            for r in lev_slice
        ],
    }
    (out / "comparison_against_n1.json").write_text(
        json.dumps(comparison, indent=2), encoding="utf-8"
    )

    provenance = {
        "data": "MT5 data/cache/mt5/utc_v2",
        "spreads": "June N1 Raw from mt5_1m_n1_oos_jun2026/index.json",
        "universe_n": len(symbols),
        "universe_sha256": uhash,
        "tapes": {
            "june": str(JUN_TAPE),
            "july": str(JUL_TAPE),
            "august": str(AUG_TAPE),
            "september_todate": "scanned_in_run",
        },
        "simulator": "MultiMarketEngine FROZEN_FOR_STRATEGY_RESEARCH",
        "simulator_doc": "docs/research/frozen_simulator_execution_model.md",
        "gate2_census": str(CENSUS),
        "no_histdata": True,
        "no_live_trading": True,
        "mirror_traded": False,
    }
    (out / "data_provenance.json").write_text(
        json.dumps(provenance, indent=2), encoding="utf-8"
    )

    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": _git(),
        "experiment": "gate2_strategy_optimization_campaign",
        "n1_control": N1_CONTROL,
        "n_configs_tested": len(results),
        "shortlist_n": len(shortlist),
        "out_dir": str(out),
        "live_account_touched": False,
        "mirror_traded": False,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (out / "config_snapshot.json").write_text(
        json.dumps({"n1_control": N1_CONTROL, "configs": configs}, indent=2),
        encoding="utf-8",
    )

    # Report
    def fmt_period(s: dict | None) -> str:
        if not s:
            return "n/a"
        return (
            f"fills={s.get('fills')} PF={s.get('PF')} net={s.get('net_pnl')} "
            f"mean={s.get('mean_trade')} MDD={s.get('MDD')}"
        )

    lines = [
        "# Gate 2 Strategy Optimization Campaign",
        "",
        f"Generated: {manifest['created_at']}",
        f"Commit: `{manifest['git_commit']}`",
        "",
        "## Absolute safety",
        "",
        "- LIVE (52958398 / 8791 / 88001): not connected",
        "- MIRROR: not traded",
        "- Protected Mirror tickets: not modified",
        "- Simulator: frozen / unmodified",
        "- N1 control: immutable reference (this campaign uses lev=5000)",
        "",
        "## Part A — Daily Theory ↔ Broker",
        "",
        f"- Source census: `{CENSUS}`",
        f"- Trade rows: {len(daily['trade_rows'])}",
        f"- Days: {[d['day'] for d in daily['daily_aggregates']]}",
        "",
        "### Limitations",
        "",
    ]
    for lim in daily["limitations"]:
        lines.append(f"- {lim}")
    lines += ["", "### Daily aggregates", "", "```json"]
    lines.append(json.dumps(daily["daily_aggregates"], indent=2))
    lines += ["```", "", "## N1 baseline (lev=5000)", ""]
    for p in packed:
        lines.append(f"- **{p}** ({periods_meta[p]}): {fmt_period(n1['by_period'].get(p))}")
    lines += [
        "",
        "## Search space",
        "",
        f"- Configs tested: **{len(results)}**",
        f"- Levers: lev={list(LEVS)}, rr={list(RRS)}, sides={list(SIDES)}, ghost=4.0 (+ Aug extras {list(AUG_GHOSTS)})",
        "",
        "## Shortlisted candidates",
        "",
    ]
    if not shortlist:
        lines.append("_No candidate met improved_versus_N1_on_measured_sample on ≥2 core periods._")
    for r in shortlist:
        lines.append(f"### `{r['id']}` — {r['vs_n1_label']}")
        lines.append(
            f"- Changes vs N1: ghost={r['ghost_sl_mult']} (N1 4.0), "
            f"lev={r['leverage']} (N1 5000), rr={r['rr_factor']} (N1 1.0), "
            f"sides={r['sides']} (N1 both)"
        )
        lines.append(f"- agg_core: {r['agg_core']}")
        for p in core_periods:
            lines.append(f"  - {p}: {fmt_period(r['by_period'].get(p))}")
        lines.append("")
    lines += [
        "## Leverage findings (rr=1.0, both, ghost=4.0)",
        "",
        "```json",
        json.dumps(comparison["leverage_slice_at_rr1_both"], indent=2),
        "```",
        "",
        "## Next recommended experiment",
        "",
        "1. Re-extend MT5 M1 through calendar September close, then re-run N1@5000 OOS.",
        "2. Take shortlist (if any) + N1 control to Mirror **paper** validation under "
        "explicit authorization — never Live; never touch protected tickets.",
        "3. Do not launch a second giant sweep until shortlist is Mirror-checked.",
        "",
        "## What this does NOT claim",
        "",
        "Profitability, robustness, or production readiness beyond measured sample metrics.",
    ]
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (out / "README.md").write_text(
        "\n".join(
            [
                "# Gate 2 Strategy Optimization",
                "",
                "See `report.md` for conclusions.",
                "",
                "Key files: `daily_theory_broker*.csv/json`, `n1_baseline.json`, "
                "`results.csv`, `shortlisted_candidates.json`, `comparison_against_n1.json`.",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    # Compact conclusions JSON for the final response
    conclusions = {
        "daily_theory_broker_days": [d["day"] for d in daily["daily_aggregates"]],
        "daily_theory_broker_n_trades": len(daily["trade_rows"]),
        "n1_baseline": {
            p: {
                k: n1["by_period"].get(p, {}).get(k)
                for k in ("fills", "PF", "net_pnl", "mean_trade", "MDD", "ending_equity")
            }
            for p in packed
        },
        "n_configs_tested": len(results),
        "shortlist": [
            {
                "id": r["id"],
                "label": r["vs_n1_label"],
                "params": {
                    "ghost_sl_mult": r["ghost_sl_mult"],
                    "leverage": r["leverage"],
                    "rr_factor": r["rr_factor"],
                    "sides": r["sides"],
                },
                "agg_core": r["agg_core"],
            }
            for r in shortlist
        ],
        "out_dir": str(out),
    }
    (out / "conclusions.json").write_text(
        json.dumps(conclusions, indent=2), encoding="utf-8"
    )
    print(json.dumps(conclusions, indent=2), flush=True)
    print(f"DONE -> {out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
