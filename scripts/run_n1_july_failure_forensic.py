#!/usr/bin/env python3
"""Read-only forensic: N1 alexg8 Aug IS vs Jun/Jul OOS (no MT5, no sim changes).

Hypothesis
----------
July N1 failure is not explained by throughput collapse or a different exit-mix
regime versus August/June; it is driven by worse expectancy within the same
daily_close-dominated closest-sim path (with elevated margin_stop damage).

Inputs (existing artifacts only)
--------------------------------
- data/runs/mt5_1m_n1_tpfactor/sl_4p0_lev_500_rr_1p0.json   (Aug IS)
- data/runs/mt5_1m_n1_oos_jun2026/sl_4p0_lev_500.json
- data/runs/mt5_1m_n1_oos_jul2026/sl_4p0_lev_500.json

Does not modify strategy, simulator, or parameters.
"""

from __future__ import annotations

import json
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SOURCES = {
    "aug_is": ROOT / "data/runs/mt5_1m_n1_tpfactor/sl_4p0_lev_500_rr_1p0.json",
    "jun_oos": ROOT / "data/runs/mt5_1m_n1_oos_jun2026/sl_4p0_lev_500.json",
    "jul_oos": ROOT / "data/runs/mt5_1m_n1_oos_jul2026/sl_4p0_lev_500.json",
}


def _mean(xs: list[float]) -> float | None:
    return float(statistics.mean(xs)) if xs else None


def _median(xs: list[float]) -> float | None:
    return float(statistics.median(xs)) if xs else None


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _trade_pnl(t: dict[str, Any]) -> float | None:
    for k in ("net_pnl", "pnl", "profit", "net"):
        if t.get(k) is not None:
            return float(t[k])
    return None


def _trade_symbol(t: dict[str, Any]) -> str:
    return str(t.get("symbol") or t.get("pair") or "UNKNOWN")


def _trade_exit(t: dict[str, Any]) -> str:
    return str(
        t.get("exit_reason")
        or t.get("reason")
        or (t.get("exit") or {}).get("reason")
        or "unknown"
    )


def analyze_window(label: str, path: Path) -> dict[str, Any]:
    d = _load(path)
    summary = d.get("summary") or {}
    trades = list(d.get("trades") or [])
    # Some artifacts nest trades under report payload
    if not trades and isinstance(d.get("result"), dict):
        trades = list((d["result"] or {}).get("trades") or [])

    by_exit_pnl: dict[str, list[float]] = defaultdict(list)
    by_exit_count: Counter[str] = Counter()
    by_symbol_pnl: dict[str, list[float]] = defaultdict(list)
    wins = losses = 0
    pnls: list[float] = []

    for t in trades:
        # Prefer eval-window trades if flagged
        if t.get("warmup") is True:
            continue
        pnl = _trade_pnl(t)
        reason = _trade_exit(t)
        by_exit_count[reason] += 1
        if pnl is None:
            continue
        pnls.append(pnl)
        by_exit_pnl[reason].append(pnl)
        by_symbol_pnl[_trade_symbol(t)].append(pnl)
        if pnl > 0:
            wins += 1
        elif pnl < 0:
            losses += 1

    exit_stats = {
        reason: {
            "n": by_exit_count[reason],
            "share": by_exit_count[reason] / max(1, sum(by_exit_count.values())),
            "sum_pnl": sum(by_exit_pnl.get(reason) or [0.0]),
            "mean_pnl": _mean(by_exit_pnl.get(reason) or []),
            "median_pnl": _median(by_exit_pnl.get(reason) or []),
        }
        for reason in sorted(by_exit_count)
    }

    # Top damaging symbols
    sym_sum = {s: sum(xs) for s, xs in by_symbol_pnl.items()}
    worst = sorted(sym_sum.items(), key=lambda kv: kv[1])[:10]
    best = sorted(sym_sum.items(), key=lambda kv: kv[1], reverse=True)[:10]

    return {
        "label": label,
        "source": str(path.as_posix()),
        "entry_start": d.get("entry_start") or d.get("eval_start"),
        "last_bar": d.get("last_bar"),
        "ghost_sl_mult": d.get("ghost_sl_mult"),
        "leverage": d.get("leverage"),
        "rr_factor": d.get("rr_factor"),
        "intents": d.get("intents"),
        "knobs": d.get("knobs"),
        "summary": {
            "account": summary.get("account"),
            "net_pnl": summary.get("net_pnl"),
            "trades": summary.get("trades"),
            "win_rate": summary.get("win_rate"),
            "profit_factor": summary.get("profit_factor"),
            "max_drawdown_pct": summary.get("max_drawdown_pct"),
            "exit_reasons": summary.get("exit_reasons"),
        },
        "trade_rows_analyzed": len(pnls),
        "wins": wins,
        "losses": losses,
        "mean_pnl": _mean(pnls),
        "median_pnl": _median(pnls),
        "exit_stats": exit_stats,
        "worst_symbols_by_sum_pnl": [{"symbol": s, "sum_pnl": v} for s, v in worst],
        "best_symbols_by_sum_pnl": [{"symbol": s, "sum_pnl": v} for s, v in best],
    }


