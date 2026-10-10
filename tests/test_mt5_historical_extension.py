"""Tests for MT5 historical coverage extension helpers (no live MT5)."""

from __future__ import annotations

import pandas as pd

from scripts.extend_mt5_historical_coverage import (
    EXCLUDED_EVAL_MONTHS,
    month_is_closed,
    universe_hash,
)


def test_universe_hash_stable():
    a = universe_hash(["EURUSD=X", "GBPUSD=X"])
    b = universe_hash(["EURUSD=X", "GBPUSD=X"])
    c = universe_hash(["GBPUSD=X", "EURUSD=X"])
    assert a == b
    assert a != c


def test_excluded_months():
    assert EXCLUDED_MONTHS_OK()


def EXCLUDED_MONTHS_OK() -> bool:
    return EXCLUDED_EVAL_MONTHS == {(2026, 5), (2026, 6), (2026, 7), (2026, 8)}


def test_month_is_closed_september_2026_before_october():
    now = pd.Timestamp("2026-09-25T05:00:00+00:00")
    assert month_is_closed(2026, 8, now=now) is True
    assert month_is_closed(2026, 9, now=now) is False
    assert month_is_closed(2026, 9, now=pd.Timestamp("2026-10-01T00:00:00+00:00")) is True
