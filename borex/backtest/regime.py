from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from borex.backtest.engine import BacktestResult
from borex.backtest.portfolio import Trade


@dataclass(frozen=True)
class RegimeConfig:
    trend_lookback: int = 20
    trend_threshold_pct: float = 0.5
    vol_lookback: int = 20
    vol_high_percentile: float = 67.0


def _trend_label(ret_pct: float, threshold: float) -> str:
    if ret_pct > threshold:
        return "bull"
    if ret_pct < -threshold:
        return "bear"
    return "sideways"


def compute_regime_labels(df: pd.DataFrame, cfg: RegimeConfig | None = None) -> pd.DataFrame:
    """Per-bar trend + volatility regime labels aligned to OHLC index."""
    rcfg = cfg or RegimeConfig()
    close = df["Close"].astype(float)
    rets = close.pct_change()
    trend_ret = close.pct_change(rcfg.trend_lookback) * 100
    vol = rets.rolling(rcfg.vol_lookback).std() * np.sqrt(252 * 24)  # annualized-ish for intraday
    vol_thresh = vol.quantile(rcfg.vol_high_percentile / 100.0)

    trend = trend_ret.apply(lambda x: _trend_label(float(x), rcfg.trend_threshold_pct) if pd.notna(x) else "unknown")
    volatility = vol.apply(lambda x: "high_vol" if pd.notna(x) and x >= vol_thresh else "low_vol" if pd.notna(x) else "unknown")

    return pd.DataFrame({"trend": trend, "volatility": volatility}, index=df.index)


def _lookup_regime(labels: pd.DataFrame, ts: object) -> tuple[str, str]:
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    else:
        t = t.tz_convert("UTC")
    idx = labels.index
    pos = idx.searchsorted(t, side="right") - 1
    if pos < 0:
        return "unknown", "unknown"
    row = labels.iloc[pos]
    return str(row["trend"]), str(row["volatility"])


def analyze_trades_by_regime(
    result: BacktestResult,
    df: pd.DataFrame,
    *,
    cfg: RegimeConfig | None = None,
) -> dict:
    """Bucket closed trades by trend/volatility regime at entry."""
    labels = compute_regime_labels(df, cfg)
    buckets: dict[str, dict] = {}

    def _acc(key: str, trade: Trade) -> None:
        b = buckets.setdefault(
            key,
            {"trades": 0, "wins": 0, "total_pnl": 0.0, "gross_profit": 0.0, "gross_loss": 0.0},
        )
        b["trades"] += 1
        b["total_pnl"] += trade.pnl
        if trade.pnl > 0:
            b["wins"] += 1
            b["gross_profit"] += trade.pnl
        else:
            b["gross_loss"] += abs(trade.pnl)

    for trade in result.trades:
        trend, vol = _lookup_regime(labels, trade.entry_time)
        _acc(f"trend:{trend}", trade)
        _acc(f"vol:{vol}", trade)
        _acc(f"{trend}+{vol}", trade)

    out: dict[str, dict] = {}
    for key, b in buckets.items():
        gp, gl = b["gross_profit"], b["gross_loss"]
        pf = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
        out[key] = {
            "trades": b["trades"],
            "win_rate": round(b["wins"] / b["trades"], 4) if b["trades"] else 0.0,
            "total_pnl": round(b["total_pnl"], 2),
            "profit_factor": round(pf, 4) if pf != float("inf") else None,
        }
    return out