def compare(windows: dict[str, dict[str, Any]]) -> dict[str, Any]:
    aug = windows["aug_is"]
    jun = windows["jun_oos"]
    jul = windows["jul_oos"]

    def exit_share(w: dict[str, Any], reason: str) -> float:
        er = (w.get("summary") or {}).get("exit_reasons") or {}
        n = sum(int(v) for v in er.values()) or 1
        return float(er.get(reason, 0)) / n

    throughput = {
        "aug_intents": aug.get("intents"),
        "jun_intents": jun.get("intents"),
        "jul_intents": jul.get("intents"),
        "aug_trades": (aug.get("summary") or {}).get("trades"),
        "jun_trades": (jun.get("summary") or {}).get("trades"),
        "jul_trades": (jul.get("summary") or {}).get("trades"),
        "jul_vs_aug_trade_ratio": (
            None
            if not (aug.get("summary") or {}).get("trades")
            else (jul.get("summary") or {}).get("trades")
            / float((aug.get("summary") or {}).get("trades"))
        ),
    }

    exit_mix = {
        period: {
            "daily_close_share": exit_share(w, "daily_close"),
            "margin_stop_share": exit_share(w, "margin_stop"),
            "take_profit_share": exit_share(w, "take_profit"),
            "exit_reasons": (w.get("summary") or {}).get("exit_reasons"),
        }
        for period, w in windows.items()
    }

    # Expectancy contrast on daily_close / margin_stop if trade rows available
    expectancy = {}
    for period, w in windows.items():
        es = w.get("exit_stats") or {}
        expectancy[period] = {
            "daily_close_mean_pnl": (es.get("daily_close") or {}).get("mean_pnl"),
            "margin_stop_mean_pnl": (es.get("margin_stop") or {}).get("mean_pnl"),
            "take_profit_mean_pnl": (es.get("take_profit") or {}).get("mean_pnl"),
            "overall_mean_pnl": w.get("mean_pnl"),
            "overall_median_pnl": w.get("median_pnl"),
        }

    # Hypothesis tests (boolean factual checks — not scores)
    jul_trades = (jul.get("summary") or {}).get("trades") or 0
    aug_trades = (aug.get("summary") or {}).get("trades") or 1
    throughput_collapse = jul_trades < 0.7 * aug_trades

    jul_dc = exit_mix["jul_oos"]["daily_close_share"]
    aug_dc = exit_mix["aug_is"]["daily_close_share"]
    exit_mix_regime_change = abs(jul_dc - aug_dc) > 0.25

    jul_ms = exit_mix["jul_oos"]["margin_stop_share"]
    aug_ms = exit_mix["aug_is"]["margin_stop_share"]
    margin_damage_up = jul_ms > aug_ms + 0.05

    jul_mean = expectancy["jul_oos"].get("overall_mean_pnl")
    aug_mean = expectancy["aug_is"].get("overall_mean_pnl")
    expectancy_worse = (
        jul_mean is not None and aug_mean is not None and jul_mean < aug_mean
    )

    findings = [
        {
            "claim": "July throughput collapsed vs August",
            "supported": bool(throughput_collapse),
            "evidence": throughput,
        },
        {
            "claim": "July exit-mix regime differs sharply from August (daily_close share Δ>25pp)",
            "supported": bool(exit_mix_regime_change),
            "evidence": {
                "aug_daily_close_share": aug_dc,
                "jul_daily_close_share": jul_dc,
            },
        },
        {
            "claim": "July margin_stop share elevated vs August",
            "supported": bool(margin_damage_up),
            "evidence": {"aug_margin_stop_share": aug_ms, "jul_margin_stop_share": jul_ms},
        },
        {
            "claim": "July overall mean trade PnL worse than August",
            "supported": bool(expectancy_worse),
            "evidence": {"aug_mean_pnl": aug_mean, "jul_mean_pnl": jul_mean},
        },
    ]

    conclusion = {
        "hypothesis": (
            "July N1 failure is not explained by throughput collapse or a different "
            "exit-mix regime versus August/June; it is driven by worse expectancy "
            "within the same daily_close-dominated closest-sim path (with elevated "
            "margin_stop damage)."
        ),
        "supported_parts": [f["claim"] for f in findings if f["supported"]],
        "rejected_parts": [f["claim"] for f in findings if not f["supported"]],
        "verdict": None,
        "does_not_establish": [
            "A fix / new parameter set",
            "That N1 is profitable",
            "Tick-level or Live-account fidelity",
            "Which causal market feature made July worse (needs further OOS months or feature study)",
        ],
    }

    # Compose verdict from the hypothesis structure
    if (
        not throughput_collapse
        and not exit_mix_regime_change
        and (margin_damage_up or expectancy_worse)
    ):
        conclusion["verdict"] = (
            "HYPOTHESIS_SUPPORTED_BY_EXISTING_ARTIFACTS: July failed under a similar "
            "daily_close-dominated exit mix with worse expectancy / heavier margin_stop "
            "share — not a throughput collapse."
        )
    elif throughput_collapse:
        conclusion["verdict"] = (
            "HYPOTHESIS_PARTIALLY_REJECTED: July shows material throughput change; "
            "revisit signal-rate before attributing failure only to expectancy."
        )
    else:
        conclusion["verdict"] = (
            "HYPOTHESIS_INCONCLUSIVE_FROM_AGGREGATES: inspect trade-level expectancy "
            "fields and extend to another OOS month before changing strategy knobs."
        )

    return {
        "throughput": throughput,
        "exit_mix": exit_mix,
        "expectancy": expectancy,
        "findings": findings,
        "conclusion": conclusion,
        "next_smallest_followup": (
            "If hypothesis remains supported: run frozen N1 on an additional unseen "
            "calendar month (e.g. May 2026) with identical knobs — no sweeps — to test "
            "whether July is unique calendar fragility. Do not optimize WR/PF."
        ),
    }


