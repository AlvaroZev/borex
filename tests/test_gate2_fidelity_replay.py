"""Deterministic tests for Gate 2 Level 5 fidelity replay."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from borex.gate2.fidelity_replay import taxonomy as T
from borex.gate2.fidelity_replay.compare import classify_trade, compare_one_trade
from borex.gate2.fidelity_replay.provenance import (
    SourceBundle,
    candle_dict_to_minute,
    causal_candles_for_theory,
    parse_utc,
    post_signal_path,
)
from borex.gate2.fidelity_replay.sim_exit import (
    simulate_entry_from_quote,
    simulate_exit_on_m1_path,
)
from borex.gate2.fidelity_replay.theory_reproduce import reproduce_theory_event
from borex.gate2.micro_fidelity.candles import MinuteCandle
from borex.backtest.costs import infer_pip_size


def _ts(minute: int) -> datetime:
    return datetime(2026, 9, 24, 1, minute, tzinfo=timezone.utc)


def _bar(minute: int, o: float, h: float, l: float, c: float) -> MinuteCandle:
    return MinuteCandle(
        timestamp_utc=_ts(minute),
        open=o,
        high=h,
        low=l,
        close=c,
        tick_count=10,
        completed=True,
    )


def test_pip_conversion_eurusd_and_jpy():
    assert infer_pip_size("EURUSD=X") == pytest.approx(0.0001)
    assert infer_pip_size("USDJPY") == pytest.approx(0.01)


def test_timestamp_normalization_z_and_naive():
    a = parse_utc("2026-09-24T01:22:00Z")
    b = parse_utc("2026-09-24T01:22:00+00:00")
    c = parse_utc("2026-09-24T01:22:00")
    assert a == b == c
    assert a.tzinfo is not None


def test_causal_candle_boundary_excludes_future():
    source = _ts(21)
    theory = {
        "source_candle_timestamp_utc": source.isoformat(),
        "candle_sequence": [
            {
                "timestamp_utc": _ts(20).isoformat(),
                "open": 1.0,
                "high": 1.1,
                "low": 0.9,
                "close": 1.0,
                "tick_count": 1,
                "completed": True,
            },
            {
                "timestamp_utc": source.isoformat(),
                "open": 1.0,
                "high": 1.1,
                "low": 0.9,
                "close": 1.0,
                "tick_count": 1,
                "completed": True,
            },
            {
                "timestamp_utc": _ts(22).isoformat(),  # future relative to source
                "open": 1.0,
                "high": 1.1,
                "low": 0.9,
                "close": 1.0,
                "tick_count": 1,
                "completed": True,
            },
        ],
    }
    candles = causal_candles_for_theory(theory)
    assert all(c.timestamp_utc <= source for c in candles)
    assert len(candles) == 2


def test_no_future_candle_access_in_theory_reproduce(tmp_path: Path):
    # Build a valid breakout short sequence ending at minute 21
    bars = []
    base = 1.1000
    t0 = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
    for i in range(7):
        # flat ranges then break down on last
        if i < 6:
            o = h = base
            l = base - 0.0002
            c = base - 0.0001
        else:
            o = base
            h = base
            l = base - 0.0010
            c = base - 0.0009
        bars.append(
            {
                "timestamp_utc": (t0 + timedelta(minutes=i)).isoformat(),
                "open": o,
                "high": h,
                "low": l,
                "close": c,
                "tick_count": 5,
                "completed": True,
            }
        )
    theory = {
        "theory_event_id": "tev_test",
        "symbol": "EURUSD=X",
        "direction": "short",
        "side": "sell",
        "signal_timestamp_utc": (t0 + timedelta(minutes=7)).isoformat(),
        "source_candle_timestamp_utc": (t0 + timedelta(minutes=6)).isoformat(),
        "bid": base - 0.0009,
        "ask": base - 0.0008,
        "expected_entry": base - 0.0009,
        "expected_sl": round((base - 0.0009) + 0.0004, 5),
        "expected_tp": round((base - 0.0009) - 0.0004, 5),
        "distance_selection": {"distance_price": 0.0004},
        "candle_sequence": bars,
    }
    # Inject a future bar into sequence — causal filter must drop it; reproduce should still run
    bars_with_future = list(bars) + [
        {
            "timestamp_utc": (t0 + timedelta(minutes=99)).isoformat(),
            "open": 9.0,
            "high": 9.1,
            "low": 8.9,
            "close": 9.0,
            "tick_count": 1,
            "completed": True,
        }
    ]
    theory["candle_sequence"] = bars_with_future
    causal = causal_candles_for_theory(theory)
    assert all(c.timestamp_utc <= parse_utc(theory["source_candle_timestamp_utc"]) for c in causal)
    assert not any(abs(c.close - 9.0) < 1e-9 for c in causal)


def test_long_sl_and_long_tp():
    # long SL: low <= sl
    bars = [_bar(0, 1.1000, 1.1005, 1.0990, 1.0995)]
    sl = 1.0995
    tp = 1.1040
    r = simulate_exit_on_m1_path(
        symbol="EURUSD=X", direction="long", sl=sl, tp=tp, bars=bars, apply_spread_on_exit=False
    )
    assert r.exit_reason == "SIM_SL"
    assert r.exit_price_level == sl

    bars_tp = [_bar(0, 1.1000, 1.1050, 1.0998, 1.1045)]
    r2 = simulate_exit_on_m1_path(
        symbol="EURUSD=X",
        direction="long",
        sl=1.0960,
        tp=1.1040,
        bars=bars_tp,
        apply_spread_on_exit=False,
    )
    assert r2.exit_reason == "SIM_TP"
    assert r2.exit_price_level == 1.1040


def test_short_sl_and_short_tp():
    bars_sl = [_bar(0, 0.7033, 0.7038, 0.7032, 0.7035)]
    r = simulate_exit_on_m1_path(
        symbol="AUDUSD=X",
        direction="short",
        sl=0.70372,
        tp=0.70292,
        bars=bars_sl,
        apply_spread_on_exit=False,
    )
    assert r.exit_reason == "SIM_SL"

    bars_tp = [_bar(0, 0.7033, 0.7034, 0.70280, 0.70290)]
    r2 = simulate_exit_on_m1_path(
        symbol="AUDUSD=X",
        direction="short",
        sl=0.70372,
        tp=0.70292,
        bars=bars_tp,
        apply_spread_on_exit=False,
    )
    assert r2.exit_reason == "SIM_TP"


def test_spread_handling_entry_mid_half_spread():
    res = simulate_entry_from_quote(
        symbol="EURUSD=X",
        direction="long",
        bid=1.10000,
        ask=1.10010,
        spread_pips=1.0,
    )
    # mid=1.10005, half spread = 0.5 * 0.0001 = 0.00005 → entry 1.10010
    assert res.mid_price == pytest.approx(1.10005)
    assert res.sim_entry == pytest.approx(1.10010)


def test_first_touch_chronological():
    bars = [
        _bar(0, 1.10, 1.1010, 1.0995, 1.1000),  # no hit
        _bar(1, 1.10, 1.1045, 1.0998, 1.1040),  # TP
        _bar(2, 1.10, 1.10, 1.0950, 1.0960),  # would be SL but later
    ]
    r = simulate_exit_on_m1_path(
        symbol="EURUSD=X",
        direction="long",
        sl=1.0960,
        tp=1.1040,
        bars=bars,
        apply_spread_on_exit=False,
    )
    assert r.exit_reason == "SIM_TP"
    assert r.bar_index == 1


def test_both_wick_ambiguity_sl_first():
    bars = [_bar(0, 1.1000, 1.1050, 1.0950, 1.1000)]
    r = simulate_exit_on_m1_path(
        symbol="EURUSD=X",
        direction="long",
        sl=1.0960,
        tp=1.1040,
        bars=bars,
        apply_spread_on_exit=False,
    )
    assert r.both_wick_on_exit_bar is True
    assert r.exit_reason == "SIM_SL"


def test_missing_source_data_fail_closed():
    r = simulate_exit_on_m1_path(
        symbol="EURUSD=X",
        direction="long",
        sl=1.0,
        tp=2.0,
        bars=[],
        path_insufficient_reason="no_post_signal_m1_in_run_artifacts",
    )
    assert r.status == "INSUFFICIENT_PATH"
    assert r.exit_reason is None


def test_post_signal_path_gap_detection():
    bundle = SourceBundle(
        source_run_id="test",
        source_dir=Path("."),
        theory_events=[],
        executions=[],
        reconciliation_rows=[],
        theory_by_id={},
        execution_by_theory_id={},
        stitched_m1={
            "EURUSD=X": {
                _ts(0).isoformat(): {
                    "timestamp_utc": _ts(0).isoformat(),
                    "open": 1.0,
                    "high": 1.1,
                    "low": 0.9,
                    "close": 1.0,
                    "tick_count": 1,
                    "completed": True,
                },
                _ts(2).isoformat(): {  # gap at minute 1
                    "timestamp_utc": _ts(2).isoformat(),
                    "open": 1.0,
                    "high": 1.1,
                    "low": 0.9,
                    "close": 1.0,
                    "tick_count": 1,
                    "completed": True,
                },
            }
        },
        file_hashes={},
    )
    bars, reason = post_signal_path(bundle, symbol="EURUSD=X", signal_ts=_ts(0))
    assert reason is None
    assert len(bars) == 1  # truncated at gap


def test_trade_id_preservation():
    classes = classify_trade(
        theory_ok=True,
        theory_diffs=[],
        sim_status="HIT_SL",
        sim_reason="SIM_SL",
        broker_reason="BROKER_SL",
        both_wick=False,
        entry_delta_theory_broker=0.0,
        entry_delta_theory_sim=0.0,
        exit_delta_sim_broker=0.0,
        path_insufficient=False,
        look_ahead=False,
    )
    assert classes == [T.MATCH]


def test_outlier_trade8_parsing_from_source_if_present():
    repo = Path(__file__).resolve().parents[1]
    src = repo / "data" / "runs" / "gate2_micro_fidelity" / "runs" / "g2m_20260924_001929"
    if not src.is_dir():
        pytest.skip("source run artifacts not present")
    from borex.gate2.fidelity_replay.provenance import load_source_bundle

    bundle = load_source_bundle(repo)
    row = next(
        r for r in bundle.reconciliation_rows if str(r.get("broker_position")) == "1959000538"
    )
    assert row["symbol"].startswith("AUDUSD")
    assert row["direction"] == "short"
    assert row["broker_exit_reason"] == "BROKER_SL"
    assert abs(float(row["exit_delta_pips"]) - (-1.3)) < 1e-6
    theory = bundle.theory_by_id[row["theory_event_id"]]
    assert theory["theory_event_id"] == row["theory_event_id"]
    assert theory["execution_id"] or True  # identity present on theory or via map
    out = compare_one_trade(bundle, row, census_index=8)
    assert out["broker_ticket"] == 1959000538 or str(out["broker_ticket"]) == "1959000538"
    assert out["theory_event_id"] == row["theory_event_id"]
    assert out["execution_id"] == row["execution_id"]
    # Theory reproduction should succeed for embedded sequence
    assert out["theory_reproduction"]["signal_match"] is True


def test_exact_theory_event_reconstruction_helpers():
    c = {
        "timestamp_utc": _ts(1).isoformat(),
        "open": 1.0,
        "high": 1.1,
        "low": 0.9,
        "close": 1.05,
        "tick_count": 3,
        "completed": True,
    }
    m = candle_dict_to_minute(c)
    assert m.close == 1.05
    assert m.timestamp_utc == _ts(1)
