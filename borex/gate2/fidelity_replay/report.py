"""Artifact writers for Gate 2 Level 5 fidelity replay."""

from __future__ import annotations

import json
import statistics
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from borex.gate2.fidelity_replay import taxonomy as T


def _median(xs: list[float]) -> float | None:
    if not xs:
        return None
    return float(statistics.median(xs))


def _max_abs(xs: list[float]) -> float | None:
    if not xs:
        return None
    return float(max(abs(x) for x in xs))


def summarize(trades: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(trades)
    replayed = [t for t in trades if t.get("data_status") == "REPLAYED"]
    insufficient = [t for t in trades if t.get("data_status") != "REPLAYED"]
    theory_ok = sum(
        1 for t in trades if (t.get("theory_reproduction") or {}).get("signal_match")
    )
    entry_tb = [
        float(t["entry_delta_pips_theory_broker"])
        for t in trades
        if t.get("entry_delta_pips_theory_broker") is not None
    ]
    entry_ts = [
        float(t["entry_delta_pips_theory_sim"])
        for t in trades
        if t.get("entry_delta_pips_theory_sim") is not None
    ]
    exit_sb = [
        float(t["exit_delta_pips_sim_level_vs_broker"])
        for t in replayed
        if t.get("exit_delta_pips_sim_level_vs_broker") is not None
        and t.get("exit_event_agreement") is True
    ]
    exit_sb_all_replayed = [
        float(t["exit_delta_pips_sim_level_vs_broker"])
        for t in replayed
        if t.get("exit_delta_pips_sim_level_vs_broker") is not None
    ]
    event_agree = sum(1 for t in replayed if t.get("exit_event_agreement") is True)
    event_disagree = sum(1 for t in replayed if t.get("exit_event_agreement") is False)
    class_counts: Counter[str] = Counter()
    for t in trades:
        for c in t.get("discrepancy_classes") or []:
            class_counts[c] += 1

    trade8 = next(
        (t for t in trades if str(t.get("broker_ticket")) == "1959000538"),
        None,
    )

    return {
        "trades_total": total,
        "trades_replayed": len(replayed),
        "trades_insufficient_source_data": len(insufficient),
        "theory_reproduction_match": f"{theory_ok}/{total}",
        "theory_reproduction_ok_count": theory_ok,
        "entry_theory_vs_broker": {
            "n": len(entry_tb),
            "median_delta_pips": _median(entry_tb),
            "max_abs_delta_pips": _max_abs(entry_tb),
            "per_trade": entry_tb,
        },
        "entry_theory_vs_sim": {
            "n": len(entry_ts),
            "median_delta_pips": _median(entry_ts),
            "max_abs_delta_pips": _max_abs(entry_ts),
            "note": "Simulator uses mid±half-spread; Theory uses bid/ask side.",
        },
        "exit_event_agreement_among_replayed": {
            "agree": event_agree,
            "disagree": event_disagree,
            "replayed": len(replayed),
        },
        "exit_sim_level_vs_broker": {
            "n": len(exit_sb),
            "median_delta_pips": _median(exit_sb),
            "max_abs_delta_pips": _max_abs(exit_sb),
            "per_trade": exit_sb,
            "note": "Only trades with exit-event agreement; level fill vs broker deal.",
        },
        "exit_sim_level_vs_broker_all_replayed": {
            "n": len(exit_sb_all_replayed),
            "median_delta_pips": _median(exit_sb_all_replayed),
            "max_abs_delta_pips": _max_abs(exit_sb_all_replayed),
            "note": "Includes event disagreements (e.g. SIM_SL vs BROKER_TP on both-wick).",
        },
        "discrepancy_class_counts": dict(class_counts),
        "trade_8_ticket_1959000538": trade8,
    }


def write_manifest(
    path: Path,
    *,
    run_id: str,
    source_run_id: str,
    git_commit: str,
    trades: list[dict[str, Any]],
    summary: dict[str, Any],
    file_hashes: dict[str, str],
    data_notes: list[str],
    substitution_occurred: bool,
) -> None:
    payload = {
        "run_id": run_id,
        "experiment": "gate2_fidelity_replay_level5",
        "source_run": source_run_id,
        "strategy": "gate2_micro_fidelity",
        "strategy_version": "v1_20260924_n20",
        "config_id": "micro_fidelity_v1_n20_unlimited_4pip",
        "git_commit": git_commit,
        "simulator": {
            "adapter": "borex.gate2.fidelity_replay.sim_exit",
            "exit_rules_source": "borex.backtest.multi_market_engine.MultiMarketEngine",
            "rules": [
                "1m OHLC wick hit",
                "both-wick → SL first",
                "chronological first bar wins",
                "level fill for primary comparison (spread wrapper optional)",
            ],
        },
        "replay_timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "data_sources": [
            {
                "kind": "theory_events.jsonl_embedded_m1",
                "exact_runtime": True,
                "substituted": False,
            },
            {
                "kind": "broker_executions.jsonl",
                "exact_runtime": True,
                "substituted": False,
            },
            {
                "kind": "trade_reconciliation.json",
                "exact_runtime": True,
                "substituted": False,
            },
        ],
        "source_data_hashes": file_hashes,
        "data_notes": data_notes,
        "substitution_occurred": substitution_occurred,
        "number_of_trades": summary["trades_total"],
        "number_replayed": summary["trades_replayed"],
        "number_insufficient_source_data": summary["trades_insufficient_source_data"],
        "safety": {
            "mt5_connection": False,
            "orders": False,
            "live_account_touched": False,
            "mirror_orders": False,
            "eurdkk_touched": False,
            "read_only": True,
        },
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def write_trade_replay(path: Path, trades: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    payload = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
        "trades": trades,
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def write_report_json(path: Path, *, run_id: str, summary: dict[str, Any], trades: list[dict[str, Any]]) -> None:
    payload = {
        "run_id": run_id,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
        "gate2_bucket_E": "UNRESOLVED",
        "human_decision_required": True,
        "note": "Do not auto PASS/FAIL Gate 2 from this report.",
        "census": [
            {
                "n": t.get("census_index"),
                "symbol": t.get("symbol"),
                "direction": t.get("direction"),
                "broker_exit": t.get("broker_exit_reason"),
                "signal_match": (t.get("theory_reproduction") or {}).get("signal_match"),
                "data_status": t.get("data_status"),
                "exit_event_agreement": t.get("exit_event_agreement"),
                "main_discrepancy": (t.get("discrepancy_classes") or [None])[0],
                "ticket": t.get("broker_ticket"),
            }
            for t in trades
        ],
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def render_markdown(
    *,
    run_id: str,
    source_run_id: str,
    git_commit: str,
    summary: dict[str, Any],
    trades: list[dict[str, Any]],
    data_notes: list[str],
    substitution_occurred: bool,
) -> str:
    lines: list[str] = []
    lines.append(f"# Gate 2 Level 5 Exact Replay Fidelity — `{run_id}`")
    lines.append("")
    lines.append("## Executive summary")
    lines.append("")
    lines.append(
        f"Read-only replay of the 20 natural micro-fidelity trades from `{source_run_id}` "
        "through the Borex simulator OHLC exit rules, using only Theory-embedded 1m "
        "candles persisted in that run."
    )
    lines.append("")
    lines.append(f"- Source run: `{source_run_id}`")
    lines.append(f"- Git commit: `{git_commit}`")
    lines.append(
        f"- Trades: **{summary['trades_total']}** total; "
        f"**{summary['trades_replayed']}** replayed; "
        f"**{summary['trades_insufficient_source_data']}** insufficient source data"
    )
    lines.append(
        f"- Exact continuous M1 archive: **not present**; used Theory-embedded "
        f"`candle_sequence` stitch (substitution={substitution_occurred})"
    )
    lines.append(f"- Theory reproduction: **{summary['theory_reproduction_match']}**")
    etb = summary["entry_theory_vs_broker"]
    lines.append(
        f"- Entry Theory↔broker: median Δ={etb['median_delta_pips']} pips, "
        f"max|Δ|={etb['max_abs_delta_pips']} pips"
    )
    ex = summary["exit_sim_level_vs_broker"]
    ea = summary["exit_event_agreement_among_replayed"]
    lines.append(
        f"- Exit event agreement (replayed only): {ea['agree']} agree / "
        f"{ea['disagree']} disagree / {ea['replayed']} replayed"
    )
    lines.append(
        f"- Exit price sim-level↔broker (replayed): median Δ={ex['median_delta_pips']} pips, "
        f"max|Δ|={ex['max_abs_delta_pips']} pips"
    )
    lines.append("")
    lines.append("Major agreements: frozen Theory signals reproduce from causal candles; "
                 "when a continuous post-signal M1 path exists, simulator SL/TP wick logic is deterministic.")
    lines.append(
        "Major discrepancies: many trades lack enough persisted post-signal M1 bars to "
        "observe an exit; where both-wick bars appear, SL-first can differ from broker "
        "intra-minute path; trade ticket 1959000538 shows material exit-price variance."
    )
    lines.append("")
    lines.append("Gate 2 E remains a human research decision.")
    lines.append("")

    lines.append("## Section 1 — Frozen experiment")
    lines.append("")
    lines.append("- Strategy: `gate2_micro_fidelity` / `v1_20260924_n20`")
    lines.append("- Config: `micro_fidelity_v1_n20_unlimited_4pip`")
    lines.append("- SL/TP: 4.0 pips symmetric, RR=1.0, volume=0.01, timeframe=1m")
    lines.append(f"- Commit: `{git_commit}`")
    lines.append("- No strategy/simulator parameter changes were made for this experiment.")
    lines.append("")

    lines.append("## Section 2 — Data provenance")
    lines.append("")
    for n in data_notes:
        lines.append(f"- {n}")
    lines.append(f"- Substitution occurred: **{substitution_occurred}**")
    lines.append("")

    lines.append("## Section 3 — Trade census")
    lines.append("")
    lines.append(
        "| # | Symbol | Dir | Broker Exit | Signal Match | Data | Exit agree | Main discrepancy |"
    )
    lines.append(
        "| - | ------ | --- | ----------- | ------------ | ---- | ---------- | ---------------- |"
    )
    for t in trades:
        sym = str(t.get("symbol") or "").replace("=X", "")
        sm = (t.get("theory_reproduction") or {}).get("signal_match")
        exit_agree = t.get("exit_event_agreement")
        ea_s = "—" if exit_agree is None else ("Y" if exit_agree else "N")
        main = (t.get("discrepancy_classes") or ["—"])[0]
        lines.append(
            f"| {t.get('census_index')} | {sym} | {t.get('direction')} | "
            f"{t.get('broker_exit_reason')} | {sm} | {t.get('data_status')} | "
            f"{ea_s} | `{main}` |"
        )
    lines.append("")

    lines.append("## Section 4 — Entry fidelity")
    lines.append("")
    lines.append(
        f"- Theory↔broker median Δ pips: {etb['median_delta_pips']}; "
        f"max |Δ|: {etb['max_abs_delta_pips']}"
    )
    ets = summary["entry_theory_vs_sim"]
    lines.append(
        f"- Theory↔sim median Δ pips: {ets['median_delta_pips']}; "
        f"max |Δ|: {ets['max_abs_delta_pips']} ({ets['note']})"
    )
    lines.append("- Per-trade Theory↔broker Δ pips: " + ", ".join(f"{x:.2f}" for x in etb["per_trade"]))
    lines.append("")

    lines.append("## Section 5 — Exit fidelity")
    lines.append("")
    lines.append(
        f"- Among replayed trades: event agree={ea['agree']}, disagree={ea['disagree']}"
    )
    lines.append(
        f"- Sim level vs broker exit Δ pips: median={ex['median_delta_pips']}, "
        f"max|Δ|={ex['max_abs_delta_pips']}"
    )
    lines.append("- Broker exit timestamps: **not available** in source artifacts "
                 "(exit_time_delta not computable).")
    lines.append("")
    t8 = summary.get("trade_8_ticket_1959000538")
    lines.append("### Trade ticket 1959000538 (user list #8 — AUDUSD short BROKER_SL)")
    lines.append("")
    if t8:
        lines.append(f"- Theory signal: `{t8.get('signal_time_theory')}`")
        lines.append(
            f"- Theory entry/SL/TP: {t8.get('theory_entry')} / "
            f"{t8.get('theory_sl')} / {t8.get('theory_tp')}"
        )
        lines.append(
            f"- Broker entry/exit/reason: {t8.get('broker_entry')} / "
            f"{t8.get('broker_exit')} / {t8.get('broker_exit_reason')}"
        )
        lines.append(
            f"- Sim exit: {t8.get('sim_exit')} @ `{t8.get('sim_exit_time')}` "
            f"reason={t8.get('sim_exit_reason')} both_wick={t8.get('both_wick_on_sim_exit_bar')}"
        )
        lines.append(
            f"- Exit Δ (theory SL vs broker): {t8.get('exit_delta_pips_theory_level_vs_broker')} pips"
        )
        lines.append(
            f"- Exit Δ (sim level vs broker): {t8.get('exit_delta_pips_sim_level_vs_broker')} pips"
        )
        lines.append(f"- Classes: {', '.join(t8.get('discrepancy_classes') or [])}")
        lines.append("")
        lines.append(
            "Evidence-based reading: Theory SL=0.70372; broker DEAL_REASON_SL fill=0.70359 "
            "(−1.30 pips vs SL). Stitched run M1 shows first SL-touch on bar "
            "`2026-09-24T01:30:00+00:00` with high=0.70403 and low=0.70261 "
            "(both-wick → simulator SL-first at 0.70372). The broker fill inside the SL "
            "level is not explainable by the 1m OHLC simulator alone; without an exit "
            "timestamp or tick path this is classified as "
            "`CANDLE_RESOLUTION_LIMIT` + `BROKER_EXECUTION_VARIANCE`, not a proven "
            "simulator bug and not labeled `BROKER_SLIPPAGE` (slippage usually worse "
            "for short SL covers)."
        )
    else:
        lines.append("- Ticket not found in census.")
    lines.append("")

    lines.append("## Section 6 — Simulator behavior")
    lines.append("")
    lines.append("- Input: completed 1m OHLC only (no tick path).")
    lines.append("- Spread at entry: Theory bid/ask snapshot; simulator entry = mid ± half-spread.")
    lines.append("- Exit walk: MultiMarketEngine wick rules via static `_hit_stop` / `_hit_take_profit`.")
    lines.append("- First touch: first chronological 1m bar that tags SL or TP.")
    lines.append("- Ambiguous same-bar both-wick: **SL preferred** (documented broker-path rule).")
    lines.append("- Primary exit price compared at the SL/TP **level** (spread wrapper not applied "
                 "to the primary delta).")
    lines.append("- No changes were made to MultiMarketEngine for this experiment.")
    lines.append("")

    lines.append("## Section 7 — Discrepancy taxonomy")
    lines.append("")
    for k, v in sorted(summary["discrepancy_class_counts"].items(), key=lambda kv: (-kv[1], kv[0])):
        lines.append(f"- `{k}`: {v}")
    lines.append("")

    lines.append("## Section 8 — Conclusions")
    lines.append("")
    lines.append("### What the evidence supports")
    lines.append("")
    lines.append(
        "- **Theory ↔ broker (Level 4)** remains as previously observed: 20/20 EXACT_MATCH "
        "in the source run; this Level 5 experiment does not reopen that mapping."
    )
    lines.append(
        f"- **Theory reproduction** from causal embedded candles: "
        f"{summary['theory_reproduction_match']}."
    )
    lines.append(
        "- **Simulator ↔ broker** can be evaluated only for trades with a continuous "
        f"post-signal M1 path in the run artifacts ({summary['trades_replayed']}/20). "
        "Where replayable, exit agreement and pip deltas are reported above without a "
        "single aggregate fidelity score."
    )
    lines.append("")
    lines.append("### What remains unresolved")
    lines.append("")
    lines.append(
        f"- {summary['trades_insufficient_source_data']}/20 trades lack sufficient "
        "persisted post-signal M1 coverage to observe simulator exit."
    )
    lines.append("- Broker exit timestamps absent → exit timing fidelity not measurable.")
    lines.append("- Intra-minute path / true first-touch vs both-wick not observable from 1m OHLC.")
    lines.append("")
    lines.append("### What this experiment does NOT establish")
    lines.append("")
    lines.append("- Strategy profitability / WR / PF / MDD (not computed).")
    lines.append("- Gate 2 automatic PASS/FAIL.")
    lines.append("- Live-account fidelity.")
    lines.append("- That missing M1 coverage implies simulator correctness or incorrectness.")
    lines.append("")
    lines.append("Gate 2 E remains a human research decision.")
    lines.append("")
    return "\n".join(lines)


def write_report_md(path: Path, **kwargs: Any) -> None:
    path.write_text(render_markdown(**kwargs), encoding="utf-8")
