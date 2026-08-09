#!/usr/bin/env python3
"""Bar-to-bar OHLC comparison: Dukascopy cache vs MT5 for smoke window."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_JSON = ROOT / "data" / "runs" / "smoke_mt5_vs_cache.json"


def hour_key(s: str) -> str:
    ts = pd.Timestamp(s)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return ts.strftime("%Y-%m-%d %H:00")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", type=Path, default=DEFAULT_JSON)
    ap.add_argument("--pip", type=float, default=0.0001, help="pip size for EURUSD-style")
    args = ap.parse_args()

    data = json.loads(args.json.read_text(encoding="utf-8"))
    cache = {b["t"]: b for b in data["ohlc"]["cache"]}
    mt5 = {b["t"]: b for b in data["ohlc"]["mt5"]}
    ck, mk = set(cache), set(mt5)
    both = sorted(ck & mk)
    only_c = sorted(ck - mk)
    only_m = sorted(mk - ck)

    print(
        f"Window: {data['window_start'][:10]} -> {data['window_end'][:10]} "
        f"{data['symbol']}"
    )
    print(
        f"bars: cache={len(cache)} mt5={len(mt5)} shared={len(both)} "
        f"only_cache={len(only_c)} only_mt5={len(only_m)}"
    )
    if only_c[:8]:
        print("only_cache sample:", only_c[:8])
    if only_m[:8]:
        print("only_mt5 sample:", only_m[:8])

    # Normalize timestamps to hour keys in case of tz/format drift
    def norm_map(src: dict) -> dict:
        out = {}
        for t, b in src.items():
            out[hour_key(t)] = dict(b, t=t)
        return out

    cn, mn = norm_map(cache), norm_map(mt5)
    both_h = sorted(set(cn) & set(mn))
    only_ch = sorted(set(cn) - set(mn))
    only_mh = sorted(set(mn) - set(cn))
    print(
        f"by UTC hour: shared={len(both_h)} only_cache={len(only_ch)} "
        f"only_mt5={len(only_mh)}"
    )

    rows = []
    for h in both_h:
        a, b = cn[h], mn[h]
        d = {k: abs(a[k] - b[k]) for k in ("o", "h", "l", "c")}
        rng_a, rng_b = a["h"] - a["l"], b["h"] - b["l"]
        rows.append(
            {
                "h": h,
                **{f"d{k.upper()}": d[k] for k in d},
                "dRange": abs(rng_a - rng_b),
                "range_cache": rng_a,
                "range_mt5": rng_b,
                "a": a,
                "b": b,
            }
        )

    def summarize(key: str) -> None:
        vals = [r[key] for r in rows]
        nz = sum(1 for v in vals if v > 1e-12)
        ge_pip = sum(1 for v in vals if v >= args.pip)
        ge_5 = sum(1 for v in vals if v >= 5 * args.pip)
        print(
            f"{key}: mean={sum(vals)/len(vals):.6g} max={max(vals):.6g} "
            f"nonzero={nz}/{len(vals)} >=1pip={ge_pip} >=5pip={ge_5}"
        )

    print("\nAbs diffs on shared hours:")
    for k in ("dO", "dH", "dL", "dC", "dRange"):
        summarize(k)

    exact = sum(
        1
        for r in rows
        if max(r["dO"], r["dH"], r["dL"], r["dC"]) < 1e-12
    )
    print(f"\nExact OHLC match: {exact}/{len(rows)}")

    worst = sorted(
        rows, key=lambda r: max(r["dO"], r["dH"], r["dL"], r["dC"]), reverse=True
    )[:15]
    print("\nWorst OHLC mismatches (cache / mt5):")
    for r in worst:
        m = max(r["dO"], r["dH"], r["dL"], r["dC"])
        if m < 1e-12:
            continue
        a, b = r["a"], r["b"]
        print(
            f"  {r['h']} maxd={m:.6g} "
            f"O {a['o']:.5f}/{b['o']:.5f} "
            f"H {a['h']:.5f}/{b['h']:.5f} "
            f"L {a['l']:.5f}/{b['l']:.5f} "
            f"C {a['c']:.5f}/{b['c']:.5f} "
            f"range {r['range_cache']:.5f}/{r['range_mt5']:.5f}"
        )

    # Trade entry hour overlap
    for mode in ("off", "on"):
        c_trades = data["runs"][f"cache_samebar_{mode}"]["trade_list"]
        m_trades = data["runs"][f"mt5_samebar_{mode}"]["trade_list"]
        ch = {hour_key(t["entry_time"]) for t in c_trades}
        mh = {hour_key(t["entry_time"]) for t in m_trades}
        print(f"\nTrades samebar_{mode}: cache={len(c_trades)} mt5={len(m_trades)}")
        print(f"  shared entry hours: {sorted(ch & mh)}")
        print(f"  only cache hours:   {sorted(ch - mh)}")
        print(f"  only mt5 hours:     {sorted(mh - ch)}")
        # For shared hours, compare entry prices
        c_by = {hour_key(t["entry_time"]): t for t in c_trades}
        m_by = {hour_key(t["entry_time"]): t for t in m_trades}
        for h in sorted(ch & mh):
            ct, mt = c_by[h], m_by[h]
            print(
                f"  @{h} cache {ct['side']} @ {ct['entry_price']:.5f} "
                f"vs mt5 {mt['side']} @ {mt['entry_price']:.5f} "
                f"dPx={abs(ct['entry_price']-mt['entry_price']):.6g}"
            )

    # Write CSV for inspection
    out = args.json.with_name("smoke_bar_diff.csv")
    import csv

    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "hour",
                "dO",
                "dH",
                "dL",
                "dC",
                "dRange",
                "o_cache",
                "o_mt5",
                "h_cache",
                "h_mt5",
                "l_cache",
                "l_mt5",
                "c_cache",
                "c_mt5",
                "range_cache",
                "range_mt5",
            ],
        )
        w.writeheader()
        for r in rows:
            a, b = r["a"], r["b"]
            w.writerow(
                {
                    "hour": r["h"],
                    "dO": r["dO"],
                    "dH": r["dH"],
                    "dL": r["dL"],
                    "dC": r["dC"],
                    "dRange": r["dRange"],
                    "o_cache": a["o"],
                    "o_mt5": b["o"],
                    "h_cache": a["h"],
                    "h_mt5": b["h"],
                    "l_cache": a["l"],
                    "l_mt5": b["l"],
                    "c_cache": a["c"],
                    "c_mt5": b["c"],
                    "range_cache": r["range_cache"],
                    "range_mt5": r["range_mt5"],
                }
            )
    print(f"\nWrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
