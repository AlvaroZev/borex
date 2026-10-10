#!/usr/bin/env python3
"""Frozen N1 May 2026 OOS — data integrity gate (fail-closed).

Does NOT run a degraded-universe or substituted-source backtest.
Writes a research artifact classifying May as INSUFFICIENT_EVIDENCE when
MT5 M1 history and offline downloads cannot support the pinned 60-pair N1
universe for May under existing conventions.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
JUL_INDEX = ROOT / "data/runs/mt5_1m_n1_oos_jul2026/index.json"
JUN_REPORT = ROOT / "data/runs/mt5_1m_n1_oos_jun2026/sl_4p0_lev_500.json"
JUL_REPORT = ROOT / "data/runs/mt5_1m_n1_oos_jul2026/sl_4p0_lev_500.json"
AUG_REPORT = ROOT / "data/runs/mt5_1m_n1_tpfactor/sl_4p0_lev_500_rr_1p0.json"

# Same temporal convention as Jun→Jul (day-3 03:00 UTC boundaries).
MAY_EVAL_START = "2026-05-03T03:00:00+00:00"
MAY_END = "2026-06-03T03:00:00+00:00"

FROZEN_KNOBS = {
    "strategy": "alexg8",
    "ghost_sl_mult": 4.0,
    "leverage": 500.0,
    "rr_mode": "dynamic",
    "true_sl_rr": 2.0,
    "rr_factor": 1.0,
    "rr_min": 2.0,
    "rr_max": 5.0,
    "risk_include_commission": True,
    "commission_at_entry": False,
    "commission_per_lot": 7.0,
    "force_flat_friday": False,
    "force_flat_daily": True,
    "intra_hour_sl": False,
    "path": "h1_signal_1m_exit",
    "max_positions": 0,
    "signal_exec_split": True,
    "capital": 1000.0,
}


def _git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=str(ROOT), text=True
        ).strip()
    except Exception:
        return "unknown"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _window_metrics(path: Path, label: str) -> dict:
    d = _load(path)
    s = d.get("summary") or {}
    trades = [t for t in (d.get("trades") or []) if not t.get("warmup")]
    pnls = [float(t["pnl"]) for t in trades if t.get("pnl") is not None]
    er = dict(s.get("exit_reasons") or {})
    n = sum(int(v) for v in er.values()) or max(1, int(s.get("trades") or 1))
    mean_pnl = (sum(pnls) / len(pnls)) if pnls else None
    med_pnl = None
    if pnls:
        sp = sorted(pnls)
        mid = len(sp) // 2
        med_pnl = float(sp[mid] if len(sp) % 2 else 0.5 * (sp[mid - 1] + sp[mid]))
    entry = pd.Timestamp(d.get("entry_start"))
    last = pd.Timestamp(d.get("last_bar"))
    days = max(1.0, (last - entry).total_seconds() / 86400.0)
    return {
        "label": label,
        "intents": d.get("intents"),
        "fills": s.get("trades"),
        "trades_per_day": (s.get("trades") or 0) / days,
        "wr": s.get("win_rate"),
        "pf": s.get("profit_factor"),
        "net_pnl": s.get("net_pnl"),
        "mdd": s.get("max_drawdown_pct"),
        "ending_equity": s.get("account"),
        "mean_trade_pnl": mean_pnl,
        "median_trade_pnl": med_pnl,
        "daily_close_share": er.get("daily_close", 0) / n,
        "margin_stop_share": er.get("margin_stop", 0) / n,
        "take_profit_share": er.get("take_profit", 0) / n,
        "exit_reasons": er,
        "entry_start": d.get("entry_start"),
        "last_bar": d.get("last_bar"),
    }


def _probe_mt5_m1_depth() -> dict:
    load_dotenv(ROOT.parent / "borex_live" / ".env")
    import sys

    live = str(ROOT.parent / "borex_live")
    if live not in sys.path:
        sys.path.insert(0, live)
    from borex_live.mt5.client import Mt5Client
    from borex.data.mt5_time import utc_to_mt5_request

    path = os.environ.get("MIRROR_MT5_PATH") or os.environ.get("MT5_PATH", "")
    client = Mt5Client(path=path)
    client.connect()
    try:
        off = int(getattr(client, "server_offset_seconds", 0) or 0)
        sym = client.ensure_symbol("EURUSD=X")
        end = datetime.now(timezone.utc)
        depth = {}
        for days in (30, 60, 90, 120):
            start = end.replace(microsecond=0) - __import__("datetime").timedelta(days=days)
            rates = client._mt5.copy_rates_range(
                sym,
                client._tf("1m"),
                utc_to_mt5_request(start, off),
                utc_to_mt5_request(end, off),
            )
            depth[f"last_{days}d"] = 0 if rates is None else int(len(rates))
        # Explicit May window
        may_a = datetime.fromisoformat(MAY_EVAL_START)
        may_b = datetime.fromisoformat(MAY_END)
        may_rates = client._mt5.copy_rates_range(
            sym,
            client._tf("1m"),
            utc_to_mt5_request(may_a, off),
            utc_to_mt5_request(may_b, off),
        )
        return {
            "connected": True,
            "path": path,
            "offset_seconds": off,
            "probe_symbol": "EURUSD=X",
            "depth_counts": depth,
            "may_window_bars": 0 if may_rates is None else int(len(may_rates)),
            "may_window": {"start": MAY_EVAL_START, "end": MAY_END},
            "orders_placed": False,
            "note": "Diagnostic historical read only; no trading.",
        }
    finally:
        client._connected = False
        client._mt5 = None


def _download_coverage(universe: list[str]) -> dict:
    dl = ROOT / "download"
    have: list[str] = []
    missing: list[str] = []
    for s in universe:
        base = s.replace("=X", "").lower()
        hits = [h for h in dl.glob(f"{base}-m1-bid-2026-01-01-*.csv") if h.stat().st_size > 0]
        (have if hits else missing).append(s)
    return {
        "source": "download/*-m1-bid-2026-01-01-*.csv",
        "universe_n": len(universe),
        "have_n": len(have),
        "missing_n": len(missing),
        "have": have,
        "missing": missing,
        "substituted": False,
        "note": (
            "Offline CSV coverage is incomplete vs pinned N1 universe. "
            "Per research policy, do not silently substitute this source."
        ),
    }


def _cache_may_m1_coverage(universe: list[str]) -> dict:
    from borex.viewerMT5.mt5_feed import _cache_path

    start = datetime.fromisoformat(MAY_EVAL_START)
    end = datetime.fromisoformat(MAY_END)
    ok = 0
    sparse = []
    for s in universe:
        f = _cache_path(s, "1m")
        if not f.is_file():
            sparse.append({"symbol": s, "bars": 0, "reason": "missing_file"})
            continue
        df = pd.read_parquet(f, columns=["timestamp"])
        t = pd.to_datetime(df["timestamp"], utc=True)
        n = int(((t >= start) & (t < end)).sum())
        # Full May forex M1 ≈ 30d * ~1440 ≈ 40k+; require substantial coverage
        if n >= 20_000:
            ok += 1
        else:
            sparse.append({"symbol": s, "bars": n, "reason": "insufficient_may_slice"})
    return {
        "cache_root": "data/cache/mt5/utc_v2",
        "universe_n": len(universe),
        "sufficient_n": ok,
        "insufficient_n": len(sparse),
        "insufficient_sample": sparse[:10],
        "note": "Existing MT5 parquet cache does not contain May 2026 M1 for the N1 window.",
    }


def render_md(payload: dict) -> str:
    lines: list[str] = []
    lines.append(f"# Frozen N1 May 2026 OOS — `{payload['run_id']}`")
    lines.append("")
    lines.append("## Classification")
    lines.append("")
    lines.append(f"**`{payload['classification']}`**")
    lines.append("")
    lines.append(payload["classification_rationale"])
    lines.append("")
    lines.append("## Data / config integrity")
    lines.append("")
    lines.append(f"- Git commit: `{payload['git_commit']}`")
    lines.append(f"- Simulator status: `{payload['simulator_status']}`")
    lines.append(f"- Strategy: frozen N1 knobs (alexg8, ghost_sl_mult=4.0, lev=500)")
    lines.append(f"- Planned period: `{MAY_EVAL_START}` → `{MAY_END}`")
    lines.append(f"- Pinned universe: **{payload['universe_n']}** pairs from July N1 index")
    lines.append("- Cost model: Raw spreads + $7/lot commission (as N1); not re-tuned")
    lines.append("- Backtest executed: **false** (fail-closed before run)")
    lines.append("")
    lines.append("### MT5 M1 availability")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(payload["integrity"]["mt5_m1_depth"], indent=2))
    lines.append("```")
    lines.append("")
    lines.append("### Cache May M1 coverage")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(payload["integrity"]["cache_may_m1"], indent=2))
    lines.append("```")
    lines.append("")
    lines.append("### Offline download coverage (not used)")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(payload["integrity"]["download_coverage"], indent=2))
    lines.append("```")
    lines.append("")
    lines.append("## Comparison table")
    lines.append("")
    lines.append(
        "| Metric | May OOS | June OOS | July OOS | August IS |"
    )
    lines.append("| --- | ---: | ---: | ---: | ---: |")
    rows = [
        ("Intents", "intents"),
        ("Fills", "fills"),
        ("Trades/day", "trades_per_day"),
        ("WR", "wr"),
        ("PF", "pf"),
        ("Net PnL", "net_pnl"),
        ("MDD", "mdd"),
        ("Mean trade PnL", "mean_trade_pnl"),
        ("Daily-close share", "daily_close_share"),
        ("Margin-stop share", "margin_stop_share"),
    ]
    may = payload["windows"]["may_oos"]
    for label, key in rows:
        def fmt(w, k=key):
            v = w.get(k)
            if v is None:
                return "N/A"
            if k in ("wr", "mdd", "daily_close_share", "margin_stop_share"):
                return f"{100*float(v):.1f}%"
            if k in ("pf", "trades_per_day", "mean_trade_pnl", "net_pnl"):
                return f"{float(v):.3f}" if k != "net_pnl" else f"{float(v):.2f}"
            return str(v)

        lines.append(
            f"| {label} | {fmt(may)} | {fmt(payload['windows']['jun_oos'])} | "
            f"{fmt(payload['windows']['jul_oos'])} | {fmt(payload['windows']['aug_is'])} |"
        )
    lines.append("")
    lines.append("## What this establishes")
    lines.append("")
    for x in payload["establishes"]:
        lines.append(f"- {x}")
    lines.append("")
    lines.append("## What this does not establish")
    lines.append("")
    for x in payload["does_not_establish"]:
        lines.append(f"- {x}")
    lines.append("")
    lines.append("## Next research question")
    lines.append("")
    lines.append(payload["next_research_question"])
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    run_id = f"n1_may_oos_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    out = ROOT / "data" / "runs" / "research" / run_id
    out.mkdir(parents=True, exist_ok=True)

    jul_idx = _load(JUL_INDEX)
    universe = sorted((jul_idx.get("spread_pips_by_symbol") or {}).keys())
    commit = _git_commit()

    mt5_probe = _probe_mt5_m1_depth()
    cache_cov = _cache_may_m1_coverage(universe)
    dl_cov = _download_coverage(universe)

    may_runnable = (
        mt5_probe.get("may_window_bars", 0) >= 20_000
        or cache_cov.get("sufficient_n", 0) == len(universe)
    )

    windows = {
        "may_oos": {
            "label": "may_oos",
            "intents": None,
            "fills": None,
            "trades_per_day": None,
            "wr": None,
            "pf": None,
            "net_pnl": None,
            "mdd": None,
            "ending_equity": None,
            "mean_trade_pnl": None,
            "median_trade_pnl": None,
            "daily_close_share": None,
            "margin_stop_share": None,
            "take_profit_share": None,
            "exit_reasons": None,
            "status": "NOT_RUN_FAIL_CLOSED",
        },
        "jun_oos": _window_metrics(JUN_REPORT, "jun_oos"),
        "jul_oos": _window_metrics(JUL_REPORT, "jul_oos"),
        "aug_is": _window_metrics(AUG_REPORT, "aug_is"),
    }

    if may_runnable:
        classification = "INSUFFICIENT_EVIDENCE"
        rationale = (
            "Unexpected: integrity gate thought May runnable, but this script only "
            "implements the fail-closed path. Do not auto-continue."
        )
    else:
        classification = "INSUFFICIENT_EVIDENCE"
        rationale = (
            "May 2026 M1 path data is unavailable for the pinned 60-pair N1 universe "
            "under existing conventions. MT5 terminal M1 history only covers ~60 days "
            f"(May window bars for EURUSD probe={mt5_probe.get('may_window_bars')}). "
            f"Parquet cache has sufficient May M1 for {cache_cov.get('sufficient_n')}/{len(universe)} pairs. "
            f"Offline download CSVs cover {dl_cov.get('have_n')}/{len(universe)} pairs and were "
            "NOT substituted. Fail-closed: no May backtest executed."
        )

    payload = {
        "run_id": run_id,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "experiment": "n1_may_oos_frozen_validation",
        "git_commit": commit,
        "simulator_status": "FROZEN_FOR_STRATEGY_RESEARCH",
        "strategy_modified": False,
        "simulator_modified": False,
        "parameter_sweep": False,
        "gate2_reopened": False,
        "backtest_executed": False,
        "frozen_knobs": FROZEN_KNOBS,
        "planned_period": {"eval_start": MAY_EVAL_START, "end": MAY_END},
        "universe_n": len(universe),
        "universe_source": str(JUL_INDEX.as_posix()),
        "integrity": {
            "mt5_m1_depth": mt5_probe,
            "cache_may_m1": cache_cov,
            "download_coverage": dl_cov,
            "may_runnable": may_runnable,
        },
        "windows": windows,
        "classification": classification,
        "classification_rationale": rationale,
        "establishes": [
            "May 2026 cannot currently be evaluated under the exact frozen N1 "
            "closest-sim path with the pinned 60-pair universe.",
            "MT5 M1 terminal depth is insufficient to reach May from the present date.",
            "Partial offline CSVs exist but are incomplete and were not substituted.",
        ],
        "does_not_establish": [
            "Whether May resembles July's failure mode",
            "Whether July is an isolated calendar failure",
            "N1 profitability or robustness",
            "Any parameter change recommendation",
        ],
        "next_research_question": (
            "How can May 2026 (or another fully covered unseen month) be obtained "
            "under the same MT5/closest-sim M1 provenance as Jun/Jul/Aug without "
            "substituting HistData or shrinking the pinned universe? Until then, "
            "do not reinterpret July in isolation as proven unique or proven systemic."
        ),
    }

    (out / "manifest.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "git_commit": commit,
                "classification": classification,
                "backtest_executed": False,
                "planned_period": payload["planned_period"],
                "frozen_knobs": FROZEN_KNOBS,
                "universe_n": len(universe),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (out / "integrity.json").write_text(
        json.dumps(payload["integrity"], indent=2) + "\n", encoding="utf-8"
    )
    (out / "report.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    (out / "report.md").write_text(render_md(payload), encoding="utf-8")
    print(
        json.dumps(
            {
                "run_id": run_id,
                "out_dir": str(out),
                "classification": classification,
                "backtest_executed": False,
                "may_window_bars": mt5_probe.get("may_window_bars"),
                "cache_sufficient": cache_cov.get("sufficient_n"),
                "download_have": dl_cov.get("have_n"),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
