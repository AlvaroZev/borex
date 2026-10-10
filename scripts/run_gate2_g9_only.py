#!/usr/bin/env python3
"""Run alexg9liq families only (fixed on_bar signature) into existing exhaustive OUT."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import scripts.run_gate2_exhaustive_alexg_liquidity as lab
from borex.alexg import AlexG8Strategy, AlexG9LiquidityStrategy
from borex.alexg.multi_market import pick_master_symbol
from borex.gate2.exhaustive.search import ResultsDB, config_hash
from borex.gate2.weekly_research import summarize_trades, weekly_metrics_table

OUT = ROOT / "data/runs/research/gate2_exhaustive_alexg_liquidity_20260926_045232"


def main() -> int:
    jun_idx = json.loads(lab.JUN_INDEX.read_text(encoding="utf-8"))
    symbols = sorted(jun_idx["spread_pips_by_symbol"].keys())
    spreads = dict(jun_idx["spread_pips_by_symbol"])
    periods = {
        "june": lab.load_period("june", lab.JUN_INDEX, symbols, spreads),
        "july": lab.load_period("july", lab.JUL_INDEX, symbols, spreads),
        "august": lab.load_period("august", lab.AUG_INDEX, symbols, spreads),
    }
    entry_start = periods["june"]["entry_start"]
    end = periods["august"]["end"]
    h1_start = (end - pd.Timedelta(days=lab.WARMUP_DAYS)).to_pydatetime()
    m1_start = (entry_start - pd.Timedelta(days=2)).to_pydatetime()
    data, m1 = {}, {}
    for i, sym in enumerate(symbols, 1):
        bars = lab.load_cache_candles(sym, "1h", h1_start, end)
        if len(bars) < 120:
            continue
        data[sym] = bars
        minutes = lab.load_cache_candles(sym, "1m", m1_start, end)
        if minutes:
            m1[sym] = minutes
        if i % 20 == 0:
            print(f"load {i}", flush=True)
    master = pick_master_symbol(data)
    entry_i = next(i for i, c in enumerate(data[master]) if lab._ts(c.timestamp) >= entry_start)
    compound = {
        "name": "jun_aug",
        "entry_start": entry_start,
        "end": end,
        "data": data,
        "m1": m1,
        "spreads": spreads,
        "master": master,
        "prime_i": max(int(AlexG8Strategy().min_bars), entry_i - lab.GHOST_PRIME),
    }
    db = ResultsDB(OUT / "results.sqlite")
    rows = []
    for fam in lab.g9_families():
        for thu in (False, True):
            h = config_hash(
                {
                    "strategy": "alexg9liq",
                    "family": fam,
                    "thursday_gate": thu,
                    "sides": "both",
                    "v": 2,
                }
            )
            if db.has(h):
                print("skip", fam, thu, flush=True)
                continue
            print(f"g9liq {fam} thu={thu}", flush=True)
            t1 = time.perf_counter()
            try:
                strat = AlexG9LiquidityStrategy(family=fam, ghost_sl_mult=4.0)  # type: ignore[arg-type]
                s, tr = lab.run_strategy_scan(compound, strat, dict(lab.N1_BASE))
                if thu:
                    tr = [t for t in tr if lab._ts(t["entry_time"]).dayofweek != 3]
                    s = summarize_trades(tr, mdd=s.get("MDD"))
                wr = weekly_metrics_table(tr, candidate_id=h)
                db.put(
                    {
                        "config_hash": h,
                        "strategy": "alexg9liq",
                        "parent_strategy": "alexg9liq",
                        "params": {"family": fam, "thursday_gate": thu, "sides": "both", "v": 2},
                        "period": "jun_aug_scan",
                        "fills": s.get("fills"),
                        "pf": s.get("PF"),
                        "net": s.get("net_pnl"),
                        "mean_trade": s.get("mean_trade"),
                        "median_trade": s.get("median_trade"),
                        "mdd": s.get("MDD"),
                        "wr": s.get("WR"),
                        "tp": s.get("TP"),
                        "daily_close": s.get("daily_close"),
                        "margin_stop": s.get("margin_stop"),
                        "long_count": s.get("long_count"),
                        "short_count": s.get("short_count"),
                        "runtime_s": time.perf_counter() - t1,
                        "stage": "g9_family_v2",
                        "category": "DISCOVERY_CANDIDATE",
                    },
                    weekly=wr,
                )
                rows.append(
                    {
                        "family": fam,
                        "thursday_gate": thu,
                        "fills": s.get("fills"),
                        "PF": s.get("PF"),
                        "net": s.get("net_pnl"),
                        "mean": s.get("mean_trade"),
                        "MDD": s.get("MDD"),
                    }
                )
                print(f"  net={s.get('net_pnl')} PF={s.get('PF')} fills={s.get('fills')}", flush=True)
            except Exception as e:
                rows.append({"family": fam, "thursday_gate": thu, "status": "error", "error": str(e)})
                print("  ERROR", e, flush=True)
    lab.write_csv(OUT / "alexg9_results.csv", rows)
    # refresh conclusions snippet
    top = sorted([r for r in rows if r.get("net") is not None], key=lambda x: x["net"], reverse=True)
    (OUT / "alexg9_results_note.md").write_text(
        f"G9 v2 runs={len(rows)} top={top[:3]}\nAll DISCOVERY_ONLY.\n",
        encoding="utf-8",
    )
    db.close()
    print("G9 DONE", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
