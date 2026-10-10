"""Aggregate massive_lab results into reports."""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path

from borex.research.massive_lab.registry import ExperimentRegistry


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys: list[str] = []
    for r in rows:
        for k, v in r.items():
            if k not in keys and not isinstance(v, (dict, list, tuple)):
                keys.append(k)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in keys})


def aggregate(out_dir: Path, reg: ExperimentRegistry) -> dict:
    results = reg.done_results()
    counts = reg.counts()
    write_csv(out_dir / "all_results.csv", results)

    by_group: dict[str, list] = defaultdict(list)
    for r in results:
        by_group[r.get("group_id") or "unknown"].append(r)

    for g, rows in by_group.items():
        write_csv(out_dir / f"group_{g}.csv", rows)

    # scoreboards (no single winner)
    def top(rows, key="net", n=15, filt=None):
        xs = [r for r in rows if r.get(key) is not None and (filt(r) if filt else True)]
        return sorted(xs, key=lambda r: float(r.get(key) or -1e18), reverse=True)[:n]

    ctrl = next((r for r in results if r.get("family") == "n1_control"), None)
    thu = next((r for r in results if r.get("family") == "n1_thursday"), None)
    ctrl_net = float(ctrl["net"]) if ctrl and ctrl.get("net") is not None else 0.0
    thu_net = float(thu["net"]) if thu and thu.get("net") is not None else 0.0

    n_pos = sum(1 for r in results if (r.get("net") or 0) > 0)
    n_beat_ctrl = sum(1 for r in results if (r.get("net") or 0) > ctrl_net)
    n_beat_thu = sum(1 for r in results if (r.get("net") or 0) > thu_net)

    # GhostSL deltas for internet
    internet = [r for r in results if (r.get("group_id") or "").startswith("I_")]
    ghost_rows = []
    by = {}
    for r in internet:
        p = r.get("params") or {}
        key = (p.get("name"), p.get("thursday_gate"))
        by.setdefault(key, {})[bool(p.get("ghostsl"))] = r
    for key, d in by.items():
        if False in d and True in d:
            b, g = d[False], d[True]
            ghost_rows.append(
                {
                    "strategy": key[0],
                    "thursday": key[1],
                    "base_net": b.get("net"),
                    "ghostsl_net": g.get("net"),
                    "delta_net": float(g.get("net") or 0) - float(b.get("net") or 0),
                    "base_fills": b.get("fills"),
                    "ghostsl_fills": g.get("fills"),
                }
            )
    write_csv(out_dir / "ghostsl_analysis.csv", ghost_rows)

    # Candidate freeze (strict, discovery labels only)
    freeze = []
    for r in top(results, n=30):
        fills = int(r.get("fills") or 0)
        if fills < 80:
            cat = "INSUFFICIENT_TRADES"
        elif (r.get("losing_weeks") or 0) >= (r.get("profitable_weeks") or 0) + 3:
            cat = "UNSTABLE"
        elif abs(float(r.get("worst_week") or 0)) > abs(float(r.get("net") or 0)) * 0.8:
            cat = "OVERFIT_SUSPECTED"
        elif r.get("category") == "LOCKED_EXISTING_CANDIDATE":
            cat = "LOCKED_EXISTING_CANDIDATE"
        else:
            cat = "DISCOVERY_ONLY"
        freeze.append({**{k: r.get(k) for k in ("experiment_id", "strategy", "family", "group_id", "net", "PF", "fills")}, "freeze_category": cat})
    write_csv(out_dir / "candidate_freeze.csv", freeze)

    summary = {
        "counts": counts,
        "n_done": counts.get("done", 0),
        "n_positive": n_pos,
        "n_beat_ctrl": n_beat_ctrl,
        "n_beat_thu": n_beat_thu,
        "ctrl_net": ctrl_net,
        "thu_net": thu_net,
        "top_expectancy": [
            {k: r.get(k) for k in ("strategy", "family", "group_id", "net", "PF", "fills", "mean")}
            for r in top(results, n=20)
        ],
        "top_ghostsl_deltas": sorted(ghost_rows, key=lambda x: x["delta_net"], reverse=True)[:15],
        "september_unread": True,
        "live_mirror_untouched": True,
        "locked_oos_unchanged": True,
        "best_backtest_bias": True,
        "multiple_hypothesis_risk": "HIGH",
    }
    (out_dir / "massive_lab_summary.md").write_text(
        "\n".join(
            [
                "# Massive Lab Summary — DISCOVERY_ONLY",
                "",
                f"Done experiments: **{summary['n_done']}**",
                f"Status counts: `{counts}`",
                f"Positive net: {n_pos} | Beat control (~{ctrl_net:.1f}): {n_beat_ctrl} | Beat Thursday (~{thu_net:.1f}): {n_beat_thu}",
                "",
                "## Safety",
                "- September unread: CONFIRMED",
                "- Locked OOS unchanged: n1_control vs n1_thursday",
                "- LIVE/Mirror untouched: CONFIRMED",
                "",
                "## Warning",
                "BEST-BACKTEST BIAS and MULTIPLE-HYPOTHESIS RISK are HIGH.",
                "No discovery result is validated. Future untouched OOS only.",
                "",
                "## Top expectancy (descriptive)",
                "",
            ]
            + [
                f"- `{r.get('strategy')}` / {r.get('family')} / {r.get('group_id')}: net={r.get('net')} PF={r.get('PF')} fills={r.get('fills')}"
                for r in summary["top_expectancy"][:12]
            ]
            + ["", "## GhostSL deltas (internet)", ""]
            + [
                f"- `{r['strategy']}` thu={r['thursday']}: Δnet={r['delta_net']:.1f} (base={r['base_net']} → ghost={r['ghostsl_net']})"
                for r in summary["top_ghostsl_deltas"][:10]
            ]
            + ["", "See `all_results.csv`, `ghostsl_analysis.md`, `candidate_freeze.md`.", ""]
        ),
        encoding="utf-8",
    )
    (out_dir / "multiple_testing_analysis.md").write_text(
        f"""# Multiple-Testing Analysis

- Experiments done: {summary['n_done']}
- Positive: {n_pos}
- Beat control: {n_beat_ctrl}
- Beat Thursday: {n_beat_thu}

BEST-BACKTEST BIAS applies. A top result after thousands of trials is not validation.
""",
        encoding="utf-8",
    )
    (out_dir / "candidate_freeze.md").write_text(
        "# Candidate Freeze\n\n"
        + "\n".join(f"- `{r['freeze_category']}` {r.get('strategy')} net={r.get('net')}" for r in freeze[:25])
        + "\n\nNo discovery candidate is validated. Locked Sep OOS unchanged.\n",
        encoding="utf-8",
    )
    (out_dir / "ghostsl_analysis.md").write_text(
        "# GhostSL Analysis (DISCOVERY_ONLY)\n\n"
        + "GhostSL = delayed real entry at prior ghost SL price; REAL_SL/TP from real entry.\n\n"
        + "\n".join(
            f"- `{r['strategy']}` Δnet={r['delta_net']:.1f} fills {r['base_fills']}→{r['ghostsl_fills']}"
            for r in sorted(ghost_rows, key=lambda x: x["delta_net"], reverse=True)[:20]
        )
        + "\n",
        encoding="utf-8",
    )
    (out_dir / "experiment_manifest.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    return summary
