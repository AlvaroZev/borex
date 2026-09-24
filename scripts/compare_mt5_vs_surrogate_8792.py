"""Compare today's MT5 H1 vs exported SURROGATE cache for 8792 pairs.

If any mismatch or MT5 unavailable → keep SURROGATE_MT5_HISTORY label.
Never promotes surrogate to EXACT_THEORY_HISTORY without byte/value identity.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIVE = ROOT.parent / "borex_live"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(LIVE))

from dotenv import load_dotenv

load_dotenv(LIVE / ".env")

FIX = ROOT / "data" / "runs" / "mirror_2d_8792" / "reconciliation" / "golden_fixture"
OUT = FIX / "recovery_search"
WINDOW_START = "2026-08-27 01:00:00+00:00"
WINDOW_END = "2026-08-28 23:00:00+00:00"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    man = json.loads((FIX / "h1_surrogate_manifest.json").read_text(encoding="utf-8"))
    report: dict = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "label": "SURROGATE_MT5_HISTORY",
        "exact_identity_proven": False,
        "reason": "",
        "pairs_compared": 0,
        "pairs_identical_in_window": 0,
        "mismatches": [],
        "mt5_ok": False,
    }

    try:
        from borex_live.mt5.client import Mt5Client

        mt5 = Mt5Client()
        mt5.connect()
        if not mt5.connected:
            raise RuntimeError("MT5 not connected")
        report["mt5_ok"] = True
        report["server_offset_seconds"] = getattr(mt5, "server_offset_seconds", None)
    except Exception as exc:
        report["reason"] = f"MT5 unavailable or connect failed: {exc}"
        report["note"] = (
            "Cannot prove identity. Cache remains SURROGATE_MT5_HISTORY."
        )
        (OUT / "mt5_vs_surrogate.json").write_text(
            json.dumps(report, indent=2), encoding="utf-8"
        )
        print(json.dumps(report, indent=2))
        return

    from borex.models.candle import Candle
    import pandas as pd

    def _ts(x):
        t = pd.Timestamp(x)
        return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")

    w0, w1 = _ts(WINDOW_START), _ts(WINDOW_END)
    cache_dir = FIX / "h1_surrogate_cache"
    sample_syms = list((man.get("per_pair") or {}).keys())[:12]  # sample first
    # also include majors
    for must in ("EURUSD=X", "GBPUSD=X", "USDJPY=X", "AUDCAD=X"):
        if must in (man.get("per_pair") or {}) and must not in sample_syms:
            sample_syms.append(must)

    for sym in sample_syms:
        meta = man["per_pair"][sym]
        rows = json.loads((cache_dir / meta["file"]).read_text(encoding="utf-8"))
        cache = {
            str(_ts(r["timestamp"])): (
                float(r["open"]),
                float(r["high"]),
                float(r["low"]),
                float(r["close"]),
                float(r.get("volume") or 0),
            )
            for r in rows
            if w0 <= _ts(r["timestamp"]) <= w1
        }
        try:
            # Request enough bars to cover window + buffer
            live_bars = mt5.fetch_bars(sym, "1h", count=120)
        except Exception as exc:
            report["mismatches"].append({"symbol": sym, "error": str(exc)})
            continue

        report["pairs_compared"] += 1
        live = {
            str(_ts(c.timestamp)): (
                float(c.open),
                float(c.high),
                float(c.low),
                float(c.close),
                float(c.volume or 0),
            )
            for c in live_bars
            if w0 <= _ts(c.timestamp) <= w1
        }
        # Overlap timestamps
        common = sorted(set(cache) & set(live))
        if not common:
            report["mismatches"].append(
                {
                    "symbol": sym,
                    "issue": "no_overlapping_timestamps_in_window",
                    "n_cache": len(cache),
                    "n_live": len(live),
                }
            )
            continue
        diffs = []
        for ts in common:
            if cache[ts] != live[ts]:
                diffs.append(
                    {
                        "ts": ts,
                        "cache": cache[ts],
                        "live_today": live[ts],
                    }
                )
                if len(diffs) >= 5:
                    break
        missing_in_live = sorted(set(cache) - set(live))[:5]
        missing_in_cache = sorted(set(live) - set(cache))[:5]
        if diffs or missing_in_live or missing_in_cache:
            report["mismatches"].append(
                {
                    "symbol": sym,
                    "n_common": len(common),
                    "n_ohlc_diffs_sample": len(diffs),
                    "diffs_sample": diffs,
                    "missing_in_live_sample": missing_in_live,
                    "missing_in_cache_sample": missing_in_cache,
                }
            )
        else:
            report["pairs_identical_in_window"] += 1

    if report["pairs_compared"] == 0:
        report["reason"] = "No pairs successfully compared"
    elif report["mismatches"]:
        report["reason"] = (
            "OHLC/timestamp differences or coverage gaps vs today's MT5 — "
            "identity NOT proven"
        )
    elif report["pairs_identical_in_window"] == report["pairs_compared"]:
        report["reason"] = (
            "Sampled pairs match today's MT5 in-window OHLC, but this still "
            "does NOT prove August Theory identity (warmup length, forming-bar "
            "cutoff at original runtime, server_offset then). Label stays SURROGATE."
        )
        report["exact_identity_proven"] = False

    report["label"] = "SURROGATE_MT5_HISTORY"
    if report["mismatches"] and all(
        m.get("issue") == "no_overlapping_timestamps_in_window"
        for m in report["mismatches"]
    ):
        report["reason"] = (
            "Today's MT5 copy_rates_from_pos(count=120) does not cover the Aug 8792 "
            "window — cannot compare. Identity remains UNPROVEN; label stays "
            "SURROGATE_MT5_HISTORY."
        )
    (OUT / "mt5_vs_surrogate.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    print(json.dumps({k: report[k] for k in report if k != "mismatches"}, indent=2))
    print(f"mismatches: {len(report['mismatches'])}")


if __name__ == "__main__":
    main()
