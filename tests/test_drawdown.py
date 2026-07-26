"""Drawdown must be peak-to-trough of account equity, not vs initial capital."""

from __future__ import annotations

from borex.backtest.engine import BacktestEngine
from borex.backtest.multi_portfolio import MultiMarketPortfolio
from borex.backtest.portfolio import Portfolio
from borex.models.candle import SignalAction


def test_update_drawdown_uses_peak_not_initial():
    peak, max_dd = 1000.0, 0.0
    # Grow far above initial, then drop 20% from the peak.
    for eq in (1000.0, 5000.0, 10_000.0, 8000.0):
        peak, max_dd = BacktestEngine._update_drawdown(peak, max_dd, eq, eq)
    assert peak == 10_000.0
    assert abs(max_dd - 0.2) < 1e-9
    # Wrong formula vs initial $1000 would be 2.0 (200%).
    assert max_dd < 1.0


def test_margin_unrealized_capped_so_dd_not_fake_wipe():
    """5000x MTM beyond stop must not mark equity to $0 while margin remains."""
    pf = MultiMarketPortfolio(
        initial_capital=1000.0,
        position_size_pct=0.01,
        leverage=5000.0,
        size_mode="margin",
        max_positions=5,
    )
    assert pf.open_position(
        "EURUSD=X",
        SignalAction.BUY,
        index=0,
        price=1.1000,
        timestamp=None,
        pattern="test",
        stop_loss=1.0990,
        take_profit=1.1030,
    )
    # 1% adverse move ≫ margin-stop distance (1/5000) — uncapped MTM would
    # wipe the account; capped mark keeps equity at cash (lost only the margin).
    eq = pf.equity_at_prices({"EURUSD=X": 1.1000 * 0.99})
    assert eq == pf.cash  # margin + (-margin)
    assert eq > 0


def test_single_portfolio_margin_unrealized_cap():
    pf = Portfolio(
        initial_capital=1000.0,
        position_size_pct=0.01,
        leverage=5000.0,
        size_mode="margin",
    )
    pf.open_position(
        SignalAction.BUY,
        index=0,
        price=1.1000,
        timestamp=None,
        pattern="test",
    )
    trade = pf.open_trade
    assert trade is not None
    eq = pf.equity_at(trade.entry_price * 0.99)
    assert eq == pf.cash
    assert eq > 0
