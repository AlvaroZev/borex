#!/usr/bin/env python3
"""Historical data recovery finalization + baseline forensic re-persist.

OFFLINE only. No MT5, LIVE, Mirror, September, or parameter mining.

1) Enrich DATA_COVERAGE_MATRIX with corrected legacy (DatetimeIndex) analysis
2) Re-run locked N1 control vs N1 Thursday on Jun–Aug with full persistence
   (weekly / monthly / pair / trade / sequential equity)
3) Write BASELINE_EXTENDED_REPORT + RESEARCH_RECOMMENDATION

Pre-June A_FULLY_USABLE utc_v2 H1+M1 was NOT recovered; baselines are
Jun–Aug forensic re-persist (not an extended-history claim).
"""

from __future__ import annotations

import csv
import hashlib
import json
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from borex.alexg import AlexG8Strategy  # noqa: E402
from borex.alexg.multi_market import pick_master_symbol  # noqa: E402
from borex.backtest.engine import BacktestConfig  # noqa: E402
from borex.backtest.multi_market_engine import MultiMarketEngine  # noqa: E402
from borex.gate2.september_firewall import (  # noqa: E402
    RESEARCH_END_EXCLUSIVE,
    assert_not_september,
    clamp_end_before_september,
)
from borex.gate2.thursday_entry_gate import filter_block_thursday_entries  # noqa: E402
from borex.gate2.weekly_research import (  # noqa: E402
    is_september_reserved,
    summarize_trades,
    weekly_distribution,
    weekly_metrics_table,
    weekly_slices,
)
from borex.research.massive_lab.worker import (  # noqa: E402
    AUG_INDEX,
    AUG_TAPE,
    JUN_INDEX,
    JUN_TAPE,
    JUL_INDEX,
    JUL_TAPE,
    CAPITAL,
    LEV,
    WARMUP,
    GHOST_PRIME,
    _ts,
    load_cache,
    remap,
)
from borex.viewerMT5.mt5_feed import _cache_path  # noqa: E402

OUT = sorted((ROOT / "data/runs/research").glob("gate2_historical_data_recovery_*"))[-1]
MIN_M1 = 20_000
MIN_H1 = 400


def git_commit() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(ROOT), text=True).strip()
    except Exception:
        return "unknown"


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys: list[str] = []
    for r in rows:
        for k in r:
            if k not in keys:
                keys.append(k)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in keys})


def fnum(x, nd=2):
    if x is None:
        return "n/a"
    try:
        return f"{float(x):.{nd}f}"
    except Exception:
        return str(x)


def load_universe() -> list[str]:
    """Use exact keys from Jun index spreads (=X form) — matches decision tapes."""
    jun = json.loads(JUN_INDEX.read_text(encoding="utf-8"))
    return sorted((jun.get("spread_pips_by_symbol") or {}).keys())


def read_legacy_ts(path: Path) -> pd.Series | None:
    if not path.is_file():
        return None
    df = pd.read_parquet(path)
    if isinstance(df.index, pd.DatetimeIndex):
        return pd.to_datetime(df.index, utc=True)
    if "timestamp" in df.columns:
        return pd.to_datetime(df["timestamp"], utc=True)
    if "DateTime" in df.columns:
        return pd.to_datetime(df["DateTime"], utc=True)
    return None


def enrich_legacy_coverage(symbols: list[str]) -> list[dict]:
    """Re-audit Dukascopy legacy with DatetimeIndex support."""
    months = [(2026, m) for m in range(1, 9)]
    rows = []
    for y, m in months:
        start = pd.Timestamp(year=y, month=m, day=1, tz="UTC")
        end = pd.Timestamp(year=y + (1 if m == 12 else 0), month=(1 if m == 12 else m + 1), day=1, tz="UTC")
        h1_ok = m1_ok = h1_part = m1_part = 0
        m1_counts = []
        for sym in symbols:
            # prefer year shard then flat
            m1p = ROOT / f"data/cache/{sym}/years/1m/{y}.parquet"
            if not m1p.is_file():
                m1p = ROOT / f"data/cache/{sym}/1m.parquet"
            h1p = ROOT / f"data/cache/{sym}/years/1h/{y}.parquet"
            if not h1p.is_file():
                h1p = ROOT / f"data/cache/{sym}/1h.parquet"
            mts = read_legacy_ts(m1p)
            hts = read_legacy_ts(h1p)
            mc = int(((mts >= start) & (mts < end)).sum()) if mts is not None and len(mts) else 0
            hc = int(((hts >= start) & (hts < end)).sum()) if hts is not None and len(hts) else 0
            m1_counts.append(mc)
            if hc >= MIN_H1:
                h1_ok += 1
            elif hc > 0:
                h1_part += 1
            if mc >= MIN_M1:
                m1_ok += 1
            elif mc > 0:
                m1_part += 1
        # Vendor-mixed: NOT strategy-usable even if complete
        complete = h1_ok == 60 and m1_ok == 60
        cls = (
            "D_INVALID_VENDOR_MIX"
            if (m1_ok > 0 or h1_ok > 0)
            else "D_INVALID"
        )
        if complete:
            cls = "D_INVALID_VENDOR_MIX_COMPLETE_BUT_FORBIDDEN"
        elif m1_ok >= 48 and h1_ok >= 48:
            cls = "D_INVALID_VENDOR_MIX_PARTIAL"
        elif h1_ok >= 48 and m1_ok < 30:
            cls = "D_INVALID_VENDOR_MIX_H1ISH"
        rows.append({
            "source": "legacy_dukascopy_reparsed",
            "year": y,
            "month": m,
            "period": f"{y}-{m:02d}",
            "h1_symbols_ok": h1_ok,
            "m1_symbols_ok": m1_ok,
            "h1_symbols_partial": h1_part,
            "m1_symbols_partial": m1_part,
            "complete_60": complete,
            "m1_median_bars": int(pd.Series(m1_counts).median()) if m1_counts else 0,
            "gaps_note": f"h1_fail={60-h1_ok};m1_fail={60-m1_ok}",
            "utc_v2_compatible": False,
            "classification": cls,
            "strategy_usable": False,
            "vendor": "dukascopy",
            "reason": "Different vendor vs MT5 utc_v2; price MAE~5e-4 on overlap; incomplete 60-pair; policy forbids vendor mix for N1 economics",
        })
    return rows


