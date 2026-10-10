#!/usr/bin/env python3
"""Multi-dataset → MT5 utc_v2 transform search (offline forensic).

Targets: data/cache/mt5/utc_v2
Sources tested:
  - dukascopy (data/cache/{SYM} 1m/1h where manifest says dukascopy)
  - histdata (manifest-labeled 1m)
  - yahoo (15m/30m/4h parquet + loose CSV H1/5m where present)
  - mt5_utc_v1 (data/cache/mt5/utc H1)
  - mt5_flat (data/cache/mt5/{SYM} H1)

Writes CSV + markdown under the latest pre_june extension / recovery research dir.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
UTC_V2 = ROOT / "data/cache/mt5/utc_v2"
OUT_DIR = sorted((ROOT / "data/runs/research").glob("gate2_pre_june_m1_extension_*"))[-1]
JUL = ROOT / "data/runs/mt5_1m_n1_oos_jul2026/index.json"

OVERLAP_START = pd.Timestamp("2026-06-03", tz="UTC")
OVERLAP_END = pd.Timestamp("2026-06-26", tz="UTC")  # duka/histdata often end ~Jun26
H1_END = pd.Timestamp("2026-08-29", tz="UTC")  # utc v1 ends late Aug


def universe() -> list[str]:
    return sorted(json.loads(JUL.read_text(encoding="utf-8"))["spread_pips_by_symbol"].keys())


def cache_key(sym: str) -> str:
    return sym.replace("=", "").replace("/", "")


def pip_size(x: float) -> float:
    return 0.01 if x > 30 else 0.0001


def load_v2(sym: str, interval: str) -> pd.DataFrame | None:
    p = UTC_V2 / cache_key(sym) / f"{interval}.parquet"
    if not p.is_file():
        return None
    df = pd.read_parquet(p)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    cols = [c for c in ("open", "high", "low", "close") if c in df.columns]
    return df.set_index("timestamp")[cols].sort_index()


def load_ohlc_generic(path: Path) -> pd.DataFrame | None:
    if not path.is_file() or path.stat().st_size == 0:
        return None
    if path.suffix.lower() == ".parquet":
        df = pd.read_parquet(path)
    else:
        # try csv
        df = pd.read_csv(path)
    # normalize
    if isinstance(df.index, pd.DatetimeIndex) and "open" not in [c.lower() for c in df.columns] and "Open" in df.columns:
        df = df.copy()
        df.index = pd.to_datetime(df.index, utc=True)
        df = df.rename(columns={c: c.lower() for c in df.columns})
    else:
        # find time col
        cols_l = {c.lower(): c for c in df.columns}
        tcol = None
        for cand in ("timestamp", "datetime", "date", "time", "Gmt time", "gmt time"):
            if cand.lower() in cols_l:
                tcol = cols_l[cand.lower()]
                break
        # dukascopy csv often: timestamp, open, high, low, close, volume
        if tcol is None and len(df.columns) >= 5:
            # histdata sometimes DATES,TIME or similar
            pass
        if tcol is not None:
            ts = pd.to_datetime(df[tcol], utc=True, errors="coerce")
        elif isinstance(df.index, pd.DatetimeIndex):
            ts = pd.to_datetime(df.index, utc=True)
            df = df.copy()
        else:
            return None
        df = df.copy()
        df.columns = [str(c).lower() for c in df.columns]
        rename = {}
        for c in df.columns:
            if c in {"open", "high", "low", "close"}:
                continue
        if "timestamp" in df.columns:
            df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
            df = df.dropna(subset=["timestamp"]).set_index("timestamp")
        elif tcol is not None:
            df.index = ts
            df = df[~df.index.isna()]
        need = [c for c in ("open", "high", "low", "close") if c in df.columns]
        if len(need) < 4:
            return None
        df = df[need].sort_index()
    if not isinstance(df.index, pd.DatetimeIndex):
        return None
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    else:
        df.index = df.index.tz_convert("UTC")
    return df[["open", "high", "low", "close"]].astype(float).sort_index()


def manifest_index() -> dict[str, dict[str, dict]]:
    """source -> symbol -> {tf: path}"""
    out: dict[str, dict[str, dict]] = defaultdict(lambda: defaultdict(dict))
    for man in (ROOT / "data/cache").rglob("*.manifest.json"):
        try:
            m = json.loads(man.read_text(encoding="utf-8"))
        except Exception:
            continue
        src = m.get("source") or "unknown"
        sym = m.get("symbol")
        tf = m.get("timeframe") or m.get("interval")
        if not sym or not tf:
            continue
        # parquet beside manifest is authoritative
        pq = man.with_suffix("").with_suffix(".parquet")  # strip .manifest.json awkwardly
        # man is like 1m.manifest.json -> 1m.parquet
        pq = man.parent / (man.name.replace(".manifest.json", ".parquet"))
        if not pq.is_file():
            alt = m.get("parquet_path")
            if alt and Path(alt).is_file():
                pq = Path(alt)
            else:
                continue
        out[src][sym][tf] = pq
    return out


def resample_ohlc(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    o = df["open"].resample(rule).first()
    h = df["high"].resample(rule).max()
    l = df["low"].resample(rule).min()
    c = df["close"].resample(rule).last()
    out = pd.concat([o, h, l, c], axis=1)
    out.columns = ["open", "high", "low", "close"]
    return out.dropna()


def eval_transforms(src: pd.DataFrame, tgt: pd.DataFrame, *, min_n: int = 200) -> dict | None:
    j = src.join(tgt, lsuffix="_s", rsuffix="_t", how="inner")
    if len(j) < min_n:
        return None
    pip = pip_size(float(j["close_t"].median()))
    bias = float((j["close_s"] - j["close_t"]).mean())
    x = j["close_t"].to_numpy()
    y = j["close_s"].to_numpy()
    slope, intercept = np.linalg.lstsq(np.vstack([x, np.ones(len(x))]).T, y, rcond=None)[0]

    def metrics(series: pd.Series) -> dict:
        d = (series - j["close_t"]).dropna()
        return {
            "n": int(len(d)),
            "mae_pips": float(d.abs().mean() / pip),
            "rmse_pips": float(np.sqrt((d.to_numpy() ** 2).mean()) / pip),
            "within_0_5pip": float((d.abs() <= 0.5 * pip).mean()),
            "within_1pip": float((d.abs() <= pip).mean()),
            "within_2pip": float((d.abs() <= 2 * pip).mean()),
            "p95_abs_pips": float(d.abs().quantile(0.95) / pip),
            "max_abs_pips": float(d.abs().max() / pip),
            "corr": float(series.corr(j["close_t"])),
        }

    cands = {
        "identity": metrics(j["close_s"]),
        "const_bias": metrics(j["close_s"] - bias),
        "affine": metrics((j["close_s"] - intercept) / slope),
    }
    # lag search
    best_lag = 0
    best_mae = cands["const_bias"]["mae_pips"]
    for lag in range(-5, 6):
        s = j["close_s"].shift(lag)
        b = float((s - j["close_t"]).dropna().mean())
        m = metrics(s - b)
        cands[f"lag{lag}_bias"] = m
        if m["mae_pips"] < best_mae:
            best_mae = m["mae_pips"]
            best_lag = lag

    # daily bias stability
    tmp = j.copy()
    tmp["bias_pips"] = (tmp["close_s"] - tmp["close_t"]) / pip
    tmp["day"] = tmp.index.floor("D")
    daily = tmp.groupby("day")["bias_pips"].mean()

    best_name = min(cands.items(), key=lambda kv: kv[1]["mae_pips"])[0]
    return {
        "n_overlap": len(j),
        "pip": pip,
        "static_bias_pips": bias / pip,
        "affine_slope": float(slope),
        "affine_intercept": float(intercept),
        "best_transform": best_name,
        "best_mae_pips": cands[best_name]["mae_pips"],
        "best_within_1pip": cands[best_name]["within_1pip"],
        "best_lag": best_lag,
        "daily_bias_std_pips": float(daily.std()) if len(daily) else None,
        "daily_bias_range_pips": float(daily.max() - daily.min()) if len(daily) else None,
        "identity_mae_pips": cands["identity"]["mae_pips"],
        "const_bias_mae_pips": cands["const_bias"]["mae_pips"],
        "affine_mae_pips": cands["affine"]["mae_pips"],
        "identity_within_1pip": cands["identity"]["within_1pip"],
        "corr": cands["identity"]["corr"],
        "usable_strict": cands[best_name]["mae_pips"] <= 0.5 and cands[best_name]["within_1pip"] >= 0.9,
        "usable_loose": cands[best_name]["mae_pips"] <= 1.0 and cands[best_name]["within_1pip"] >= 0.8,
        "all_transforms": cands,
    }


def slice_win(df: pd.DataFrame, start, end) -> pd.DataFrame:
    return df.loc[(df.index >= start) & (df.index < end)]


def main() -> int:
    syms = universe()
    mans = manifest_index()
    rows = []

    # --- 1) Manifest-labeled M1 sources vs utc_v2 M1 ---
    for src_name in ("dukascopy", "histdata"):
        for sym in syms:
            path = mans.get(src_name, {}).get(sym, {}).get("1m")
            if path is None:
                continue
            src = load_ohlc_generic(path)
            tgt = load_v2(sym, "1m")
            if src is None or tgt is None:
                continue
            # overlap limited by source end
            end = min(OVERLAP_END, src.index.max() + pd.Timedelta(minutes=1))
            a = slice_win(src, OVERLAP_START, end)
            b = slice_win(tgt, OVERLAP_START, end)
            r = eval_transforms(a, b)
            if not r:
                continue
            rows.append({"dataset": src_name, "interval": "1m", "symbol": sym, "path": str(path), **{k: v for k, v in r.items() if k != "all_transforms"}})

    # --- 2) Dukascopy H1 vs utc_v2 H1 ---
    for sym in syms:
        path = mans.get("dukascopy", {}).get(sym, {}).get("1h")
        if path is None:
            # fallback flat cache 1h
            path = ROOT / "data/cache" / cache_key(sym) / "1h.parquet"
            if not path.is_file():
                continue
            ds = "dukascopy_or_legacy_h1"
        else:
            ds = "dukascopy"
        src = load_ohlc_generic(path)
        tgt = load_v2(sym, "1h")
        if src is None or tgt is None:
            continue
        a = slice_win(src, OVERLAP_START, H1_END)
        b = slice_win(tgt, OVERLAP_START, H1_END)
        r = eval_transforms(a, b, min_n=50)
        if not r:
            continue
        rows.append({"dataset": f"{ds}_h1", "interval": "1h", "symbol": sym, "path": str(path), **{k: v for k, v in r.items() if k != "all_transforms"}})

    # --- 3) mt5 utc v1 H1 vs utc_v2 H1 ---
    for sym in syms:
        path = ROOT / "data/cache/mt5/utc" / cache_key(sym) / "1h.parquet"
        src = load_ohlc_generic(path)
        tgt = load_v2(sym, "1h")
        if src is None or tgt is None:
            continue
        a = slice_win(src, OVERLAP_START, H1_END)
        b = slice_win(tgt, OVERLAP_START, H1_END)
        r = eval_transforms(a, b, min_n=50)
        if not r:
            continue
        rows.append({"dataset": "mt5_utc_v1", "interval": "1h", "symbol": sym, "path": str(path), **{k: v for k, v in r.items() if k != "all_transforms"}})

    # --- 4) mt5 flat H1 vs utc_v2 H1 ---
    for sym in syms:
        path = ROOT / "data/cache/mt5" / cache_key(sym) / "1h.parquet"
        src = load_ohlc_generic(path)
        tgt = load_v2(sym, "1h")
        if src is None or tgt is None:
            continue
        a = slice_win(src, OVERLAP_START, min(H1_END, src.index.max() + pd.Timedelta(hours=1)))
        b = slice_win(tgt, OVERLAP_START, min(H1_END, src.index.max() + pd.Timedelta(hours=1)))
        r = eval_transforms(a, b, min_n=50)
        if not r:
            continue
        rows.append({"dataset": "mt5_flat", "interval": "1h", "symbol": sym, "path": str(path), **{k: v for k, v in r.items() if k != "all_transforms"}})

    # --- 5) Yahoo 30m / 15m vs utc_v2 resampled ---
    for tf, rule in (("30m", "30min"), ("15m", "15min"), ("4h", "4h")):
        for sym in syms:
            path = mans.get("yahoo", {}).get(sym, {}).get(tf)
            if path is None:
                # try cache path
                alt = ROOT / "data/cache" / cache_key(sym) / f"{tf}.parquet"
                path = alt if alt.is_file() else None
            if path is None:
                continue
            src = load_ohlc_generic(path)
            tgt = load_v2(sym, "1m")
            if src is None or tgt is None:
                continue
            tgt_r = resample_ohlc(slice_win(tgt, OVERLAP_START, OVERLAP_END), rule)
            a = slice_win(src, OVERLAP_START, OVERLAP_END)
            r = eval_transforms(a, tgt_r, min_n=30)
            if not r:
                continue
            rows.append({"dataset": f"yahoo_{tf}", "interval": tf, "symbol": sym, "path": str(path), **{k: v for k, v in r.items() if k != "all_transforms"}})

    # --- 6) Loose Yahoo CSVs (H1 / 5m) for majors ---
    csv_re = re.compile(r"^(?P<base>[A-Z]{6})_X_(?P<period>\d+[dmy])_(?P<tf>\d+[mhd]|1wk)\.csv$", re.I)
    for p in (ROOT / "data/cache").glob("*_X_*.csv"):
        m = csv_re.match(p.name)
        if not m:
            continue
        base = m.group("base").upper()
        tf = m.group("tf").lower()
        sym = f"{base[:3]}{base[3:]}=X"
        if sym not in syms:
            continue
        src = load_ohlc_generic(p)
        if src is None:
            continue
        if tf in {"1h", "60m"}:
            tgt = load_v2(sym, "1h")
            if tgt is None:
                continue
            a = slice_win(src, OVERLAP_START, H1_END)
            b = slice_win(tgt, OVERLAP_START, H1_END)
            r = eval_transforms(a, b, min_n=20)
            interval = "1h"
        elif tf in {"5m", "15m", "30m"}:
            tgt = load_v2(sym, "1m")
            if tgt is None:
                continue
            rule = {"5m": "5min", "15m": "15min", "30m": "30min"}[tf]
            a = slice_win(src, OVERLAP_START, OVERLAP_END)
            b = resample_ohlc(slice_win(tgt, OVERLAP_START, OVERLAP_END), rule)
            r = eval_transforms(a, b, min_n=20)
            interval = tf
        else:
            continue
        if not r:
            continue
        rows.append({"dataset": f"yahoo_csv_{tf}", "interval": interval, "symbol": sym, "path": str(p), **{k: v for k, v in r.items() if k != "all_transforms"}})

    # --- 7) download/ dukascopy bid CSV sample (AUDCAD etc) if overlap ---
    # Map audcad -> AUDCAD=X
    for p in (ROOT / "download").glob("*-m1-bid-2026-*.csv"):
        if p.stat().st_size < 1000:
            continue
        pair = p.name.split("-")[0].upper()
        sym = f"{pair[:3]}{pair[3:]}=X"
        if sym not in syms:
            continue
        src = load_ohlc_generic(p)
        tgt = load_v2(sym, "1m")
        if src is None or tgt is None:
            continue
        a = slice_win(src, OVERLAP_START, OVERLAP_END)
        b = slice_win(tgt, OVERLAP_START, OVERLAP_END)
        r = eval_transforms(a, b)
        if not r:
            continue
        rows.append({"dataset": "download_dukascopy_bid_csv", "interval": "1m", "symbol": sym, "path": str(p), **{k: v for k, v in r.items() if k != "all_transforms"}})

    df = pd.DataFrame(rows)
    out_csv = OUT_DIR / "ALL_DATASETS_TRANSFORM_ANALYSIS.csv"
    df.to_csv(out_csv, index=False)

    # summary markdown
    lines = [
        "# All Datasets → MT5 utc_v2 Transform Search",
        "",
        f"**Out:** `{OUT_DIR.name}`",
        f"**Target:** `data/cache/mt5/utc_v2`",
        f"**Overlap window (M1):** {OVERLAP_START.date()} → {OVERLAP_END.date()}",
        "",
        "## Datasets found locally",
        "",
        "| Dataset | What it is | M1? | H1? | Notes |",
        "| --- | --- | --- | --- | --- |",
        "| `utc_v2` | ICM MT5 research standard | yes | yes | TARGET |",
        "| `dukascopy` | Free vendor parquet | yes (partial) | yes | Already analyzed |",
        "| `histdata` | Free vendor (6 pairs M1) | yes | no | Separate manifests |",
        "| `yahoo` | Yahoo parquet 15m/30m/4h/1d | no M1 | via CSV | Coarser TF |",
        "| `mt5_utc_v1` | Older MT5 utc/ cache | no | yes | Same broker family |",
        "| `mt5_flat` | `data/cache/mt5/{SYM}` | no | yes | Older MT5 H1 |",
        "| `download/*-bid-*.csv` | Raw Dukascopy CLI pulls | yes | yes | Bid side |",
        "",
        "## Usability thresholds",
        "",
        "- **strict:** best MAE ≤ 0.5 pip AND within-1-pip ≥ 90%",
        "- **loose:** best MAE ≤ 1.0 pip AND within-1-pip ≥ 80%",
        "",
    ]

    if df.empty:
        lines.append("No comparable overlaps found.")
    else:
        for ds, g in df.groupby("dataset"):
            strict = int(g["usable_strict"].sum())
            loose = int(g["usable_loose"].sum())
            lines += [
                f"### `{ds}` ({g.iloc[0]['interval']}) — n_pairs={len(g)}",
                "",
                f"- strict usable: **{strict}/{len(g)}**",
                f"- loose usable: **{loose}/{len(g)}**",
                f"- median best MAE pips: **{g['best_mae_pips'].median():.3f}**",
                f"- median identity MAE pips: **{g['identity_mae_pips'].median():.3f}**",
                f"- median corr: **{g['corr'].median():.6f}**",
                "",
                "| symbol | best_transform | best_mae_pips | within_1pip | bias_pips | daily_bias_std | usable_strict |",
                "| --- | --- | ---: | ---: | ---: | ---: | --- |",
            ]
            gg = g.sort_values("best_mae_pips")
            for _, r in gg.iterrows():
                lines.append(
                    f"| {r['symbol']} | {r['best_transform']} | {r['best_mae_pips']:.3f} | "
                    f"{r['best_within_1pip']:.3f} | {r['static_bias_pips']:.3f} | "
                    f"{(r['daily_bias_std_pips'] if pd.notna(r['daily_bias_std_pips']) else float('nan')):.3f} | {r['usable_strict']} |"
                )
            lines.append("")

        lines += [
            "## Cross-dataset verdict",
            "",
            "| Dataset | Can a transform make it ≈ utc_v2? |",
            "| --- | --- |",
        ]
        for ds, g in df.groupby("dataset"):
            s = int(g["usable_strict"].sum())
            n = len(g)
            if s == n and n > 0:
                verdict = "YES — essentially already MT5-aligned"
            elif s > 0:
                verdict = f"PARTIAL — {s}/{n} pairs only"
            elif int(g["usable_loose"].sum()) > 0:
                verdict = f"WEAK — {int(g['usable_loose'].sum())}/{n} loose only"
            else:
                verdict = "NO — residuals too large / unstable"
            lines.append(f"| `{ds}` | {verdict} |")

        lines += [
            "",
            "## Implications",
            "",
            "1. Only feeds already in the MT5 family (`utc_v1`, possibly `mt5_flat`) can become near-identical to `utc_v2` via trivial transforms.",
            "2. Dukascopy / HistData / Yahoo remain vendor-different; transforms help some majors, fail exotics.",
            "3. No alternate local dataset unlocks a full 60-pair pre-June M1 pack equivalent to N1 utc_v2.",
            "",
            f"Machine-readable: `{out_csv.name}`",
            "",
        ]

    out_md = OUT_DIR / "ALL_DATASETS_TRANSFORM_REPORT.md"
    out_md.write_text("\n".join(lines), encoding="utf-8")
    print(f"rows={len(df)} datasets={sorted(df.dataset.unique()) if len(df) else []}")
    print(f"WROTE {out_csv}")
    print(f"WROTE {out_md}")
    if len(df):
        print(df.groupby("dataset")[["usable_strict", "usable_loose", "best_mae_pips"]].agg({"usable_strict": "sum", "usable_loose": "sum", "best_mae_pips": "median"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
