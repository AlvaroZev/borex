from __future__ import annotations

from datetime import datetime, timezone

from borex.backtest.decision_replay import index_decisions_by_bar, signal_from_decision
from borex.backtest.engine import BacktestConfig
from borex.backtest.multi_market_engine import MultiMarketEngine
from borex.models.candle import Candle, SignalAction
from borex.alexg.strategy3 import AlexG3Strategy


def _candle(ts: datetime, o: float, h: float, l: float, c: float) -> Candle:
    return Candle(timestamp=ts, open=o, high=h, low=l, close=c, volume=1.0)


def test_signal_from_decision_roundtrip():
    row = {
        "symbol": "EURUSD=X",
        "index": 42,
        "action": "buy",
        "pattern": "alexg7|EURUSD=X|demand|1h|engulf|g:sl=1.08",
        "price": 1.0850,
        "stop_loss": 1.0800,
        "take_profit": 1.1000,
        "time_unix": 1_700_000_000,
    }
    sig = signal_from_decision(row, default_score=3.0)
    assert sig.action == SignalAction.BUY
    assert sig.index == 42
    assert abs(sig.price - 1.0850) < 1e-9
    assert abs(sig.stop_loss - 1.0800) < 1e-9
    assert sig.score == 3.0
    assert "|g:" in sig.pattern


def test_index_decisions_by_bar():
    decisions = [
        {
            "symbol": "EURUSD=X",
            "index": 10,
            "action": "buy",
            "pattern": "p",
            "price": 1.1,
            "stop_loss": 1.0,
            "take_profit": 1.2,
            "time_unix": 100,
        },
        {
            "symbol": "GBPUSD=X",
            "index": 10,
            "action": "sell",
            "pattern": "p",
            "price": 1.2,
            "stop_loss": 1.3,
            "take_profit": 1.0,
            "time_unix": 100,
        },
    ]
    by_bar = index_decisions_by_bar(decisions, default_score=2.0)
    assert set(by_bar) == {("EURUSD=X", 10), ("GBPUSD=X", 10)}
    assert by_bar[("EURUSD=X", 10)][0].action == SignalAction.BUY


def test_replay_matches_live_open_count():
    """Synthetic: one replayed ghost buy should open via true-SL path."""
    from datetime import timedelta

    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    candles = []
    for i in range(150):
        px = 1.1000 + i * 0.0001
        ts = base + timedelta(hours=i)
        candles.append(_candle(ts, px, px + 0.0005, px - 0.0005, px))

    fill_i = 130
    fill_px = float(candles[fill_i].low)
    decisions = [
        {
            "symbol": "EURUSD=X",
            "index": fill_i,
            "action": "buy",
            "pattern": "alexg7|EURUSD=X|demand|1h|engulf|g:sl=1.0",
            "price": fill_px,
            "stop_loss": fill_px - 0.001,
            "take_profit": fill_px + 0.003,
            "time_unix": int(candles[fill_i].timestamp.timestamp()),
        }
    ]

    strategy = AlexG3Strategy(min_rr=3.0, min_bars=120)
    config = BacktestConfig(
        initial_capital=1000.0,
        leverage=5000.0,
        position_size_pct=0.01,
        size_mode="margin",
        true_sl=True,
        true_sl_rr=3.0,
        rr_mode="fixed",
        rr_factor=1.0,
        stop_loss_pct=None,
        take_profit_pct=None,
    )
    engine = MultiMarketEngine(strategy, config, max_positions=5)
    result = engine.run(
        {"EURUSD=X": candles},
        timeframe="1h",
        master_symbol="EURUSD=X",
        decisions=decisions,
    )
    assert result.total_trades >= 1
