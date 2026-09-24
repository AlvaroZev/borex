"""Gate 2 fidelity reconciliation — READ-ONLY.

Compares broker-observed controlled probes to frozen plans, and summarizes
existing natural Theory↔broker mappings. Does NOT submit orders, modify
strategy/simulator, or force-match.

Usage:
  python scripts/run_gate2_fidelity_reconciliation.py
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT)]

from borex.gate2.fidelity_map import (  # noqa: E402
    MAP_AMBIGUOUS,
    MAP_THEORY_NA,
    MAP_THEORY_ONLY,
    MAP_UNMAPPED,
    controlled_trade_row,
    natural_row_from_registry_match,
    summarize_populations,
)

OUT_ROOT = ROOT / "data" / "runs" / "gate2_fidelity_reconciliation"
G2P = ROOT / "data" / "runs" / "gate2_controlled_probe" / "batches" / "g2p_20260922_162842"
G2E = ROOT / "data" / "runs" / "gate2_broker_exit_probe" / "runs" / "g2e_20260923_170458"
NAT_REG = ROOT / "data" / "runs" / "gate2_natural_sample" / "registry.json"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso() -> str:
    return _utc_now().isoformat()


def _git() -> str:
    try:
        return (
            subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(ROOT), stderr=subprocess.DEVNULL)
            .decode()
            .strip()
        )
    except Exception:
        return "unknown"


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_controlled_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    g2p = _load(G2P / "live_result.json")
    for p in g2p.get("results") or []:
        rows.append(controlled_trade_row(p, sample="CONTROLLED", source_batch=str(g2p.get("batch_id"))))
    g2e = _load(G2E / "live_result.json")
    for p in g2e.get("results") or []:
        rows.append(controlled_trade_row(p, sample="CONTROLLED", source_batch=str(g2e.get("run_id"))))
    return rows


def load_natural_rows(reg: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    maps = reg.get("mappings") or {}
    clean = list(maps.get("clean_natural") or [])
    rows = [natural_row_from_registry_match(m) for m in clean]
    # Explicit unmapped / theory-only / ambiguous as separate ledger rows
    extra: list[dict[str, Any]] = []
    for u in maps.get("unmapped_broker") or []:
        extra.append(
            {
                "sample": "NATURAL",
                "source_batch": u.get("source_window") or u.get("population"),
                "broker_position": u.get("entry_ticket") or u.get("broker_id") or u.get("id"),
                "symbol": u.get("symbol"),
                "direction": u.get("direction"),
                "broker_entry_utc": u.get("broker_entry_timestamp_utc"),
                "theory_event_utc": None,
                "mapping_status": MAP_UNMAPPED,
                "theory_mapping_status": MAP_UNMAPPED,
                "discrepancy_classes": ["MAPPING_AMBIGUITY", "MISSING_DATA"],
                "confidence": "LOW",
                "note": "Unmapped broker natural RT (no unique Theory candidate).",
                "population": u.get("population"),
            }
        )
    for a in maps.get("ambiguous") or []:
        extra.append(
            {
                "sample": "NATURAL",
                "broker_position": a.get("broker_id"),
                "symbol": a.get("symbol"),
                "mapping_status": MAP_AMBIGUOUS,
                "theory_mapping_status": MAP_AMBIGUOUS,
                "discrepancy_classes": ["MAPPING_AMBIGUITY"],
                "confidence": "LOW",
                "note": a.get("reason"),
                "candidates": a.get("candidates"),
            }
        )
    for t in maps.get("unmapped_theory") or []:
        extra.append(
            {
                "sample": "NATURAL",
                "broker_position": None,
                "symbol": t.get("symbol"),
                "theory_event_utc": t.get("signal_intent_timestamp"),
                "mapping_status": MAP_THEORY_ONLY,
                "theory_mapping_status": MAP_THEORY_ONLY,
                "discrepancy_classes": ["MAPPING_AMBIGUITY", "MISSING_DATA"],
                "confidence": "LOW",
                "note": "Theory-only (no broker RT mapped).",
                "theory_id": t.get("theory_id"),
            }
        )
    census = {
        "clean_natural": len(clean),
        "ambiguous": len(maps.get("ambiguous") or []),
        "unmapped_broker": len(maps.get("unmapped_broker") or []),
        "unmapped_theory": len(maps.get("unmapped_theory") or []),
        "excluded_catch_up": len(maps.get("excluded_catch_up") or []),
        "excluded_pre_window": len(maps.get("excluded_pre_window") or []),
        "still_open": len(maps.get("still_open") or []),
        "broker_round_trips_total": len(reg.get("broker_round_trips") or []),
        "theory_trades_total": len(reg.get("theory_trades") or []),
    }
    return rows + extra, census


def build_metrics(controlled: list[dict[str, Any]], natural_census: dict[str, Any]) -> dict[str, Any]:
    c_sl = [r for r in controlled if r.get("broker_exit_reason") == "BROKER_SL"]
    c_tp = [r for r in controlled if r.get("broker_exit_reason") == "BROKER_TP"]
    c_mc = [
        r
        for r in controlled
        if r.get("broker_exit_reason") in ("probe_market_close", "PROBE_MARKET_CLOSE")
    ]
    return {
        "controlled": {
            "broker_trades": len(controlled),
            "theory_trades": 0,
            "theory_mapping": MAP_THEORY_NA,
            "exact_or_plan_match": sum(
                1 for r in controlled if str(r.get("mapping_status") or "").startswith("PLAN")
            ),
            "ambiguous": 0,
            "unmapped": 0,
            "entry_side_convention_documented": True,
            "broker_sl_trigger_agreements": len(c_sl),
            "broker_tp_trigger_agreements": len(c_tp),
            "market_close_plumbing_rows": len(c_mc),
            "median_abs_entry_delta_pips_vs_plan": _median(
                [abs(r["entry_delta_pips_vs_plan"]) for r in controlled if r.get("entry_delta_pips_vs_plan") is not None]
            ),
            "total_broker_net_pnl": sum(float(r.get("net_pnl") or 0) for r in controlled),
            "total_commission": sum(float(r.get("commission") or 0) for r in controlled),
        },
        "natural": {
            **natural_census,
            "note": (
                "Natural Theory↔broker mapping is the existing Gate2 registry census. "
                "Simulator H1/H1+1m replay was NOT re-run in this report."
            ),
            "sim_h1_only": "not_replayed_in_this_report",
            "sim_h1_1m": "not_replayed_in_this_report",
        },
    }


def _median(xs: list[float]) -> float | None:
    if not xs:
        return None
    ys = sorted(xs)
    mid = len(ys) // 2
    if len(ys) % 2:
        return ys[mid]
    return 0.5 * (ys[mid - 1] + ys[mid])


def remaining_uncertainty() -> list[str]:
    return [
        "Controlled probes do not test alexg/Theory signal→fill fidelity; Theory mapping is NOT APPLICABLE.",
        "Natural clean_natural sample size remains 1 (target 20–50+); insufficient for Gate 2 signal fidelity.",
        "Simulator H1-only and H1+1m replay were not re-executed against these controlled trades in this report.",
        "1m tick evidence for most natural RTs is unavailable (AUDJPY Level-2 is a single forensic case).",
        "H1 Theory timestamps are bar/fill:close intent, not broker wall-clock fills — time skew is structural.",
        "g2p scenario labels (LONG_SL/SHORT_TP) do not imply broker SL/TP; those exits were probe_market_close.",
        "Overall Gate 2 remains a human research decision; this report does not auto-PASS or auto-FAIL.",
    ]


def write_markdown(path: Path, *, run_id: str, metrics: dict, rows: list[dict], pop: dict) -> None:
    lines = [
        f"# Gate 2 Fidelity Reconciliation — `{run_id}`",
        "",
        "**READ-ONLY.** No orders. No strategy/simulator changes. Gate 2 decision is human.",
        "",
        "## Principle",
        "",
        "- `BROKER_OBSERVED` — execution record from MT5 deals",
        "- `FROZEN_PLAN` — controlled harness reference (not Theory)",
        "- `THEORY_EXPECTED` — natural alexg/Theory only",
        "- `SIMULATOR_REPLAYED` — **not run** in this report",
        "- Controlled harness probes are **NOT** Theory signals",
        "",
        "## Inputs",
        "",
        f"- Controlled entry plumbing: `g2p_20260922_162842`",
        f"- Controlled broker SL/TP: `g2e_20260923_170458`",
        f"- Natural registry: `data/runs/gate2_natural_sample/registry.json`",
        "",
        "## Population census",
        "",
        "### CONTROLLED",
        "",
        f"- Broker trades analyzed: **{metrics['controlled']['broker_trades']}**",
        f"- Theory mapping: **{MAP_THEORY_NA}**",
        f"- Plan mappings: `{pop['controlled']['mapping_counts']}`",
        f"- Broker SL rows: **{metrics['controlled']['broker_sl_trigger_agreements']}**",
        f"- Broker TP rows: **{metrics['controlled']['broker_tp_trigger_agreements']}**",
        f"- Market-close plumbing rows: **{metrics['controlled']['market_close_plumbing_rows']}**",
        "",
        "### NATURAL",
        "",
        f"- clean_natural: **{metrics['natural']['clean_natural']}**",
        f"- ambiguous: **{metrics['natural']['ambiguous']}**",
        f"- unmapped_broker: **{metrics['natural']['unmapped_broker']}**",
        f"- unmapped_theory: **{metrics['natural']['unmapped_theory']}**",
        f"- excluded_catch_up: **{metrics['natural']['excluded_catch_up']}**",
        f"- still_open: **{metrics['natural']['still_open']}**",
        f"- Simulator replay this run: **not_replayed**",
        "",
        "## Main discrepancies",
        "",
        "1. **NOT_SIGNAL_DRIVEN** — all controlled rows (by design).",
        "2. **EXIT_MODEL** — g2p scenario names SL/TP vs actual `probe_market_close`.",
        "3. **TIME_REPRESENTATION / H1_RESOLUTION** — natural sample (H1 intent vs broker UTC).",
        "4. **MISSING_DATA / 1M_RESOLUTION** — most natural RTs lack Level-2 ticks; sim not replayed here.",
        "5. **MAPPING** — natural: 1 clean vs many catch-up/unmapped (insufficient N).",
        "",
        "## Remaining uncertainty (blocks Gate 2 approval as research GO)",
        "",
    ]
    for u in remaining_uncertainty():
        lines.append(f"- {u}")
    lines += [
        "",
        "## Trade table (compact)",
        "",
        "| sample | pos | symbol | dir | broker entry | plan/theory entry | Δpips | broker exit | exit reason | mapping | disc | conf |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        if r.get("sample") == "NATURAL" and r.get("mapping_status") in (MAP_UNMAPPED, MAP_AMBIGUOUS, MAP_THEORY_ONLY):
            # keep table focused; still listed in JSON
            continue
        ref_e = r.get("plan_entry") if r.get("sample") == "CONTROLLED" else r.get("theory_entry")
        dlt = r.get("entry_delta_pips_vs_plan") if r.get("sample") == "CONTROLLED" else r.get("entry_delta_pips_vs_theory")
        lines.append(
            "| {sample} | {pos} | {sym} | {d} | {be} | {re} | {dp} | {bx} | {er} | {mp} | {dc} | {cf} |".format(
                sample=r.get("sample"),
                pos=r.get("broker_position"),
                sym=r.get("symbol"),
                d=r.get("direction"),
                be=r.get("broker_entry"),
                re=ref_e,
                dp=None if dlt is None else round(float(dlt), 3),
                bx=r.get("broker_exit"),
                er=r.get("broker_exit_reason"),
                mp=r.get("mapping_status"),
                dc=",".join(r.get("discrepancy_classes") or [])[:40],
                cf=r.get("confidence"),
            )
        )
    lines += [
        "",
        "## Evidence buckets",
        "",
        "| Bucket | Status |",
        "|---|---|",
        "| A Market entry plumbing | validated (controlled) |",
        "| B Broker SL | validated (`g2e`, reason=4) |",
        "| C Broker TP | validated (`g2e`, reason=5) |",
        "| D Deal/history reconcile | validated |",
        "| E Simulator/Theory ↔ broker fidelity | **UNRESOLVED** (natural N=1; sim not replayed here; controlled≠Theory) |",
        "",
        "## Gate 2",
        "",
        "**NO-GO — pending human research review.** This artifact does not declare PASS/FAIL.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    run_id = f"g2f_{_utc_now().strftime('%Y%m%d_%H%M%S')}"
    out = OUT_ROOT / "runs" / run_id
    out.mkdir(parents=True, exist_ok=True)

    controlled = load_controlled_rows()
    reg = _load(NAT_REG)
    natural_rows, census = load_natural_rows(reg)
    rows = controlled + natural_rows
    pop = summarize_populations(rows)
    metrics = build_metrics(controlled, census)

    manifest = {
        "run_id": run_id,
        "generated_at_utc": _iso(),
        "git_commit": _git(),
        "mode": "read-only",
        "orders_submitted": 0,
        "strategy_modified": False,
        "simulator_modified": False,
        "inputs": {
            "controlled_entry_batch": "g2p_20260922_162842",
            "controlled_exit_batch": "g2e_20260923_170458",
            "natural_registry": str(NAT_REG.relative_to(ROOT)).replace("\\", "/"),
        },
        "assumptions": {
            "controlled_reference": "FROZEN_PLAN (not Theory)",
            "theory_for_controlled": MAP_THEORY_NA,
            "server_offset_seconds_assumed_for_natural_summary": 10800,
            "mapping_algorithm": "borex.gate2.fidelity_map + existing natural attempt_maps census",
            "mapping_version": "v1_fidelity_20260923",
            "no_forced_matching": True,
            "simulator_replay_this_run": False,
        },
        "safety": {
            "live_8791_untouched": True,
            "magic_88001_untouched": True,
            "eurdkk_untouched": True,
        },
        "gate2": "NO-GO",
    }

    trade_recon = {
        "run_id": run_id,
        "rows": rows,
        "population_summary": pop,
    }
    fidelity_report = {
        "run_id": run_id,
        "generated_at_utc": _iso(),
        "metrics": metrics,
        "population_summary": pop,
        "remaining_uncertainty": remaining_uncertainty(),
        "evidence_buckets": {
            "A_market_entry": "validated_controlled",
            "B_broker_sl": "validated_g2e",
            "C_broker_tp": "validated_g2e",
            "D_deal_history": "validated",
            "E_theory_sim_broker_fidelity": "UNRESOLVED",
        },
        "primary_discrepancy_themes": [
            "NOT_SIGNAL_DRIVEN (controlled)",
            "EXIT_MODEL (g2p labels vs market close)",
            "TIME_REPRESENTATION / H1_RESOLUTION (natural)",
            "MISSING_DATA / insufficient natural N",
            "SIMULATOR_REPLAYED absent in this report",
        ],
        "gate2": "NO-GO",
        "human_decision_required": True,
    }

    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    (out / "trade_reconciliation.json").write_text(
        json.dumps(trade_recon, indent=2, default=str), encoding="utf-8"
    )
    (out / "fidelity_report.json").write_text(
        json.dumps(fidelity_report, indent=2, default=str), encoding="utf-8"
    )
    write_markdown(out / "fidelity_report.md", run_id=run_id, metrics=metrics, rows=rows, pop=pop)

    print("Gate 2 Fidelity Reconciliation (READ-ONLY)")
    print(f"run_id: {run_id}")
    print(f"controlled broker trades: {len(controlled)}")
    print(f"natural clean: {census['clean_natural']} ambiguous: {census['ambiguous']} "
          f"unmapped_broker: {census['unmapped_broker']} unmapped_theory: {census['unmapped_theory']}")
    print("theory mapping for controlled: THEORY_SIGNAL_NOT_APPLICABLE")
    print("Gate 2: NO-GO (human decision)")
    print(f"Artifact: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
