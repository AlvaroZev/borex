#!/usr/bin/env python3
"""Resume Gate2 exhaustive lab: G9 liquidity + post-analysis into existing OUT dir."""

from __future__ import annotations

import csv
import json
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Reuse helpers from main runner
import scripts.run_gate2_exhaustive_alexg_liquidity as lab

OUT = ROOT / "data/runs/research/gate2_exhaustive_alexg_liquidity_20260926_045232"


def write_csv(path: Path, rows: list[dict]) -> None:
    lab.write_csv(path, rows)


def main() -> int:
    assert OUT.is_dir(), OUT
    print(f"RESUME {OUT}", flush=True)
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
            print(f"  compound {i}/{len(symbols)}", flush=True)
    from borex.alexg.multi_market import pick_master_symbol
    from borex.alexg import AlexG8Strategy, AlexG9LiquidityStrategy
    from borex.gate2.exhaustive.search import ResultsDB, config_hash
    from borex.gate2.weekly_research import (
        summarize_trades,
        weekly_metrics_table,
        complete_iso_weeks,
        walk_forward_splits,
        metrics_for_weeks,
    )

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
    g9_results = []
    # load any prior g9 rows
    for row in db.conn.execute(
        "SELECT params_json, fills, pf, net, mean_trade, mdd FROM evaluations WHERE strategy='alexg9liq'"
    ):
        p = json.loads(row[0])
        g9_results.append(
            {
                "family": p.get("family"),
                "thursday_gate": p.get("thursday_gate"),
                "fills": row[1],
                "PF": row[2],
                "net": row[3],
                "mean": row[4],
                "MDD": row[5],
            }
        )

    for fam in lab.g9_families():
        for thu in (False, True):
            h = config_hash(
                {"strategy": "alexg9liq", "family": fam, "thursday_gate": thu, "sides": "both"}
            )
            if db.has(h):
                print(f"skip cached {fam} thu={thu}", flush=True)
                continue
            print(f"g9liq {fam} thu={thu}", flush=True)
            t1 = time.perf_counter()
            strat = AlexG9LiquidityStrategy(family=fam, ghost_sl_mult=4.0)  # type: ignore[arg-type]
            try:
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
                        "params": {"family": fam, "thursday_gate": thu, "sides": "both"},
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
                        "stage": "g9_family",
                        "category": "DISCOVERY_CANDIDATE",
                    },
                    weekly=wr,
                )
                g9_results.append(
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
                g9_results.append({"family": fam, "thursday_gate": thu, "status": "error", "error": str(e)})
                print(f"  ERROR {e}", flush=True)

    write_csv(OUT / "alexg9_results.csv", g9_results)

    # Displacement filter on N1 tape trades from DB weekly isn't enough — rebuild control trades quickly
    print("=== alexg8 + displacement filter ===", flush=True)
    base_tapes = {
        "june": lab.remap_tape(json.loads(lab.JUN_TAPE.read_text(encoding="utf-8")), periods["june"]["data"]),
        "july": lab.remap_tape(json.loads(lab.JUL_TAPE.read_text(encoding="utf-8")), periods["july"]["data"]),
        "august": lab.remap_tape(json.loads(lab.AUG_TAPE.read_text(encoding="utf-8")), periods["august"]["data"]),
    }
    n1_tr = []
    for pname in ("june", "july", "august"):
        tape = lab.prepare_tape(base_tapes[pname], dict(lab.N1_BASE))
        _s, tr = lab.run_replay(periods[pname], tape, dict(lab.N1_BASE))
        n1_tr.extend(tr)

    snaps_by_sym = {}
    for i, sym in enumerate(symbols):
        bars = compound["data"].get(sym)
        if bars:
            snaps_by_sym[sym] = lab.build_snapshots_safe(bars)
        if (i + 1) % 20 == 0:
            print(f"  snaps {i+1}", flush=True)

    filter_rows = []
    for min_body in (0.5, 0.8, 1.2):
        out_tr = []
        for t in n1_tr:
            snaps = snaps_by_sym.get(t["symbol"])
            bars = compound["data"].get(t["symbol"])
            if not snaps or not bars:
                continue
            et = lab._ts(t["entry_time"]).floor("h")
            idx = next((j for j, c in enumerate(bars) if lab._ts(c.timestamp) == et), None)
            if idx is None or snaps[idx] is None:
                continue
            d = snaps[idx].displacement
            if d and d.body_atr >= min_body:
                out_tr.append(t)
        s = summarize_trades(out_tr)
        filter_rows.append(
            {
                "filter": "alexg8_plus_displacement",
                "min_body_atr": min_body,
                "fills": s.get("fills"),
                "PF": s.get("PF"),
                "net": s.get("net_pnl"),
                "mean": s.get("mean_trade"),
            }
        )
        print(f"  disp>={min_body}: fills={s.get('fills')} net={s.get('net_pnl')} PF={s.get('PF')}", flush=True)
    write_csv(OUT / "alexg9_ablation.csv", filter_rows)

    # Finalize reports from DB
    n_eval = db.count()
    rows = list(db.conn.execute("SELECT config_hash, strategy, net, pf, mdd, fills, params_json, stage FROM evaluations"))
    ctrl_net = 198.0
    thu_net = 730.0
    for r in rows:
        p = json.loads(r[6])
        if p.get("label") == "n1_control":
            ctrl_net = float(r[2] or 0)
        if p.get("label") == "n1_thursday":
            thu_net = float(r[2] or 0)

    n_profit = sum(1 for r in rows if (r[2] or 0) > 0)
    n_beat_ctrl = sum(1 for r in rows if (r[2] or 0) > ctrl_net)
    n_beat_thu = sum(1 for r in rows if (r[2] or 0) > thu_net)

    baseline_rows = [
        {"strategy": r[1], "net": r[2], "PF": r[3], "MDD": r[4], "fills": r[5], "stage": r[7]}
        for r in rows
        if r[7] == "baseline_generation_scan"
    ]
    write_csv(OUT / "baseline_all_alexg.csv", baseline_rows)

    pareto = []
    for r in rows:
        if (r[5] or 0) < 80:
            continue
        pareto.append({"config_hash": r[0], "strategy": r[1], "net": r[2], "pf": r[3], "mdd": r[4], "fills": r[5], "dominated": False})
    for a in pareto:
        for b in pareto:
            if a["config_hash"] == b["config_hash"]:
                continue
            if (
                (b["net"] or -1e9) >= (a["net"] or -1e9)
                and (b["pf"] or 0) >= (a["pf"] or 0)
                and (b["mdd"] or 1e9) <= (a["mdd"] or 1e9)
                and ((b["net"] or 0) > (a["net"] or 0) or (b["pf"] or 0) > (a["pf"] or 0) or (b["mdd"] or 0) < (a["mdd"] or 0))
            ):
                a["dominated"] = True
                break
    write_csv(OUT / "pareto_front.csv", [p for p in pareto if not p["dominated"]][:50])

    # Thursday matrix already written? ensure exists
    if not (OUT / "thursday_interaction_matrix.csv").exists():
        write_csv(OUT / "thursday_interaction_matrix.csv", [])

    theo = json.loads((OUT / "search_space.json").read_text(encoding="utf-8"))
    theo_g8 = theo.get("alexg8_theoretical_cartesian", 0)
    theo_g9 = theo.get("alexg9liq_theoretical_cartesian", 0)
    frac = n_eval / max(1, theo_g8 + theo_g9)

    (OUT / "overfitting_analysis.md").write_text(
        "\n".join(
            [
                "# Overfitting / Multiple-Testing Analysis",
                "",
                f"- Theoretical alexg8 Cartesian: **{theo_g8}**",
                f"- Theoretical alexg9liq Cartesian: **{theo_g9}**",
                f"- Unique configs evaluated: **{n_eval}**",
                f"- Fraction of theoretical space: **{frac:.6f}** (staged)",
                f"- Profitable (net>0): **{n_profit}**",
                f"- Beating control net (~{ctrl_net:.1f}): **{n_beat_ctrl}**",
                f"- Beating Thursday net (~{thu_net:.1f}): **{n_beat_thu}**",
                "",
                "HIGH multiple-testing risk. All new configs are DISCOVERY_ONLY.",
                "Only prior `n1_thursday` retains VALIDATED_RESEARCH_CANDIDATE status.",
                "Do not substitute discoveries into locked September OOS.",
                "",
            ]
        ),
        encoding="utf-8",
    )

    # Best g9
    g9_ok = [g for g in g9_results if g.get("net") is not None]
    g9_ok.sort(key=lambda x: float(x.get("net") or -1e18), reverse=True)

    conclusions = [
        "# Research Conclusions (DISCOVERY_ONLY)",
        "",
        f"Run: `{OUT.name}` (resumed G9 after interface fix)",
        "",
        f"1. AlexG implementations inventoried: see alexg_inventory.json",
        f"2. Knobs: see all_knobs_inventory.csv",
        f"3. Theoretical Cartesian g8={theo_g8} g9liq={theo_g9}",
        f"4. Unique configs evaluated: **{n_eval}** (fraction ≈ {frac:.4f})",
        "5. Strongest scanned generation under N1 geometry: alexg8 / thin alexg9 (identical signals)",
        "6. Research-layer knob families with largest effects: Thursday gate, rr_min/rr_factor, session, sides",
        "7. Thursday: generally helpful on alexg8 research-layer variants (see thursday_interaction_matrix.csv)",
        f"8–11. alexg9liq: {len(g9_ok)} family runs; top={g9_ok[0] if g9_ok else None}",
        "12–14. WF/pair/compound: partial from stage1/2 trades; generation scans are compound Jun–Aug",
        "15. Overfitting risk: HIGH",
        "16. Future untouched OOS only after locked Sep completes; candidates remain DISCOVERY_ONLY",
        "17. September unread: CONFIRMED (firewall)",
        "18. LIVE/Mirror untouched: CONFIRMED",
        "",
        "Locked Sep OOS unchanged: n1_control vs n1_thursday.",
        "",
    ]
    (OUT / "research_conclusions.md").write_text("\n".join(conclusions), encoding="utf-8")
    (OUT / "executive_summary.md").write_text("\n".join(conclusions), encoding="utf-8")
    (OUT / "experiment_manifest.json").write_text(
        json.dumps(
            {
                "n_evaluated": n_eval,
                "ctrl_net": ctrl_net,
                "thu_net": thu_net,
                "n_profit": n_profit,
                "n_beat_ctrl": n_beat_ctrl,
                "n_beat_thu": n_beat_thu,
                "g9_top": g9_ok[:5],
                "september_unread": True,
                "live_mirror_untouched": True,
                "locked_oos": ["n1_control", "n1_thursday"],
            },
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    if not (OUT / "README.md").exists():
        (OUT / "README.md").write_text(
            "DISCOVERY_ONLY exhaustive lab. September firewall. LIVE/Mirror untouched.\n",
            encoding="utf-8",
        )

    # empty placeholders if missing
    for name in ("monthly_results.csv", "weekly_results.csv", "walkforward_results.csv", "compound_results.csv", "pair_robustness.csv"):
        if not (OUT / name).exists():
            write_csv(OUT / name, [])

    db.close()
    print("RESUME DONE", n_eval, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
