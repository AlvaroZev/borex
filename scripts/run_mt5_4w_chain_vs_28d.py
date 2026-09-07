#!/usr/bin/env python3
"""Last 4 weeks: chained weekly runs vs one 28-day run.

6 variants = 3 strategies × daily-close ON/OFF.
Each week starts at the previous week's finishing equity (same config).
The 28-day run uses the same $1000 start and the same eval window.

Candle history is the full year pickle prefix (AOI / HTF / weekly bias).
Entries are still gated to each eval week / the 28-day window.
"""

from __future__ import annotations

import json
import os
import pickle
import sys
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
LIVE = ROOT.parent / "borex_live"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(LIVE))
load_dotenv(LIVE / ".env")

YEAR_DIR = ROOT / "data" / "runs" / "mt5_1y_rrclamp_and_g9"
OUT_DIR = ROOT / "data" / "runs" / "mt5_4w_chain_vs_28d_fullhist"
CAPITAL = 1000.0
N_WEEKS = 4
# 0 = keep the full year pickle prefix (AOI/HTF need months, not 14d).
WARMUP_DAYS = 0
MATCH_EPS = 0.05

CLAMP_RR = dict(
    rr_mode="dynamic",
    true_sl_rr=2.0,
    rr_factor=1.0,
    rr_min=2.0,
    rr_max=6.0,
    risk_include_commission=True,
    commission_at_entry=False,
    force_flat_friday=False,
)

JOBS = tuple(
    {
        "tag": f"{strat}_daily_{'on' if daily else 'off'}",
        "strategy": strat,
        **CLAMP_RR,
        "force_flat_daily": daily,
    }
    for strat in ("alexg7aligned", "alexg8", "alexg9")
    for daily in (True, False)
)

_PAYLOAD: dict | None = None


