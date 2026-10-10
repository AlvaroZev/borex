#!/usr/bin/env python3
"""Generate MASSIVE_LAB_FORENSIC_REPORT from existing gate2_massive_lab artifacts.

NO new strategy experiments. September unread. LIVE/Mirror untouched.
"""

from __future__ import annotations

import csv
import json
import math
import sqlite3
import statistics
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data/runs/research/gate2_massive_lab_20260926_190200"
ASSETS = OUT / "report_assets"

# Source attribution for internet strategies (from factory script metadata)
INTERNET_SOURCES = {
    "internet1": ("Turtle System 1 Donchian 20", "donchian_breakout", "https://www.theturtletrader.com/turtle-trading-rules/"),
    "internet2": ("Turtle System 2 Donchian 55", "donchian_breakout", "https://oxfordstrat.com/coasdfASD32/uploads/2016/01/turtle-rules.pdf"),
    "internet3": ("SMA crossover", "ma_crossover", "https://www.investopedia.com/terms/m/movingaverage.asp"),
    "internet4": ("EMA trend cross", "trend", "https://www.investopedia.com/terms/e/ema.asp"),
    "internet5": ("ATR breakout", "atr_breakout", "https://www.investopedia.com/terms/a/atr.asp"),
    "internet6": ("RSI mean reversion", "mean_reversion", "https://www.investopedia.com/terms/r/rsi.asp"),
    "internet7": ("Bollinger mean reversion", "mean_reversion", "https://www.investopedia.com/articles/trading/07/bollinger.asp"),
    "internet8": ("MA pullback continuation", "pullback", "https://www.investopedia.com/articles/trading/09/how-to-use-moving-averages.asp"),
    "internet9": ("ROC momentum", "momentum", "https://www.investopedia.com/terms/r/rateofchange.asp"),
    "internet10": ("Volatility expansion breakout", "volatility_breakout", "https://www.investopedia.com/terms/v/volatility.asp"),
    "internet11": ("Tight range breakout", "range_breakout", "https://www.investopedia.com/terms/b/breakout.asp"),
    "internet12": ("Previous day high/low breakout", "pdh_pdl", "https://www.investopedia.com/articles/trading/05/openingrangebreakout.asp"),
    "internet13": ("London break of Asia range", "session_breakout", "https://www.investopedia.com/terms/f/forex.asp"),
    "internet14": ("Swing fractal breakout", "swing_breakout", "https://www.investopedia.com/terms/f/fractal.asp"),
    "internet15": ("ADX/DI trend", "adx_trend", "https://www.investopedia.com/terms/a/adx.asp"),
    "internet16": ("MACD crossover", "momentum", "https://www.investopedia.com/terms/m/macd.asp"),
    "internet17": ("Stochastic mean reversion", "mean_reversion", "https://www.investopedia.com/terms/s/stochasticoscillator.asp"),
    "internet18": ("Keltner channel breakout", "channel_breakout", "https://www.investopedia.com/terms/k/keltnerchannel.asp"),
    "internet19": ("PDH/PDL sweep reclaim", "liquidity_sweep_ohlc", "https://www.investopedia.com/terms/s/support.asp"),
    "internet20": ("MA trend + RSI pullback hybrid", "hybrid", "https://www.investopedia.com/articles/active-trading/042114/overbought-or-oversold-use-relative-strength-index-find-out.asp"),
}


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys: list[str] = []
    for r in rows:
        for k in r:
            if k not in keys:
                keys.append(k)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in keys})


def fnum(x, nd=2):
    if x is None:
        return "n/a"
    try:
        if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
            return str(x)
        return f"{float(x):.{nd}f}"
    except Exception:
        return str(x)


def load_rows() -> list[dict]:
    c = sqlite3.connect(OUT / "results.sqlite")
    meta = {k: json.loads(v) for k, v in c.execute("select key, value from meta")}
    rows = []
    for i, r in enumerate(
        c.execute(
            "select experiment_id, parent_id, group_id, strategy, family, config_hash, "
            "params_json, data_range, execution_model, status, worker_id, start_time, "
            "end_time, runtime_s, result_json, category from experiments order by group_id, strategy, family"
        ),
        1,
    ):
        params = json.loads(r[6] or "{}")
        res = json.loads(r[14] or "{}")
        rows.append(
            {
                "row_num": i,
                "experiment_id": r[0],
                "parent_id": r[1],
                "group_id": r[2],
                "strategy": r[3],
                "family": r[4],
                "config_hash": r[5],
                "params": params,
                "params_json": json.dumps(params, sort_keys=True),
                "data_range": r[7],
                "execution_model": r[8],
                "status": r[9],
                "worker_id": r[10],
                "start_time": r[11],
                "end_time": r[12],
                "runtime_s": r[13],
                "category": r[15],
                **res,
                "thursday_status": bool(params.get("thursday_gate")),
                "ghostsl_status": bool(params.get("ghostsl") or params.get("mode") in {"ghostsl", "delayed_equiv"}),
                "liquidity_status": params.get("kind") == "n1_disp_filter",
                "kind": params.get("kind"),
                "rr_factor": params.get("rr_factor"),
                "rr_min": params.get("rr_min"),
                "session": params.get("session"),
                "sides": params.get("sides"),
                "ghost_sl_mult": params.get("ghost_sl_mult"),
                "exclude_dow": params.get("exclude_dow"),
                "lookback": params.get("lookback"),
                "min_body_atr": params.get("min_body_atr"),
                "mode": params.get("mode"),
                "name": params.get("name") or r[3],
            }
        )
    c.close()
    return rows, meta


def stats(vals):
    vals = [float(v) for v in vals if v is not None]
    if not vals:
        return {"n": 0}
    return {
        "n": len(vals),
        "mean": statistics.mean(vals),
        "median": statistics.median(vals),
        "min": min(vals),
        "max": max(vals),
        "stdev": statistics.stdev(vals) if len(vals) > 1 else 0.0,
        "positive": sum(1 for v in vals if v > 0),
        "negative": sum(1 for v in vals if v <= 0),
    }