def render_md(payload: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append(f"# N1 July Failure Forensic — `{payload['run_id']}`")
    lines.append("")
    lines.append("## Hypothesis")
    lines.append("")
    lines.append(payload["comparison"]["conclusion"]["hypothesis"])
    lines.append("")
    lines.append("## Verdict")
    lines.append("")
    lines.append(payload["comparison"]["conclusion"]["verdict"])
    lines.append("")
    lines.append("## Window summaries")
    lines.append("")
    lines.append("| Window | Trades | WR | PF | MDD | End$ | daily_close | margin_stop | TP |")
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for key in ("aug_is", "jun_oos", "jul_oos"):
        w = payload["windows"][key]
        s = w["summary"]
        er = s.get("exit_reasons") or {}
        lines.append(
            f"| {key} | {s.get('trades')} | "
            f"{(s.get('win_rate') or 0)*100:.1f}% | "
            f"{s.get('profit_factor'):.3f} | "
            f"{(s.get('max_drawdown_pct') or 0)*100:.1f}% | "
            f"{s.get('account'):.2f} | "
            f"{er.get('daily_close', 0)} | {er.get('margin_stop', 0)} | "
            f"{er.get('take_profit', 0)} |"
        )
    lines.append("")
    lines.append("## Findings")
    lines.append("")
    for f in payload["comparison"]["findings"]:
        mark = "SUPPORTED" if f["supported"] else "NOT SUPPORTED"
        lines.append(f"- **{mark}**: {f['claim']}")
    lines.append("")
    lines.append("## What this establishes")
    lines.append("")
    for x in payload["comparison"]["conclusion"]["supported_parts"]:
        lines.append(f"- {x}")
    lines.append("")
    lines.append("## What this does not establish")
    lines.append("")
    for x in payload["comparison"]["conclusion"]["does_not_establish"]:
        lines.append(f"- {x}")
    lines.append("")
    lines.append("## Next follow-up")
    lines.append("")
    lines.append(payload["comparison"]["next_smallest_followup"])
    lines.append("")
    lines.append("## Execution model (unchanged)")
    lines.append("")
    lines.append("- Strategy: alexg8 / N1 ghost_sl_mult=4.0")
    lines.append("- Path: H1 signal + 1m exit (`h1_signal_1m_exit`)")
    lines.append("- Costs: Raw spreads + commission-include-risk (as in source runs)")
    lines.append("- Simulator: FROZEN_FOR_STRATEGY_RESEARCH")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    run_id = f"n1_jul_forensic_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    out_dir = ROOT / "data" / "runs" / "research" / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    windows = {k: analyze_window(k, p) for k, p in SOURCES.items()}
    comparison = compare(windows)
    payload = {
        "run_id": run_id,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "experiment": "n1_july_failure_forensic",
        "read_only": True,
        "mt5_connection": False,
        "strategy_modified": False,
        "simulator_modified": False,
        "gate2_reopened": False,
        "hypothesis": comparison["conclusion"]["hypothesis"],
        "sources": {k: str(v.as_posix()) for k, v in SOURCES.items()},
        "windows": windows,
        "comparison": comparison,
        "cost_assumptions": "Inherited from source N1 runs (Raw spreads + commission risk-include).",
        "is_oos": {
            "aug_is": "IS reference (do not treat as success criterion)",
            "jun_oos": "OOS adjacent",
            "jul_oos": "OOS failure under test",
        },
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "experiment": payload["experiment"],
                "read_only": True,
                "sources": payload["sources"],
                "verdict": comparison["conclusion"]["verdict"],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (out_dir / "report.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    (out_dir / "report.md").write_text(render_md(payload), encoding="utf-8")
    print(json.dumps({"run_id": run_id, "out_dir": str(out_dir), "verdict": comparison["conclusion"]["verdict"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
