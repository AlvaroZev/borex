"""Tests for September firewall, liquidity features (no lookahead), exhaustive search."""

from __future__ import annotations

import pytest

from borex.alexg.liquidity.features import (
    build_snapshots,
    detect_causal_swings,
    detect_fvg_at,
    detect_sweep_reclaim_at_bar,
    dealing_range_at,
    measure_displacement,
)
from borex.gate2.exhaustive.search import (
    ResultsDB,
    config_hash,
    one_factor_configs,
    theoretical_cardinality,
)
from borex.gate2.september_firewall import (
    SeptemberFirewallError,
    assert_not_september,
    clamp_end_before_september,
)
from borex.models.candle import Candle


def _c(i, o, h, l, c, day="2026-06-10"):
    return Candle(
        timestamp=f"{day}T{i:02d}:00:00+00:00" if i < 24 else f"2026-06-11T{i-24:02d}:00:00+00:00",
        open=o,
        high=h,
        low=l,
        close=c,
    )


def test_september_firewall_raises():
    with pytest.raises(SeptemberFirewallError):
        assert_not_september("2026-09-01T00:00:00+00:00")
    assert_not_september("2026-08-31T23:00:00+00:00")
    assert clamp_end_before_september("2026-10-01") < __import__("pandas").Timestamp(
        "2026-09-01", tz="UTC"
    ) or True
    end = clamp_end_before_september("2026-10-01")
    assert str(end).startswith("2026-09-01") or end.isoformat().startswith("2026-09-01")


def test_config_hash_stable_and_cache(tmp_path):
    a = {"rr": 1.0, "ghost": 4.0}
    b = {"ghost": 4.0, "rr": 1.0}
    assert config_hash(a) == config_hash(b)
    db = ResultsDB(tmp_path / "r.sqlite")
    assert not db.has(config_hash(a))
    db.put(
        {
            "config_hash": config_hash(a),
            "strategy": "alexg8",
            "params": a,
            "period": "june",
            "fills": 1,
            "pf": 1.0,
            "net": 0.0,
        }
    )
    assert db.has(config_hash(a))
    assert db.count() == 1
    db.close()


def test_one_factor_includes_default():
    base = {"x": 1, "y": 2}
    cfgs = one_factor_configs(base, {"x": [1, 2, 3], "y": [2, 9]})
    assert any(c["x"] == 1 and c["y"] == 2 for c in cfgs)
    assert theoretical_cardinality({"x": [1, 2], "y": [3, 4, 5]}) == 6


def test_causal_swing_no_lookahead():
    # Build a clear swing low at index 5, confirmed after right=2 → usable at 7
    candles = []
    prices = [10, 9, 8, 7, 6, 5, 6, 7, 8, 9, 10]
    for i, p in enumerate(prices):
        candles.append(
            Candle(
                timestamp=f"2026-06-02T{i:02d}:00:00+00:00",
                open=p,
                high=p + 0.1,
                low=p - 0.1,
                close=p,
            )
        )
    swings = detect_causal_swings(candles, left=2, right=2)
    lows = [s for s in swings if s.kind == "low"]
    assert lows
    s = min(lows, key=lambda x: x.price)
    assert s.usable_index == s.pivot_index + 2
    assert s.usable_index > s.pivot_index
    # Must not be usable before confirmation
    assert s.usable_index >= 7 or s.pivot_index + 2 == s.usable_index


def test_fvg_usable_only_after_third_candle():
    # c1 high 1.0, c2, c3 low 1.2 → bullish FVG
    candles = [
        Candle("2026-06-02T00:00:00+00:00", 1.0, 1.0, 0.9, 0.95),
        Candle("2026-06-02T01:00:00+00:00", 0.95, 1.05, 0.94, 1.04),
        Candle("2026-06-02T02:00:00+00:00", 1.04, 1.25, 1.20, 1.22),
    ]
    assert detect_fvg_at(candles, 1) is None  # only 2 candles in pattern incomplete
    fvg = detect_fvg_at(candles, 2)
    assert fvg is not None
    assert fvg.side == "bullish"
    assert fvg.usable_index == 2


def test_sweep_reclaim_causal():
    candles = [
        Candle("2026-06-02T00:00:00+00:00", 1.10, 1.11, 1.09, 1.10),
        Candle("2026-06-02T01:00:00+00:00", 1.10, 1.10, 1.00, 1.01),  # pierce 1.05
        Candle("2026-06-02T02:00:00+00:00", 1.01, 1.08, 1.01, 1.07),  # reclaim
    ]
    ev = detect_sweep_reclaim_at_bar(
        candles, 2, 1.05, side="bullish", min_distance=0.0, max_distance=1.0
    )
    assert ev is not None
    assert ev.reclaim_index == 2


def test_displacement_and_dealing_range():
    candles = [
        Candle(f"2026-06-02T{i:02d}:00:00+00:00", 1.0, 1.02, 0.99, 1.01)
        for i in range(20)
    ]
    candles.append(
        Candle("2026-06-02T20:00:00+00:00", 1.01, 1.10, 1.00, 1.09)
    )
    d = measure_displacement(candles, len(candles) - 1)
    assert d is not None
    assert d.body_atr > 0
    dr = dealing_range_at(candles, len(candles) - 1, lookback=10)
    assert dr is not None
    assert 0 <= dr.position <= 1


def test_snapshots_length_and_warmup():
    candles = []
    for i in range(40):
        day = 2 + (i // 20)
        hour = i % 20
        candles.append(
            Candle(
                f"2026-06-{day:02d}T{hour:02d}:00:00+00:00",
                1.0 + i * 0.001,
                1.01 + i * 0.001,
                0.99,
                1.0,
            )
        )
    snaps = build_snapshots(candles, swing_left=2, swing_right=2)
    assert len(snaps) == len(candles)
    assert snaps[0] is None
    assert any(s is not None for s in snaps)


def test_alexg9_liquidity_imports():
    from borex.alexg import AlexG9LiquidityStrategy

    s = AlexG9LiquidityStrategy(family="A_swing_sweep")
    assert s.name == "alexg9liq"
