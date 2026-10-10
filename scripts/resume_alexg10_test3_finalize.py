#!/usr/bin/env python3
"""Resume Test3: finish missing BFIX_full gsm jobs + write final reports.

DIAGNOSTIC / MECHANISTIC / NOT FOR DEPLOYMENT.
"""

from __future__ import annotations

import gc
import json
import pickle
import shutil
import sys
import traceback
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from run_alexg10_test3_loo_ghost import (  # noqa: E402
    GHOST_SL_GRID,
    GHOST_SL_LOO,
    LOO_UNIVERSES,
    T1_HASH,
    _branch,
    _git,
    first_divergence,
    reconcile_runs,
    run_once,
    symbol_geometry,
    write_csv,
)

OUT = ROOT / "data/runs/research/alexg10_test3_20261004_141920"
LOG = ROOT / "data/runs/research/alexg10_test3_resume.log"


def log(msg: str) -> None:
    line = f"{datetime.now(timezone.utc).isoformat()} {msg}"
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_summary(name: str) -> dict:
    return json.loads((OUT / f"{name}_SUMMARY.json").read_text(encoding="utf-8"))


def load_detail(name: str) -> dict:
    import pandas as pd

    s = load_summary(name)
    trades_p = OUT / f"{name}_TRADES.csv"
    events_p = OUT / f"{name}_EVENTS.csv"
    rejects_p = OUT / f"{name}_REJECTS.csv"
    tl_p = OUT / f"{name}_TIMELINE.csv"
    trades = (
        pd.read_csv(trades_p).to_dict("records")
        if trades_p.exists() and trades_p.stat().st_size > 0
        else []
    )
    # events can be huge; only load if small enough for reconciliation needs
    events = []
    if events_p.exists() and events_p.stat().st_size < 2_000_000:
        events = pd.read_csv(events_p).to_dict("records")
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
    for t in trades:
        for k in ("pnl", "hold_h", "margin", "rr_resolved"):
            if k in t and t[k] == t[k]:  # not NaN
                try:
                    t[k] = float(t[k])
                except Exception:
                    pass
    return {**s, "trades": trades, "events": events, "rejects": rejects, "timeline": timeline}


def run_bfix_light(gsm: float, data, spreads, master) -> dict:
    """Run POST_FIX full gsm; save trades+summary only (skip huge events dump)."""
    name = f"BFIX_full_gsm{gsm:g}"
    if (OUT / f"{name}_SUMMARY.json").exists():
        log(f"skip existing {name}")
        return load_summary(name)
    log(f"start {name}")
    r = run_once(
        name=name,
        data=data,
        spreads=spreads,
        master=master,
        exclude=set(),
        ghost_sl_mult=float(gsm),
        sample_every=48,
        code_mode="POST_FIX",
        progress_path=OUT / "PROGRESS.log",
    )
    write_csv(OUT / f"{name}_TRADES.csv", r["trades"])
    # keep a tiny event sample only
    sample_events = r["events"][:50] + r["events"][-50:] if len(r["events"]) > 100 else r["events"]
    write_csv(OUT / f"{name}_EVENTS_SAMPLE.csv", sample_events)
    write_csv(OUT / f"{name}_TIMELINE.csv", r["timeline"])
    summary = {
        "name": r["name"],
        "code_mode": r["code_mode"],
        "ghost_sl_mult": r["ghost_sl_mult"],
        "exclude": r["exclude"],
        "n_symbols": r["n_symbols"],
        "metrics": r["metrics"],
        "funnel": r["funnel"],
        "cache_stats": r["cache_stats"],
        "elapsed_s": r["elapsed_s"],
        "final_equity_engine": r["final_equity_engine"],
        "active_symbols": sorted({t["symbol"] for t in r["trades"]}),
        "n_events": len(r["events"]),
        "n_rejects": len(r["rejects"]),
        "n_timeline": len(r["timeline"]),
        "note": "EVENTS full dump skipped to avoid OOM; sample saved",
    }
    (OUT / f"{name}_SUMMARY.json").write_text(
        json.dumps(summary, indent=2, default=str), encoding="utf-8"
    )
    log(
        f"done {name}: fills={summary['metrics']['fills']} "
        f"net={summary['metrics']['net_pnl']:.2f} "
        f"cand={summary['funnel']['candidate_signals']} "
        f"polluted={summary['cache_stats']['zone_polluted_reuses']} "
        f"({summary['elapsed_s']:.0f}s)"
    )
    return summary


