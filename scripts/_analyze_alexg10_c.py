import json
from pathlib import Path

import pandas as pd

root = Path(
    r"C:\Users\azeva\OneDrive\Documentos\work\trading\borex-main\data\runs\research"
    r"\frozen_thu_only_alexg10_rrdyn_noflat_20261002_144951"
)
trades = json.loads((root / "C_alexg10_entry_rrdyn_noflat_trades.json").read_text(encoding="utf-8"))
summary = json.loads((root / "C_alexg10_entry_rrdyn_noflat_summary.json").read_text(encoding="utf-8"))

df = pd.DataFrame(trades)
df["entry_time"] = pd.to_datetime(df["entry_time"], utc=True)
df["exit_time"] = pd.to_datetime(df["exit_time"], utc=True, errors="coerce")
df["hold_h"] = (df["exit_time"] - df["entry_time"]).dt.total_seconds() / 3600.0
df["month"] = df["entry_time"].dt.strftime("%Y-%m")
df["win"] = df["pnl"] > 0

aw = float(df.loc[df.win, "pnl"].mean()) if df.win.any() else 0.0
al = float(df.loc[~df.win, "pnl"].mean()) if (~df.win).any() else 0.0
wr = float(df.win.mean())
cum = df.sort_values("entry_time")["pnl"].cumsum()
wins = df.sort_values("entry_time")["win"].tolist()
best_w = best_l = cur_w = cur_l = 0
for w in wins:
    if w:
        cur_w += 1
        cur_l = 0
        best_w = max(best_w, cur_w)
    else:
        cur_l += 1
        cur_w = 0
        best_l = max(best_l, cur_l)

pf = None
loss_sum = float(df.loc[~df.win, "pnl"].sum())
if loss_sum != 0:
    pf = abs(float(df.loc[df.win, "pnl"].sum()) / loss_sum)

sym = (
    df.groupby("symbol")
    .agg(n=("pnl", "count"), wr=("win", "mean"), pnl=("pnl", "sum"))
    .sort_values("pnl")
)

by_exit = {}
for reason, g in df.groupby("exit_reason"):
    by_exit[str(reason)] = {
        "n": int(len(g)),
        "wr": float(g.win.mean()),
        "pnl": float(g.pnl.sum()),
        "avg": float(g.pnl.mean()),
        "hold_h_mean": float(g.hold_h.mean()),
        "hold_h_med": float(g.hold_h.median()),
    }

by_month = {
    m: {"n": int(len(g)), "wr": float(g.win.mean()), "pnl": float(g.pnl.sum())}
    for m, g in df.groupby("month")
}
dow_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
by_dow = {
    dow_names[int(d)]: {
        "n": int(len(g)),
        "wr": float(g.win.mean()),
        "pnl": float(g.pnl.sum()),
    }
    for d, g in df.groupby(df.entry_time.dt.dayofweek)
}

detail = {
    "run": str(root),
    "summary_file": summary,
    "basic": {
        "n": int(len(df)),
        "wins": int(df.win.sum()),
        "losses": int((~df.win).sum()),
        "wr": wr,
        "pnl": float(df.pnl.sum()),
        "avg_pnl": float(df.pnl.mean()),
        "median_pnl": float(df.pnl.median()),
        "avg_win": aw,
        "avg_loss": al,
        "best": float(df.pnl.max()),
        "worst": float(df.pnl.min()),
        "commission": float(df.commission.fillna(0).sum()),
        "margin_mean": float(df.margin.mean()),
        "margin_median": float(df.margin.median()),
        "hold_h_mean": float(df.hold_h.mean()),
        "hold_h_median": float(df.hold_h.median()),
        "hold_h_p90": float(df.hold_h.quantile(0.9)),
        "expectancy": wr * aw + (1 - wr) * al,
        "profit_factor": pf,
        "best_win_streak": best_w,
        "best_loss_streak": best_l,
        "cum_min": float(cum.min()),
        "cum_max": float(cum.max()),
        "first_entry": str(df.entry_time.min()),
        "last_entry": str(df.entry_time.max()),
    },
    "side": {str(k): int(v) for k, v in df.side.value_counts().items()},
    "by_exit": by_exit,
    "by_month": by_month,
    "by_dow": by_dow,
    "worst_symbols": sym.head(10).reset_index().to_dict(orient="records"),
    "best_symbols": sym.tail(10).iloc[::-1].reset_index().to_dict(orient="records"),
    "top_wins": df.nlargest(5, "pnl")[
        ["symbol", "side", "entry_time", "exit_time", "pnl", "exit_reason", "margin", "hold_h"]
    ].astype({"entry_time": str, "exit_time": str}).to_dict(orient="records"),
    "top_losses": df.nsmallest(5, "pnl")[
        ["symbol", "side", "entry_time", "exit_time", "pnl", "exit_reason", "margin", "hold_h"]
    ].astype({"entry_time": str, "exit_time": str}).to_dict(orient="records"),
}

(root / "C_alexg10_DETAIL.json").write_text(json.dumps(detail, indent=2), encoding="utf-8")
print(json.dumps(detail, indent=2))
