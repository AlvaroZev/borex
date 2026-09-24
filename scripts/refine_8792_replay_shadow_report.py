"""Refine Case-2 Layer-A report with architecture recommendation."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

OUT = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "runs"
    / "mirror_2d_8792"
    / "reconciliation"
    / "replay_shadow"
)


def main() -> int:
    path = OUT / "layer_a_report.json"
    report = json.loads(path.read_text(encoding="utf-8"))

    report["refined_interpretation"] = {
        "case": 2,
        "summary": (
            "ReplayShadow using live ShadowEngine.step_master_bar on MT5 H1 cache "
            "still only recovers ~33.5% of saved Theory fills (best mode: bulk). "
            "MultiMarket on the same window recovered ~23.6%. Both fail; the gap "
            "between Shadow replay and MultiMarket (~10pp) is real but secondary "
            "to the ~66% Theory miss shared by both."
        ),
        "first_divergence": (
            "0/51 pairs have zero divergence. Divergences appear at the open of "
            "the window (Aug 27), not as a late cascade from one bad fill. "
            "Missing-cause tags: ~111 A3/A4 (no ghost/pattern on that bar), "
            "~10 A5 occupancy. Occupancy is not the primary story."
        ),
        "tested_hypotheses": {
            "A5_occupancy": "Minor (~10 missing). Not the main Layer-A break.",
            "A6_cash": (
                "Signals do not read cash; cash only gates fills. Cannot explain "
                "missing ghosts/patterns."
            ),
            "A7_flatten_order": (
                "Preserved: exits before scan in ShadowEngine and MultiMarket."
            ),
            "A2_A4_warmup_ghost": (
                "Tried bulk / ghost_prime / fill_prime. ghost_prime and fill_prime "
                "did NOT beat bulk vs Theory. Suggests carried live state or bar "
                "identity matters more than 72-bar prime style."
            ),
            "A9_data": (
                "Could not verify live_candles vs cache: local Postgres :5433 "
                "refused connection. No golden fixture of the exact H1 bars the "
                "8792 process consumed was saved with the dump. This is the leading "
                "blocker for Case-2 resolution."
            ),
            "A1_clock": (
                "Replay master hours align to :00 UTC; timing deltas on matches "
                "are ~0h median. Clock recognition for matched trades looks OK."
            ),
        },
        "why_not_case_1": (
            "Case 1 requires ReplayShadow ≈ Theory. We got 33.5%, not ≥90%."
        ),
        "why_not_blame_multimarket_alone": (
            "Even the live ShadowEngine path fails on cache bars. Fixing only "
            "MultiMarket would not make this window reproduce Theory."
        ),
        "cheapest_next_step": (
            "Export a golden fixture while mirror DB/MT5 history is available: "
            "(1) live_candles H1 for warmup+window, (2) theory_state blob at "
            "window start or continuous checkpoint, (3) config_hash/knobs. "
            "Then re-run ReplayShadow checkpoint/epoch tiers from "
            "shadow_parity_replay.py against that fixture. "
            "Do NOT tune RR/SL/spreads. Do NOT run N1 grids."
        ),
    }

    report["architecture_recommendation"] = {
        "preferred_when_fixture_works": {
            "option": 2,
            "name": "ShadowEngine as signal generator + deterministic exec layer",
            "why": (
                "Same code as live Theory; best Layer-A fidelity; natural "
                "signal/exec split for later 1m tests."
            ),
        },
        "not_yet": [
            "Option 4 (1m/tick) — premature until Layer A reproduces Theory",
            "Option 3 (full broker emulator) — too expensive before fixture proof",
            "Option 5 (MT5 tester) — weak for alexg8 ghost state",
        ],
        "ranking_now": report.get("architecture_ranking"),
        "immediate_work": (
            "Collect golden inputs/state (fixture), not rewrite MultiMarket yet."
        ),
    }

    report["answers"] = {
        "1_can_replay_reproduce_theory": False,
        "1_detail": (
            f"Best Theory recall {report['verdict']['theory_overlap_pct']}% "
            f"(mode={report['verdict']['best_mode']}); threshold for yes is ≥90%."
        ),
        "2_first_deterministic_divergence": (
            "Immediate window-open divergence on essentially all pairs "
            "(0 zero-divergence pairs). Dominant tag: no matching ghost/pattern "
            "on that bar (A3/A4), not occupancy cascade."
        ),
        "3_multimarket_materially_different_from_live_path": (
            "Partially. On identical cache bars, Shadow bulk recall 33.5% vs "
            "MultiMarket 23.6% — modest implementation gap. Both are far from "
            "live Theory, so MultiMarket is not the sole Layer-A failure mode "
            "for this experiment."
        ),
        "4_h1_1m_improves_broker_fidelity": (
            "Not tested — Layer A did not pass; execution isolation skipped."
        ),
        "5_canonical_research_instrument": (
            "Provisionally option 2 (ShadowEngine + exec layer), contingent on "
            "golden fixture proving ReplayShadow can hit Theory. Until then the "
            "instrument is unvalidated for live transfer."
        ),
        "6_ready_for_strategy_optimization": False,
        "6_detail": (
            "NO-GO. Simulator fidelity work must continue: golden bar/state "
            "fixture → ReplayShadow parity → only then execution isolation → "
            "only then strategy sweeps."
        ),
    }

    report["verdict"]["policy_update"] = {
        "system_a": (
            "MultiMarket sweeps NOT validated for live transfer. ReplayShadow "
            "also not yet validated without golden fixture."
        ),
        "system_b": "Do not transfer System-A findings to funded-account work.",
        "system_c": (
            "Older sims may be used as labeled signal-quality research only."
        ),
    }
    report["refined_at"] = datetime.now(timezone.utc).isoformat()

    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    lines = [
        "",
        "=== REFINED CASE-2 INTERPRETATION ===",
        report["refined_interpretation"]["summary"],
        "",
        "First divergence:",
        report["refined_interpretation"]["first_divergence"],
        "",
        "Cheapest next step:",
        report["refined_interpretation"]["cheapest_next_step"],
        "",
        "Answers:",
        "1. Reproduce Theory? NO (33.5% best)",
        "2. First divergence: window-open, all pairs, A3/A4 no ghost/pattern",
        "3. MultiMarket vs live path: modest gap on same bars; both fail vs Theory",
        "4. H1+1m exec isolation: SKIPPED (Layer A failed)",
        "5. Canonical instrument: option 2 provisional, blocked on golden fixture",
        "6. Ready to optimize strategies? NO-GO",
    ]
    summary = OUT / "SUMMARY.txt"
    summary.write_text(
        summary.read_text(encoding="utf-8").rstrip() + "\n" + "\n".join(lines) + "\n",
        encoding="utf-8",
    )
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
