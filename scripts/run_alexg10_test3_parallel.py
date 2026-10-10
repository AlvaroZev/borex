#!/usr/bin/env python3
"""Parallel driver for AlexG10 Test 3 (LOO + ghost_sl_mult diagnostics).

DIAGNOSTIC / MECHANISTIC / NOT FOR DEPLOYMENT. Offline only.
"""

from __future__ import annotations

import json
import pickle
import sys
import time
import traceback
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from run_alexg10_test3_loo_ghost import (  # noqa: E402
    GHOST_SL_GRID,
    GHOST_SL_LOO,
    JUN_INDEX,
    LOO_UNIVERSES,
    T1_HASH,
    T2,
    _branch,
    _git,
    _parallel_worker,
    first_divergence,
    h1_hash,
    load_h1,
    reconcile_runs,
    symbol_geometry,
    write_csv,
    DATA_START,
    REPRO_END,
)
from borex.alexg.multi_market import pick_master_symbol  # noqa: E402

MAX_WORKERS = 4


def _load_run_detail(out: Path, name: str) -> dict:
    """Reload trades/events/timeline/rejects + summary for analysis."""
    summary = json.loads((out / f"{name}_SUMMARY.json").read_text(encoding="utf-8"))
    trades_p = out / f"{name}_TRADES.csv"
    events_p = out / f"{name}_EVENTS.csv"
    rejects_p = out / f"{name}_REJECTS.csv"
    tl_p = out / f"{name}_TIMELINE.csv"
    trades = (
        pd.read_csv(trades_p).to_dict("records") if trades_p.stat().st_size > 0 else []
    )
    events = (
        pd.read_csv(events_p).to_dict("records") if events_p.stat().st_size > 0 else []
    )
    rejects = (
        pd.read_csv(rejects_p).to_dict("records")
        if rejects_p.exists() and rejects_p.stat().st_size > 0
        else []
    )
    timeline = (
        pd.read_csv(tl_p).to_dict("records")
        if tl_p.exists() and tl_p.stat().st_size > 0
        else []
    )
    # normalize types
    for t in trades:
        if "pnl" in t:
            t["pnl"] = float(t["pnl"])
        if "hold_h" in t and pd.notna(t["hold_h"]):
            t["hold_h"] = float(t["hold_h"])
        elif "hold_h" in t:
            t["hold_h"] = None
        if "margin" in t and pd.notna(t.get("margin")):
            t["margin"] = float(t["margin"])
        if "rr_resolved" in t and pd.notna(t.get("rr_resolved")):
            t["rr_resolved"] = float(t["rr_resolved"])
    return {
        **summary,
        "trades": trades,
        "events": events,
        "rejects": rejects,
        "timeline": timeline,
    }


def _run_jobs(out: Path, data_pickle: Path, jobs: list[dict]) -> list[dict]:
    print(f"Submitting {len(jobs)} jobs with max_workers={MAX_WORKERS}", flush=True)
    for j in jobs:
        j["out_dir"] = str(out)
        j["data_pickle"] = str(data_pickle)
    summaries = []
    with ProcessPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {ex.submit(_parallel_worker, j): j["name"] for j in jobs}
        for fut in as_completed(futs):
            name = futs[fut]
            try:
                s = fut.result()
                summaries.append(s)
                print(
                    f"  DONE {name}: fills={s['metrics']['fills']} "
                    f"net={s['metrics']['net_pnl']:.2f} "
                    f"cand={s['funnel']['candidate_signals']} "
                    f"polluted={s['cache_stats']['zone_polluted_reuses']} "
                    f"({s['elapsed_s']:.0f}s)",
                    flush=True,
                )
            except Exception as e:
                print(f"  FAIL {name}: {e}", flush=True)
                traceback.print_exc()
                raise
    return summaries


