from __future__ import annotations

from borex.backtest.costs import ExecutionStats
from borex.backtest.portfolio import Portfolio, Trade


def finalize_execution_stats(portfolio: Portfolio, stats: ExecutionStats) -> ExecutionStats:
    """Compute execution drag after all trades are closed."""
    closed = portfolio.closed_trades
    if not closed:
        stats.execution_drag = 0.0
        return stats

    if stats.theoretical_pnl == 0.0:
        theo = 0.0
        actual = sum(t.pnl for t in closed)
        for t in closed:
            if t.signal_entry_price > 0 and t.signal_exit_price is not None:
                theo_pct = portfolio._pnl_pct_at(t, t.signal_entry_price, t.signal_exit_price)
                theo += t.margin * theo_pct * portfolio.leverage
        stats.theoretical_pnl = theo
        stats.actual_pnl = actual
    else:
        stats.actual_pnl = sum(t.pnl for t in closed)

    drag = stats.theoretical_pnl - stats.actual_pnl
    stats.execution_drag = drag
    return stats
