#!/usr/bin/env python3
"""MT5 H1 backtests in parallel (usual MultiMarketEngine).

1) Strategies from the paper RR sweep with WR > 15%
   (alexg7aligned, alexg8, alexg9) with dynamic RR clamped 2:1–6:1.
2) Native alexg9 daily-close ON vs OFF (Friday weekend flatten on both).
   Daily close exits each trade at the close-hour bar open of *its originating
   session* (Asia 09:00, London/overlap 16:00, NY 21:00), not a global 19:00 UTC.

Use --week to score the last 7 days ($1000 start; 14d warmup bars for setups).
"""

from __future__ import annotations

import argparse
import json
import os
import pickle
import sys
import time
from collections import Counter, defaultdict
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
WEEK_DIR = ROOT / "data" / "runs" / "mt5_1w_rrclamp_and_g9"
CAPITAL = 1000.0

# Usual 1y compounding knobs + dynamic RR band.
CLAMP_RR = dict(
    rr_mode="dynamic",
    true_sl_rr=2.0,
    rr_factor=1.0,
    rr_min=2.0,
    rr_max=6.0,
    risk_include_commission=True,
    commission_at_entry=False,
    force_flat_friday=False,
    force_flat_daily=False,
)

# Live alexg9 engine defaults. Daily close is session-of-fill, not 19:00 UTC.
G9_NATIVE_BASE = dict(
    rr_mode="fixed",
    true_sl_rr=3.0,
    rr_factor=1.0,
    rr_min=0.0,
    rr_max=0.0,
    risk_include_commission=False,
    commission_at_entry=True,
    force_flat_friday=True,
)

JOBS = (
    {"tag": "alexg7aligned_dyn_rr2_6", "strategy": "alexg7aligned", **CLAMP_RR},
    {"tag": "alexg8_dyn_rr2_6", "strategy": "alexg8", **CLAMP_RR},
    {"tag": "alexg9_dyn_rr2_6", "strategy": "alexg9", **CLAMP_RR},
    {
        "tag": "alexg9_native_daily_on",
        "strategy": "alexg9",
        **G9_NATIVE_BASE,
        "force_flat_daily": True,
    },
    {
        "tag": "alexg9_native_daily_off",
        "strategy": "alexg9",
        **G9_NATIVE_BASE,
        "force_flat_daily": False,
    },
)

_PAYLOAD: dict | None = None