def main() -> int:
    LOG.write_text(f"resume start {datetime.now(timezone.utc).isoformat()}\n", encoding="utf-8")
    assert OUT.is_dir(), OUT
    pkl = OUT / "_h1_payload.pkl"
    assert pkl.is_file(), "missing pickle; cannot resume without reloading H1"
    with open(pkl, "rb") as f:
        payload = pickle.load(f)
    data, spreads, master = payload["data"], payload["spreads"], payload["master"]

    # Finish missing BFIX jobs sequentially (GC between jobs — prior death was
    # hard-kill with no traceback after gsm2/3/5, likely RAM pressure from
    # retained POST_FIX in-memory event ledgers).
    for gsm in GHOST_SL_GRID:
        if abs(gsm - 4.0) < 1e-12:
            continue  # already have gsm4 from AFIX copy
        summary = run_bfix_light(gsm, data, spreads, master)
        del summary
        gc.collect()
        log(f"gc after gsm={gsm:g}")

    # Ensure gsm4 summary exists under BFIX name
    if not (OUT / "BFIX_full_gsm4_SUMMARY.json").exists():
        s = load_summary("AFIX_full")
        s["name"] = "BFIX_full_gsm4"
        s["ghost_sl_mult"] = 4.0
        (OUT / "BFIX_full_gsm4_SUMMARY.json").write_text(
            json.dumps(s, indent=2, default=str), encoding="utf-8"
        )

    # ---- Rebuild POST_FIX ghost grid CSV ----
    post_grid = []
    for gsm in GHOST_SL_GRID:
        s = load_summary(f"BFIX_full_gsm{gsm:g}")
        m, f = s["metrics"], s["funnel"]
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
                "zone_polluted_reuses": s["cache_stats"]["zone_polluted_reuses"],
            }
        )
    write_csv(OUT / "GHOST_SL_GRID_POST_FIX_FULL.csv", post_grid)

    # ---- Load PRE_FIX core ----
    runs_a = {u: load_detail(f"A_{u}") for u in LOO_UNIVERSES}
    runs_a_fix = {
        u: load_detail(f"AFIX_{u}")
        for u in ["full", "minus_NOKSEK", "minus_top3", "minus_top8"]
    }
    ctrl = runs_a["full"]
    cm = ctrl["metrics"]
    control_ok = (
        cm["fills"] == 297
        and abs(cm["net_pnl"] - 1143.08) < 1.0
        and abs(float(cm["profit_factor"]) - 1.4514) < 0.01
    )

    # ghost effect from existing GHOST_SL_GRID.csv if present else rebuild
    import pandas as pd

    grid_df = pd.read_csv(OUT / "GHOST_SL_GRID.csv")
    ghost_effect = {}
    for uname in GHOST_SL_LOO:
        series = grid_df[grid_df["universe"] == uname].sort_values("ghost_sl_mult")
        fills = series["fills"].tolist()
        cands = series["candidates"].tolist()
        ghost_effect[uname] = {
            "ghost_sl_mult": series["ghost_sl_mult"].tolist(),
            "fills": fills,
            "candidates": cands,
            "ghost_fill_rate": series["ghost_fill_rate"].tolist()
            if "ghost_fill_rate" in series
            else [],
            "net": series["net"].tolist(),
            "candidates_constant": len(set(cands)) == 1,
            "fills_range": [min(fills), max(fills)] if fills else None,
            "fills_all_equal": len(set(fills)) == 1,
        }
    (OUT / "GHOST_SL_EFFECT_SUMMARY.json").write_text(
        json.dumps(ghost_effect, indent=2, default=str), encoding="utf-8"
    )

    # Mechanism / answers
    mechanism = json.loads((OUT / "A4_MECHANISM_MINUS_TOP8.json").read_text(encoding="utf-8"))
    pre_full = runs_a["full"]["metrics"]
    pre_m8 = runs_a["minus_top8"]["metrics"]
    post_full = runs_a_fix["full"]["metrics"]
    post_m8 = runs_a_fix["minus_top8"]["metrics"]
    full_fill_series = ghost_effect["full"]["fills"]
    ghost_controls_fills = (
        (max(full_fill_series) - min(full_fill_series) >= 10)
        if full_fill_series
        else False
    )
    # POST_FIX ghost control
    post_fills = [r["fills"] for r in post_grid]
    ghost_controls_fills_postfix = max(post_fills) - min(post_fills) >= 10

    hh = json.loads((OUT / "TEST3_MANIFEST.json").read_text(encoding="utf-8")).get(
        "h1_dataset_hash", T1_HASH
    )
    div = json.loads((OUT / "FIRST_DIVERGENCE.json").read_text(encoding="utf-8"))

    q_answers = {
        "interpretation_regime": {
            "PRE_FIX": "INVALIDATED_FOR_CAUSAL_INFERENCE — SHARED_ZONE_HTF_CACHE_BUG",
            "POST_FIX": "VALID Test3 regime (per-symbol AOI/HTF cache ownership)",
            "preserve_pre_fix_artifacts": True,
            "note": (
                "PRE_FIX LOO (e.g. 297→371 fills) must NOT be treated as evidence of "
                "genuine concentration/path dependence. Audit trail only."
            ),
        },
        "Q1_why_more_fills_when_removing_symbols": (
            "AUDIT/INVALIDATED (PRE_FIX): shared AOI/HTF cache pollution made candidate "
            "generation universe-dependent. Minus-top-8 activated a nearly disjoint active "
            f"set {mechanism.get('symbols_only_in_m8')} "
            f"(Δfills={mechanism['observed_fills']['delta']}, "
            f"Δcandidates={mechanism['candidates_delta']}). "
            "This is an implementation artifact, not a clean strategy-state effect. "
            "Causal LOO questions belong under POST_FIX only."
        ),
        "Q2_path_dependence_or_ghost_geometry": (
            "PRE_FIX: dominated by SHARED_ZONE_HTF_CACHE_BUG (invalid for causal LOO). "
            "POST_FIX: valid regime — characterize full vs LOO vs ghost_sl_mult 2–6 here. "
            f"PRE_FIX ghost fills_all_equal={ghost_effect['full'].get('fills_all_equal')}; "
            f"POST_FIX full gsm fills={post_fills}."
        ),
        "Q3_ghost_sl_mult_controls_fill_count": bool(ghost_controls_fills_postfix),
        "Q3_note": (
            "Interpret ghost_sl_mult ONLY within POST_FIX. PRE_FIX grid is audit-only."
        ),
        "Q3_detail_pre_fix_AUDIT_ONLY": ghost_effect["full"],
        "Q3_detail_post_fix_full": post_grid,
        "Q4_ghost_sl_explains_minus_top8": (
            "NO for the PRE_FIX LOO anomaly (cache bug). Re-ask under POST_FIX LOO only."
        ),
        "Q5_still_mechanism_dependent": True,
        "Q5_detail": (
            f"PRE_FIX (invalidated) control net={pre_full['net_pnl']:.2f} "
            f"fills={pre_full['fills']}; "
            f"POST_FIX (valid) control net={post_full['net_pnl']:.2f} "
            f"fills={post_full['fills']}. "
            "POST_FIX ~8× fill flood vs Test2/PRE_FIX is a major regime change and must "
            "be characterized before treating as AlexG10 improvement."
        ),
        "Q6_next_experiment": (
            "Complete POST_FIX ghost_sl 2–6 + LOO characterization; reconcile AFIX_full "
            "(~2389 fills / ~$5k) vs Test2 (297 / +$1143) before any Live promotion. "
            "Do NOT preregister from PRE_FIX/Test2 numbers."
        ),
        "resume_note": (
            "Parallel run OOM'd on huge POST_FIX EVENTS dumps. Sequential light resume "
            "finished remaining BFIX jobs (GC between gsm). Live/Mirror not touched for this."
        ),
    }

    ended = datetime.now(timezone.utc)
    exec_md = f"""# AlexG10 Test 3 — Executive Summary

**Label:** DIAGNOSTIC / MECHANISTIC / NOT FOR DEPLOYMENT

## Interpretation (read first)
| Regime | Status |
|---|---|
| **PRE_FIX** | **INVALIDATED for causal inference** — `SHARED_ZONE_HTF_CACHE_BUG` |
| **POST_FIX** | **Valid Test 3 regime** — per-symbol AOI/HTF cache ownership |
| PRE_FIX artifacts | **Preserved for audit trail only** (do not delete; do not cite as strategy evidence) |

Primary scientific comparisons: **POST_FIX full vs POST_FIX LOO vs POST_FIX ghost_sl_mult 2–6**.
Do **not** treat PRE_FIX `297 → 371` (or similar LOO fill inflation) as genuine AlexG10
concentration/path dependence.

`AFIX_full` (~{post_full['fills']} fills / ~${post_full['net_pnl']:.0f} net) is a major
regime change vs Test2 PRE_FIX (~297 / +$1143) and must be characterized before any
Live promotion. **Live/Mirror not updated from this offline result.**

## What was tested
- Test A: LOO fill-path / state (PRE_FIX = contaminated Test2 shared-cache semantics)
- Test B: ghost_sl_mult ∈ {{2,3,4,5,6}} × LOO universes (PRE_FIX = audit; POST_FIX = valid)
- POST_FIX: per-symbol cache fix + full ghost grid

## Frozen
Test-2 knobs/dataset hash `{hh}` (match Test2: {hh == T1_HASH}). No optimization. No MT5.

## Control reproduction (PRE_FIX — audit only)
- Expected ≈ 297 / +1143.08 / PF 1.4514
- Observed: fills={cm['fills']} net={cm['net_pnl']:.2f} PF={cm['profit_factor']}
- **reproduced={control_ok}** (reproduction of contaminated baseline, not validity)
- zone_polluted_reuses={ctrl['cache_stats']['zone_polluted_reuses']}

## Key PRE_FIX LOO (INVALIDATED)
| Universe | Fills | Net | Candidates | Zone polluted |
|---|---:|---:|---:|---:|
| Full | {pre_full['fills']} | {pre_full['net_pnl']:.2f} | {runs_a['full']['funnel']['candidate_signals']} | {runs_a['full']['cache_stats']['zone_polluted_reuses']} |
| −top8 | {pre_m8['fills']} | {pre_m8['net_pnl']:.2f} | {runs_a['minus_top8']['funnel']['candidate_signals']} | {runs_a['minus_top8']['cache_stats']['zone_polluted_reuses']} |

Active symbols Full: {runs_a['full']['active_symbols']}
Active symbols −top8: {runs_a['minus_top8']['active_symbols']}

## POST_FIX (valid)
| Universe | Fills | Net | Zone polluted |
|---|---:|---:|---:|
| Full | {post_full['fills']} | {post_full['net_pnl']:.2f} | {runs_a_fix['full']['cache_stats']['zone_polluted_reuses']} |
| −top8 | {post_m8['fills']} | {post_m8['net_pnl']:.2f} | {runs_a_fix['minus_top8']['cache_stats']['zone_polluted_reuses']} |

## Why PRE_FIX minus-top-8 showed MORE fills
Shared AOI/HTF cache pollution (implementation bug). Not usable as strategy evidence.

## Is ghost_sl_mult implicated?
- **PRE_FIX grid (audit):** fills largely unchanged across gsm 2→6.
- **POST_FIX full grid (valid):** fills={post_fills} (material: {ghost_controls_fills_postfix}).

## Verdict classes (PRE_FIX mechanism audit)
{", ".join(mechanism.get('classification_pre_fix', []))}

## Q1–Q6
- **Q1:** {q_answers['Q1_why_more_fills_when_removing_symbols']}
- **Q2:** {q_answers['Q2_path_dependence_or_ghost_geometry']}
- **Q3:** POST_FIX controls fills? **{q_answers['Q3_ghost_sl_mult_controls_fill_count']}** ({q_answers['Q3_note']})
- **Q4:** {q_answers['Q4_ghost_sl_explains_minus_top8']}
- **Q5:** mechanism-dependent? **True** — {q_answers['Q5_detail']}
- **Q6:** {q_answers['Q6_next_experiment']}
"""
    (OUT / "TEST3_EXECUTIVE_SUMMARY.md").write_text(exec_md, encoding="utf-8")

    report = f"""# AlexG10 Test 3 — Full Report

**Status:** DIAGNOSTIC / MECHANISTIC / NOT FOR DEPLOYMENT  
**Run dir:** `{OUT}`  
**Git:** `{_git()}` ({_branch()})  
**H1 hash:** `{hh}`  
**Control reproduced (PRE_FIX):** {control_ok}  
**Ended (resume):** {ended.isoformat()}

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
- Full: {div.get('active_symbols_full')}
- −top8: {div.get('active_symbols_minus_top8')}

Fill count rises because candidates/triggers rise under cache cross-talk
(Δfills={mechanism['observed_fills']['delta']}, Δcand={mechanism['candidates_delta']},
Δtrig={mechanism['triggers_delta']}, Δrejects={mechanism['rejects_delta']}).

## 3. First divergence
See `FIRST_DIVERGENCE.json`. Primary layer: **signal generation** (shared cache).

## 4. Test A verdict
{", ".join(mechanism.get('classification_pre_fix', []))}

Signal-tape independence: theoretically intended (peer_blend=0); PRE_FIX implementation: **FALSE**.

## 5. Bug fix
`BUG_SHARED_ZONE_CACHE.json`. Source patched with `_zones_cache_symbol` / `_htf_cache_symbol`.
PRE_FIX monkeypatches the bug back for Test2 fidelity. See `PRE_VS_POST_FIX.json`.

## 6. Test B ghost_sl_mult
PRE_FIX Full fills by gsm: {ghost_effect['full']['fills']}  
Candidates constant? {ghost_effect['full']['candidates_constant']}  
All fills equal? {ghost_effect['full'].get('fills_all_equal')}  

POST_FIX Full fills by gsm: {post_fills}

ghost_sl explains minus-top-8? **No (primary).** Cache bug does.

## 7. Causal chain
Universe → cache ownership → candidate AOI tape → ghost geometry (ghost_sl_mult) →
pending → triggers → engine accept/reject → positions → exits → WR → dynamic RR →
equity/sizing → P&L

## 8. Q1–Q6
```json
{json.dumps(q_answers, indent=2, default=str)}
```

Do **not** preregister OOS deployment from these results.
"""
    (OUT / "TEST3_REPORT.md").write_text(report, encoding="utf-8")
    (OUT / "FINAL_ANSWERS.json").write_text(
        json.dumps(q_answers, indent=2, default=str), encoding="utf-8"
    )

    docs = ROOT / "docs/research/alexg10_test3_loo_ghost.md"
    docs.write_text(
        f"# AlexG10 Test 3\n\nSee `{OUT.as_posix()}/TEST3_EXECUTIVE_SUMMARY.md` "
        f"and `TEST3_REPORT.md`.\n\nRoot cause of LOO fill inflation: shared AOI/HTF "
        f"cache cross-symbol pollution (fixed in strategy5_revised.py). Under PRE_FIX, "
        f"ghost_sl_mult did not change fill counts on the diagnostic grid; the LOO "
        f"anomaly is cache-driven, not ghost-distance-driven.\n",
        encoding="utf-8",
    )

    manifest = json.loads((OUT / "TEST3_MANIFEST.json").read_text(encoding="utf-8"))
    manifest["ended_utc"] = ended.isoformat()
    manifest["control_reproduced_pre_fix"] = control_ok
    manifest["resumed_utc"] = ended.isoformat()
    manifest["resume_note"] = q_answers["resume_note"]
    (OUT / "TEST3_MANIFEST.json").write_text(
        json.dumps(manifest, indent=2, default=str), encoding="utf-8"
    )

    log("TEST3 COMPLETE (resumed)")
    print(exec_md[:2500], flush=True)
    return 0 if control_ok else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
