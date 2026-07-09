from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from borex.backtest.portfolio import Portfolio, Trade


@dataclass
class BacktestMetrics:
    total_return_pct: float = 0.0
    cagr_pct: float = 0.0
    sharpe: float = 0.0
    max_drawdown_pct: float = 0.0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    trades: int = 0
    liquidated: bool = False
    final_equity: float = 0.0

    def to_dict(self) -> dict:
        return {
            "total_return_pct": round(self.total_return_pct, 4),
            "cagr_pct": round(self.cagr_pct, 4),
            "sharpe": round(self.sharpe, 4),
            "max_drawdown_pct": round(self.max_drawdown_pct, 4),
            "win_rate": round(self.win_rate, 4),
            "profit_factor": round(self.profit_factor, 4),
            "trades": self.trades,
            "liquidated": self.liquidated,
            "final_equity": round(self.final_equity, 2),
        }


def compute_metrics(
    portfolio: Portfolio,
    equity_curve: list[float],
    bar_count: int,
    bars_per_year: float = 252 * 24,
) -> BacktestMetrics:
    closed = portfolio.closed_trades
    wins = [t for t in closed if t.pnl > 0]
    losses = [t for t in closed if t.pnl <= 0]
    gross_profit = sum(t.pnl for t in wins)
    gross_loss = abs(sum(t.pnl for t in losses))

    initial = portfolio.initial_capital
    final = equity_curve[-1] if equity_curve else portfolio.equity
    total_return = (final / initial - 1) * 100 if initial > 0 else 0.0

    years = bar_count / bars_per_year if bars_per_year > 0 else 0
    cagr = 0.0
    if years > 0 and final > 0 and initial > 0:
        cagr = (math.pow(final / initial, 1 / years) - 1) * 100

    sharpe = 0.0
    if len(equity_curve) > 2:
        rets = np.diff(equity_curve) / np.maximum(equity_curve[:-1], 1e-9)
        std = float(np.std(rets))
        if std > 0:
            sharpe = float(np.mean(rets) / std * math.sqrt(bars_per_year))

    peak = initial
    max_dd = 0.0
    for eq in equity_curve:
        peak = max(peak, eq)
        dd = (peak - eq) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)

    pf = gross_profit / gross_loss if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)

    return BacktestMetrics(
        total_return_pct=total_return,
        cagr_pct=cagr,
        sharpe=sharpe,
        max_drawdown_pct=max_dd * 100,
        win_rate=len(wins) / len(closed) if closed else 0.0,
        profit_factor=pf,
        trades=len(closed),
        liquidated=portfolio.liquidated,
        final_equity=final,
    )