def _ts(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    if t.tzinfo is None:
        return t.tz_localize("UTC")
    return t.tz_convert("UTC")


def make_strategy(name: str, min_rr: float):
    from borex.alexg import AlexG7AlignedStrategy, AlexG8Strategy, AlexG9Strategy

    kwargs = dict(min_rr=min_rr, execution_interval="1h", peer_blend=0.0)
    if name == "alexg9":
        return AlexG9Strategy(**kwargs)
    if name == "alexg8":
        return AlexG8Strategy(**kwargs)
    return AlexG7AlignedStrategy(**kwargs)


def _init_worker(payload_path: str) -> None:
    global _PAYLOAD
    os.environ["OPENBLAS_NUM_THREADS"] = "1"
    os.environ["OMP_NUM_THREADS"] = "1"
    os.environ["MKL_NUM_THREADS"] = "1"
    with open(payload_path, "rb") as f:
        _PAYLOAD = pickle.load(f)


def cut_data(data: dict, end: pd.Timestamp) -> dict:
    out = {}
    for sym, bars in data.items():
        keep = [c for c in bars if _ts(c.timestamp) <= end]
        if len(keep) >= 120:
            out[sym] = keep
    return out


def entry_bar_index(master_bars: list, entry_start: pd.Timestamp, min_bars: int) -> int:
    start_i = next(
        (i for i, c in enumerate(master_bars) if _ts(c.timestamp) >= entry_start),
        min_bars,
    )
    return max(int(min_bars), int(start_i))


def equity_at_ts(result, master_bars: list, min_bars: int, ts: pd.Timestamp) -> float:
    """Mark-to-market equity after the master bar at/after ts (before end_of_data)."""
    curve = list(result.equity_curve or [])
    if not curve:
        return float(result.final_equity)
    i = next((k for k, c in enumerate(master_bars) if _ts(c.timestamp) >= ts), None)
    if i is None:
        return float(curve[-1])
    if i < min_bars:
        return float(curve[0])
    idx = 1 + (i - min_bars)
    idx = min(max(idx, 0), len(curve) - 1)
    return float(curve[idx])


def run_engine(job: dict, data: dict, capital: float, entry_start: pd.Timestamp):
    from borex.alexg.multi_market import pick_master_symbol
    from borex.backtest.engine import BacktestConfig
    from borex.backtest.multi_market_engine import MultiMarketEngine

    strategy = make_strategy(job["strategy"], min_rr=float(job["true_sl_rr"]))
    strategy.ohlc_quantize_pips = 0.0
    config = BacktestConfig(
        initial_capital=float(capital),
        leverage=5000.0,
        position_size_pct=0.01,
        size_mode="margin",
        true_sl=True,
        true_sl_rr=float(job["true_sl_rr"]),
        rr_mode=job["rr_mode"],
        rr_factor=float(job["rr_factor"]),
        rr_min=float(job["rr_min"]),
        rr_max=float(job["rr_max"]),
        stop_loss_pct=None,
        take_profit_pct=None,
        commission_per_lot=7.0,
        risk_include_commission=bool(job["risk_include_commission"]),
        commission_at_entry=bool(job["commission_at_entry"]),
        force_flat_friday=bool(job["force_flat_friday"]),
        force_flat_daily=bool(job["force_flat_daily"]),
        force_flat_utc_hour=19,
        force_flat_friday_from_hour=19,
        winrate_min_trades=20,
        spread_pips=0.0,
        slippage_pips=0.0,
    )
    master = pick_master_symbol(data)
    min_bars = entry_bar_index(data[master], entry_start, int(strategy.min_bars))
    strategy.min_bars = min_bars
    engine = MultiMarketEngine(strategy, config, max_positions=60)
    engine._progress_every = 50
    result = engine.run(data, timeframe="1h", master_symbol=master, same_bar_exit=False)
    return result, master, min_bars


def summarize(result) -> dict:
    return {
        "account": float(result.final_equity),
        "net_pnl": float(result.final_equity) - float(result.config.initial_capital),
        "return_pct": float(result.total_return_pct),
        "trades": int(result.total_trades),
        "wins": int(result.winning_trades),
        "losses": int(result.losing_trades),
        "win_rate": float(result.win_rate),
        "profit_factor": float(result.profit_factor),
        "max_drawdown_pct": float(result.max_drawdown_pct),
        "avg_planned_rr": float(result.avg_planned_rr),
        "exit_reasons": dict(Counter(t.exit_reason for t in result.trades)),
    }


def _run_job(job: dict) -> dict:
    assert _PAYLOAD is not None
    data = _PAYLOAD["data"]
    weeks = _PAYLOAD["weeks"]
    tag = job["tag"]
    t0 = time.perf_counter()
    print(f"[{tag}] start", flush=True)

    capital = CAPITAL
    week_rows = []
    for w in weeks:
        w_start = _ts(w["start"])
        w_end = _ts(w["end"])
        sliced = cut_data(data, w_end)
        result, master, min_bars = run_engine(job, sliced, capital, w_start)
        row = {
            "week": w["week"],
            "start": w["start"],
            "end": w["end"],
            "start_capital": capital,
            "master": master,
            "min_bars": min_bars,
            "master_bars": len(sliced[master]),
            **summarize(result),
        }
        week_rows.append(row)
        print(
            f"[{tag}] W{w['week']} ${capital:,.2f} → ${row['account']:,.2f}  "
            f"trades={row['trades']} WR={row['win_rate']*100:.1f}%  "
            f"eod={row['exit_reasons'].get('end_of_data', 0)}",
            flush=True,
        )
        capital = float(result.final_equity)

    month_start = _ts(weeks[0]["start"])
    month_end = _ts(weeks[-1]["end"])
    month_data = cut_data(data, month_end)
    month_result, month_master, month_min = run_engine(
        job, month_data, CAPITAL, month_start
    )
    month_summary = summarize(month_result)
    mtm = []
    for w in weeks:
        mtm.append(
            {
                "week": w["week"],
                "end": w["end"],
                "mtm_equity": equity_at_ts(
                    month_result, month_data[month_master], month_min, _ts(w["end"])
                ),
            }
        )

    chain_final = float(week_rows[-1]["account"])
    month_final = float(month_summary["account"])
    match = abs(chain_final - month_final) <= MATCH_EPS
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tag": tag,
        "strategy": job["strategy"],
        "force_flat_daily": bool(job["force_flat_daily"]),
        "params": {
            "capital0": CAPITAL,
            "leverage": 5000.0,
            "position_size_pct": 0.01,
            "rr_mode": job["rr_mode"],
            "true_sl_rr": job["true_sl_rr"],
            "rr_min": job["rr_min"],
            "rr_max": job["rr_max"],
            "force_flat_friday": job["force_flat_friday"],
            "force_flat_daily": job["force_flat_daily"],
            "winrate_min_trades": 20,
        },
        "chain": {
            "weeks": week_rows,
            "final_account": chain_final,
            "trades": sum(w["trades"] for w in week_rows),
        },
        "month_28d": {
            **month_summary,
            "master": month_master,
            "min_bars": month_min,
            "master_bars": len(month_data[month_master]),
            "mtm_at_week_ends": mtm,
        },
        "compare": {
            "chain_final": chain_final,
            "month_final": month_final,
            "delta": chain_final - month_final,
            "match": match,
            "eps": MATCH_EPS,
        },
        "elapsed_sec": time.perf_counter() - t0,
    }
    out_dir = OUT_DIR / tag
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "report.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        f"[{tag}] chain=${chain_final:,.2f}  28d=${month_final:,.2f}  "
        f"delta={chain_final - month_final:+.2f}  match={match}  "
        f"in {report['elapsed_sec']:.1f}s → {path}",
        flush=True,
    )
    return {
        "tag": tag,
        "ok": True,
        "report": str(path),
        "compare": report["compare"],
        "chain_weeks": [
            {
                "week": w["week"],
                "start_capital": w["start_capital"],
                "account": w["account"],
                "trades": w["trades"],
                "win_rate": w["win_rate"],
                "profit_factor": w["profit_factor"],
                "max_drawdown_pct": w["max_drawdown_pct"],
                "exit_reasons": w["exit_reasons"],
            }
            for w in week_rows
        ],
        "month_28d": {
            "account": month_summary["account"],
            "trades": month_summary["trades"],
            "win_rate": month_summary["win_rate"],
            "profit_factor": month_summary["profit_factor"],
            "max_drawdown_pct": month_summary["max_drawdown_pct"],
            "exit_reasons": month_summary["exit_reasons"],
            "mtm_at_week_ends": mtm,
        },
        "elapsed_sec": report["elapsed_sec"],
    }