def try_charts(rows, ctrl_net, thu_net):
    ASSETS.mkdir(exist_ok=True)
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as e:
        (ASSETS / "CHARTS_UNAVAILABLE.txt").write_text(f"matplotlib unavailable: {e}\n", encoding="utf-8")
        return []

    made = []

    # 1 group net distributions
    by_g = defaultdict(list)
    for r in rows:
        if r.get("net") is not None:
            by_g[r["group_id"]].append(float(r["net"]))
    fig, ax = plt.subplots(figsize=(12, 5))
    labels = sorted(by_g)
    data = [by_g[k] for k in labels]
    ax.boxplot(data, tick_labels=labels, vert=True)
    ax.axhline(ctrl_net, color="C0", ls="--", label=f"control {ctrl_net:.0f}")
    ax.axhline(thu_net, color="C1", ls="--", label=f"thursday {thu_net:.0f}")
    ax.set_title("Net by experiment group (Jun–Aug aggregate)")
    ax.tick_params(axis="x", rotation=45)
    ax.legend()
    fig.tight_layout()
    p = ASSETS / "01_group_net_boxplots.png"
    fig.savefig(p, dpi=120)
    plt.close(fig)
    made.append(p.name)

    # 2 RR factor surface
    rf = [r for r in rows if r["group_id"] == "C_RR" and r["family"] == "rr_factor"]
    rf = sorted(rf, key=lambda x: float(x.get("rr_factor") or 0))
    if rf:
        fig, ax = plt.subplots(figsize=(8, 4))
        ax.plot([float(r["rr_factor"]) for r in rf], [float(r["net"]) for r in rf], "o-")
        ax.axhline(ctrl_net, color="C0", ls="--")
        ax.set_xlabel("rr_factor")
        ax.set_ylabel("net")
        ax.set_title("AlexG8 RR factor one-factor surface")
        fig.tight_layout()
        p = ASSETS / "02_rr_factor_surface.png"
        fig.savefig(p, dpi=120)
        plt.close(fig)
        made.append(p.name)

    # 3 RR min surface
    rm = [r for r in rows if r["group_id"] == "C_RR" and r["family"] == "rr_min"]
    rm = sorted(rm, key=lambda x: float(x.get("rr_min") or 0))
    if rm:
        fig, ax = plt.subplots(figsize=(8, 4))
        ax.plot([float(r["rr_min"]) for r in rm], [float(r["net"]) for r in rm], "o-")
        ax.axhline(ctrl_net, color="C0", ls="--")
        ax.set_xlabel("rr_min")
        ax.set_ylabel("net")
        ax.set_title("AlexG8 RR min one-factor surface")
        fig.tight_layout()
        p = ASSETS / "03_rr_min_surface.png"
        fig.savefig(p, dpi=120)
        plt.close(fig)
        made.append(p.name)

    # 4 RR pair heatmap
    pairs = [r for r in rows if r["group_id"] == "C_RR" and r["family"] == "rr_pair"]
    if pairs:
        xs = sorted({float(r["rr_factor"]) for r in pairs})
        ys = sorted({float(r["rr_min"]) for r in pairs})
        import numpy as np

        Z = np.full((len(ys), len(xs)), np.nan)
        for r in pairs:
            xi = xs.index(float(r["rr_factor"]))
            yi = ys.index(float(r["rr_min"]))
            Z[yi, xi] = float(r["net"] or 0)
        fig, ax = plt.subplots(figsize=(6, 4))
        im = ax.imshow(Z, aspect="auto", origin="lower")
        ax.set_xticks(range(len(xs)))
        ax.set_xticklabels(xs)
        ax.set_yticks(range(len(ys)))
        ax.set_yticklabels(ys)
        ax.set_xlabel("rr_factor")
        ax.set_ylabel("rr_min")
        ax.set_title("RR pair net heatmap")
        fig.colorbar(im, ax=ax)
        fig.tight_layout()
        p = ASSETS / "04_rr_pair_heatmap.png"
        fig.savefig(p, dpi=120)
        plt.close(fig)
        made.append(p.name)

    # 5 GhostSL deltas internet
    by = {}
    for r in rows:
        if r["group_id"] != "I_INTERNET":
            continue
        key = (r["name"], bool(r["thursday_status"]))
        by.setdefault(key, {})[bool(r["params"].get("ghostsl"))] = r
    deltas = []
    for key, d in by.items():
        if False in d and True in d:
            deltas.append(
                (
                    key[0],
                    key[1],
                    float(d[True].get("net") or 0) - float(d[False].get("net") or 0),
                    d[False].get("net"),
                    d[True].get("net"),
                )
            )
    deltas.sort(key=lambda x: x[2], reverse=True)
    if deltas:
        fig, ax = plt.subplots(figsize=(12, 5))
        labels = [f"{a}{'/thu' if b else ''}" for a, b, *_ in deltas]
        vals = [d[2] for d in deltas]
        ax.bar(range(len(vals)), vals)
        ax.set_xticks(range(len(labels)))
        ax.set_xticklabels(labels, rotation=90, fontsize=7)
        ax.axhline(0, color="k", lw=0.8)
        ax.set_title("Internet GhostSL Δnet (ghost − base)")
        fig.tight_layout()
        p = ASSETS / "05_internet_ghostsl_deltas.png"
        fig.savefig(p, dpi=120)
        plt.close(fig)
        made.append(p.name)

    # 6 MDD vs expectancy scatter
    fig, ax = plt.subplots(figsize=(7, 5))
    xs = [float(r.get("MDD") or 0) for r in rows if r.get("net") is not None]
    ys = [float(r["net"]) for r in rows if r.get("net") is not None]
    ax.scatter(xs, ys, s=12, alpha=0.5)
    ax.set_xlabel("MDD")
    ax.set_ylabel("net")
    ax.set_title("MDD vs net (all 237)")
    fig.tight_layout()
    p = ASSETS / "06_mdd_vs_net.png"
    fig.savefig(p, dpi=120)
    plt.close(fig)
    made.append(p.name)

    # 7 Thursday deltas on alexg8 n1_tape
    # compare matched where possible: same params except thursday
    tape = [r for r in rows if r.get("kind") == "n1_tape"]
    idx = {}
    for r in tape:
        p = dict(r["params"])
        thu = bool(p.pop("thursday_gate", False))
        key = json.dumps(p, sort_keys=True)
        idx.setdefault(key, {})[thu] = r
    thu_d = []
    for k, d in idx.items():
        if False in d and True in d:
            thu_d.append(float(d[True]["net"] or 0) - float(d[False]["net"] or 0))
    if thu_d:
        fig, ax = plt.subplots(figsize=(6, 4))
        ax.hist(thu_d, bins=15, edgecolor="k")
        ax.axvline(0, color="r")
        ax.set_title(f"Thursday Δnet on matched n1_tape pairs (n={len(thu_d)})")
        ax.set_xlabel("Δnet (thu − base)")
        fig.tight_layout()
        p = ASSETS / "07_thursday_delta_hist.png"
        fig.savefig(p, dpi=120)
        plt.close(fig)
        made.append(p.name)

    # 8 Donchian lookback
    don = [r for r in rows if r["group_id"] == "K_DONCHIAN" and not r["ghostsl_status"] and not r["thursday_status"]]
    don = sorted(don, key=lambda x: int(x.get("lookback") or 0))
    if don:
        fig, ax = plt.subplots(figsize=(8, 4))
        ax.plot([int(r["lookback"]) for r in don], [float(r["net"]) for r in don], "o-")
        ax.set_title("Donchian BASE net vs lookback")
        ax.set_xlabel("lookback")
        ax.set_ylabel("net")
        fig.tight_layout()
        p = ASSETS / "08_donchian_lookback.png"
        fig.savefig(p, dpi=120)
        plt.close(fig)
        made.append(p.name)

    # 9 fills vs net
    fig, ax = plt.subplots(figsize=(7, 5))
    ax.scatter(
        [float(r.get("fills") or 0) for r in rows],
        [float(r.get("net") or 0) for r in rows],
        s=12,
        alpha=0.5,
    )
    ax.set_xlabel("fills")
    ax.set_ylabel("net")
    ax.set_title("Fills vs net")
    fig.tight_layout()
    p = ASSETS / "09_fills_vs_net.png"
    fig.savefig(p, dpi=120)
    plt.close(fig)
    made.append(p.name)

    # 10 ghost_sl_mult
    g = [r for r in rows if r["group_id"] == "B_GHOST" and not r["thursday_status"]]
    g = sorted(g, key=lambda x: float(x.get("ghost_sl_mult") or 0))
    if g:
        fig, ax = plt.subplots(figsize=(8, 4))
        ax.plot([float(r["ghost_sl_mult"]) for r in g], [float(r["net"]) for r in g], "o-")
        ax.axhline(ctrl_net, ls="--")
        ax.set_title("ghost_sl_mult one-factor (no Thursday)")
        fig.tight_layout()
        p = ASSETS / "10_ghost_sl_mult.png"
        fig.savefig(p, dpi=120)
        plt.close(fig)
        made.append(p.name)

    return made


def md_table(headers, rows):
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
    for r in rows:
        lines.append("| " + " | ".join(str(x) for x in r) + " |")
    return "\n".join(lines)