def _ts(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    if t.tzinfo is None:
        return t.tz_localize("UTC")
    return t.tz_convert("UTC")


def slice_last_eval(
    data: dict, *, eval_days: int, warmup_days: int
) -> tuple[dict, dict, pd.Timestamp, pd.Timestamp]:
    """Keep warmup + last eval window. Strategy needs ~120 H1 bars before it trades."""
    from borex.alexg.multi_market import pick_master_symbol

    master = pick_master_symbol(data)
    last = _ts(data[master][-1].timestamp)
    eval_start = last - timedelta(days=eval_days)
    load_start = eval_start - timedelta(days=warmup_days)
    sliced: dict = {}
    coverage: dict = {}
    for sym, bars in data.items():
        keep = [c for c in bars if load_start <= _ts(c.timestamp) <= last]
        if len(keep) >= 120:
            sliced[sym] = keep
            coverage[sym] = {
                "bars": len(keep),
                "first": str(keep[0].timestamp),
                "last": str(keep[-1].timestamp),
            }
    return sliced, coverage, eval_start, last


def _pip_size(symbol: str) -> float:
    sym = symbol.upper().replace("=X", "")
    return 0.01 if "JPY" in sym else 0.0001


def prequantize(candles, symbol: str, pips: float):
    from borex.models.candle import Candle

    step = pips * _pip_size(symbol)
    if step <= 0:
        return candles

    def snap(x: float) -> float:
        return round(x / step) * step

    out = []
    for c in candles:
        o, h, l, cl = snap(c.open), snap(c.high), snap(c.low), snap(c.close)
        hi = max(h, o, cl)
        lo = min(l, o, cl)
        out.append(
            Candle(
                timestamp=c.timestamp,
                open=o,
                high=hi,
                low=lo,
                close=cl,
                volume=c.volume,
            )
        )
    return out


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


def _run_job(job: dict) -> dict:
    from borex.alexg.multi_market import pick_master_symbol
    from borex.backtest.engine import BacktestConfig
    from borex.backtest.multi_market_engine import MultiMarketEngine

    assert _PAYLOAD is not None
    data = _PAYLOAD["data"]
    coverage = _PAYLOAD["coverage"]
    capital = float(_PAYLOAD["capital"])
    window = _PAYLOAD["window"]
    tag = job["tag"]
    t0 = time.perf_counter()
    print(f"[{tag}] start", flush=True)

    strategy = make_strategy(job["strategy"], min_rr=float(job["true_sl_rr"]))
    strategy.ohlc_quantize_pips = 0.0
    config = BacktestConfig(
        initial_capital=capital,
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
    eval_start = _PAYLOAD.get("eval_start")
    if eval_start is not None:
        ev = _ts(eval_start)
        start_i = next(
            (i for i, c in enumerate(data[master]) if _ts(c.timestamp) >= ev),
            int(strategy.min_bars),
        )
        strategy.min_bars = max(int(strategy.min_bars), int(start_i))
        print(
            f"[{tag}] entries from bar {strategy.min_bars}/{len(data[master])} "
            f"(eval {ev.date()})",
            flush=True,
        )
    engine = MultiMarketEngine(strategy, config, max_positions=60)
    engine._progress_every = 25 if eval_start is not None else 100
    result = engine.run(
        data,
        timeframe="1h",
        master_symbol=master,
        same_bar_exit=False,
    )

    trades = list(result.trades)
    monthly: dict[str, dict] = defaultdict(lambda: {"pnl": 0.0, "trades": 0, "wins": 0})
    by_symbol: dict[str, dict] = defaultdict(lambda: {"pnl": 0.0, "trades": 0, "wins": 0})
    for t in trades:
        ts = pd.Timestamp(t.exit_time or t.entry_time)
        key = ts.strftime("%Y-%m")
        monthly[key]["pnl"] += float(t.pnl)
        monthly[key]["trades"] += 1
        monthly[key]["wins"] += int(t.pnl > 0)
        s = by_symbol[t.symbol]
        s["pnl"] += float(t.pnl)
        s["trades"] += 1
        s["wins"] += int(t.pnl > 0)

    eq = list(result.equity_curve or [])
    if len(eq) > 120:
        step = max(1, len(eq) // 120)
        eq_chart = [{"i": i, "equity": float(eq[i])} for i in range(0, len(eq), step)]
        if eq_chart[-1]["i"] != len(eq) - 1:
            eq_chart.append({"i": len(eq) - 1, "equity": float(eq[-1])})
    else:
        eq_chart = [{"i": i, "equity": float(v)} for i, v in enumerate(eq)]

    session = "overlap" if job["strategy"] == "alexg7aligned" else "all"
    win = dict(window)
    if eval_start is not None:
        win["eval_start"] = str(eval_start)
        win["score"] = "entries_from_eval_start_1000_compounding"
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tag": tag,
        "window": {
            **win,
            "first_master_bar": coverage[master]["first"],
            "last_master_bar": coverage[master]["last"],
        },
        "params": {
            "strategy": job["strategy"],
            "feed": "MT5 H1",
            "capital": capital,
            "leverage": 5000.0,
            "position_size_pct": 0.01,
            "min_rr": job["true_sl_rr"],
            "rr_mode": job["rr_mode"],
            "rr_factor": job["rr_factor"],
            "rr_min": job["rr_min"],
            "rr_max": job["rr_max"],
            "winrate_min_trades": 20,
            "max_positions": 60,
            "same_bar_exit": False,
            "commission_per_lot": 7.0,
            "risk_include_commission": job["risk_include_commission"],
            "commission_at_entry": job["commission_at_entry"],
            "force_flat_friday": job["force_flat_friday"],
            "force_flat_daily": job["force_flat_daily"],
            "peer_blend": 0.0,
            "session": session,
        },
        "coverage": {
            "pairs": len(data),
            "full_year_pairs": sum(v["bars"] >= 6000 for v in coverage.values()),
            "limited_pairs": sum(v["bars"] < 6000 for v in coverage.values()),
            "master": master,
            "master_bars": len(data[master]),
        },
        "summary": {
            "initial_equity": capital,
            "account": float(result.final_equity),
            "final_equity": float(result.final_equity),
            "net_pnl": float(result.final_equity) - capital,
            "return_pct": float(result.total_return_pct),
            "max_drawdown_pct": float(result.max_drawdown_pct),
            "trades": int(result.total_trades),
            "wins": int(result.winning_trades),
            "losses": int(result.losing_trades),
            "win_rate": float(result.win_rate),
            "profit_factor": float(result.profit_factor),
            "avg_win": float(result.avg_win),
            "avg_loss": float(result.avg_loss),
            "total_commission": float(result.total_commission),
            "avg_planned_rr": float(result.avg_planned_rr),
        },
        "monthly": dict(sorted(monthly.items())),
        "exit_reasons": dict(Counter(t.exit_reason for t in trades)),
        "sides": dict(Counter(t.side.value for t in trades)),
        "top_symbols": sorted(
            ({"symbol": k, **v} for k, v in by_symbol.items()),
            key=lambda x: x["pnl"],
            reverse=True,
        )[:10],
        "bottom_symbols": sorted(
            ({"symbol": k, **v} for k, v in by_symbol.items()),
            key=lambda x: x["pnl"],
        )[:10],
        "equity_curve": eq_chart,
        "elapsed_sec": time.perf_counter() - t0,
    }
    out_root = Path(_PAYLOAD.get("out_dir") or YEAR_DIR)
    out_dir = out_root / tag
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "report.json"
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    s = report["summary"]
    line = (
        f"[{tag}] RETURN {s['return_pct']*100:+.2f}% | final ${s['final_equity']:.2f} | "
        f"trades={s['trades']} WR={s['win_rate']*100:.1f}% MDD={s['max_drawdown_pct']*100:.1f}% "
        f"PF={s['profit_factor']:.2f} avgRR={s['avg_planned_rr']:.2f} "
        f"in {report['elapsed_sec']/60:.1f}m → {out_path}"
    )
    print(line, flush=True)
    return {
        "tag": tag,
        "ok": True,
        "report": str(out_path),
        "summary": s,
        "elapsed_sec": report["elapsed_sec"],
        "exit_reasons": report["exit_reasons"],
    }


def load_year() -> dict:
    from borex.viewerMT5.mt5_feed import (
        connect_mt5_client,
        fetch_mt5_candles,
        list_tradeable_yahoo_symbols,
    )

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=365)
    print(f"Loading MT5 H1 {start.date()} → {end.date()}", flush=True)

    client = connect_mt5_client()
    raw: dict = {}
    coverage: dict = {}
    try:
        symbols = list_tradeable_yahoo_symbols()
        for i, sym in enumerate(symbols, 1):
            bars = fetch_mt5_candles(
                sym,
                "1h",
                start,
                end,
                client=client,
                use_cache=True,
                write_cache=True,
            )
            if len(bars) >= 120:
                raw[sym] = bars
                coverage[sym] = {
                    "bars": len(bars),
                    "first": str(bars[0].timestamp),
                    "last": str(bars[-1].timestamp),
                }
            if i % 15 == 0 or i == len(symbols):
                print(f"  loaded {len(raw)}/{i}", flush=True)
    finally:
        client.disconnect()

    from borex.alexg import AlexG7AlignedStrategy

    q_pips = AlexG7AlignedStrategy().ohlc_quantize_pips
    data = {sym: prequantize(bars, sym, q_pips) for sym, bars in raw.items()}
    print(f"Prequantized {len(data)} pairs @ {q_pips:g} pips", flush=True)
    return {
        "data": data,
        "coverage": coverage,
        "capital": CAPITAL,
        "window": {
            "requested_start": start.isoformat(),
            "requested_end": end.isoformat(),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--workers",
        type=int,
        default=min(5, os.cpu_count() or 5),
        help="Process pool size (default: min(5, cpu_count))",
    )
    parser.add_argument(
        "--from-pickle",
        action="store_true",
        help="Reuse year _candles.pkl instead of reloading MT5",
    )
    parser.add_argument(
        "--tags",
        default="",
        help="Comma-separated job tags to run (default: all)",
    )
    parser.add_argument(
        "--week",
        action="store_true",
        help="Score the last 7 days only (14d warmup so setups can form)",
    )
    parser.add_argument("--eval-days", type=int, default=0, help="Override eval window days")
    parser.add_argument("--warmup-days", type=int, default=14)
    args = parser.parse_args()

    eval_days = 7 if args.week else int(args.eval_days or 0)
    out_dir = WEEK_DIR if eval_days == 7 or args.week else YEAR_DIR
    year_pickle = YEAR_DIR / "_candles.pkl"

    if args.from_pickle:
        if not year_pickle.is_file():
            raise SystemExit(f"missing pickle {year_pickle}")
        print(f"Reusing {year_pickle}", flush=True)
        with open(year_pickle, "rb") as f:
            payload = pickle.load(f)
    else:
        payload = load_year()
        YEAR_DIR.mkdir(parents=True, exist_ok=True)
        year_pickle.write_bytes(pickle.dumps(payload, protocol=pickle.HIGHEST_PROTOCOL))
        print(
            f"Wrote {year_pickle} ({year_pickle.stat().st_size / 1e6:.1f} MB)",
            flush=True,
        )

    if eval_days > 0:
        sliced, cov, eval_start, last = slice_last_eval(
            payload["data"], eval_days=eval_days, warmup_days=args.warmup_days
        )
        master_bars = max((len(v) for v in sliced.values()), default=0)
        print(
            f"Eval {eval_start.date()} → {last.date()} | warmup {args.warmup_days}d | "
            f"pairs={len(sliced)} bars~{master_bars}",
            flush=True,
        )
        payload = {
            "data": sliced,
            "coverage": cov,
            "capital": CAPITAL,
            "window": {
                **(payload.get("window") or {}),
                "eval_days": eval_days,
                "eval_start": eval_start.isoformat(),
                "eval_end": last.isoformat(),
            },
            "eval_start": eval_start.isoformat(),
            "out_dir": str(out_dir),
        }
    else:
        payload = dict(payload)
        payload["out_dir"] = str(out_dir)
        payload["capital"] = float(payload.get("capital") or CAPITAL)

    out_dir.mkdir(parents=True, exist_ok=True)
    payload_path = out_dir / "_candles.pkl"
    payload_path.write_bytes(pickle.dumps(payload, protocol=pickle.HIGHEST_PROTOCOL))

    wanted = {t.strip() for t in args.tags.split(",") if t.strip()}
    jobs = [j for j in JOBS if not wanted or j["tag"] in wanted]
    if not jobs:
        raise SystemExit(f"no jobs matched --tags {args.tags!r}")
    workers = max(1, min(args.workers, len(jobs)))
    print(f"{len(jobs)} jobs × {workers} workers → {out_dir}", flush=True)

    results = []
    with ProcessPoolExecutor(
        max_workers=workers,
        initializer=_init_worker,
        initargs=(str(payload_path),),
    ) as pool:
        futs = {pool.submit(_run_job, dict(job)): job["tag"] for job in jobs}
        for fut in as_completed(futs):
            tag = futs[fut]
            try:
                results.append(fut.result())
            except Exception as exc:
                print(f"[{tag}] FAILED: {exc}", flush=True)
                results.append({"tag": tag, "ok": False, "error": str(exc)})

    index_path = out_dir / "index.json"
    index = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "jobs": results,
    }
    index_path.write_text(json.dumps(index, indent=2), encoding="utf-8")
    label = "WEEK" if eval_days == 7 or args.week else "YEAR"
    print(f"\n======== {label} BACKTEST SUMMARY ========", flush=True)
    for row in sorted(results, key=lambda r: r.get("tag", "")):
        if not row.get("ok"):
            print(f"FAIL  {row['tag']}  {row.get('error')}", flush=True)
            continue
        s = row["summary"]
        print(
            f"{row['tag']:28s}  account=${s.get('account', s['final_equity']):,.2f}  "
            f"WR={s['win_rate']*100:5.1f}%  "
            f"trades={s['trades']:5d}  "
            f"PF={s['profit_factor']:.2f}  avgRR={s['avg_planned_rr']:.2f}  "
            f"MDD={s['max_drawdown_pct']*100:.1f}%",
            flush=True,
        )
    print(f"Index {index_path}", flush=True)
    return 0 if all(r.get("ok") for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