def week_windows(last: pd.Timestamp, n: int = N_WEEKS) -> list[dict]:
    """Half-open weeks: W1..W3 end bar is not reused as W(n+1) first entry bar."""
    out = []
    for i in range(n):
        end = last - timedelta(days=7 * (n - 1 - i))
        start = last - timedelta(days=7 * (n - i))
        if i > 0:
            start = start + timedelta(microseconds=1)
        out.append(
            {
                "week": i + 1,
                "start": start.isoformat(),
                "end": end.isoformat(),
            }
        )
    return out


def main() -> int:
    year_pickle = YEAR_DIR / "_candles.pkl"
    if not year_pickle.is_file():
        raise SystemExit(f"missing pickle {year_pickle}")
    print(f"Reusing {year_pickle}", flush=True)
    with open(year_pickle, "rb") as f:
        payload = pickle.load(f)

    from borex.alexg.multi_market import pick_master_symbol

    master = pick_master_symbol(payload["data"])
    last = _ts(payload["data"][master][-1].timestamp)
    eval_start = last - timedelta(days=7 * N_WEEKS)
    first = _ts(payload["data"][master][0].timestamp)
    if WARMUP_DAYS and WARMUP_DAYS > 0:
        load_start = eval_start - timedelta(days=WARMUP_DAYS)
        if load_start < first:
            load_start = first
        warmup_label = f"{WARMUP_DAYS}d"
    else:
        load_start = first
        warmup_label = f"full prefix {(eval_start - first).days}d"
    sliced = {}
    coverage = {}
    for sym, bars in payload["data"].items():
        keep = [c for c in bars if load_start <= _ts(c.timestamp) <= last]
        if len(keep) >= 120:
            sliced[sym] = keep
            coverage[sym] = {
                "bars": len(keep),
                "first": str(keep[0].timestamp),
                "last": str(keep[-1].timestamp),
            }
    weeks = week_windows(last, N_WEEKS)
    print(
        f"Eval {eval_start.date()} → {last.date()} | warmup {warmup_label} | "
        f"pairs={len(sliced)} bars~{max(len(v) for v in sliced.values())}",
        flush=True,
    )
    for w in weeks:
        print(f"  W{w['week']} {_ts(w['start']).date()} → {_ts(w['end']).date()}", flush=True)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    run_payload = {
        "data": sliced,
        "coverage": coverage,
        "capital": CAPITAL,
        "weeks": weeks,
        "eval_start": eval_start.isoformat(),
        "out_dir": str(OUT_DIR),
    }
    payload_path = OUT_DIR / "_candles.pkl"
    payload_path.write_bytes(pickle.dumps(run_payload, protocol=pickle.HIGHEST_PROTOCOL))

    workers = min(6, os.cpu_count() or 6, len(JOBS))
    print(f"{len(JOBS)} jobs × {workers} workers → {OUT_DIR}", flush=True)
    results = []
    with ProcessPoolExecutor(
        max_workers=workers,
        initializer=_init_worker,
        initargs=(str(payload_path),),
    ) as pool:
        futs = {pool.submit(_run_job, dict(job)): job["tag"] for job in JOBS}
        for fut in as_completed(futs):
            tag = futs[fut]
            try:
                results.append(fut.result())
            except Exception as exc:
                print(f"[{tag}] FAILED: {exc}", flush=True)
                results.append({"tag": tag, "ok": False, "error": str(exc)})

    index = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "jobs": results,
    }
    index_path = OUT_DIR / "index.json"
    index_path.write_text(json.dumps(index, indent=2), encoding="utf-8")
    print("\n======== 4W CHAIN vs 28D ========", flush=True)
    for row in sorted(results, key=lambda r: r.get("tag", "")):
        if not row.get("ok"):
            print(f"FAIL  {row['tag']}  {row.get('error')}", flush=True)
            continue
        c = row["compare"]
        flag = "MATCH" if c["match"] else "DIFF "
        print(
            f"{flag}  {row['tag']:28s}  chain=${c['chain_final']:,.2f}  "
            f"28d=${c['month_final']:,.2f}  delta={c['delta']:+.2f}",
            flush=True,
        )
    print(f"Index {index_path}", flush=True)
    return 0 if all(r.get("ok") for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
