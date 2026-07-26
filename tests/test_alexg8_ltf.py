"""Tests for alexg8 LTF TP-direction confirmation at ghost SL fill."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from borex.alexg.ghost_entry import PendingSetup
from borex.alexg.ltf_confirm import (
    LtfSeries,
    confirm_intervals,
    first_sl_touch_index,
    ltf_tp_direction_ok,
    momentum_toward_tp,
    touch_rejects_toward_tp,
)
from borex.alexg.strategy8 import AlexG8Strategy
from borex.alexg.ablation import video2_ghost
from borex.models.candle import Candle, SignalAction


def _ts(hour: int = 12, minute: int = 0) -> datetime:
    return datetime(2024, 6, 3, hour, minute, tzinfo=timezone.utc)


def _c(ts: datetime, o: float, h: float, l: float, c: float) -> Candle:
    return Candle(timestamp=ts, open=o, high=h, low=l, close=c, volume=1.0)


def _reject_long_at_sl() -> list[Candle]:
    """1m bars: stay above SL, then touch+reject upward (close back above SL)."""
    bars: list[Candle] = []
    base = _ts(12, 0)
    price = 1.1030
    for i in range(30):
        o = price
        c = max(1.1005, price - 0.00005)
        bars.append(_c(base + timedelta(minutes=i), o, o + 0.00005, c - 0.00002, c))
        price = c
    touch_ts = base + timedelta(minutes=30)
    # Tags SL=1.1000 with low, closes back above (rejection toward TP).
    bars.append(_c(touch_ts, 1.1006, 1.1008, 1.0998, 1.1005))
    return bars


def _drive_through_long_sl() -> list[Candle]:
    """1m bars: tag SL and close through it (wrong way)."""
    bars: list[Candle] = []
    base = _ts(12, 0)
    price = 1.1030
    for i in range(30):
        o = price
        c = max(1.1005, price - 0.00005)
        bars.append(_c(base + timedelta(minutes=i), o, o + 0.00005, c - 0.00002, c))
        price = c
    touch_ts = base + timedelta(minutes=30)
    bars.append(_c(touch_ts, 1.1006, 1.1007, 1.0995, 1.0997))  # close still below SL
    return bars


def test_alexg8_defaults():
    s = AlexG8Strategy()
    assert s.name == "alexg8"
    assert s.ablation == video2_ghost()
    assert s.ablation.require_ghost_sl_entry is True
    assert s.ltf_intervals == ("1m",)
    assert s.ltf_require_touch_reject is True


def test_momentum_toward_tp_buy_vs_sell():
    rising = [
        _c(_ts(12, i), 1.0 + i * 0.01, 1.02 + i * 0.01, 0.99 + i * 0.01, 1.01 + i * 0.01)
        for i in range(4)
    ]
    assert momentum_toward_tp(SignalAction.BUY, rising) is True
    assert momentum_toward_tp(SignalAction.SELL, rising) is False


def test_touch_reject_rules():
    reject = _c(_ts(12, 30), 1.1006, 1.1008, 1.0998, 1.1005)
    through = _c(_ts(12, 30), 1.1006, 1.1007, 1.0995, 1.0997)
    assert touch_rejects_toward_tp(SignalAction.BUY, reject, 1.1000) is True
    assert touch_rejects_toward_tp(SignalAction.BUY, through, 1.1000) is False


def test_first_sl_touch_index_buy():
    bars = _reject_long_at_sl()
    i = first_sl_touch_index(bars, SignalAction.BUY, 1.1000)
    assert i is not None
    assert bars[i].low <= 1.1000


def test_ltf_ok_on_touch_reject():
    bars = _reject_long_at_sl()
    ok = ltf_tp_direction_ok(
        SignalAction.BUY,
        LtfSeries.from_candles(bars),
        stop_loss=1.1000,
        htf_start=_ts(12, 0),
        htf_end=_ts(13, 0),
        require_structure=False,
        require_momentum=False,
        require_touch_reject=True,
    )
    assert ok is True


def test_ltf_rejects_drive_through():
    bars = _drive_through_long_sl()
    ok = ltf_tp_direction_ok(
        SignalAction.BUY,
        LtfSeries.from_candles(bars),
        stop_loss=1.1000,
        htf_start=_ts(12, 0),
        htf_end=_ts(13, 0),
        require_structure=False,
        require_momentum=False,
        require_touch_reject=True,
    )
    assert ok is False


def test_confirm_intervals_missing_policy():
    htf = _c(_ts(12, 0), 1.1, 1.11, 1.09, 1.1)
    ok, passed = confirm_intervals(
        SignalAction.BUY,
        {},
        ("1m",),
        stop_loss=1.09,
        htf_candle=htf,
        execution_interval="1h",
        missing_policy="reject",
    )
    assert ok is False and passed == []
    ok2, _ = confirm_intervals(
        SignalAction.BUY,
        {},
        ("1m",),
        stop_loss=1.09,
        htf_candle=htf,
        execution_interval="1h",
        missing_policy="allow",
    )
    assert ok2 is True


def test_alexg8_confirm_ghost_fill_uses_attached_ltf():
    s = AlexG8Strategy(
        ltf_require_structure=False,
        ltf_require_momentum=False,
        ltf_require_touch_reject=True,
    )
    s.set_context("EURUSD=X")
    s.attach_ltf({"EURUSD=X": {"1m": _reject_long_at_sl()}})

    pending = PendingSetup(
        action=SignalAction.BUY,
        pattern="test",
        stop_loss=1.1000,
        take_profit=1.1100,
        planned_entry=1.1030,
        created_index=0,
        expires_index=100,
    )
    htf_bars = [
        _c(_ts(11, 0), 1.10, 1.11, 1.09, 1.105),
        _c(_ts(12, 0), 1.1020, 1.1030, 1.0998, 1.1010),
    ]
    assert s._confirm_ghost_fill(pending, 1, htf_bars) is True

    s.attach_ltf({"EURUSD=X": {"1m": _drive_through_long_sl()}})
    assert s._confirm_ghost_fill(pending, 1, htf_bars) is False


def test_alexg8_rejects_fill_when_ltf_missing():
    s = AlexG8Strategy(ltf_missing_policy="reject")
    s.set_context("EURUSD=X")
    pending = PendingSetup(
        action=SignalAction.BUY,
        pattern="test",
        stop_loss=1.1000,
        take_profit=1.1100,
        planned_entry=1.1030,
        created_index=0,
        expires_index=100,
    )
    htf_bars = [_c(_ts(12, 0), 1.1020, 1.1030, 1.0998, 1.1010)]
    assert s._confirm_ghost_fill(pending, 0, htf_bars) is False
