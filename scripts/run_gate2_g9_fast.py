#!/usr/bin/env python3
"""Fast alexg9liq evaluation: precompute snaps once per symbol → decision tape → N1 replay."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import scripts.run_gate2_exhaustive_alexg_liquidity as lab
from borex.alexg import AlexG8Strategy
from borex.alexg.liquidity.features import build_snapshots, detect_sweep_reclaim_at_bar
from borex.alexg.multi_market import pick_master_symbol
from borex.alexg.sessions import in_session
from borex.alexg.strategy9_liquidity import AlexG9LiquidityStrategy, G9Family
from borex.gate2.exhaustive.search import ResultsDB, config_hash
from borex.gate2.weekly_research import summarize_trades, weekly_metrics_table
from borex.models.candle import SignalAction

OUT = ROOT / "data/runs/research/gate2_exhaustive_alexg_liquidity_20260926_045232"


def generate_tape(compound: dict, strat: AlexG9LiquidityStrategy) -> list[dict]:
    """Causal signal tape without MultiMarketEngine scan."""
    tape: list[dict] = []
    for sym, bars in compound["data"].items():
        snaps = build_snapshots(
            bars,
            swing_left=strat.swing_left,
            swing_right=strat.swing_right,
            equal_tol_atr=strat.equal_tol_atr,
            min_equal_touches=strat.min_equal_touches,
            max_level_age=strat.max_level_age,
            dealing_lookback=strat.dealing_lookback,
        )
        last_i = -10**9
        for i in range(strat.min_bars, len(bars)):
            if i - last_i < strat.signal_cooldown:
                continue
            c = bars[i]
            if lab._ts(c.timestamp) < compound["entry_start"]:
                continue
            if not in_session(c.timestamp, strat.session_filter):
                continue
            snap = snaps[i]
            if snap is None or snap.atr <= 0:
                continue
            # reuse family logic
            strat._current_symbol = sym
            action = strat._family_signal(bars, i, snap)
            if action is None:
                continue
            if strat.sides == "long_only" and action != SignalAction.BUY:
                continue
            if strat.sides == "short_only" and action != SignalAction.SELL:
                continue
            sl_dist = strat.sl_atr_mult * snap.atr
            entry = float(c.close)
            if action == SignalAction.BUY:
                sl, tp = entry - sl_dist, entry + sl_dist * strat.tp_rr
            else:
                sl, tp = entry + sl_dist, entry - sl_dist * strat.tp_rr
            tu = int(lab._ts(c.timestamp).timestamp())
            tape.append(
                {
                    "symbol": sym,
                    "time_unix": tu,
                    "action": "buy" if action == SignalAction.BUY else "sell",
                    "index": i,
                    "stop_loss": sl,
                    "take_profit": tp,
                    "pattern": f"alexg9liq|{strat.family}",
                }
            )
            last_i = i
    tape.sort(key=lambda r: (r["time_unix"], r["symbol"]))
    return tape


def main() -> int:
    jun_idx = json.loads(lab.JUN_INDEX.read_text(encoding="utf-8"))
    symbols = sorted(jun_idx["spread_pips_by_symbol"].keys())
    spreads = dict(jun_idx["spread_pips_by_symbol"])
    print("loading periods…", flush=True)
    periods = {
        "june": lab.load_period("june", lab.JUN_INDEX, symbols, spreads),
        "july": lab.load_period("july", lab.JUL_INDEX, symbols, spreads),
        "august": lab.load_period("august", lab.AUG_INDEX, symbols, spreads),
    }
    # Use per-month periods for replay (already loaded) — build compound only for snaps on each period
    db = ResultsDB(OUT / "results.sqlite")
    rows = []
    for fam in lab.g9_families():
        for thu in (False, True):
            h = config_hash(
                {"strategy": "alexg9liq_fast", "family": fam, "thursday_gate": thu, "v": 3}
            )
            if db.has(h):
                print("skip", fam, thu, flush=True)
                continue
            print(f"FAST g9liq {fam} thu={thu}", flush=True)
            t1 = time.perf_counter()
            strat = AlexG9LiquidityStrategy(family=fam, ghost_sl_mult=4.0)  # type: ignore[arg-type]
            all_tr = []
            try:
                for pname in ("june", "july", "august"):
                    tape = generate_tape(periods[pname], strat)
                    if thu:
                        from borex.gate2.thursday_entry_gate import filter_block_thursday_entries

                        tape = filter_block_thursday_entries(tape)
                    # remap indices
                    tape = lab.remap_tape(tape, periods[pname]["data"])
                    s, tr = lab.run_replay(periods[pname], tape, dict(lab.N1_BASE))
                    for t in tr:
                        t = dict(t)
                        t["month"] = pname
                        all_tr.append(t)
                    print(f"  {pname}: fills={s.get('fills')} net={s.get('net_pnl'):.1f}", flush=True)
                full = summarize_trades(all_tr)
                wr = weekly_metrics_table(all_tr, candidate_id=h)
                db.put(
                    {
                        "config_hash": h,
                        "strategy": "alexg9liq",
                        "parent_strategy": "alexg9liq",
                        "params": {"family": fam, "thursday_gate": thu, "fast": True},
                        "period": "jun_aug",
                        "fills": full.get("fills"),
                        "pf": full.get("PF"),
                        "net": full.get("net_pnl"),
                        "mean_trade": full.get("mean_trade"),
                        "median_trade": full.get("median_trade"),
                        "mdd": None,
                        "wr": full.get("WR"),
                        "tp": full.get("TP"),
                        "daily_close": full.get("daily_close"),
                        "margin_stop": full.get("margin_stop"),
                        "long_count": full.get("long_count"),
                        "short_count": full.get("short_count"),
                        "runtime_s": time.perf_counter() - t1,
                        "stage": "g9_family_fast",
                        "category": "DISCOVERY_CANDIDATE",
                    },
                    weekly=wr,
                )
                rows.append(
                    {
                        "family": fam,
                        "thursday_gate": thu,
                        "fills": full.get("fills"),
                        "PF": full.get("PF"),
                        "net": full.get("net_pnl"),
                        "mean": full.get("mean_trade"),
                    }
                )
                print(f"  TOTAL net={full.get('net_pnl')} PF={full.get('PF')} fills={full.get('fills')}", flush=True)
            except Exception as e:
                rows.append({"family": fam, "thursday_gate": thu, "status": "error", "error": str(e)})
                print("  ERROR", e, flush=True)
    lab.write_csv(OUT / "alexg9_results.csv", rows)
    top = sorted([r for r in rows if r.get("net") is not None], key=lambda x: float(x["net"]), reverse=True)
    (OUT / "alexg9_results_note.md").write_text(
        f"Fast G9 families evaluated={len(rows)}\nTop: {top[:5]}\nDISCOVERY_ONLY.\n",
        encoding="utf-8",
    )
    # Update conclusions
    n_eval = db.count()
    (OUT / "research_conclusions.md").write_text(
        "\n".join(
            [
                "# Research Conclusions (DISCOVERY_ONLY)",
                "",
                f"Authoritative run: `{OUT.name}`",
                "",
                f"- Evaluations in DB: **{n_eval}**",
                f"- alexg9liq fast families: **{len(rows)}**",
                f"- Top G9: `{top[0] if top else None}`",
                "",
                "September unread. LIVE/Mirror untouched.",
                "Locked Sep OOS unchanged: n1_control vs n1_thursday.",
                "All new findings are DISCOVERY_ONLY — not validated.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    db.close()
    print("FAST G9 DONE", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