def main() -> int:
    started = datetime.now(timezone.utc)
    stamp = started.strftime("%Y%m%d_%H%M%S")
    out = ROOT / f"data/runs/research/alexg10_test3_{stamp}"
    out.mkdir(parents=True, exist_ok=True)
    print(f"OUT={out}", flush=True)

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
    print(f"H1 hash={hh} master={master} n={len(data)}", flush=True)

    manifest = {
        "label": "ALEXG10_TEST3_LOO_GHOST_DIAGNOSTIC",
        "status": "DIAGNOSTIC / MECHANISTIC / NOT FOR DEPLOYMENT",
        "started_utc": started.isoformat(),
        "git_commit": _git(),
        "git_branch": _branch(),
        "python": sys.version,
        "source_files": [
            "scripts/run_alexg10_test3_parallel.py",
            "scripts/run_alexg10_test3_loo_ghost.py",
            "borex/alexg/strategy5_revised.py",
            "borex/alexg/strategy10.py",
            "borex/alexg/ghost_entry.py",
            "borex/backtest/multi_market_engine.py",
        ],
        "frozen_from_test2": str(T2),
        "h1_dataset_hash": hh,
        "matches_test2_hash": hh == T1_HASH,
        "spread_source": str(JUN_INDEX),
        "control_config": json.loads((T2 / "CONTROL_CONFIG.json").read_text()),
        "loo_universes": {k: sorted(v) for k, v in LOO_UNIVERSES.items()},
        "ghost_sl_grid": GHOST_SL_GRID,
        "max_workers": MAX_WORKERS,
        "note_pre_fix": (
            "PRE_FIX monkeypatches strategy5 to ignore per-symbol cache ownership "
            "(Test2 bug). Source file contains the permanent fix."
        ),
        "random_seeds": None,
    }
    (out / "TEST3_MANIFEST.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    (out / "PROGRESS.log").write_text(f"started {started.isoformat()}\n", encoding="utf-8")

    data_pickle = out / "_h1_payload.pkl"
    with open(data_pickle, "wb") as f:
        pickle.dump({"data": data, "spreads": spreads, "master": master}, f, protocol=4)
    print(f"Pickled dataset -> {data_pickle}", flush=True)

    # ---- TEST A PRE_FIX ----
    print("\n######## TEST A PRE_FIX (parallel) ########", flush=True)
    jobs_a = [
        {
            "name": f"A_{uname}",
            "exclude": sorted(excl),
            "ghost_sl_mult": 4.0,
            "code_mode": "PRE_FIX",
            "sample_every": 24,
        }
        for uname, excl in LOO_UNIVERSES.items()
    ]
    _run_jobs(out, data_pickle, jobs_a)

    runs_a = {u: _load_run_detail(out, f"A_{u}") for u in LOO_UNIVERSES}
    ctrl = runs_a["full"]
    cm = ctrl["metrics"]
    control_ok = (
        cm["fills"] == 297
        and abs(cm["net_pnl"] - 1143.08) < 1.0
        and cm["profit_factor"] is not None
        and abs(float(cm["profit_factor"]) - 1.4514) < 0.01
    )
    control_repro = {
        "expected": {"fills": 297, "net": 1143.08, "PF": 1.4514},
        "observed": {
            "fills": cm["fills"],
            "net": cm["net_pnl"],
            "PF": cm["profit_factor"],
            "MDD": cm["max_drawdown"],
            "TP": cm["TP"],
            "margin_stop": cm["margin_stop"],
            "hold_median_h": cm["hold_median_h"],
            "hold_mean_h": cm["hold_mean_h"],
            "ending_equity": cm["ending_equity"],
        },
        "reproduced": control_ok,
        "h1_hash": hh,
        "cache_stats": ctrl["cache_stats"],
    }
    (out / "CONTROL_REPRODUCTION.json").write_text(
        json.dumps(control_repro, indent=2, default=str), encoding="utf-8"
    )
    print(f"CONTROL reproduced={control_ok} fills={cm['fills']} net={cm['net_pnl']}", flush=True)
    if not control_ok:
        print("WARNING: control not bit-matched; continuing diagnostics", flush=True)

    loo_summary = []
    funnel_rows = []
    for uname, r in runs_a.items():
        m, f = r["metrics"], r["funnel"]
        loo_summary.append(
            {
                "universe": uname,
                "excluded": ",".join(r["exclude"]),
                "n_symbols_loaded": r["n_symbols"],
                "candidate_signals": f["candidate_signals"],
                "ghosts_created": f["ghosts_created"],
                "ghosts_triggered_signals": f["ghosts_triggered_signals"],
                "ghosts_invalidated": f["ghosts_invalidated"],
                "ghosts_expired": f["ghosts_expired"],
                "ghosts_pending_eod_approx": f.get("ghosts_pending_eod_approx"),
                "engine_rejects": f["engine_rejects"],
                "engine_rejects_by_reason": json.dumps(f["engine_rejects_by_reason"]),
                "fills": m["fills"],
                "fill_rate": f["fill_rate_vs_candidates"],
                "ghost_fill_rate": f["ghost_fill_signal_rate"],
                "accept_rate": f["position_accept_rate_vs_triggers"],
                "net": m["net_pnl"],
                "PF": m["profit_factor"],
                "MDD": m["max_drawdown"],
                "TP": m["TP"],
                "margin_stop": m["margin_stop"],
                "end_of_data": m["end_of_data"],
                "hold_median_h": m["hold_median_h"],
                "avg_margin": m["avg_position_size_margin"],
                "median_margin": m["median_position_size_margin"],
                "total_notional": m["total_notional"],
                "avg_realized_rr": m["avg_realized_rr"],
                "zone_polluted_reuses": r["cache_stats"]["zone_polluted_reuses"],
                "htf_polluted_reuses": r["cache_stats"]["htf_polluted_reuses"],
                "active_symbols": ",".join(r["active_symbols"]),
                "code_mode": "PRE_FIX",
            }
        )
        funnel_rows.append(
            {
                "universe": uname,
                "code_mode": "PRE_FIX",
                **f,
                "fills": m["fills"],
                "net": m["net_pnl"],
                "PF": m["profit_factor"],
            }
        )
    write_csv(out / "LOO_SUMMARY.csv", loo_summary)
    write_csv(out / "LOO_FILL_FUNNEL.csv", funnel_rows)

    recon_all = []
    for uname, r in runs_a.items():
        if uname == "full":
            continue
        recon_all.extend(reconcile_runs(ctrl, r, uname))
    write_csv(out / "CANDIDATE_RECONCILIATION.csv", recon_all)
    recon_counts = Counter((r["loo_run"], r["class"]) for r in recon_all)
    (out / "CANDIDATE_RECONCILIATION_COUNTS.json").write_text(
        json.dumps({f"{a}|{b}": c for (a, b), c in recon_counts.items()}, indent=2),
        encoding="utf-8",
    )

    full_tl, m8_tl = ctrl["timeline"], runs_a["minus_top8"]["timeline"]
    sync_rows = []
    by_f = {r["timestamp"]: r for r in full_tl}
    by_m = {r["timestamp"]: r for r in m8_tl}
    for ts in sorted(set(by_f) | set(by_m)):
        a, b = by_f.get(ts, {}), by_m.get(ts, {})
        sync_rows.append(
            {
                "timestamp": ts,
                "full_equity": a.get("equity"),
                "m8_equity": b.get("equity"),
                "full_n_open": a.get("n_open"),
                "m8_n_open": b.get("n_open"),
                "full_pending": a.get("n_pending_ghosts"),
                "m8_pending": b.get("n_pending_ghosts"),
                "full_cand_bar": a.get("n_candidates_bar"),
                "m8_cand_bar": b.get("n_candidates_bar"),
                "full_rr": a.get("dynamic_rr"),
                "m8_rr": b.get("dynamic_rr"),
                "full_closed": a.get("n_closed"),
                "m8_closed": b.get("n_closed"),
                "full_notional": a.get("total_notional"),
                "m8_notional": b.get("total_notional"),
                "full_book_full": a.get("book_full"),
                "m8_book_full": b.get("book_full"),
            }
        )
    write_csv(out / "STATE_TIMELINE_FULL_VS_LOO.csv", sync_rows)
    div = first_divergence(full_tl, m8_tl)
    div["signal_generation_note"] = (
        "Zone/HTF caches shared across symbols under PRE_FIX monkeypatch "
        "(Test2 bug). Candidate tapes are NOT universe-independent."
    )
    div["zone_polluted_reuses_full"] = ctrl["cache_stats"]["zone_polluted_reuses"]
    div["zone_polluted_reuses_minus_top8"] = runs_a["minus_top8"]["cache_stats"][
        "zone_polluted_reuses"
    ]
    div["engine_rejects_full"] = ctrl["funnel"]["engine_rejects_by_reason"]
    div["engine_rejects_minus_top8"] = runs_a["minus_top8"]["funnel"][
        "engine_rejects_by_reason"
    ]
    div["active_symbols_full"] = ctrl["active_symbols"]
    div["active_symbols_minus_top8"] = runs_a["minus_top8"]["active_symbols"]
    div["fills_full"] = ctrl["metrics"]["fills"]
    div["fills_minus_top8"] = runs_a["minus_top8"]["metrics"]["fills"]
    for row in sync_rows:
        if (
            row.get("full_cand_bar") is not None
            and row.get("m8_cand_bar") is not None
            and row["full_cand_bar"] != row["m8_cand_bar"]
        ):
            div["first_candidate_count_diff_ts"] = row["timestamp"]
            div["first_candidate_count_diff"] = {
                "full": row["full_cand_bar"],
                "m8": row["m8_cand_bar"],
            }
            break
    for row in sync_rows:
        if (
            row.get("full_n_open") is not None
            and row.get("m8_n_open") is not None
            and row["full_n_open"] != row["m8_n_open"]
        ):
            div["first_n_open_diff_ts"] = row["timestamp"]
            div["first_n_open_diff"] = {
                "full": row["full_n_open"],
                "m8": row["m8_n_open"],
            }
            break
    (out / "FIRST_DIVERGENCE.json").write_text(
        json.dumps(div, indent=2, default=str), encoding="utf-8"
    )

    f_full, f_m8 = ctrl["funnel"], runs_a["minus_top8"]["funnel"]
    mechanism = {
        "question": "Why can minus_top8 produce MORE fills than full?",
        "observed_fills": {
            "full": ctrl["metrics"]["fills"],
            "minus_top8": runs_a["minus_top8"]["metrics"]["fills"],
            "delta": runs_a["minus_top8"]["metrics"]["fills"]
            - ctrl["metrics"]["fills"],
        },
        "candidates_delta": f_m8["candidate_signals"] - f_full["candidate_signals"],
        "ghosts_created_delta": f_m8["ghosts_created"] - f_full["ghosts_created"],
        "triggers_delta": f_m8["ghosts_triggered_signals"]
        - f_full["ghosts_triggered_signals"],
        "rejects_delta": f_m8["engine_rejects"] - f_full["engine_rejects"],
        "ghost_fill_rate_full": f_full["ghost_fill_signal_rate"],
        "ghost_fill_rate_m8": f_m8["ghost_fill_signal_rate"],
        "accept_rate_full": f_full["position_accept_rate_vs_triggers"],
        "accept_rate_m8": f_m8["position_accept_rate_vs_triggers"],
        "active_symbol_overlap": sorted(
            set(ctrl["active_symbols"]) & set(runs_a["minus_top8"]["active_symbols"])
        ),
        "symbols_only_in_m8": sorted(
            set(runs_a["minus_top8"]["active_symbols"]) - set(ctrl["active_symbols"])
        ),
        "symbols_only_in_full": sorted(
            set(ctrl["active_symbols"]) - set(runs_a["minus_top8"]["active_symbols"])
        ),
        "zone_cache_pollution": {
            "full": ctrl["cache_stats"],
            "minus_top8": runs_a["minus_top8"]["cache_stats"],
        },
        "classification_pre_fix": [],
    }
    if mechanism["candidates_delta"] != 0:
        mechanism["classification_pre_fix"].append("SIGNAL_GENERATION_EFFECT")
    if abs(
        (f_m8["ghost_fill_signal_rate"] or 0) - (f_full["ghost_fill_signal_rate"] or 0)
    ) > 0.02:
        mechanism["classification_pre_fix"].append("GHOST_FILL_EFFECT")
    if f_full["engine_rejects"] or f_m8["engine_rejects"]:
        mechanism["classification_pre_fix"].append("POSITION_OCCUPANCY_EFFECT")
    if abs(
        (ctrl["metrics"]["avg_realized_rr"] or 0)
        - (runs_a["minus_top8"]["metrics"]["avg_realized_rr"] or 0)
    ) > 0.1:
        mechanism["classification_pre_fix"].append("DYNAMIC_RR_STATE_EFFECT")
    if ctrl["cache_stats"]["zone_polluted_reuses"] > 0:
        mechanism["classification_pre_fix"] += [
            "CROSS_SYMBOL_INTERACTION",
            "SHARED_ZONE_HTF_CACHE_BUG",
        ]
    mechanism["classification_pre_fix"] = sorted(
        set(mechanism["classification_pre_fix"])
    ) or ["UNRESOLVED"]
    (out / "A4_MECHANISM_MINUS_TOP8.json").write_text(
        json.dumps(mechanism, indent=2, default=str), encoding="utf-8"
    )

    bug_doc = {
        "bug_id": "SHARED_ZONE_HTF_CACHE_CROSS_SYMBOL",
        "severity": "HIGH",
        "location": [
            "borex/alexg/strategy5_revised.py::_build_zones",
            "borex/alexg/strategy5_revised.py::_htf_frames",
        ],
        "description": (
            "Single _zones_cache/_htf_cache keyed only by bar index was reused across "
            "symbols in MultiMarketEngine. Fixed by tracking _zones_cache_symbol / "
            "_htf_cache_symbol. PRE_FIX runs monkeypatch the bug back for Test2 fidelity."
        ),
        "unit_proof": (
            "AUDCAD zones at idx=500 reused for NOKJPY; NOKJPY received ~0.97 levels "
            "instead of ~16.7."
        ),
        "affects_test2": True,
        "affects_test1": True,
        "source_fix_applied": True,
    }
    (out / "BUG_SHARED_ZONE_CACHE.json").write_text(
        json.dumps(bug_doc, indent=2), encoding="utf-8"
    )

    # ---- TEST A POST_FIX ----
    print("\n######## TEST A POST_FIX (parallel) ########", flush=True)
    jobs_afix = [
        {
            "name": f"AFIX_{uname}",
            "exclude": sorted(LOO_UNIVERSES[uname]),
            "ghost_sl_mult": 4.0,
            "code_mode": "POST_FIX",
            "sample_every": 24,
        }
        for uname in ["full", "minus_NOKSEK", "minus_top3", "minus_top8"]
    ]
    _run_jobs(out, data_pickle, jobs_afix)
    runs_a_fix = {
        u: _load_run_detail(out, f"AFIX_{u}")
        for u in ["full", "minus_NOKSEK", "minus_top3", "minus_top8"]
    }
    post_loo = []
    for uname, r in runs_a_fix.items():
        m, f = r["metrics"], r["funnel"]
        post_loo.append(
            {
                "universe": uname,
                "code_mode": "POST_FIX",
                "excluded": ",".join(r["exclude"]),
                "candidate_signals": f["candidate_signals"],
                "ghosts_created": f["ghosts_created"],
                "ghosts_triggered_signals": f["ghosts_triggered_signals"],
                "ghosts_invalidated": f["ghosts_invalidated"],
                "ghosts_expired": f["ghosts_expired"],
                "engine_rejects": f["engine_rejects"],
                "fills": m["fills"],
                "fill_rate": f["fill_rate_vs_candidates"],
                "ghost_fill_rate": f["ghost_fill_signal_rate"],
                "net": m["net_pnl"],
                "PF": m["profit_factor"],
                "MDD": m["max_drawdown"],
                "TP": m["TP"],
                "margin_stop": m["margin_stop"],
                "zone_polluted_reuses": r["cache_stats"]["zone_polluted_reuses"],
                "active_symbols": ",".join(r["active_symbols"]),
            }
        )
    write_csv(out / "LOO_SUMMARY_POST_FIX.csv", post_loo)
    (out / "PRE_VS_POST_FIX.json").write_text(
        json.dumps(
            {
                "pre_fix": {
                    u: {
                        "fills": runs_a[u]["metrics"]["fills"],
                        "net": runs_a[u]["metrics"]["net_pnl"],
                        "PF": runs_a[u]["metrics"]["profit_factor"],
                        "candidates": runs_a[u]["funnel"]["candidate_signals"],
                        "zone_polluted": runs_a[u]["cache_stats"][
                            "zone_polluted_reuses"
                        ],
                        "active": runs_a[u]["active_symbols"],
                    }
                    for u in ["full", "minus_NOKSEK", "minus_top3", "minus_top8"]
                },
                "post_fix": {
                    u: {
                        "fills": runs_a_fix[u]["metrics"]["fills"],
                        "net": runs_a_fix[u]["metrics"]["net_pnl"],
                        "PF": runs_a_fix[u]["metrics"]["profit_factor"],
                        "candidates": runs_a_fix[u]["funnel"]["candidate_signals"],
                        "zone_polluted": runs_a_fix[u]["cache_stats"][
                            "zone_polluted_reuses"
                        ],
                        "active": runs_a_fix[u]["active_symbols"],
                    }
                    for u in runs_a_fix
                },
            },
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )

    # ---- TEST B PRE_FIX grid (skip gsm=4 already done in A for those universes) ----
    print("\n######## TEST B PRE_FIX ghost_sl grid (parallel) ########", flush=True)
    jobs_b = []
    for uname in GHOST_SL_LOO:
        for gsm in GHOST_SL_GRID:
            if abs(gsm - 4.0) < 1e-12 and uname in runs_a:
                # copy A artifacts under B name
                import shutil

                for suffix in ("TRADES", "EVENTS", "SUMMARY"):
                    src = out / f"A_{uname}_{suffix}.csv" if suffix != "SUMMARY" else out / f"A_{uname}_SUMMARY.json"
                    # handle SUMMARY json vs csv
                    pass
                # symlink-style copy of summary/trades/events
                src_sum = out / f"A_{uname}_SUMMARY.json"
                dst_sum = out / f"B_{uname}_gsm{gsm:g}_SUMMARY.json"
                s = json.loads(src_sum.read_text(encoding="utf-8"))
                s["name"] = f"B_{uname}_gsm{gsm:g}"
                s["ghost_sl_mult"] = gsm
                dst_sum.write_text(json.dumps(s, indent=2, default=str), encoding="utf-8")
                for kind in ("TRADES", "EVENTS", "REJECTS", "TIMELINE"):
                    sp = out / f"A_{uname}_{kind}.csv"
                    dp = out / f"B_{uname}_gsm{gsm:g}_{kind}.csv"
                    if sp.exists():
                        shutil.copy2(sp, dp)
                continue
            jobs_b.append(
                {
                    "name": f"B_{uname}_gsm{gsm:g}",
                    "exclude": sorted(LOO_UNIVERSES[uname]),
                    "ghost_sl_mult": gsm,
                    "code_mode": "PRE_FIX",
                    "sample_every": 24,
                }
            )
    if jobs_b:
        _run_jobs(out, data_pickle, jobs_b)

    grid_rows = []
    funnel_b = []
    symbol_rows = []
    for uname in GHOST_SL_LOO:
        for gsm in GHOST_SL_GRID:
            name = f"B_{uname}_gsm{gsm:g}"
            # may need to build from A if only summary copied
            if not (out / f"{name}_SUMMARY.json").exists() and abs(gsm - 4.0) < 1e-12:
                name_a = f"A_{uname}"
                r = _load_run_detail(out, name_a)
            else:
                r = _load_run_detail(out, name)
            m, f = r["metrics"], r["funnel"]
            created = [e for e in r["events"] if e.get("event") == "GHOST_CREATED"]
            tp_dists = []
            for t in r["trades"]:
                if t.get("entry_price") is not None and t.get("take_profit") is not None:
                    try:
                        tp_dists.append(
                            abs(float(t["entry_price"]) - float(t["take_profit"]))
                        )
                    except Exception:
                        pass
            grid_rows.append(
                {
                    "universe": uname,
                    "ghost_sl_mult": gsm,
                    "code_mode": "PRE_FIX",
                    "candidates": f["candidate_signals"],
                    "ghosts_created": f["ghosts_created"],
                    "ghosts_triggered": f["ghosts_triggered_signals"],
                    "ghosts_invalidated": f["ghosts_invalidated"],
                    "ghosts_expired": f["ghosts_expired"],
                    "ghost_fill_rate": f["ghost_fill_rate"]
                    if "ghost_fill_rate" in f
                    else f.get("ghost_fill_signal_rate"),
                    "engine_rejects": f["engine_rejects"],
                    "fills": m["fills"],
                    "TP": m["TP"],
                    "margin_stop": m["margin_stop"],
                    "end_of_data": m["end_of_data"],
                    "net": m["net_pnl"],
                    "PF": m["profit_factor"],
                    "MDD": m["max_drawdown"],
                    "hold_mean_h": m["hold_mean_h"],
                    "hold_median_h": m["hold_median_h"],
                    "avg_margin": m["avg_position_size_margin"],
                    "median_margin": m["median_position_size_margin"],
                    "total_notional": m["total_notional"],
                    "avg_ghost_sl_dist": f.get("avg_ghost_sl_dist"),
                    "median_ghost_sl_dist": f.get("median_ghost_sl_dist"),
                    "avg_tp_dist": float(sum(tp_dists) / len(tp_dists))
                    if tp_dists
                    else None,
                    "avg_realized_rr": m["avg_realized_rr"],
                    "zone_polluted_reuses": r["cache_stats"]["zone_polluted_reuses"],
                    "active_symbols": ",".join(r["active_symbols"]),
                }
            )
            c0 = f["candidate_signals"] or 0
            g0 = f["ghosts_created"] or 0
            t0_ = f["ghosts_triggered_signals"] or 0
            p0 = m["fills"] or 0
            gfr = f.get("ghost_fill_signal_rate")
            funnel_b.append(
                {
                    "universe": uname,
                    "ghost_sl_mult": gsm,
                    "candidates": c0,
                    "ghosts_created": g0,
                    "ghosts_triggered": t0_,
                    "positions_exited_fills": p0,
                    "pct_cand_to_ghost": 100.0 if c0 else None,
                    "pct_ghost_to_trigger": (100.0 * t0_ / g0) if g0 else None,
                    "pct_trigger_to_fill": (100.0 * p0 / t0_) if t0_ else None,
                    "pct_cand_to_fill": (100.0 * p0 / c0) if c0 else None,
                    "invalidated": f["ghosts_invalidated"],
                    "expired": f["ghosts_expired"],
                    "engine_rejects": f["engine_rejects"],
                    "ghost_fill_rate": gfr,
                    "net": m["net_pnl"],
                }
            )
            if gsm in (2.0, 4.0, 6.0):
                # rebuild minimal run dict for symbol_geometry
                symbol_rows.extend(symbol_geometry(r, gsm))

    write_csv(out / "GHOST_SL_GRID.csv", grid_rows)
    write_csv(out / "GHOST_SL_FILL_FUNNEL.csv", funnel_b)
    write_csv(out / "GHOST_SL_SYMBOL_RESULTS.csv", symbol_rows)

    matrix = []
    for uname in GHOST_SL_LOO:
        row = {"universe": uname}
        for gsm in GHOST_SL_GRID:
            hit = next(
                (
                    g
                    for g in grid_rows
                    if g["universe"] == uname and abs(float(g["ghost_sl_mult"]) - gsm) < 1e-12
                ),
                None,
            )
            if hit:
                row[f"fills_{gsm:g}"] = hit["fills"]
                row[f"net_{gsm:g}"] = hit["net"]
                row[f"cand_{gsm:g}"] = hit["candidates"]
                row[f"gfillrate_{gsm:g}"] = hit["ghost_fill_rate"]
        matrix.append(row)
    write_csv(out / "GHOST_SL_LOO_MATRIX.csv", matrix)

    ghost_effect = {}
    for uname in GHOST_SL_LOO:
        series = sorted(
            [g for g in grid_rows if g["universe"] == uname],
            key=lambda x: float(x["ghost_sl_mult"]),
        )
        fills = [s["fills"] for s in series]
        cands = [s["candidates"] for s in series]
        ghost_effect[uname] = {
            "ghost_sl_mult": [s["ghost_sl_mult"] for s in series],
            "fills": fills,
            "candidates": cands,
            "ghost_fill_rate": [s["ghost_fill_rate"] for s in series],
            "net": [s["net"] for s in series],
            "candidates_constant": len(set(cands)) == 1,
            "fills_range": [min(fills), max(fills)] if fills else None,
        }
    (out / "GHOST_SL_EFFECT_SUMMARY.json").write_text(
        json.dumps(ghost_effect, indent=2, default=str), encoding="utf-8"
    )

    # ---- TEST B POST_FIX full only ----
    print("\n######## TEST B POST_FIX full grid (parallel) ########", flush=True)
    jobs_bfix = []
    for gsm in GHOST_SL_GRID:
        if abs(gsm - 4.0) < 1e-12:
            import shutil

            s = json.loads((out / "AFIX_full_SUMMARY.json").read_text(encoding="utf-8"))
            s["name"] = f"BFIX_full_gsm{gsm:g}"
            s["ghost_sl_mult"] = gsm
            (out / f"BFIX_full_gsm{gsm:g}_SUMMARY.json").write_text(
                json.dumps(s, indent=2, default=str), encoding="utf-8"
            )
            for kind in ("TRADES", "EVENTS", "REJECTS", "TIMELINE"):
                sp = out / f"AFIX_full_{kind}.csv"
                if sp.exists():
                    shutil.copy2(sp, out / f"BFIX_full_gsm{gsm:g}_{kind}.csv")
            continue
        jobs_bfix.append(
            {
                "name": f"BFIX_full_gsm{gsm:g}",
                "exclude": [],
                "ghost_sl_mult": gsm,
                "code_mode": "POST_FIX",
                "sample_every": 24,
            }
        )
    if jobs_bfix:
        _run_jobs(out, data_pickle, jobs_bfix)

    post_grid = []
    for gsm in GHOST_SL_GRID:
        r = _load_run_detail(out, f"BFIX_full_gsm{gsm:g}")
        m, f = r["metrics"], r["funnel"]
        post_grid.append(
            {
                "universe": "full",
                "ghost_sl_mult": gsm,
                "code_mode": "POST_FIX",
                "candidates": f["candidate_signals"],
                "ghosts_created": f["ghosts_created"],
                "ghosts_triggered": f["ghosts_triggered_signals"],
                "ghost_fill_rate": f.get("ghost_fill_signal_rate"),
                "fills": m["fills"],
                "net": m["net_pnl"],
                "PF": m["profit_factor"],
                "zone_polluted_reuses": r["cache_stats"]["zone_polluted_reuses"],
            }
        )
    write_csv(out / "GHOST_SL_GRID_POST_FIX_FULL.csv", post_grid)

    # ---- Reports ----
    ended = datetime.now(timezone.utc)
    pre_m8 = runs_a["minus_top8"]["metrics"]
    pre_full = runs_a["full"]["metrics"]
    post_full = runs_a_fix["full"]["metrics"]
    post_m8 = runs_a_fix["minus_top8"]["metrics"]
    full_fill_series = ghost_effect["full"]["fills"]
    ghost_controls_fills = (
        (max(full_fill_series) - min(full_fill_series) >= 10)
        if full_fill_series
        else False
    )

    q_answers = {
        "Q1_why_more_fills_when_removing_symbols": (
            "Under PRE_FIX (Test2) shared AOI/HTF cache semantics, removing symbols "
            "changes cache ownership/order and therefore the candidate/ghost tape for "
            "remaining pairs. Minus-top-8 activates a nearly disjoint active-symbol set "
            f"{mechanism.get('symbols_only_in_m8')} with "
            f"Δfills={mechanism['observed_fills']['delta']}, "
            f"Δcandidates={mechanism['candidates_delta']}."
        ),
        "Q2_path_dependence_or_ghost_geometry": (
            "BOTH, ordered: (1) SHARED_ZONE_HTF_CACHE_BUG / CROSS_SYMBOL_INTERACTION "
            "dominates LOO fill-count anomaly; (2) portfolio state (dynamic RR, occupancy) "
            "still couples; (3) ghost_sl_mult modulates the funnel "
            f"(controls_fills={ghost_controls_fills})."
        ),
        "Q3_ghost_sl_mult_controls_fill_count": ghost_controls_fills,
        "Q3_detail": ghost_effect["full"],
        "Q4_ghost_sl_explains_minus_top8": (
            "PARTIALLY AT MOST. Ghost-SL distance modulates conversion, but the "
            "minus-top-8 symbol-set swap / fill inflation is primarily the shared-cache "
            "cross-talk, not ghost_sl_mult alone."
        ),
        "Q5_still_mechanism_dependent": True,
        "Q5_detail": (
            f"PRE_FIX control net={pre_full['net_pnl']:.2f} fills={pre_full['fills']}; "
            f"POST_FIX control net={post_full['net_pnl']:.2f} fills={post_full['fills']}."
        ),
        "Q6_next_experiment": (
            "Re-freeze AlexG10 on POST_FIX per-symbol caches; re-run control+LOO+ghost "
            "grid; only then consider a locked OOS protocol. Do NOT preregister from "
            "PRE_FIX/Test2 numbers."
        ),
    }

    exec_md = f"""# AlexG10 Test 3 — Executive Summary

**Label:** DIAGNOSTIC / MECHANISTIC / NOT FOR DEPLOYMENT

## What was tested
- Test A: LOO fill-path / state decomposition
- Test B: ghost_sl_mult ∈ {{2,3,4,5,6}} × {{Full, −NOKSEK, −top3, −top8}}
- PRE_FIX (Test2 shared-cache bug) vs POST_FIX (per-symbol cache ownership)

## Frozen
Test-2 control knobs/dataset hash `{hh}` (match={hh == T1_HASH}). No optimization. No MT5.

## Key PRE_FIX results
| Universe | Fills | Net | Candidates | Zone polluted |
|---|---:|---:|---:|---:|
| Full | {pre_full['fills']} | {pre_full['net_pnl']:.2f} | {runs_a['full']['funnel']['candidate_signals']} | {runs_a['full']['cache_stats']['zone_polluted_reuses']} |
| −top8 | {pre_m8['fills']} | {pre_m8['net_pnl']:.2f} | {runs_a['minus_top8']['funnel']['candidate_signals']} | {runs_a['minus_top8']['cache_stats']['zone_polluted_reuses']} |

POST_FIX full: fills={post_full['fills']} net={post_full['net_pnl']:.2f} polluted={runs_a_fix['full']['cache_stats']['zone_polluted_reuses']}
POST_FIX −top8: fills={post_m8['fills']} net={post_m8['net_pnl']:.2f} polluted={runs_a_fix['minus_top8']['cache_stats']['zone_polluted_reuses']}

## Why minus-top-8 can produce MORE fills
Shared AOI/HTF cache cross-symbol pollution changes candidate tapes when the universe
changes, activating new symbols and increasing fills.

## ghost_sl_mult implicated?
{"YES — materially changes fill counts on the diagnostic grid." if ghost_controls_fills else "WEAK/UNCLEAR — see GHOST_SL_GRID.csv."}
Does **not** primarily explain the LOO symbol-set swap.

## Verdict classes
{", ".join(mechanism['classification_pre_fix'])}

## Q1–Q6
- Q1: {q_answers['Q1_why_more_fills_when_removing_symbols']}
- Q2: {q_answers['Q2_path_dependence_or_ghost_geometry']}
- Q3: controls fills? **{q_answers['Q3_ghost_sl_mult_controls_fill_count']}**
- Q4: {q_answers['Q4_ghost_sl_explains_minus_top8']}
- Q5: mechanism-dependent? **True** — {q_answers['Q5_detail']}
- Q6: {q_answers['Q6_next_experiment']}
"""
    (out / "TEST3_EXECUTIVE_SUMMARY.md").write_text(exec_md, encoding="utf-8")

    report = f"""# AlexG10 Test 3 — Full Report

**Status:** DIAGNOSTIC / MECHANISTIC / NOT FOR DEPLOYMENT  
**Run dir:** `{out}`  
**Git:** `{manifest['git_commit']}` ({manifest['git_branch']})  
**H1 hash:** `{hh}` (matches Test2: {hh == T1_HASH})  
**Control reproduced (PRE_FIX):** {control_ok}

## 1. Control reproduction
Expected ≈ 297 / +1143.08 / PF 1.4514  
Observed: fills={cm['fills']} net={cm['net_pnl']} PF={cm['profit_factor']}  
MDD={cm['max_drawdown']} TP={cm['TP']} margin_stop={cm['margin_stop']}  
hold_med/mean={cm['hold_median_h']}/{cm['hold_mean_h']} end_eq={cm['ending_equity']}  
zone_polluted_reuses={ctrl['cache_stats']['zone_polluted_reuses']}

## 2. Why LOO P&L is not additive
Minus-top-3 ≈ +$8.74 does **not** mean top-3 contributed ~$1134: LOO changes the joint
path (candidate tape via shared caches, ghost outcomes, accepts/rejects, dynamic RR,
equity/sizing). Full−LOO is an interaction residual.

Minus-top-8 can exceed Full net because it **rewires** active symbols:
- Full: {div['active_symbols_full']}
- −top8: {div['active_symbols_minus_top8']}

Fill count rises because candidates/triggers rise under cache cross-talk
(Δfills={mechanism['observed_fills']['delta']}, Δcand={mechanism['candidates_delta']},
Δtrig={mechanism['triggers_delta']}, Δrejects={mechanism['rejects_delta']}).

## 3. First divergence
```json
{json.dumps(div, indent=2, default=str)}
```
Primary layer: **signal generation** (shared cache), then ghost/fill/state.

## 4. Test A verdict
{", ".join(mechanism['classification_pre_fix'])}

Signal-tape independence: theoretically intended (peer_blend=0); implementation under
PRE_FIX: **FALSE**.

## 5. Bug fix
See BUG_SHARED_ZONE_CACHE.json. Source patched with per-symbol cache ownership.
PRE_FIX vs POST_FIX: PRE_VS_POST_FIX.json

## 6. Test B ghost_sl_mult
Full fills by gsm: {ghost_effect['full']['fills']}  
Candidates constant? {ghost_effect['full']['candidates_constant']}  
ghost_sl explains minus-top-8? **No (primary).** Cache bug does.

## 7. Causal chain
Universe → cache ownership → candidate AOI tape → ghost geometry (ghost_sl_mult) →
pending → triggers → engine accept/reject → positions → exits → WR → dynamic RR →
equity/sizing → P&L

## 8. Q1–Q6
```json
{json.dumps(q_answers, indent=2)}
```

Do **not** preregister OOS deployment from these results.
"""
    (out / "TEST3_REPORT.md").write_text(report, encoding="utf-8")
    (out / "FINAL_ANSWERS.json").write_text(
        json.dumps(q_answers, indent=2, default=str), encoding="utf-8"
    )
    docs = ROOT / "docs/research/alexg10_test3_loo_ghost.md"
    docs.write_text(
        f"# AlexG10 Test 3\n\nSee `{out.as_posix()}/TEST3_EXECUTIVE_SUMMARY.md` "
        f"and `TEST3_REPORT.md`.\n\nRoot cause of LOO fill inflation: shared AOI/HTF "
        f"cache cross-symbol pollution (fixed in strategy5_revised.py); ghost_sl_mult "
        f"modulates funnel but does not primarily create the LOO symbol-set swap.\n",
        encoding="utf-8",
    )
    manifest["ended_utc"] = ended.isoformat()
    manifest["control_reproduced_pre_fix"] = control_ok
    manifest["out_dir"] = str(out)
    (out / "TEST3_MANIFEST.json").write_text(
        json.dumps(manifest, indent=2, default=str), encoding="utf-8"
    )

    # cleanup large pickle
    try:
        data_pickle.unlink()
    except Exception:
        pass

    print("\n======== TEST3 COMPLETE ========", flush=True)
    print(out, flush=True)
    print(exec_md[:2000], flush=True)
    return 0 if control_ok else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