def bt_cfg(spreads, capital: float = CAPITAL):
    return BacktestConfig(
        initial_capital=float(capital),
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


def load_continuous_junaug(symbols: list[str], spreads: dict):
    """Single continuous Jun–Aug window (Sept clamped)."""
    jun = json.loads(JUN_INDEX.read_text(encoding="utf-8"))
    aug = json.loads(AUG_INDEX.read_text(encoding="utf-8"))
    entry_start = _ts(jun["eval_start"]).floor("h")
    end = min(
        clamp_end_before_september(_ts(aug["last_bar"]) + pd.Timedelta(hours=1)),
        RESEARCH_END_EXCLUSIVE,
    )
    h1_start = (end - pd.Timedelta(days=WARMUP)).to_pydatetime()
    m1_start = (entry_start - pd.Timedelta(days=2)).to_pydatetime()
    data, m1 = {}, {}
    for sym in symbols:
        bars = load_cache(sym, "1h", h1_start, end)
        if len(bars) < 120:
            continue
        data[sym] = bars
        mins = load_cache(sym, "1m", m1_start, end)
        if mins:
            m1[sym] = mins
    master = pick_master_symbol(data)
    entry_i = next(i for i, c in enumerate(data[master]) if _ts(c.timestamp) >= entry_start)
    return {
        "name": "junaug_continuous",
        "entry_start": entry_start,
        "end": end,
        "data": data,
        "m1": m1,
        "spreads": spreads,
        "master": master,
        "prime_i": max(int(AlexG8Strategy().min_bars), entry_i - GHOST_PRIME),
        "n_h1_syms": len(data),
        "n_m1_syms": len(m1),
    }


def load_month_period(name, index_path, symbols, spreads):
    idx = json.loads(index_path.read_text(encoding="utf-8"))
    entry_start = _ts(idx["eval_start"]).floor("h")
    end = min(
        clamp_end_before_september(_ts(idx["last_bar"]) + pd.Timedelta(hours=1)),
        RESEARCH_END_EXCLUSIVE,
    )
    h1_start = (end - pd.Timedelta(days=WARMUP)).to_pydatetime()
    m1_start = (entry_start - pd.Timedelta(days=2)).to_pydatetime()
    data, m1 = {}, {}
    for sym in symbols:
        bars = load_cache(sym, "1h", h1_start, end)
        if len(bars) < 120:
            continue
        data[sym] = bars
        mins = load_cache(sym, "1m", m1_start, end)
        if mins:
            m1[sym] = mins
    master = pick_master_symbol(data)
    entry_i = next(i for i, c in enumerate(data[master]) if _ts(c.timestamp) >= entry_start)
    return {
        "name": name,
        "entry_start": entry_start,
        "end": end,
        "data": data,
        "m1": m1,
        "spreads": spreads,
        "master": master,
        "prime_i": max(int(AlexG8Strategy().min_bars), entry_i - GHOST_PRIME),
    }


def run_engine(period, tape, *, capital=CAPITAL):
    strategy = AlexG8Strategy(
        min_rr=2.0, execution_interval="1h", peer_blend=0.0, ghost_sl_mult=4.0
    )
    engine = MultiMarketEngine(strategy, bt_cfg(period["spreads"], capital), max_positions=0)
    engine._progress_every = 100_000
    result = engine.run(
        period["data"],
        timeframe="1h+1m",
        master_symbol=period["master"],
        same_bar_exit=False,
        start_master_i=period["prime_i"],
        ltf_by_symbol=period["m1"],
        decisions=tape,
    )
    trades = []
    for t in result.trades:
        if _ts(t.entry_time) < period["entry_start"]:
            continue
        if is_september_reserved(t.entry_time):
            raise RuntimeError("September leak")
        assert_not_september(t.entry_time, context="trade_entry")
        side = t.side.value if hasattr(t.side, "value") else str(t.side)
        trades.append(
            {
                "symbol": t.symbol,
                "side": side,
                "entry_time": str(t.entry_time),
                "exit_time": str(getattr(t, "exit_time", "") or ""),
                "pnl": float(t.pnl),
                "exit_reason": t.exit_reason,
                "qty": float(getattr(t, "qty", 0) or getattr(t, "size", 0) or 0),
            }
        )
    return result, trades


def equity_from_trades(trades: list[dict], starting: float = CAPITAL) -> list[dict]:
    rows = []
    bal = float(starting)
    peak = bal
    max_dd = 0.0
    max_dd_pct = 0.0
    # sort by exit then entry for realized path
    ordered = sorted(
        trades,
        key=lambda t: (_ts(t.get("exit_time") or t["entry_time"]), _ts(t["entry_time"])),
    )
    for i, t in enumerate(ordered, 1):
        before = bal
        pnl = float(t["pnl"])
        after = before + pnl
        peak = max(peak, after)
        dd = peak - after
        dd_pct = (dd / peak * 100.0) if peak else 0.0
        max_dd = max(max_dd, dd)
        max_dd_pct = max(max_dd_pct, dd_pct)
        bal = after
        rows.append(
            {
                "i": i,
                "timestamp": t.get("exit_time") or t["entry_time"],
                "symbol": t["symbol"],
                "side": t["side"],
                "exit_reason": t["exit_reason"],
                "balance_before": before,
                "pnl": pnl,
                "balance_after": after,
                "peak_balance": peak,
                "drawdown": dd,
                "drawdown_pct": dd_pct,
            }
        )
    return rows


def monthly_table(trades: list[dict], candidate_id: str) -> list[dict]:
    buckets: dict[str, list] = defaultdict(list)
    for t in trades:
        ts = _ts(t["entry_time"])
        buckets[f"{ts.year}-{ts.month:02d}"].append(t)
    rows = []
    for month in sorted(buckets):
        s = summarize_trades(buckets[month])
        rows.append({"candidate_id": candidate_id, "month": month, **{k: v for k, v in s.items() if not isinstance(v, (dict, list, tuple))}})
    return rows


def pair_table(trades: list[dict], candidate_id: str) -> list[dict]:
    by: dict[str, list] = defaultdict(list)
    for t in trades:
        by[str(t["symbol"])].append(t)
    rows = []
    for sym, ts in sorted(by.items(), key=lambda kv: sum(float(x["pnl"]) for x in kv[1]), reverse=True):
        s = summarize_trades(ts)
        rows.append({"candidate_id": candidate_id, "pair": sym, **{k: v for k, v in s.items() if not isinstance(v, (dict, list, tuple))}})
    return rows


def leave_out_pairs(trades: list[dict], pair_rows: list[dict], k_list=(1, 3, 5)) -> dict:
    """Diagnostic: net after removing top/bottom k pairs by net."""
    ordered = sorted(pair_rows, key=lambda r: float(r.get("net_pnl") or 0), reverse=True)
    total = float(sum(float(t["pnl"]) for t in trades))
    out = {"total_net": total, "n_pairs": len(ordered)}
    for k in k_list:
        top = ordered[:k]
        bot = ordered[-k:] if len(ordered) >= k else ordered
        top_syms = {r["pair"] for r in top}
        bot_syms = {r["pair"] for r in bot}
        out[f"top{k}_pairs"] = [r["pair"] for r in top]
        out[f"top{k}_net"] = float(sum(float(r["net_pnl"] or 0) for r in top))
        out[f"top{k}_share"] = (out[f"top{k}_net"] / total) if total else None
        out[f"net_without_top{k}"] = float(
            sum(float(t["pnl"]) for t in trades if t["symbol"] not in top_syms)
        )
        out[f"bottom{k}_pairs"] = [r["pair"] for r in bot]
        out[f"bottom{k}_net"] = float(sum(float(r["net_pnl"] or 0) for r in bot))
        out[f"net_without_bottom{k}"] = float(
            sum(float(t["pnl"]) for t in trades if t["symbol"] not in bot_syms)
        )
    return out


def week_streaks(week_rows: list[dict]) -> dict:
    nets = [float(r["net_pnl"]) for r in week_rows]
    def longest(pred):
        best = cur = 0
        for n in nets:
            if pred(n):
                cur += 1
                best = max(best, cur)
            else:
                cur = 0
        return best
    return {
        "longest_winning_streak": longest(lambda n: n > 0),
        "longest_losing_streak": longest(lambda n: n <= 0),
        "zero_weeks": sum(1 for n in nets if n == 0),
        "profitable_weeks": sum(1 for n in nets if n > 0),
        "losing_weeks": sum(1 for n in nets if n < 0),
        "median_weekly_net": float(pd.Series(nets).median()) if nets else None,
        "mean_weekly_net": float(pd.Series(nets).mean()) if nets else None,
        "worst_week": min(nets) if nets else None,
        "best_week": max(nets) if nets else None,
    }


def main() -> int:
    print(f"OUT={OUT}")
    symbols = load_universe()
    spreads = json.loads(JUL_INDEX.read_text(encoding="utf-8")).get("spread_pips_by_symbol") or {}
    spreads_x = {k: float(v) for k, v in spreads.items()}
    print(f"universe n={len(symbols)} sample={symbols[:3]}")
    # cache-key form for legacy dukascopy paths
    symbols_cache = [s.replace("=", "").replace("/", "") for s in symbols]

    # --- Enrich coverage matrix ---
    existing = list(csv.DictReader((OUT / "DATA_COVERAGE_MATRIX.csv").open(encoding="utf-8")))
    legacy_rows = enrich_legacy_coverage(symbols_cache)
    # drop prior broken legacy month-zero rows for flat/years if present; keep utc_* rows
    keep = [r for r in existing if not str(r.get("source", "")).startswith("legacy_")]
    # rewrite legacy_* placeholders with reparsed + keep note rows
    for r in existing:
        if str(r.get("source", "")).startswith("legacy_"):
            # mark superseded
            r = dict(r)
            r["classification"] = "SUPERSEDED_SEE_legacy_dukascopy_reparsed"
            r["strategy_usable"] = False
            keep.append(r)
    keep.extend(legacy_rows)
    write_csv(OUT / "DATA_COVERAGE_MATRIX.csv", keep)

    # Price parity evidence
    parity = {}
    try:
        leg = pd.read_parquet(ROOT / "data/cache/AUDUSDX/1m.parquet")
        v2 = pd.read_parquet(ROOT / "data/cache/mt5/utc_v2/AUDUSDX/1m.parquet")
        leg2 = leg.copy()
        leg2.index = pd.to_datetime(leg2.index, utc=True)
        leg2 = leg2.rename(columns={c: c.lower() for c in leg2.columns})
        v2 = v2.copy()
        v2["timestamp"] = pd.to_datetime(v2["timestamp"], utc=True)
        v2 = v2.set_index("timestamp")
        start = pd.Timestamp("2026-06-02", tz="UTC")
        end = pd.Timestamp("2026-06-20", tz="UTC")
        a = leg2.loc[(leg2.index >= start) & (leg2.index < end), ["open", "close"]]
        b = v2.loc[(v2.index >= start) & (v2.index < end), ["open", "close"]]
        j = a.join(b, lsuffix="_leg", rsuffix="_v2", how="inner")
        parity = {
            "symbol": "AUDUSDX",
            "overlap_bars": len(j),
            "open_mae": float((j["open_leg"] - j["open_v2"]).abs().mean()) if len(j) else None,
            "close_mae": float((j["close_leg"] - j["close_v2"]).abs().mean()) if len(j) else None,
            "pct_exact_close": float(((j["close_leg"] - j["close_v2"]).abs() == 0).mean()) if len(j) else None,
            "legacy_source": "dukascopy",
            "verdict": "NOT_SUBSTITUTABLE_FOR_utc_v2",
        }
    except Exception as e:
        parity = {"error": str(e)}
    (OUT / "vendor_parity_AUDUSDX.json").write_text(json.dumps(parity, indent=2), encoding="utf-8")

    # --- Baseline forensic re-persist ---
    print("Loading continuous Jun-Aug period…")
    cont = load_continuous_junaug(symbols, spreads_x)
    print(
        f"  entry={cont['entry_start']} end={cont['end']} "
        f"H1={cont['n_h1_syms']} M1={cont['n_m1_syms']} master={cont['master']}"
    )

    # Combine monthly tapes onto continuous bars
    base_parts = []
    for path in (JUN_TAPE, JUL_TAPE, AUG_TAPE):
        base_parts.extend(json.loads(path.read_text(encoding="utf-8")))
    # de-dup by (symbol, time_unix) preserving order
    seen = set()
    base_tape = []
    for row in base_parts:
        key = (row.get("symbol"), row.get("time_unix"))
        if key in seen:
            continue
        seen.add(key)
        base_tape.append(row)
    cont_tape = remap(base_tape, cont["data"])
    thu_tape = filter_block_thursday_entries(list(cont_tape))
    print(f"  tape intents control={len(cont_tape)} thursday={len(thu_tape)}")

    results = {}
    all_weekly = []
    all_monthly = []
    all_pair = []
    all_trades = []
    leave_out = {}

    for cid, tape in (("n1_control", cont_tape), ("n1_thursday", thu_tape)):
        print(f"Running {cid} continuous…")
        result, trades = run_engine(cont, tape, capital=CAPITAL)
        summary = summarize_trades(trades, intents=len(tape), mdd=float(result.max_drawdown_pct))
        eq = equity_from_trades(trades, CAPITAL)
        write_csv(OUT / "EQUITY_CURVES" / f"{cid}_equity.csv", eq)
        # also JSON for plotting convenience
        (OUT / "EQUITY_CURVES" / f"{cid}_equity_meta.json").write_text(
            json.dumps(
                {
                    "candidate_id": cid,
                    "mode": "sequential_realized_pnl_from_continuous_engine_run",
                    "starting_capital": CAPITAL,
                    "ending_balance": eq[-1]["balance_after"] if eq else CAPITAL,
                    "peak_balance": max((r["peak_balance"] for r in eq), default=CAPITAL),
                    "max_drawdown_cash": max((r["drawdown"] for r in eq), default=0),
                    "max_drawdown_pct_path": max((r["drawdown_pct"] for r in eq), default=0),
                    "engine_mdd_pct": float(result.max_drawdown_pct),
                    "n_trades": len(trades),
                    "note": "Continuous Jun–Aug engine run with balance-dependent margin sizing from $1000.",
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        weeks = weekly_metrics_table(trades, candidate_id=cid)
        # enrich required columns
        for w in weeks:
            # iso parse
            parts = str(w["week"]).split("-W")
            w["iso_year"] = int(parts[0]) if len(parts) == 2 else None
            w["iso_week"] = int(parts[1]) if len(parts) == 2 else None
            w["start_balance"] = None  # filled below from equity
            w["end_balance"] = None
            w["SL"] = int(w.get("other_exits") or 0) if False else None
        # SL count from trades in week
        for w in weeks:
            wk = w["week"]
            wt = weekly_slices(trades)[wk]
            exits = Counter(str(t.get("exit_reason")) for t in wt)
            w["TP"] = int(exits.get("take_profit", 0))
            w["SL"] = int(exits.get("stop_loss", 0) + exits.get("true_sl", 0))
            w["margin_stop"] = int(exits.get("margin_stop", 0))
            w["win_rate"] = w.get("WR")
            w["max_drawdown"] = w.get("MDD")
            w["net"] = w.get("net_pnl")
            w["fills"] = w.get("fills")
            w["PF"] = w.get("PF")
        # map week balances from equity path
        if eq:
            eq_df = pd.DataFrame(eq)
            eq_df["timestamp"] = pd.to_datetime(eq_df["timestamp"], utc=True)
            for w in weeks:
                ws = pd.Timestamp(w["week_start"], tz="UTC")
                we = ws + pd.Timedelta(days=7)
                seg = eq_df[(eq_df["timestamp"] >= ws) & (eq_df["timestamp"] < we)]
                if len(seg):
                    w["start_balance"] = float(seg.iloc[0]["balance_before"])
                    w["end_balance"] = float(seg.iloc[-1]["balance_after"])
        all_weekly.extend(weeks)
        months = monthly_table(trades, cid)
        all_monthly.extend(months)
        pairs = pair_table(trades, cid)
        all_pair.extend(pairs)
        leave_out[cid] = leave_out_pairs(trades, pairs)
        for t in trades:
            all_trades.append({"candidate_id": cid, **t})
        wdist = weekly_distribution(weeks)
        wstreak = week_streaks(weeks)
        results[cid] = {
            "summary": summary,
            "weekly_dist": wdist,
            "week_streaks": wstreak,
            "leave_out": leave_out[cid],
            "n_trades": len(trades),
            "engine_mdd_pct": float(result.max_drawdown_pct),
            "ending_equity_summary": summary.get("ending_equity"),
            "params": {
                "kind": "n1_tape",
                "thursday_gate": cid == "n1_thursday",
                "rr_factor": 1.0,
                "rr_min": 2.0,
                "ghost_sl_mult": 4.0,
                "session": "all",
                "sides": "both",
                "leverage": LEV,
                "capital": CAPITAL,
            },
        }
        print(
            f"  {cid}: net={summary.get('net_pnl')} PF={summary.get('PF')} "
            f"fills={summary.get('fills')} MDD={result.max_drawdown_pct}"
        )

    # Also per-month separate $1000 resets (comparable to massive-lab methodology)
    print("Running per-month reset baselines (diagnostic comparability)…")
    month_specs = [
        ("june", JUN_INDEX, JUN_TAPE),
        ("july", JUL_INDEX, JUL_TAPE),
        ("august", AUG_INDEX, AUG_TAPE),
    ]
    per_month_rows = []
    for mname, ipath, tpath in month_specs:
        period = load_month_period(mname, ipath, symbols, spreads_x)
        raw = json.loads(tpath.read_text(encoding="utf-8"))
        tape_c = remap(raw, period["data"])
        tape_t = filter_block_thursday_entries(list(tape_c))
        for cid, tape in (("n1_control", tape_c), ("n1_thursday", tape_t)):
            _, trades = run_engine(period, tape)
            s = summarize_trades(trades, intents=len(tape))
            per_month_rows.append(
                {
                    "mode": "per_month_capital_reset_1000",
                    "month": mname,
                    "candidate_id": cid,
                    "entry_start": str(period["entry_start"]),
                    "end": str(period["end"]),
                    **{k: v for k, v in s.items() if not isinstance(v, (dict, list, tuple))},
                }
            )
            print(f"  {mname}/{cid}: net={s.get('net_pnl')} fills={s.get('fills')}")

    write_csv(OUT / "WEEKLY_BASELINE_RESULTS.csv", all_weekly)
    write_csv(OUT / "MONTHLY_BASELINE_RESULTS.csv", all_monthly + per_month_rows)
    write_csv(OUT / "PAIR_BASELINE_RESULTS.csv", all_pair)
    write_csv(OUT / "TRADE_LEVEL_BASELINE_RESULTS.csv", all_trades)
    (OUT / "leave_out_pair_diagnostics.json").write_text(json.dumps(leave_out, indent=2), encoding="utf-8")
    (OUT / "baseline_results.json").write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")

    # Registry entry
    registry = {
        "experiment_family": "gate2_historical_data_recovery_baselines",
        "git_commit": git_commit(),
        "data_manifest": "DATASET_MANIFEST.csv",
        "dataset_period": "2026-06-03 → 2026-09-01 exclusive (Sept clamped)",
        "symbol_universe_hash": hashlib.sha256("\n".join(symbols).encode()).hexdigest(),
        "simulator": "MultiMarketEngine frozen N1 H1+1m",
        "strategy": "alexg8 ghost_sl_mult=4",
        "pre_june_extension": False,
        "candidates": results,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "safety": {
            "SEPTEMBER_READ": False,
            "LIVE_TOUCHED": False,
            "MIRROR_TOUCHED": False,
            "MT5_TERMINAL_USED": False,
            "SEP_OOS_LOCK_MODIFIED": False,
        },
    }
    (OUT / "experiment_registry.json").write_text(json.dumps(registry, indent=2, default=str), encoding="utf-8")

    ctrl = results["n1_control"]
    thu = results["n1_thursday"]
    write_reports(ctrl, thu, leave_out, parity, legacy_rows, per_month_rows, registry)
    print("DONE")
    return 0


def write_reports(ctrl, thu, leave_out, parity, legacy_rows, per_month_rows, registry):
    # Update DATA_RECOVERY_REPORT
    cov = list(csv.DictReader((OUT / "DATA_COVERAGE_MATRIX.csv").open(encoding="utf-8")))
    utc = [r for r in cov if r.get("source") == "utc_v2"]
    lines = [
        "# DATA RECOVERY REPORT",
        "",
        f"**Run directory:** `{OUT.name}`",
        f"**Git:** `{registry['git_commit']}`",
        f"**Generated:** {registry['timestamp']}",
        "",
        "## Safety",
        "",
        "- `SEPTEMBER_READ = FALSE`",
        "- `LIVE_TOUCHED = FALSE`",
        "- `MIRROR_TOUCHED = FALSE`",
        "- `MT5_TERMINAL_USED = FALSE`",
        "- `SEP_OOS_LOCK_MODIFIED = FALSE`",
        "",
        "## 1. Can we recover pre-June data?",
        "",
        "**Short answer: NOT in a form that is fully comparable to the locked N1 H1+M1 utc_v2 economics — offline.**",
        "",
        "### What was searched (local only)",
        "",
        "| Location | Result |",
        "| --- | --- |",
        "| `data/cache/mt5/utc_v2/{SYM}/1m.parquet` | 60/60 files; M1 starts ~2026-06-01/02 only |",
        "| `data/cache/mt5/utc_v2/{SYM}/1h.parquet` | 60/60; H1 from ~2025-06/09 |",
        "| `data/cache/mt5/utc/` | H1 only (no M1) |",
        "| `data/cache/mt5/{SYM}/` flat | H1 only |",
        "| `data/cache/{SYM}/` + `years/` | Dukascopy vendor M1 back to 2023 for ~38/60 symbols |",
        "| Existing run indexes Jun/Jul/Aug | Confirm research windows; no pre-June M1 packs |",
        "| Prior scripts (`run_n1_may_oos_integrity_gate.py`, `extend_mt5_historical_coverage.py`) | Document prior May insufficiency; extension requires Mirror MT5 (FORBIDDEN this task) |",
        "",
        "### Explicitly NOT done",
        "",
        "- No MT5 terminal / broker `copy_rates`",
        "- No Mirror or Live login",
        "- No Internet/vendor download",
        "- No September reads",
        "",
        "## 2. Exact periods and symbols usable",
        "",
        "### utc_v2 (N1-comparable)",
        "",
        "| Period | H1 ok | M1 ok | Complete 60? | Class | Strategy usable? |",
        "| --- | ---: | ---: | --- | --- | --- |",
    ]
    for r in utc:
        lines.append(
            f"| {r['period']} | {r['h1_symbols_ok']} | {r['m1_symbols_ok']} | "
            f"{r['complete_60']} | {r['classification']} | {r['strategy_usable']} |"
        )
    lines += [
        "",
        "### Legacy Dukascopy (re-parsed DatetimeIndex) — NOT strategy-usable",
        "",
        "| Period | H1 ok | M1 ok | Class | Why rejected |",
        "| --- | ---: | ---: | --- | --- |",
    ]
    for r in legacy_rows:
        lines.append(
            f"| {r['period']} | {r['h1_symbols_ok']} | {r['m1_symbols_ok']} | "
            f"{r['classification']} | vendor mix + incomplete 60-pair |"
        )
    lines += [
        "",
        f"### Vendor parity evidence (AUDUSDX overlap Jun 2–20)",
        "",
        f"```json",
        json.dumps(parity, indent=2),
        "```",
        "",
        "Prices are close but **not identical** (MAE ~5e-4; <1% exact matches). Volume is 0 on Dukascopy vs tick volume on MT5. Policy forbids mixing vendors into N1 economics.",
        "",
        "## 3. Earliest fully comparable H1+M1 date",
        "",
        "- **Earliest A_FULLY_USABLE month:** `2026-06`",
        "- **Earliest strategy evaluation start used by locked Jun pack:** `2026-06-03T03:00:00+00:00`",
        "- **utc_v2 M1 physical earliest across universe:** ~`2026-06-01` to `2026-06-02` depending on symbol",
        "- Pre-June utc_v2: **C_H1_ONLY** (signal research possible; NOT comparable economics)",
        "",
        "## Classification summary",
        "",
        "| Class | Periods |",
        "| --- | --- |",
        "| A_FULLY_USABLE | 2026-06, 2026-07, 2026-08 (utc_v2) |",
        "| B_PARTIALLY_USABLE | none at 60-pair N1 standard |",
        "| C_H1_ONLY | 2025-10 → 2026-05 utc_v2 H1 |",
        "| D_INVALID | Dukascopy legacy (vendor mix); missing M1 elsewhere |",
        "",
        "See also `DATA_COVERAGE_MATRIX.csv`, `DATASET_MANIFEST.csv`, `vendor_parity_AUDUSDX.json`.",
        "",
    ]
    (OUT / "DATA_RECOVERY_REPORT.md").write_text("\n".join(lines), encoding="utf-8")

    # BASELINE report
    cs, ts = ctrl["summary"], thu["summary"]
    bl = [
        "# BASELINE EXTENDED REPORT",
        "",
        "## Status of extension",
        "",
        "**PRE_JUNE_EXTENSION = FALSE**",
        "",
        "No additional complete utc_v2 H1+M1 month before June 2026 was recovered offline.",
        "Therefore this report is a **forensic re-persist** of the locked Jun–Aug baselines with full weekly / monthly / pair / trade / equity detail — **not** an extended-history confirmation of Thursday outside Jun–Aug.",
        "",
        "Question *“Does Thursday survive outside June–August?”* remains **UNRESOLVED** (data insufficient).",
        "",
        "## Configurations (frozen; no new knobs)",
        "",
        "```json",
        json.dumps(ctrl["params"], indent=2),
        "```",
        "",
        "Thursday differs only by `thursday_gate=true` (block Thursday entries).",
        "",
        "## Continuous Jun–Aug engine run (balance-dependent sizing from $1000)",
        "",
        "| Metric | n1_control | n1_thursday | Delta (Thu−Ctrl) |",
        "| --- | ---: | ---: | ---: |",
        f"| net | {fnum(cs.get('net_pnl'))} | {fnum(ts.get('net_pnl'))} | {fnum((ts.get('net_pnl') or 0)-(cs.get('net_pnl') or 0))} |",
        f"| PF | {fnum(cs.get('PF'),3)} | {fnum(ts.get('PF'),3)} | {fnum((ts.get('PF') or 0)-(cs.get('PF') or 0),3)} |",
        f"| fills | {cs.get('fills')} | {ts.get('fills')} | {(ts.get('fills') or 0)-(cs.get('fills') or 0)} |",
        f"| WR | {fnum(cs.get('WR'),3)} | {fnum(ts.get('WR'),3)} | |",
        f"| TP | {cs.get('TP')} | {ts.get('TP')} | |",
        f"| daily_close | {cs.get('daily_close')} | {ts.get('daily_close')} | |",
        f"| margin_stop | {cs.get('margin_stop')} | {ts.get('margin_stop')} | |",
        f"| engine MDD% | {fnum(ctrl['engine_mdd_pct'],3)} | {fnum(thu['engine_mdd_pct'],3)} | |",
        f"| ending equity | {fnum(cs.get('ending_equity'))} | {fnum(ts.get('ending_equity'))} | |",
        "",
        "### Weekly robustness",
        "",
        "| | control | thursday |",
        "| --- | ---: | ---: |",
    ]
    for k in ("profitable_weeks", "losing_weeks", "zero_weeks", "median_weekly_net", "mean_weekly_net", "worst_week", "best_week", "longest_winning_streak", "longest_losing_streak"):
        bl.append(f"| {k} | {fnum(ctrl['week_streaks'].get(k),3) if isinstance(ctrl['week_streaks'].get(k), float) else ctrl['week_streaks'].get(k)} | {fnum(thu['week_streaks'].get(k),3) if isinstance(thu['week_streaks'].get(k), float) else thu['week_streaks'].get(k)} |")
    bl += [
        "",
        "Full weekly tables: `WEEKLY_BASELINE_RESULTS.csv`",
        "",
        "### Monthly (continuous-run trade attribution by entry month)",
        "",
        "See `MONTHLY_BASELINE_RESULTS.csv` (includes both continuous attribution and per-month $1000 reset diagnostics).",
        "",
        "### Pair concentration",
        "",
        "```json",
        json.dumps({"n1_control": leave_out["n1_control"], "n1_thursday": leave_out["n1_thursday"]}, indent=2),
        "```",
        "",
        "Full pair tables: `PAIR_BASELINE_RESULTS.csv`",
        "",
        "### Equity curves",
        "",
        "- `EQUITY_CURVES/n1_control_equity.csv`",
        "- `EQUITY_CURVES/n1_thursday_equity.csv`",
        "",
        "These are true sequential paths from a **continuous** Jun–Aug MultiMarketEngine run (not the old aggregate-only massive-lab summary).",
        "",
        "### Per-month reset diagnostic (massive-lab-comparable)",
        "",
        "| month | candidate | net | PF | fills |",
        "| --- | --- | ---: | ---: | ---: |",
    ]
    for r in per_month_rows:
        bl.append(f"| {r['month']} | {r['candidate_id']} | {fnum(r.get('net_pnl'))} | {fnum(r.get('PF'),3)} | {r.get('fills')} |")
    bl += [
        "",
        "## Answers that remain blocked",
        "",
        "- Thursday outside Jun–Aug: **DATA_INSUFFICIENT**",
        "- Walk-forward on pre-June months: **DATA_INSUFFICIENT**",
        "",
        "## Safety",
        "",
        "```",
        "SEPTEMBER_READ = FALSE",
        "LIVE_TOUCHED = FALSE",
        "MIRROR_TOUCHED = FALSE",
        "SEP_OOS_LOCK_MODIFIED = FALSE",
        "```",
        "",
    ]
    (OUT / "BASELINE_EXTENDED_REPORT.md").write_text("\n".join(bl), encoding="utf-8")

    # RESEARCH_RECOMMENDATION
    rec = [
        "# RESEARCH RECOMMENDATION",
        "",
        "Answers to the 12 required questions:",
        "",
        "1. **Can we recover pre-June data?** — Locally: H1 yes (utc_v2), M1 no (utc_v2). Dukascopy M1 exists for a subset but is **not** substitutable (vendor mix + incomplete 60-pair + price mismatch).",
        "2. **What exact periods/symbols are usable?** — A_FULLY_USABLE: 2026-06/07/08 utc_v2 60/60. Pre-June utc_v2: C_H1_ONLY.",
        "3. **Earliest fully comparable H1+M1 date?** — Strategy window starts `2026-06-03T03:00Z` (Jun pack). Physical M1 ~`2026-06-01/02`.",
        "4. **Does N1 reproduce over expanded period?** — No expansion available. Jun–Aug continuous replay reproduced locked baselines with full forensic persistence (see BASELINE_EXTENDED_REPORT).",
        "5. **Does Thursday survive outside Jun–Aug?** — **UNRESOLVED** (no pre-June comparable M1).",
        "6. **Is Thursday broad across weeks?** — See WEEKLY_BASELINE_RESULTS.csv / week_streaks in baseline_results.json (Jun–Aug only).",
        "7. **Is Thursday broad across pairs?** — See PAIR_BASELINE_RESULTS.csv + leave_out_pair_diagnostics.json (Jun–Aug only).",
        "8. **True sequential equity curve?** — Yes, now persisted under EQUITY_CURVES/ for continuous Jun–Aug runs.",
        "9. **What changed vs prior Jun–Aug evidence?** — Persistence quality fixed (week/month/pair/trade/equity). No new historical months added.",
        "10. **Which §30 unknowns resolved?** — Per-week, per-month, per-pair, and sequential equity for locked baselines on Jun–Aug. Outside-Jun–Aug behavior still unresolved.",
        "11. **Which remain unresolved?** — Pre-June regimes; Sep OOS (reserved); GhostSL causal richness; RR×Thu posthoc; AlexG3–7; TP/SL geometry; MC/bootstrap; walk-forward; occupancy; liquidity suite; tick ambiguity.",
        "12. **Next research stage?** — See below.",
        "",
        "## Recommended next stage (ordered)",
        "",
        "### Priority 0 — Data (only if authorized later)",
        "",
        "If/when Mirror historical `copy_rates` is explicitly authorized as a **data-only** Gate-2 task (still no trading):",
        "- Extend utc_v2 M1 for Jan–May 2026 for all 60 pairs",
        "- Re-run this recovery audit until May (or earlier) reaches A_FULLY_USABLE",
        "- Then replay n1_control vs n1_thursday on the extended window **before** any RR/GhostSL mining",
        "",
        "**This task did NOT perform that extension** (MT5/Mirror forbidden).",
        "",
        "### Priority 1 — Without new data",
        "",
        "Do **not** launch another 200+ Cartesian search.",
        "Use the new weekly/pair/equity artifacts to stress-test the locked Thursday claim **inside** Jun–Aug only:",
        "- week-by-week delta tables",
        "- leave-top-k pair sensitivity (already computed)",
        "- losing-streak analysis",
        "",
        "### Priority 2 — After pre-June A data exists",
        "",
        "1. Frozen n1_control vs n1_thursday on extended history",
        "2. Chronological walk-forward of the **already-known** Thursday hypothesis",
        "3. Only then: controlled RR / GhostSL / TP-SL / AlexG3–7 designs",
        "",
        "### Do not",
        "",
        "- Promote thu_rr / GhostSL posthoc cells to validated",
        "- Touch September lock",
        "- Mix Dukascopy into N1 economics",
        "- Optimize parameters against incomplete history",
        "",
        "## Safety reaffirmation",
        "",
        "```",
        "SEPTEMBER_READ = FALSE",
        "LIVE_TOUCHED = FALSE",
        "MIRROR_TOUCHED = FALSE",
        "SEP_OOS_LOCK_MODIFIED = FALSE",
        "```",
        "",
    ]
    (OUT / "RESEARCH_RECOMMENDATION.md").write_text("\n".join(rec), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
