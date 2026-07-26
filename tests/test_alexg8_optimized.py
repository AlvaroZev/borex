"""Tests for alexg8optimized lazy LTF vicinity loading."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from borex.alexg.ghost_entry import PendingSetup
from borex.alexg.strategy8_optimized import AlexG8OptimizedStrategy, _df_window_to_series
from borex.alexg.ablation import video2_ghost
from borex.models.candle import Candle, SignalAction


def _ts(hour: int = 12, minute: int = 0) -> datetime:
    return datetime(2024, 6, 3, hour, minute, tzinfo=timezone.utc)


def _c(ts: datetime, o: float, h: float, l: float, c: float) -> Candle:
    return Candle(timestamp=ts, open=o, high=h, low=l, close=c, volume=1.0)


def test_alexg8optimized_defaults():
    s = AlexG8OptimizedStrategy()
    assert s.name == "alexg8optimized"
    assert s.ablation == video2_ghost()
    assert s.ltf_intervals == ("1m",)


def test_df_window_to_series_bounds():
    idx = pd.date_range("2024-06-03 11:00", periods=180, freq="1min", tz="UTC")
    df = pd.DataFrame(
        {
            "Open": 1.1,
            "High": 1.101,
            "Low": 1.099,
            "Close": 1.1005,
            "Volume": 1.0,
        },
        index=idx,
    )
    start = pd.Timestamp("2024-06-03 11:30", tz="UTC")
    end = pd.Timestamp("2024-06-03 12:00", tz="UTC")
    series = _df_window_to_series(df, start, end)
    assert len(series.candles) == 30
    assert series.candles[0].timestamp == start
    assert series.candles[-1].timestamp < end


def test_confirm_uses_only_vicinity_attached():
    """With attached LTF, confirm still works but only scans the HTF window."""
    s = AlexG8OptimizedStrategy(
        ltf_require_structure=False,
        ltf_require_momentum=False,
        ltf_require_touch_reject=True,
        ltf_structure_lookback=30,
        ltf_vicinity_pad_bars=5,
    )
    base = _ts(12, 0)
    # Extra history before the HTF hour — should be excluded from vicinity.
    ltf = []
    for i in range(120):
        ts = base - timedelta(minutes=120 - i)
        ltf.append(_c(ts, 1.11, 1.111, 1.109, 1.110))
    price = 1.1030
    for i in range(30):
        o = price
        c = max(1.1005, price - 0.00005)
        ltf.append(_c(base + timedelta(minutes=i), o, o + 0.00005, c - 0.00002, c))
        price = c
    ltf.append(_c(base + timedelta(minutes=30), 1.1006, 1.1008, 1.0998, 1.1005))
    for i in range(31, 60):
        ltf.append(_c(base + timedelta(minutes=i), 1.1005, 1.1007, 1.1003, 1.1006))

    s.attach_ltf({"EURUSD=X": {"1m": ltf}})
    s.set_context("EURUSD=X")

    htf = [
        _c(base - timedelta(hours=1), 1.11, 1.12, 1.10, 1.11),
        _c(base, 1.1010, 1.1015, 1.0995, 1.1008),
    ]
    pending = PendingSetup(
        action=SignalAction.BUY,
        pattern="test",
        stop_loss=1.1000,
        take_profit=1.1100,
        planned_entry=1.1050,
        created_index=0,
        expires_index=10,
    )
    assert s._confirm_ghost_fill(pending, 1, htf) is True
    assert s._ltf_stats["confirms"] == 1
    # Vicinity = lookback(30)+pad(5)+htf hour(60) ≈ 95, not the full 180.
    assert s._ltf_stats["window_bars"] < len(ltf)
    assert s._ltf_stats["window_bars"] <= 30 + 5 + 60