def main():
    assert OUT.is_dir(), OUT
    rows, meta = load_rows()
    ASSETS.mkdir(exist_ok=True)

    ctrl = next(r for r in rows if r["family"] == "n1_control")
    thu = next(r for r in rows if r["family"] == "n1_thursday")
    ctrl_net = float(ctrl["net"])
    thu_net = float(thu["net"])

    # ---- master CSVs ----
    master = []
    for r in rows:
        master.append(
            {
                "row_num": r["row_num"],
                "experiment_id": r["experiment_id"],
                "config_hash": r["config_hash"],
                "group_id": r["group_id"],
                "strategy": r["strategy"],
                "family": r["family"],
                "kind": r["kind"],
                "params_json": r["params_json"],
                "thursday": r["thursday_status"],
                "ghostsl": r["ghostsl_status"],
                "liqfilter": r["liquidity_status"],
                "rr_factor": r.get("rr_factor"),
                "rr_min": r.get("rr_min"),
                "session": r.get("session"),
                "sides": r.get("sides"),
                "ghost_sl_mult": r.get("ghost_sl_mult"),
                "exclude_dow": r.get("exclude_dow"),
                "lookback": r.get("lookback"),
                "min_body_atr": r.get("min_body_atr"),
                "mode": r.get("mode"),
                "net": r.get("net"),
                "PF": r.get("PF"),
                "fills": r.get("fills"),
                "mean": r.get("mean"),
                "WR": r.get("WR"),
                "MDD": r.get("MDD"),
                "TP": r.get("TP"),
                "daily_close": r.get("daily_close"),
                "margin_stop": r.get("margin_stop"),
                "long_count": r.get("long_count"),
                "short_count": r.get("short_count"),
                "profitable_weeks": r.get("profitable_weeks"),
                "losing_weeks": r.get("losing_weeks"),
                "worst_week": r.get("worst_week"),
                "best_week": r.get("best_week"),
                "median_week": r.get("median_week"),
                "runtime_s": r.get("runtime_s"),
                "worker_id": r.get("worker_id"),
                "category": r.get("category"),
                "beat_control": (r.get("net") or 0) > ctrl_net,
                "beat_thursday": (r.get("net") or 0) > thu_net,
                "June_net": "NOT_STORED",
                "July_net": "NOT_STORED",
                "August_net": "NOT_STORED",
                "walkforward_status": "NOT_RUN_IN_STAGE1",
                "compound_status": "AGGREGATE_JUN_AUG_ONLY",
            }
        )
    write_csv(OUT / "experiment_index.csv", master)
    write_csv(OUT / "master_results_summary.csv", master)

    # strategy family summary
    fam_rows = []
    by_sf = defaultdict(list)
    for r in rows:
        by_sf[(r["group_id"], r["strategy"], r["family"])].append(r)
    for (g, s, f), xs in sorted(by_sf.items()):
        nets = [float(x["net"]) for x in xs if x.get("net") is not None]
        st = stats(nets)
        fam_rows.append(
            {
                "group_id": g,
                "strategy": s,
                "family": f,
                "n": st.get("n", 0),
                "median_net": st.get("median"),
                "mean_net": st.get("mean"),
                "min_net": st.get("min"),
                "max_net": st.get("max"),
                "positive": st.get("positive"),
                "negative": st.get("negative"),
                "beat_control": sum(1 for x in xs if (x.get("net") or 0) > ctrl_net),
                "beat_thursday": sum(1 for x in xs if (x.get("net") or 0) > thu_net),
                "median_fills": statistics.median([float(x.get("fills") or 0) for x in xs]),
                "median_PF": statistics.median([float(x["PF"]) for x in xs if x.get("PF") is not None]) if any(x.get("PF") is not None for x in xs) else None,
            }
        )
    write_csv(OUT / "strategy_family_summary.csv", fam_rows)

    # parameter family
    param_rows = []
    for label, subset in [
        ("rr_factor_onefactor", [r for r in rows if r["family"] == "rr_factor"]),
        ("rr_min_onefactor", [r for r in rows if r["family"] == "rr_min"]),
        ("rr_pair", [r for r in rows if r["family"] == "rr_pair"]),
        ("ghost_sl_mult", [r for r in rows if r["group_id"] == "B_GHOST"]),
        ("weekday_exclude", [r for r in rows if r["family"] == "exclude_dow"]),
        ("session", [r for r in rows if r["group_id"] == "G_SESSION"]),
        ("internet_base", [r for r in rows if r["group_id"] == "I_INTERNET" and not r["ghostsl_status"] and not r["thursday_status"]]),
        ("internet_ghostsl", [r for r in rows if r["group_id"] == "I_INTERNET" and r["ghostsl_status"] and not r["thursday_status"]]),
        ("internet_thu", [r for r in rows if r["group_id"] == "I_INTERNET" and not r["ghostsl_status"] and r["thursday_status"]]),
        ("internet_ghostsl_thu", [r for r in rows if r["group_id"] == "I_INTERNET" and r["ghostsl_status"] and r["thursday_status"]]),
        ("donchian", [r for r in rows if r["group_id"] == "K_DONCHIAN"]),
        ("liqfilter", [r for r in rows if r["group_id"] == "H_LIQFILTER"]),
        ("meanrev", [r for r in rows if r["group_id"] == "M_MEANREV"]),
        ("ghostsl_causal", [r for r in rows if r["group_id"] == "D_GHOSTSL_CAUSAL"]),
    ]:
        nets = [float(r["net"]) for r in subset if r.get("net") is not None]
        st = stats(nets)
        param_rows.append(
            {
                "parameter_family": label,
                "n": st.get("n", 0),
                "median_net": st.get("median"),
                "mean_net": st.get("mean"),
                "min_net": st.get("min"),
                "max_net": st.get("max"),
                "stdev_net": st.get("stdev"),
                "positive": st.get("positive"),
                "negative": st.get("negative"),
                "beat_control": sum(1 for r in subset if (r.get("net") or 0) > ctrl_net),
                "beat_thursday": sum(1 for r in subset if (r.get("net") or 0) > thu_net),
            }
        )
    write_csv(OUT / "parameter_family_summary.csv", param_rows)

    # GhostSL internet + donchian table
    ghost_tbl = []
    by = {}
    for r in rows:
        if r["group_id"] not in {"I_INTERNET", "K_DONCHIAN", "L_TREND", "M_MEANREV"}:
            continue
        if r["group_id"] == "K_DONCHIAN":
            key = (f"donchian_lb{r.get('lookback')}", bool(r["thursday_status"]))
        else:
            key = (r["name"], bool(r["thursday_status"]))
        by.setdefault(key, {})[bool(r["params"].get("ghostsl"))] = r
    for key, d in by.items():
        if False not in d or True not in d:
            continue
        b, g = d[False], d[True]
        ghost_tbl.append(
            {
                "strategy": key[0],
                "thursday": key[1],
                "base_fills": b.get("fills"),
                "ghost_fills": g.get("fills"),
                "fill_retention": (g.get("fills") or 0) / (b.get("fills") or 1),
                "base_net": b.get("net"),
                "ghost_net": g.get("net"),
                "delta_net": float(g.get("net") or 0) - float(b.get("net") or 0),
                "base_PF": b.get("PF"),
                "ghost_PF": g.get("PF"),
                "delta_PF": (float(g["PF"]) - float(b["PF"])) if b.get("PF") is not None and g.get("PF") is not None else None,
                "base_MDD": b.get("MDD"),
                "ghost_MDD": g.get("MDD"),
                "delta_MDD": (float(g.get("MDD") or 0) - float(b.get("MDD") or 0)),
                "base_mean": b.get("mean"),
                "ghost_mean": g.get("mean"),
                "base_WR": b.get("WR"),
                "ghost_WR": g.get("WR"),
                "base_experiment_id": b.get("experiment_id"),
                "ghost_experiment_id": g.get("experiment_id"),
                "base_config_hash": b.get("config_hash"),
                "ghost_config_hash": g.get("config_hash"),
            }
        )
    ghost_tbl.sort(key=lambda x: x["delta_net"], reverse=True)
    write_csv(OUT / "ghostsl_forensic_table.csv", ghost_tbl)

    # Candidate summary
    candidates = []
    for r in sorted(rows, key=lambda x: float(x.get("net") or -1e18), reverse=True)[:40]:
        net = float(r.get("net") or 0)
        if r["category"] == "LOCKED_EXISTING_CANDIDATE":
            cat = "LOCKED_EXISTING"
        elif net > thu_net and int(r.get("fills") or 0) >= 200:
            cat = "DISCOVERY_POSTHOC_HIGH_OVERFIT_RISK"
        elif net > ctrl_net:
            cat = "DISCOVERY_ONLY"
        elif net > 0:
            cat = "PROMISING_WEAK"
        else:
            cat = "REJECTED_OR_NEGATIVE"
        risk = []
        if net > thu_net:
            risk.append("selected_after_full_sample_search")
        if (r.get("losing_weeks") or 0) >= (r.get("profitable_weeks") or 0):
            risk.append("weekly_fragile")
        if abs(float(r.get("worst_week") or 0)) > abs(net) * 0.5 and net > 0:
            risk.append("worst_week_large_vs_total")
        candidates.append(
            {
                "experiment_id": r["experiment_id"],
                "config_hash": r["config_hash"],
                "strategy": r["strategy"],
                "family": r["family"],
                "group_id": r["group_id"],
                "params_json": r["params_json"],
                "net": r.get("net"),
                "PF": r.get("PF"),
                "fills": r.get("fills"),
                "MDD": r.get("MDD"),
                "category": cat,
                "risks": "|".join(risk) if risk else "see_report",
                "future_test": "untouched_OOS_after_Sep_lock_only" if cat.startswith("DISCOVERY") or cat == "LOCKED_EXISTING" else "none",
            }
        )
    write_csv(OUT / "candidate_summary.csv", candidates)

    charts = try_charts(rows, ctrl_net, thu_net)

    # ---- Build long report ----
    n = len(rows)
    n_pos = sum(1 for r in rows if (r.get("net") or 0) > 0)
    n_beat_c = sum(1 for r in rows if (r.get("net") or 0) > ctrl_net)
    n_beat_t = sum(1 for r in rows if (r.get("net") or 0) > thu_net)
    elapsed = meta.get("elapsed_min")
    hw = meta.get("hardware") or {}
    wstats = meta.get("worker_stats") or []

    lines: list[str] = []
    def w(s=""):
        lines.append(s)

    w("# MASSIVE LAB FORENSIC REPORT")
    w()
    w(f"**Source run:** `{OUT.name}`")
    w()
    w("**Mode:** DISCOVERY_ONLY forensic reconstruction from existing artifacts.")
    w("**No new strategy experiments were executed for this report.**")
    w()
    w("Labels used below:")
    w()
    w("- `DISCOVERY_ONLY` — observed on Jun–Aug research window after search")
    w("- `POSTHOC` — selected with knowledge of full-sample outcomes")
    w("- `LOCKED_EXISTING` — pre-registered Sep OOS control/treatment")
    w("- `NOT_RUN_IN_STAGE1` — requested in the mega-brief but absent from this 237-job stage")
    w("- `DATA_INSUFFICIENT` — metric cannot be reconstructed without re-running experiments")
    w()

    # 1 Executive
    w("## 1. Executive Summary")
    w()
    w(f"- Total experiments completed: **{n}/237** (0 failed)")
    w(f"- Runtime: **{fnum(elapsed, 1)} minutes** with workers `{wstats}`")
    w(f"- Hardware meta: `{hw}`")
    w(f"- Positive net configs: **{n_pos}** ({100*n_pos/n:.1f}%)")
    w(f"- Beat N1 control (net={fnum(ctrl_net)}): **{n_beat_c}**")
    w(f"- Beat N1 Thursday (net={fnum(thu_net)}): **{n_beat_t}**")
    w(f"- Experiment groups: **{len(set(r['group_id'] for r in rows))}**")
    w("- AlexG generations actually evaluated in this stage: **alexg8 only** (via N1 decision tape + engine knobs)")
    w("- Internet strategies: **internet1–internet20**")
    w("- GhostSL variants: internet × ghostsl ± Thursday; causal trio on internet1/2; plus alexg8 `ghost_sl_mult` one-factor")
    w("- Liquidity variants: displacement body/ATR filter thresholds only (`H_LIQFILTER`)")
    w()
    w("### Major patterns (descriptive, not validated)")
    w()
    w("1. **AlexG8 + Thursday** remains the strongest locked lineage reference on this window (LOCKED_EXISTING / prior research).")
    w("2. **Thursday × mild RR** (rr_factor≈0.9–1.1, rr_min≈2.0–2.5) produces the highest aggregate nets in this search — labeled **POSTHOC / DISCOVERY_ONLY** with **HIGH multiple-testing risk**.")
    w("3. **GhostSL** on many Internet strategies produces large positive Δnet versus their own base, often by cutting catastrophic losses — but absolute nets frequently remain negative; only a few approach/exceed control.")
    w("4. **Standalone Internet BASE** strategies are mostly strongly negative on this N1 economics window.")
    w("5. **Displacement filter** on N1 tape is mixed/threshold-sensitive; not a clear generalizer from this stage alone.")
    w("6. Many briefed labs (independent TP/SL geometry grids, Monte Carlo, walk-forward, pair leave-one-out, full AlexG3–7 scans, regime labs) were **NOT RUN in stage-1** — see §35.")
    w()

    # 2 Environment
    w("## 2. Research Environment")
    w()
    w(f"- git: `{meta.get('git')}`")
    w(f"- september_firewall meta: `{meta.get('september_firewall')}`")
    w(f"- locked_oos meta: `{meta.get('locked_oos')}`")
    w(f"- live_mirror_untouched meta: `{meta.get('live_mirror_untouched')}`")
    w("- Orchestrator: `python -m borex.research.massive_lab`")
    w("- Worker: `borex/research/massive_lab/worker.py`")
    w("- Job catalog: `borex/research/massive_lab/jobs.py`")
    w()

    # 3 Data
    w("## 3. Data")
    w()
    w("- Window: **2026-06-01 → 2026-08-31** (September reserved / unread)")
    w("- H1 + M1 utc_v2, 60-pair universe")
    w("- Periods loaded from: `mt5_1m_n1_oos_jun2026`, `mt5_1m_n1_oos_jul2026`, `mt5_1m_slmult_lev_split`")
    w("- N1 decision tapes: `decisions_sl_4p0.json` per month (alexg8 ghost=4 lineage)")
    w("- **Important:** result rows store **Jun–Aug aggregate** metrics only. Per-month nets/PFs were **not persisted** in `results.sqlite` → marked `DATA_INSUFFICIENT` / `NOT_STORED` in tables.")
    w()

    # 4 Simulator
    w("## 4. Simulator / Execution Model")
    w()
    w("- Frozen MultiMarketEngine, H1 signal / 1m exit closest-sim")
    w("- Leverage geometry **500**, true_sl + margin sizing, Raw spreads, $7/lot")
    w("- force_flat_daily=true, force_flat_friday=false")
    w("- capital=$1000 research account")
    w("- Internet/Donchian/GhostSL generate decision tapes then same engine replay")
    w("- GhostSL mechanism (from `borex/gate2/ghostsl`): G → GHOST_SL → REAL_ENTRY=GHOST_SL → REAL_SL/TP from REAL_ENTRY")
    w()

    # 5 Inventory
    w("## 5. Complete Experiment Inventory")
    w()
    w("Artifacts present in run directory:")
    for p in sorted(OUT.iterdir()):
        if p.is_file():
            w(f"- `{p.name}` ({p.stat().st_size} bytes)")
    w()
    w("Group counts:")
    w()
    gc = Counter(r["group_id"] for r in rows)
    w(md_table(["group_id", "n"], [[g, gc[g]] for g in sorted(gc)]))
    w()
    w("Full machine-readable index: `experiment_index.csv` / `master_results_summary.csv` (all 237 rows).")
    w()
    w("### Compact index (all experiments)")
    w()
    w(md_table(
        ["#", "group", "strategy", "family", "net", "PF", "fills", "MDD", "Thu", "GhostSL", "hash"],
        [
            [
                r["row_num"],
                r["group_id"],
                r["strategy"],
                r["family"],
                fnum(r.get("net")),
                fnum(r.get("PF"), 3),
                r.get("fills"),
                fnum(r.get("MDD"), 3),
                r["thursday_status"],
                r["ghostsl_status"],
                r["config_hash"][:10],
            ]
            for r in rows
        ],
    ))
    w()

    w("### How to answer: “What exactly did experiment #137 do?”")
    w()
    w("1. Open `experiment_index.csv`")
    w("2. Find `row_num=137`")
    w("3. Read `experiment_id`, `params_json`, `group_id`, `family`")
    w("4. Trace job construction in `borex/research/massive_lab/jobs.py`")
    w("5. Trace execution in `borex/research/massive_lab/worker.py`")
    w()
    # show #137 explicitly if exists
    r137 = next((r for r in rows if r["row_num"] == 137), None)
    if r137:
        w(f"**Experiment #137 snapshot:** `{r137['experiment_id']}`")
        w()
        w(f"- group/strategy/family: `{r137['group_id']}` / `{r137['strategy']}` / `{r137['family']}`")
        w(f"- params: `{r137['params_json']}`")
        w(f"- net={fnum(r137.get('net'))} PF={fnum(r137.get('PF'),3)} fills={r137.get('fills')} MDD={fnum(r137.get('MDD'),3)}")
        w(f"- config_hash=`{r137['config_hash']}`")
        w()

    # 6 AlexG
    w("## 6. AlexG Results")
    w()
    w("### What was actually tested")
    w()
    w("Stage-1 did **not** run full generation scans for alexg3/4/5/6/7. All AlexG-lineage results here are **alexg8 N1 tape** with research-layer / engine-parameter transforms.")
    w()
    w("Prior exhaustive lab (`gate2_exhaustive_alexg_liquidity_*`) had generation scans (alexg8≈alexg9_thin best; g7aligned/g7/g5revised weaker). Those are **outside this run's 237** and are not re-executed here.")
    w()
    w(md_table(
        ["Strategy", "Jun", "Jul", "Aug", "Aggregate net", "PF", "MDD", "Fills", "Notes"],
        [
            ["alexg8 n1_control", "NOT_STORED", "NOT_STORED", "NOT_STORED", fnum(ctrl_net), fnum(ctrl["PF"], 3), fnum(ctrl["MDD"], 3), ctrl["fills"], "LOCKED"],
            ["alexg8 n1_thursday", "NOT_STORED", "NOT_STORED", "NOT_STORED", fnum(thu_net), fnum(thu["PF"], 3), fnum(thu["MDD"], 3), thu["fills"], "LOCKED"],
        ],
    ))
    w()

    # 7 AlexG8 deep dive
    w("## 7. AlexG8 Deep Dive")
    w()
    w(f"Control: net={fnum(ctrl_net)} PF={fnum(ctrl['PF'],3)} fills={ctrl['fills']} WR={fnum(ctrl['WR'],3)} "
      f"TP={ctrl['TP']} daily_close={ctrl['daily_close']} margin_stop={ctrl['margin_stop']} "
      f"weeks+/{'-'}={ctrl['profitable_weeks']}/{ctrl['losing_weeks']} worst_week={fnum(ctrl['worst_week'])}")
    w()
    w(f"Thursday locked: net={fnum(thu_net)} PF={fnum(thu['PF'],3)} fills={thu['fills']} "
      f"Δnet vs control={fnum(thu_net-ctrl_net)} fill retention={fnum((thu['fills'] or 0)/(ctrl['fills'] or 1),3)}")
    w()
    w("### 7.1 RR one-factor (`C_RR` / `rr_factor`)")
    w()
    rf = sorted([r for r in rows if r["family"] == "rr_factor"], key=lambda x: float(x["rr_factor"]))
    w(md_table(
        ["rr_factor", "net", "Δcontrol", "PF", "fills", "mean", "WR", "MDD", "weeks+/-"],
        [[
            r["rr_factor"], fnum(r["net"]), fnum(float(r["net"])-ctrl_net), fnum(r["PF"],3), r["fills"],
            fnum(r["mean"],4), fnum(r["WR"],3), fnum(r["MDD"],3),
            f"{r['profitable_weeks']}/{r['losing_weeks']}"
        ] for r in rf],
    ))
    w()
    w("Interpretation: low rr_factor (0.25–0.75) underperforms or roughly matches control; mid factors near 1.0 are close to control; high factors (≥2) generally degrade. This is a **ridge/plateau near ~1.0**, not a single spike — still **POSTHOC** relative to search.")
    w()
    w("### 7.2 RR min one-factor (`rr_min`)")
    w()
    rm = sorted([r for r in rows if r["family"] == "rr_min"], key=lambda x: float(x["rr_min"]))
    w(md_table(
        ["rr_min", "net", "Δcontrol", "PF", "fills", "mean", "MDD"],
        [[r["rr_min"], fnum(r["net"]), fnum(float(r["net"])-ctrl_net), fnum(r["PF"],3), r["fills"], fnum(r["mean"],4), fnum(r["MDD"],3)] for r in rm],
    ))
    w()
    best_rm = max(rm, key=lambda x: float(x["net"]))
    w(f"Best one-factor rr_min in this surface: **{best_rm['rr_min']}** net={fnum(best_rm['net'])} (POSTHOC). Neighbors must be inspected for plateau vs spike.")
    w()
    w("### 7.3 RR pairs")
    w()
    pairs = [r for r in rows if r["family"] == "rr_pair"]
    w(md_table(
        ["rr_factor", "rr_min", "net", "PF", "fills", "Δcontrol"],
        [
            [r["rr_factor"], r["rr_min"], fnum(r["net"]), fnum(r["PF"], 3), r["fills"], fnum(float(r["net"]) - ctrl_net)]
            for r in sorted(pairs, key=lambda x: float(x["net"]), reverse=True)
        ],
    ))
    w()
    w("### 7.4 Thursday × RR (`F_WEEKDAY` / `thu_rr`)")
    w()
    tr = [r for r in rows if r["family"] == "thu_rr"]
    w(md_table(
        ["rr_factor", "rr_min", "net", "PF", "fills", "Δcontrol", "Δthursday"],
        [
            [
                r["rr_factor"], r["rr_min"], fnum(r["net"]), fnum(r["PF"], 3), r["fills"],
                fnum(float(r["net"]) - ctrl_net), fnum(float(r["net"]) - thu_net),
            ]
            for r in sorted(tr, key=lambda x: float(x["net"]), reverse=True)
        ],
    ))
    w()
    w("These are the strongest aggregate nets in the entire 237 set. They are **POSTHOC discoveries** after searching RR×Thursday space. They must **not** replace the locked Sep treatment.")
    w()
    w("### 7.5 ghost_sl_mult (`B_GHOST`)")
    w()
    for thu_flag, label in [(False, "no Thursday"), (True, "with Thursday")]:
        gs = [r for r in rows if r["group_id"] == "B_GHOST" and r["thursday_status"] == thu_flag]
        gs = sorted(gs, key=lambda x: float(x["ghost_sl_mult"]))
        w(f"#### {label}")
        w()
        w(md_table(
            ["ghost_sl_mult", "net", "PF", "fills", "Δcontrol"],
            [[r["ghost_sl_mult"], fnum(r["net"]), fnum(r["PF"],3), r["fills"], fnum(float(r["net"])-ctrl_net)] for r in gs],
        ))
        w()
    w("Note: N1 tapes were generated at ghost=4. Changing `ghost_sl_mult` at **replay** without regenerating decisions does not fully re-simulate signal geometry; treat B_GHOST as **partial / engine-parameter sensitivity**, not a pure ghost-entry re-optimization. Several rows collapsing to Thursday-equivalent nets indicate limited independent effect under this methodology.")
    w()
    w("### 7.6 Session & sides (`G_SESSION`)")
    w()
    sess = [r for r in rows if r["group_id"] == "G_SESSION"]
    w(md_table(
        ["session", "sides", "thu", "net", "PF", "fills", "Δcontrol"],
        [
            [r.get("session"), r.get("sides"), r["thursday_status"], fnum(r["net"]), fnum(r["PF"],3), r["fills"], fnum(float(r["net"])-ctrl_net)]
            for r in sorted(sess, key=lambda x: float(x["net"]), reverse=True)
        ],
    ))
    w()
    w("### 7.7 Weekday exclusions (`F_WEEKDAY` / `exclude_dow`)")
    w()
    dow_names = {0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu", 4: "Fri"}
    ex = [r for r in rows if r["family"] == "exclude_dow"]
    w(md_table(
        ["exclude", "net", "PF", "fills", "Δcontrol", "notes"],
        [
            [
                dow_names.get(int(r["exclude_dow"]), r["exclude_dow"]),
                fnum(r["net"]), fnum(r["PF"],3), r["fills"], fnum(float(r["net"])-ctrl_net),
                "equals Thursday gate" if int(r["exclude_dow"]) == 3 else "",
            ]
            for r in sorted(ex, key=lambda x: int(x["exclude_dow"]))
        ],
    ))
    w()

    # 8-9 TP/SL
    w("## 8. TP Forensic Analysis")
    w()
    w("**NOT_RUN_IN_STAGE1.** No independent TP mechanism grid (fixed pip / ATR / structural / swing / session / hybrid) was executed in these 237 jobs.")
    w("TP behavior appears only indirectly via engine `rr_factor`/`rr_min` dynamic RR resolution and Internet signal built-in RR.")
    w()
    w("## 9. SL Forensic Analysis")
    w()
    w("**NOT_RUN_IN_STAGE1** as an independent SL geometry lab.")
    w("Related proxies present: `ghost_sl_mult` (partial), GhostSL real-SL recalculation on Internet/Donchian, Internet ATR-multiple stops.")
    w()

    # 10 RR
    w("## 10. RR Forensic Analysis")
    w()
    w("See §7.1–7.4 for full tables.")
    w()
    w("### Plateau vs spike assessment around rr_min≈2.5 and rr_factor≈0.75–1.0")
    w()
    # neighbors narrative from data
    rm25 = [r for r in rm if float(r["rr_min"]) in {2.0, 2.25, 2.5, 3.0}]
    w("rr_min neighborhood:")
    for r in rm25:
        w(f"- rr_min={r['rr_min']}: net={fnum(r['net'])} PF={fnum(r['PF'],3)} fills={r['fills']}")
    w()
    w("Combined with Thursday (§7.4), several cells beat locked Thursday aggregate — this is exactly the region previously flagged as POSTHOC in weekly/WF labs. Without month splits and walk-forward in *this* DB, we **cannot** re-certify prospective validity here.")
    w()

    # 11 GhostSL
    w("## 11. GhostSL Forensic Analysis")
    w()
    w("### Mechanism restatement")
    w()
    w("```")
    w("G (ghost entry from signal)")
    w("→ GHOST_SL (normal SL from G)")
    w("→ REAL_ENTRY = GHOST_SL")
    w("→ when price reaches REAL_ENTRY → activate")
    w("→ REAL_SL / REAL_TP recalculated FROM REAL_ENTRY (not anchored to G)")
    w("```")
    w()
    w("Implementation: `borex/gate2/ghostsl/`. Naming note: older scripts sometimes called this `liq`; this report uses **GhostSL** only. Liquidity filters are separate (`H_LIQFILTER`).")
    w()
    w("### Activation rate")
    w()
    w("**DATA_INSUFFICIENT in this SQLite.** Activation/skip counts were not persisted in stage-1 result_json. Fill retention (ghost fills / base fills) is the available proxy.")
    w()
    w("### Full GhostSL comparison table (Internet + Donchian + Trend/MR slices)")
    w()
    w("Source: `ghostsl_forensic_table.csv` (matched base vs ghostsl pairs).")
    w()
    w(md_table(
        ["strategy", "thu", "base_fills", "ghost_fills", "retention", "base_net", "ghost_net", "Δnet", "base_PF", "ghost_PF", "ΔMDD"],
        [
            [
                g["strategy"], g["thursday"], g["base_fills"], g["ghost_fills"], fnum(g["fill_retention"], 3),
                fnum(g["base_net"]), fnum(g["ghost_net"]), fnum(g["delta_net"]),
                fnum(g["base_PF"], 3), fnum(g["ghost_PF"], 3), fnum(g["delta_MDD"], 3),
            ]
            for g in ghost_tbl
        ],
    ))
    w()
    w("### Top 20 GhostSL Δnet improvements")
    w()
    for g in ghost_tbl[:20]:
        w(f"- `{g['strategy']}` thu={g['thursday']}: Δnet={fnum(g['delta_net'])} ({fnum(g['base_net'])} → {fnum(g['ghost_net'])}), retention={fnum(g['fill_retention'],3)}")
    w()
    w("### Bottom 20 GhostSL Δnet (worst / least helpful)")
    w()
    for g in ghost_tbl[-20:]:
        w(f"- `{g['strategy']}` thu={g['thursday']}: Δnet={fnum(g['delta_net'])} ({fnum(g['base_net'])} → {fnum(g['ghost_net'])})")
    w()
    pos_d = sum(1 for g in ghost_tbl if g["delta_net"] > 0)
    w(f"Summary: {pos_d}/{len(ghost_tbl)} matched pairs show positive Δnet. Median Δnet={fnum(statistics.median([g['delta_net'] for g in ghost_tbl]))}.")
    w()

    # 12 Causal
    w("## 12. GhostSL Causal Question")
    w()
    causal = [r for r in rows if r["group_id"] == "D_GHOSTSL_CAUSAL"]
    w(md_table(
        ["strategy", "mode", "net", "PF", "fills", "mean", "MDD"],
        [
            [r["name"], r["mode"], fnum(r["net"]), fnum(r["PF"],3), r["fills"], fnum(r["mean"],4), fnum(r["MDD"],3)]
            for r in sorted(causal, key=lambda x: (x["name"], x["mode"]))
        ],
    ))
    w()
    w("In this implementation, `ghostsl` and `delayed_equiv` use the **same code path** (pending entry at ghost-SL price). Therefore:")
    w()
    w("- A vs B (GhostSL vs equivalent delayed entry): **NOT SEPARATED** — they are identical by construction in stage-1.")
    w("- This is itself informative: any GhostSL benefit observed is consistent with a **delayed-entry / pending-level** effect, not a separately coded 'liquidity hunt' narrative.")
    w("- Randomized entry-level control / risk-matched / PnL decomposition: **NOT_RUN_IN_STAGE1** → `DATA_INSUFFICIENT` for claims A–H beyond fill-reduction inference.")
    w()
    w("Inferred (weak): GhostSL often **reduces fills** and **cuts left-tail losses** on Internet strategies (gross-loss reduction pathway), but we lack stored per-trade entry-improvement metrics.")
    w()

    # 13-14 Internet
    w("## 13–14. Internet1–20 Mini-Reports + Comparison")
    w()
    for i in range(1, 21):
        name = f"internet{i}"
        title, fam, url = INTERNET_SOURCES.get(name, (name, "?", "?"))
        w(f"### {name} — {title}")
        w()
        w(f"- Family: `{fam}`")
        w(f"- Source URL (metadata): {url}")
        w(f"- Stage-1 config: `kind=internet`, `rr=2.0`, crossed with GhostSL ∈ {{False,True}} × Thursday ∈ {{False,True}}")
        w()
        subset = [r for r in rows if r["group_id"] == "I_INTERNET" and r["name"] == name]
        w(md_table(
            ["ghostsl", "thursday", "net", "PF", "fills", "mean", "WR", "MDD", "weeks+/-", "worst_week", "best_week", "config_hash", "experiment_id"],
            [
                [
                    r["ghostsl_status"], r["thursday_status"], fnum(r["net"]), fnum(r["PF"],3),
                    r["fills"], fnum(r["mean"],4), fnum(r["WR"],3), fnum(r["MDD"],3),
                    f"{r['profitable_weeks']}/{r['losing_weeks']}", fnum(r["worst_week"]), fnum(r["best_week"]),
                    r["config_hash"][:10], r["experiment_id"],
                ]
                for r in sorted(subset, key=lambda x: (x["ghostsl_status"], x["thursday_status"]))
            ],
        ))
        w()
        base = next((r for r in subset if not r["ghostsl_status"] and not r["thursday_status"]), None)
        g0 = next((r for r in subset if r["ghostsl_status"] and not r["thursday_status"]), None)
        t0 = next((r for r in subset if not r["ghostsl_status"] and r["thursday_status"]), None)
        gt = next((r for r in subset if r["ghostsl_status"] and r["thursday_status"]), None)
        if base and g0:
            w(
                f"Interpretation: BASE net={fnum(base['net'])}; GhostSL net={fnum(g0['net'])}; "
                f"ΔGhost={fnum(float(g0['net'])-float(base['net']))}; fills {base['fills']}→{g0['fills']}."
            )
        if base and t0:
            w(f"Thursday-only: net={fnum(t0['net'])}; ΔThu vs BASE={fnum(float(t0['net'])-float(base['net']))}.")
        if gt and base:
            w(
                f"GhostSL+Thursday: net={fnum(gt['net'])}; Δ vs BASE={fnum(float(gt['net'])-float(base['net']))}; "
                f"vs control={fnum(float(gt['net'])-ctrl_net)}; vs locked Thursday={fnum(float(gt['net'])-thu_net)}."
            )
        if base and float(base.get("net") or 0) > 0:
            w("Note: rare positive BASE — treat as DISCOVERY_ONLY / possible noise.")
        elif base:
            w("BASE is negative under N1 economics on this window.")
        w()

    w("### Aggregate Internet distributions")
    w()
    w(md_table(
        ["slice", "n", "median_net", "mean_net", "positive", "beat_control", "beat_thursday"],
        [
            [
                p["parameter_family"], p["n"], fnum(p["median_net"]), fnum(p["mean_net"]),
                p["positive"], p["beat_control"], p["beat_thursday"],
            ]
            for p in param_rows if p["parameter_family"].startswith("internet")
        ],
    ))
    w()
    w("Cross-slice reading: GhostSL shifts the Internet distribution upward (median/mean), but absolute profitability remains uncommon. Thursday alone is weaker than GhostSL for Internet. Combinations rarely beat locked AlexG8 Thursday.")
    w()

    # Donchian deep section (was only charted)
    w("## 14b. Donchian Lookback Surface (`K_DONCHIAN`)")
    w()
    w("Job grid: lookback ∈ {5,10,15,20,25,30,40,50,55,60,75,100} × GhostSL × Thursday; `rr=2.0`, `atr_mult=2.0`.")
    w()
    don_rows = [r for r in rows if r["group_id"] == "K_DONCHIAN"]
    w(md_table(
        ["lookback", "ghostsl", "thursday", "net", "PF", "fills", "mean", "MDD", "Δcontrol", "config_hash"],
        [
            [
                r["lookback"], r["ghostsl_status"], r["thursday_status"], fnum(r["net"]), fnum(r["PF"], 3),
                r["fills"], fnum(r["mean"], 4), fnum(r["MDD"], 3), fnum(float(r["net"]) - ctrl_net), r["config_hash"][:10],
            ]
            for r in sorted(don_rows, key=lambda x: (int(x["lookback"]), x["ghostsl_status"], x["thursday_status"]))
        ],
    ))
    w()
    # Donchian GhostSL deltas by lookback
    don_by = {}
    for r in don_rows:
        key = (int(r["lookback"]), bool(r["thursday_status"]))
        don_by.setdefault(key, {})[bool(r["ghostsl_status"])] = r
    don_deltas = []
    for key, d in don_by.items():
        if False in d and True in d:
            don_deltas.append(
                {
                    "lookback": key[0],
                    "thursday": key[1],
                    "base_net": d[False]["net"],
                    "ghost_net": d[True]["net"],
                    "delta_net": float(d[True]["net"]) - float(d[False]["net"]),
                    "base_fills": d[False]["fills"],
                    "ghost_fills": d[True]["fills"],
                }
            )
    don_deltas.sort(key=lambda x: x["delta_net"], reverse=True)
    w("### Donchian GhostSL Δnet by lookback")
    w()
    w(md_table(
        ["lookback", "thu", "base_net", "ghost_net", "Δnet", "base_fills", "ghost_fills"],
        [
            [d["lookback"], d["thursday"], fnum(d["base_net"]), fnum(d["ghost_net"]), fnum(d["delta_net"]), d["base_fills"], d["ghost_fills"]]
            for d in don_deltas
        ],
    ))
    w()
    if don_deltas:
        w(
            f"Donchian GhostSL positive Δnet count: {sum(1 for d in don_deltas if d['delta_net']>0)}/{len(don_deltas)}; "
            f"median Δ={fnum(statistics.median([d['delta_net'] for d in don_deltas]))}."
        )
    w()
    w("BASE Donchian alone is generally weak/negative. GhostSL often improves Δnet but absolute nets still rarely threaten AlexG8 Thursday. Lookback=55 (Turtle System 2 analogue) is of special interest alongside internet2.")
    w()

    w("## 14c. Trend / Mean-Reversion Slices (`L_TREND`, `M_MEANREV`)")
    w()
    w("`L_TREND` re-runs internet4 (EMA trend) at rr=2.0 — duplicates I_INTERNET internet4 cells if hashes collide (deduped in catalog).")
    w("`M_MEANREV` re-runs internet6/7/17 at **rr=1.5** (different from I_INTERNET rr=2.0) — distinct configs.")
    w()
    for g in ("L_TREND", "M_MEANREV"):
        xs = [r for r in rows if r["group_id"] == g]
        w(f"### {g}")
        w()
        w(md_table(
            ["strategy", "ghostsl", "thursday", "net", "PF", "fills", "mean", "MDD", "params"],
            [
                [
                    r["strategy"], r["ghostsl_status"], r["thursday_status"], fnum(r["net"]), fnum(r["PF"], 3),
                    r["fills"], fnum(r["mean"], 4), fnum(r["MDD"], 3), r["params_json"][:80],
                ]
                for r in sorted(xs, key=lambda x: float(x["net"]), reverse=True)
            ],
        ))
        w()
    w()

    # 15 Liquidity
    w("## 15. Liquidity Filter Analysis")
    w()
    w("Only **displacement body/ATR filter** on N1 tape was run (`H_LIQFILTER`).")
    w("NOT_RUN_IN_STAGE1 as standalone/filter/veto grids: equal H/L, sweep/reclaim, FVG, structure shift, session liquidity, PDH/PDL, premium/discount (except insofar as internet19 encodes PDH sweep).")
    w()
    liq = [r for r in rows if r["group_id"] == "H_LIQFILTER"]
    w(md_table(
        ["min_body_atr", "thursday", "net", "PF", "fills", "Δcontrol", "mean"],
        [
            [r["min_body_atr"], r["thursday_status"], fnum(r["net"]), fnum(r["PF"],3), r["fills"], fnum(float(r["net"])-ctrl_net), fnum(r["mean"],4)]
            for r in sorted(liq, key=lambda x: (float(x["min_body_atr"]), x["thursday_status"]))
        ],
    ))
    w()
    w("Displacement looks **threshold-sensitive**. Mild thresholds can look better than control in-sample; stricter thresholds collapse expectancy. Treat as DISCOVERY_ONLY / possible selection noise unless confirmed on untouched data.")
    w()

    # 16 Thursday
    w("## 16. Thursday Analysis")
    w()
    # matched n1_tape
    tape = [r for r in rows if r.get("kind") == "n1_tape"]
    idx = {}
    for r in tape:
        p = dict(r["params"])
        thu_flag = bool(p.pop("thursday_gate", False))
        key = json.dumps(p, sort_keys=True)
        idx.setdefault(key, {})[thu_flag] = r
    matched = []
    for k, d in idx.items():
        if False in d and True in d:
            matched.append(float(d[True]["net"] or 0) - float(d[False]["net"] or 0))
    w(f"Matched n1_tape Thursday pairs: n={len(matched)}")
    if matched:
        w(f"- improved: {sum(1 for x in matched if x>0)}")
        w(f"- worsened: {sum(1 for x in matched if x<0)}")
        w(f"- median Δnet: {fnum(statistics.median(matched))}")
        w(f"- mean Δnet: {fnum(statistics.mean(matched))}")
        w(f"- best Δnet: {fnum(max(matched))}")
        w(f"- worst Δnet: {fnum(min(matched))}")
    w()
    w("On AlexG8 N1-tape lineage, Thursday exclusion is broadly helpful in this window (consistent with prior labs).")
    w("On Internet strategies, Thursday is mixed and often secondary to GhostSL's loss-cutting.")
    w("**Lineage-specific caution:** Thursday generalization beyond alexg8/N1 is **not established** as a universal law by this stage.")
    w()

    # 17 Weekday
    w("## 17. Weekday Analysis")
    w()
    w("See §7.7. Excluding Thursday reproduces the Thursday gate. Other weekday exclusions do not automatically deserve promotion.")
    w()

    # 18 Session
    w("## 18. Session Analysis")
    w()
    w("See §7.6 full table. Overlap/session restrictions change fill counts dramatically; some session×Thursday cells inherit Thursday's aggregate strength.")
    w()

    # 19-21 timing / compound / weekly
    w("## 19. Entry / Exit Timing")
    w()
    w("**NOT_RUN_IN_STAGE1** as dedicated offset/expiration/time-in-trade labs. GhostSL is the main timing-related transform present.")
    w()
    w("## 20. Compound Analysis")
    w()
    w("Stage-1 stored **aggregate Jun–Aug** results from sequential monthly replays summed into one trade list (not a single continuous equity path with compounding sizing across months).")
    w("True path-preserving compound equity curves: **NOT_STORED** here (`DATA_INSUFFICIENT` without re-run).")
    w()
    w("## 21. Weekly Analysis")
    w()
    w("Per-week tables were not persisted. Available weekly summary fields per experiment: profitable_weeks, losing_weeks, worst_week, best_week, median_week (see `experiment_index.csv`).")
    w()
    important = []
    for label, pred in [
        ("N1 control", lambda r: r["family"] == "n1_control"),
        ("N1 Thursday", lambda r: r["family"] == "n1_thursday"),
        ("Best thu_rr", lambda r: r["family"] == "thu_rr"),
        ("Best rr_min one-factor", lambda r: r["family"] == "rr_min"),
        ("Best internet ghostsl", lambda r: r["group_id"] == "I_INTERNET" and r["ghostsl_status"]),
        ("Best internet base", lambda r: r["group_id"] == "I_INTERNET" and not r["ghostsl_status"] and not r["thursday_status"]),
        ("Best liqfilter", lambda r: r["group_id"] == "H_LIQFILTER"),
        ("Best donchian ghostsl", lambda r: r["group_id"] == "K_DONCHIAN" and r["ghostsl_status"]),
    ]:
        xs = [r for r in rows if pred(r)]
        if not xs:
            continue
        best = max(xs, key=lambda x: float(x.get("net") or -1e18))
        important.append((label, best))
    w("### Key-candidate weekly summary fields")
    w()
    w(md_table(
        ["label", "strategy/family", "net", "+weeks", "-weeks", "worst_week", "best_week", "median_week", "fills", "hash"],
        [
            [
                lab,
                f"{r['strategy']}/{r['family']}",
                fnum(r["net"]),
                r["profitable_weeks"],
                r["losing_weeks"],
                fnum(r["worst_week"]),
                fnum(r["best_week"]),
                fnum(r["median_week"]),
                r["fills"],
                r["config_hash"][:10],
            ]
            for lab, r in important
        ],
    ))
    w()
    w("Without true week-by-week vectors we cannot build `| Week | Control | Candidate | Delta |` tables from this DB. That is a major forensic gap.")
    w()
    for lab, r in important:
        pw = float(r.get("profitable_weeks") or 0)
        lw = float(r.get("losing_weeks") or 0)
        ratio = pw / (pw + lw) if (pw + lw) else float("nan")
        w(
            f"- **{lab}**: week-hit-rate≈{fnum(ratio,3)}; worst_week={fnum(r['worst_week'])} vs total net={fnum(r['net'])} "
            f"→ {'FRAGILE_WEEKLY' if abs(float(r.get('worst_week') or 0)) > 0.4 * abs(float(r.get('net') or 1)) else 'weekly_summary_ok_but_unverified'}."
        )
    w()

    w("## 22. Walk-Forward")
    w()
    w("**NOT_RUN_IN_STAGE1.** No train/validate splits stored. Do not retrofit prospective claims onto full-sample rankings.")
    w()
    w("## 23. Pair Robustness")
    w()
    w("**NOT_RUN_IN_STAGE1 / NOT_STORED.** No per-symbol breakdown in results.")
    w()
    w("## 24. Parameter Sensitivity")
    w()
    w("Surfaces with neighbor structure in this run:")
    w("- RR factor / RR min / RR pair (charts 02–04)")
    w("- ghost_sl_mult (chart 10)")
    w("- Donchian lookback BASE (chart 08)")
    w("- Displacement thresholds (§15)")
    w()
    w("### RR factor neighbors (full one-factor)")
    w()
    for r in rf:
        mark = ""
        if float(r["rr_factor"]) in {0.75, 1.0, 1.25}:
            mark = " ← focus band"
        w(f"- rr_factor={r['rr_factor']}: net={fnum(r['net'])} PF={fnum(r['PF'],3)} fills={r['fills']} mean={fnum(r['mean'],4)}{mark}")
    w()
    w("### RR min neighbors (full one-factor)")
    w()
    for r in rm:
        mark = ""
        if float(r["rr_min"]) in {2.0, 2.25, 2.5, 3.0}:
            mark = " ← focus band"
        w(f"- rr_min={r['rr_min']}: net={fnum(r['net'])} PF={fnum(r['PF'],3)} fills={r['fills']} mean={fnum(r['mean'],4)}{mark}")
    w()
    w("Classification sketch:")
    w("- rr_factor near 1.0: **PLATEAU/RIDGE** (not a lone spike)")
    w("- rr_min around 2–3: **RIDGE** with POSTHOC peaks when combined with Thursday")
    w("- Donchian lookback: inspect chart — often **NOISY/NEGATIVE** on BASE")
    w("- Displacement: **SPIKY/THRESHOLD** behavior")
    w("- ghost_sl_mult with Thursday: many values **FLAT** at Thursday-equivalent (engine/replay limitation)")
    w()

    w("## 25. Ablation")
    w()
    w("No formal ablation grid beyond natural comparisons (Thu on/off, GhostSL on/off, RR changes).")
    w()
    # Formal-ish ablations for top thu_rr
    top_tr = max([r for r in rows if r["family"] == "thu_rr"], key=lambda x: float(x["net"]))
    w(f"### Ablation around top `thu_rr` cell (net={fnum(top_tr['net'])})")
    w()
    w(f"FULL params: `{top_tr['params_json']}`")
    w()
    # compare to thursday locked, control, and matching rr without thursday if present
    w(md_table(
        ["component_removed_or_baseline", "net", "Δ from FULL", "fills", "note"],
        [
            ["FULL thu_rr", fnum(top_tr["net"]), "0", top_tr["fills"], "POSTHOC peak"],
            ["LOCKED Thursday only", fnum(thu_net), fnum(thu_net - float(top_tr["net"])), thu["fills"], "remove RR tweak"],
            ["LOCKED Control", fnum(ctrl_net), fnum(ctrl_net - float(top_tr["net"])), ctrl["fills"], "remove Thu + RR tweak"],
        ],
    ))
    w()
    # find closest rr_pair / rr_factor without thursday
    rf_match = next((r for r in rows if r["family"] == "rr_factor" and float(r["rr_factor"]) == float(top_tr["rr_factor"])), None)
    rm_match = next((r for r in rows if r["family"] == "rr_min" and float(r["rr_min"]) == float(top_tr["rr_min"])), None)
    if rf_match:
        w(f"- Same rr_factor={top_tr['rr_factor']} **without Thursday** (rr_min fixed 2.0): net={fnum(rf_match['net'])} ΔFULL={fnum(float(rf_match['net'])-float(top_tr['net']))}")
    if rm_match:
        w(f"- Same rr_min={top_tr['rr_min']} **without Thursday** (rr_factor fixed 1.0): net={fnum(rm_match['net'])} ΔFULL={fnum(float(rm_match['net'])-float(top_tr['net']))}")
    w()
    w("Informal ablation evidence:")
    w("- Removing Thursday from strong thu_rr cells returns toward control-like nets (large negative ablation delta).")
    w("- Removing GhostSL from Internet pairs often reintroduces large losses (GhostSL is doing heavy lifting on left tail).")
    w("- RR tweak alone without Thursday is far weaker than RR×Thursday stack — suggesting **interaction**, not additive independent edges.")
    w()

    w("## 26. Multiple Testing")
    w()
    w(f"- Configs evaluated: **{n}**")
    w(f"- Families/groups: {sorted(gc.keys())}")
    w(f"- Positive: {n_pos} ({100*n_pos/n:.1f}%)")
    w(f"- Beat control: {n_beat_c} ({100*n_beat_c/n:.1f}%)")
    w(f"- Beat Thursday: {n_beat_t} ({100*n_beat_t/n:.1f}%)")
    w()
    # expected positives under naive null - descriptive only
    w("Descriptive multiple-testing framing (not a formal p-value):")
    w(f"- If nets were symmetric noise around zero, ~50% positive would be unsurprising; we observed {100*n_pos/n:.1f}%.")
    w(f"- Beating a strong locked Thursday bar is rare ({n_beat_t}/{n}); those rare wins are exactly where selection bias concentrates.")
    w("- Family mining (Internet×GhostSL×Thu×Donchian×RR×session) multiplies hypotheses beyond the raw 237 count because many are highly correlated transforms of the same sample.")
    w()
    w("Therefore: **BEST-BACKTEST BIAS = HIGH**, **MULTIPLE-HYPOTHESIS RISK = HIGH**.")
    w()

    w("## 27. Negative Findings (What did NOT work)")
    w()
    neg = sorted([r for r in rows if (r.get("net") or 0) <= 0], key=lambda x: float(x.get("net") or 0))
    w(f"Negative or zero-net configs: **{len(neg)}/{n}**")
    w()
    w("### Worst 40 by net")
    w()
    w(md_table(
        ["group", "strategy", "family", "net", "PF", "fills", "ghostsl", "thu", "hash"],
        [[
            r["group_id"], r["strategy"], r["family"], fnum(r["net"]), fnum(r["PF"],3), r["fills"],
            r["ghostsl_status"], r["thursday_status"], r["config_hash"][:10],
        ] for r in neg[:40]],
    ))
    w()
    w("Themes:")
    w("- Most Internet BASE strategies lose badly under N1 economics on this window.")
    w("- Mean-reversion Internet slices (M_MEANREV) are deeply negative even with GhostSL in several cases.")
    w("- High rr_factor without Thursday often fails.")
    w("- Strict displacement filters can destroy expectancy.")
    w("- Donchian BASE across many lookbacks remains weak.")
    w("- Session-restricted Asia/London-only slices often underperform full-session control.")
    w("- Side restrictions (long_only / short_only) without Thursday do not magically create edge.")
    w()
    w("### Failed families checklist")
    w()
    for p in param_rows:
        if (p.get("median_net") or 0) <= 0:
            w(f"- `{p['parameter_family']}`: median_net={fnum(p['median_net'])}, positive={p['positive']}/{p['n']}")
    w()

    w("## 28. Discoveries Worth Attacking")
    w()
    discoveries = [
        ("Thursday on AlexG8/N1", "Locked + matched-pair positive Δnets", "Avoids harmful Thursday entries", "Seasonality coincidence on 3 months", "HIGH if generalized casually", "Locked Sep OOS already tests this"),
        ("Thursday × RR floor/factor", "Top nets in 237; prior weekly labs similar", "Payoff shaping + day filter", "Posthoc mining on same sample", "VERY HIGH", "Future untouched OOS after Sep; do not alter Sep lock"),
        ("GhostSL delayed entry on Internet", "Mostly positive Δnet vs own base; causal alias to delayed entry", "Pending better price / skip failures", "Still absolute losers; selection on Δnet", "HIGH", "GhostSL vs pure delay already aliased — need richer controls on new data"),
        ("Internet2 Donchian55 + GhostSL + Thu", "Among better Internet absolute nets after transforms", "Breakout + delay + day filter", "Multiple transforms stacked", "VERY HIGH", "Untouched OOS with pre-registered config only"),
        ("Displacement mild filter", "Some thresholds beat control in-sample", "Trade quality filter", "Threshold fishing", "HIGH", "Pre-register threshold before OOS"),
        ("Donchian GhostSL Δnet pattern", "Consistent positive Δnet across many lookbacks", "Pending entry cuts failed breakouts", "Absolute nets still poor; correlated with Internet GhostSL story", "HIGH", "Pre-register lookback+GhostSL before OOS"),
    ]
    for title, evidence, mech, alt, risk, falsify in discoveries:
        w(f"### {title}")
        w()
        w(f"- Observation/evidence: {evidence}")
        w(f"- Possible mechanism: {mech}")
        w(f"- Alternative: {alt}")
        w(f"- Overfit risk: {risk}")
        w(f"- Falsifying future test: {falsify}")
        w()

    w("## 29. Candidate Table")
    w()
    w(md_table(
        ["Candidate", "Evidence", "Main Risk", "Future Test", "Category"],
        [
            ["n1_control", f"net={fnum(ctrl_net)}", "baseline", "Locked Sep CONTROL", "LOCKED_EXISTING"],
            ["n1_thursday", f"net={fnum(thu_net)}", "3-month seasonality", "Locked Sep TREATMENT", "LOCKED_EXISTING"],
            ["thu_rr top cells", "best nets in search", "posthoc RR×Thu mining", "Only after Sep; separate OOS", "DISCOVERY_POSTHOC"],
            ["internet2 ghostsl±thu", "strong Δnet vs base", "absolute edge unclear", "Pre-registered internet OOS", "DISCOVERY_ONLY"],
            ["disp filter 0.5", "beats control in-sample", "threshold search", "Pre-register threshold", "HIGH_OVERFIT_RISK"],
        ],
    ))
    w()
    w("Full `candidate_summary.csv` has top-40 with categories.")
    w()

    w("## 30. What We Still Don't Know")
    w()
    for q in [
        "Live expectancy under brokerage frictions beyond frozen model",
        "Behavior outside Jun–Aug 2026 (including true post-Sep OOS)",
        "Per-month decomposition for these 237 configs (not stored)",
        "Per-week and per-pair contribution for these 237 configs",
        "Whether GhostSL benefit survives when delayed-entry is matched with richer nulls",
        "Whether rr_min≈2.5 / thu_rr survives untouched data",
        "Tick-level activation ambiguity beyond current bar rules",
        "Full AlexG3–7 comparative surface under identical N1 economics in this run",
        "Independent TP/SL geometry (non-RR) performance",
        "Monte Carlo / bootstrap uncertainty bands",
        "Walk-forward prospective selection within this DB",
        "Occupancy/portfolio coupling effects",
        "Whether liquidity features beyond displacement matter",
        "Whether GhostSL activation rate correlates with month-level regimes",
        "Whether Thursday effect is weekday-identity or a proxy for some other hidden covariate",
    ]:
        w(f"- {q}")
    w()

    w("## 31. Future OOS Candidates (small list)")
    w()
    w("Do **not** expand this list. Multiple attractive in-sample configs are themselves evidence of mining.")
    w()
    w("1. **LOCKED:** `n1_control` vs `n1_thursday` (unchanged Sep experiment)")
    w("2. **Future only (after Sep):** one pre-registered `thu_rr` config exactly specified from params_json of the chosen cell — not re-optimized")
    # print exact top thu_rr
    w(f"   - Example peak observed (NOT promoted): `{top_tr['params_json']}` hash=`{top_tr['config_hash']}` net={fnum(top_tr['net'])} **POSTHOC**")
    w("3. **Future only:** one Internet2 GhostSL config pre-registered")
    i2 = next((r for r in rows if r["name"] == "internet2" and r["ghostsl_status"] and r["thursday_status"] and r["group_id"] == "I_INTERNET"), None)
    if i2:
        w(f"   - Example observed: `{i2['params_json']}` hash=`{i2['config_hash']}` net={fnum(i2['net'])} **DISCOVERY_ONLY**")
    w("4. Everything else: remain DISCOVERY_ONLY / watchlist")
    w()
    w(f"If more than ~3–5 configs look 'must test', that is itself evidence of multiple-testing risk. This stage produced {n_beat_c} control-beaters.")
    w()

    w("## 32. Visuals")
    w()
    if charts:
        for name in charts:
            w(f"![{name}](report_assets/{name})")
            w()
    else:
        w("No charts generated (see report_assets/CHARTS_UNAVAILABLE.txt).")
    w()

    w("## 33. Source Traceability")
    w()
    w("- Numbers generated from `results.sqlite` via `scripts/generate_massive_lab_forensic_report.py`")
    w("- experiment_id / config_hash on every row in `experiment_index.csv`")
    w("- Job definitions: `borex/research/massive_lab/jobs.py`")
    w("- Internet sources: INTERNET_SOURCES table in generator + factory metadata")
    w("- Prior aggregate notes: `massive_lab_summary.md`, `ghostsl_analysis.md`, `candidate_freeze.md`")
    w()

    w("## 34–37. Safety Verification + Gaps + Stop")
    w()
    w("### Safety")
    w()
    w("- `SEPTEMBER_READ = FALSE` (campaign used september_firewall; research end exclusive 2026-09-01; no Sep metrics in results)")
    w("- `LIVE_TOUCHED = FALSE` (offline; meta live_mirror_untouched=true; no account I/O in worker)")
    w("- `MIRROR_TOUCHED = FALSE` (same)")
    w("- `SEP_OOS_LOCK_MODIFIED = FALSE` (locked pair only referenced as baseline; Sep OOS runner not modified by this forensic pass)")
    w()
    w("Verification method: code path inspection + meta flags + absence of September fields in result schema. No separate OS-level file-access audit log was found in the run directory.")
    w()
    w("### Labs requested in mega-brief but absent from these 237")
    w()
    for item in [
        "Full AlexG3–7 generation matrix",
        "Independent TP mechanism lab",
        "Independent SL mechanism lab",
        "SL×TP×RR full geometry",
        "GhostSL depth levels 2–3",
        "Entry offset lab",
        "Time-to-entry / time-in-trade labs",
        "Full liquidity feature suite beyond displacement",
        "Internet21–100",
        "Trend/MR/volatility/regime labs beyond the small L/M slices",
        "MTF labs",
        "Pair/currency cluster labs",
        "Occupancy/portfolio labs",
        "Monte Carlo / bootstrap / null randomization",
        "Walk-forward / leave-one-week / leave-one-month",
        "Formal ablation grids",
        "True sequential compound equity paths",
    ]:
        w(f"- {item}: **NOT_RUN_IN_STAGE1**")
    w()

    # APPENDIX: all beat-control configs
    w("## Appendix A — Every config that beat N1 control (full params)")
    w()
    beaters = sorted([r for r in rows if (r.get("net") or 0) > ctrl_net], key=lambda x: float(x["net"]), reverse=True)
    w(f"Count: **{len(beaters)}** / {n}")
    w()
    w(md_table(
        ["rank", "net", "PF", "fills", "MDD", "group", "family", "thu", "ghostsl", "params_json", "experiment_id", "config_hash"],
        [
            [
                i, fnum(r["net"]), fnum(r["PF"], 3), r["fills"], fnum(r["MDD"], 3), r["group_id"], r["family"],
                r["thursday_status"], r["ghostsl_status"], r["params_json"], r["experiment_id"], r["config_hash"],
            ]
            for i, r in enumerate(beaters, 1)
        ],
    ))
    w()

    w("## Appendix B — Full ranking of all 237 by net")
    w()
    ranked = sorted(rows, key=lambda x: float(x.get("net") or -1e18), reverse=True)
    w(md_table(
        ["rank", "row_num", "net", "PF", "fills", "group", "strategy", "family", "thu", "ghostsl", "config_hash"],
        [
            [
                i, r["row_num"], fnum(r["net"]), fnum(r["PF"], 3), r["fills"], r["group_id"], r["strategy"], r["family"],
                r["thursday_status"], r["ghostsl_status"], r["config_hash"][:12],
            ]
            for i, r in enumerate(ranked, 1)
        ],
    ))
    w()

    w("## Appendix C — Artifact inventory with sizes")
    w()
    for p in sorted(OUT.rglob("*")):
        if p.is_file():
            rel = p.relative_to(OUT).as_posix()
            w(f"- `{rel}` — {p.stat().st_size} bytes")
    w()
    w("---")
    w()
    w("**STOP.** Forensic report generated from existing artifacts only. No new strategy experiments were run.")
    w()

    report_path = OUT / "MASSIVE_LAB_FORENSIC_REPORT.md"
    report_path.write_text("\n".join(lines), encoding="utf-8")

    # Executive summary 2-4 pages
    top5 = ranked[:5]
    exec_lines = [
        "# MASSIVE LAB EXECUTIVE SUMMARY",
        "",
        f"Run `{OUT.name}` — 237/237 done, 0 failed, ~{fnum(elapsed,1)} min, workers={wstats}.",
        "",
        "## Bottom line",
        "",
        f"- Control net={fnum(ctrl_net)}; Thursday net={fnum(thu_net)} (LOCKED_EXISTING).",
        f"- {n_pos} positive; {n_beat_c} beat control; {n_beat_t} beat Thursday.",
        "- Strongest search tails: AlexG8 Thursday×RR (POSTHOC / DISCOVERY_ONLY).",
        "- GhostSL helps many Internet/Donchian strategies vs their own base (often still absolute losers).",
        "- Multiple-testing risk HIGH. Nothing new is validated.",
        "- Sep unread; LIVE/Mirror untouched; Sep OOS lock unchanged.",
        "",
        "## Top 5 descriptive nets (do not validate)",
        "",
    ]
    for i, r in enumerate(top5, 1):
        exec_lines.append(
            f"{i}. `{r['group_id']}` `{r['family']}` net={fnum(r['net'])} PF={fnum(r['PF'],3)} fills={r['fills']} "
            f"params=`{r['params_json']}`"
        )
    exec_lines += [
        "",
        "## Major patterns",
        "",
        "1. AlexG8 Thursday remains the strongest locked reference.",
        "2. thu_rr cells around rr_factor≈0.9–1.1 and rr_min≈2.5 are the only ones beating locked Thursday — POSTHOC.",
        "3. GhostSL ≈ delayed entry (causal modes identical); large Δnet on Internet but rare absolute winners.",
        "4. Displacement filter is threshold-sensitive / selection-risk high.",
        "5. Large fractions of the mega-brief were NOT_RUN_IN_STAGE1 (TP/SL labs, WF, MC, pair leave-one-out, AlexG3–7, etc.).",
        "",
        "## Read next",
        "",
        "1. `MASSIVE_LAB_FORENSIC_REPORT.md` (full; includes Appendix A–C)",
        "2. `experiment_index.csv`",
        "3. `ghostsl_forensic_table.csv`",
        "4. `candidate_summary.csv`",
        "5. `report_assets/`",
        "",
        "## Do not",
        "",
        "- Replace Sep treatment with thu_rr search winners",
        "- Treat GhostSL Δnet as proof of standalone profitability",
        "- Ignore the large set of failures (§27 + Appendix B bottom ranks)",
        "- Run new strategy experiments until this report is reviewed",
        "",
        f"SEPTEMBER_READ=FALSE | LIVE_TOUCHED=FALSE | MIRROR_TOUCHED=FALSE | SEP_OOS_LOCK_MODIFIED=FALSE",
        "",
    ]
    exec_text = "\n".join(exec_lines)
    (OUT / "MASSIVE_LAB_EXECUTIVE_SUMMARY.md").write_text(exec_text, encoding="utf-8")
    # Brief requested by forensic brief (same content, canonical short name)
    (OUT / "EXECUTIVE_SUMMARY.md").write_text(exec_text, encoding="utf-8")

    print("WROTE", report_path, "lines", len(lines), "charts", charts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
