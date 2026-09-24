"""Tests for Gate 2 micro fidelity strategy, Theory store, mapping, safety."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from borex.gate2.exit_classify import DEAL_REASON_SL, DEAL_REASON_TP
from borex.gate2.micro_fidelity.candles import CandleBuilder, MinuteCandle, floor_minute_utc
from borex.gate2.micro_fidelity.config import (
    EXPECTED_MIRROR_LOGIN,
    FORBIDDEN_LIVE_LOGIN,
    FROZEN_CONFIG,
    LIVE_EXECUTION_ENABLED,
    REQUIRED_MAGIC,
)
from borex.gate2.micro_fidelity.report import (
    EXIT_CLEANUP_MARKET_EXIT,
    EXIT_STRATEGY_MARKET_EXIT,
    classify_micro_exit,
    early_diagnostic,
    entry_delta_pips,
)
from borex.gate2.micro_fidelity.safety import (
    MicroSafetyError,
    assert_mirror_identity,
    assert_not_live_port,
    assert_protected_eurdkk_intact,
    filter_experiment_positions,
    reject_if_duplicate_theory,
    spread_gate,
)
from borex.gate2.micro_fidelity.strategy import (
    choose_sl_tp_distance,
    evaluate_completed_bar,
    plan_symmetric_sl_tp,
)
from borex.gate2.micro_fidelity.theory import (
    ImmutableJsonlStore,
    build_theory_event,
    map_theory_to_execution,
    new_theory_event_id,
)


def _bars(n: int, *, base: float = 1.1000, step: float = 0.0001) -> list[MinuteCandle]:
    t0 = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)
    out: list[MinuteCandle] = []
    px = base
    for i in range(n):
        o = px
        h = px + 0.0003
        l = px - 0.0003
        c = px + step
        out.append(
            MinuteCandle(
                timestamp_utc=t0 + timedelta(minutes=i),
                open=o,
                high=h,
                low=l,
                close=c,
                tick_count=10,
                completed=True,
            )
        )
        px = c
    return out


def test_1m_candle_construction():
    b = CandleBuilder()
    t0 = datetime(2026, 9, 23, 12, 0, 10, tzinfo=timezone.utc)
    assert b.update(1.1, t0) is None
    assert b.forming is not None
    assert b.forming.open == 1.1
    done = b.update(1.2, t0 + timedelta(minutes=1))
    assert done is not None
    assert done.completed is True
    assert done.close == 1.1
    assert len(b.completed) == 1


def test_timestamp_backwards_rejected():
    b = CandleBuilder()
    t0 = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)
    b.update(1.1, t0)
    with pytest.raises(ValueError, match="backwards"):
        b.update(1.1, t0 - timedelta(minutes=1))


def test_h1_not_used_signal_is_1m():
    assert FROZEN_CONFIG.timeframe == "1m"


def test_deterministic_long_signal():
    bars = _bars(8, step=0.0)
    # Force breakout: last close above prev high + threshold
    prev = bars[-2]
    curr = MinuteCandle(
        timestamp_utc=bars[-1].timestamp_utc,
        open=prev.close,
        high=prev.high + 0.0020,
        low=prev.low,
        close=prev.high + 0.0015,
        completed=True,
    )
    bars[-1] = curr
    sig = evaluate_completed_bar(symbol="EURUSD=X", completed=bars, pip_size=0.0001)
    assert sig is not None
    assert sig.direction == "long"
    assert sig.side == "buy"
    assert sig.expected_entry_side == "ASK"
    assert sig.timeframe == "1m"


def test_deterministic_short_signal():
    bars = _bars(8, step=0.0)
    prev = bars[-2]
    curr = MinuteCandle(
        timestamp_utc=bars[-1].timestamp_utc,
        open=prev.close,
        high=prev.high,
        low=prev.low - 0.0020,
        close=prev.low - 0.0015,
        completed=True,
    )
    bars[-1] = curr
    sig = evaluate_completed_bar(symbol="EURUSD=X", completed=bars, pip_size=0.0001)
    assert sig is not None
    assert sig.direction == "short"
    assert sig.expected_entry_side == "BID"


def test_cooldown_suppresses_signal():
    bars = _bars(8, step=0.0)
    prev = bars[-2]
    bars[-1] = MinuteCandle(
        timestamp_utc=bars[-1].timestamp_utc,
        open=prev.close,
        high=prev.high + 0.002,
        low=prev.low,
        close=prev.high + 0.0015,
        completed=True,
    )
    sig = evaluate_completed_bar(
        symbol="EURUSD=X",
        completed=bars,
        pip_size=0.0001,
        bars_since_last_signal=0,
    )
    assert sig is None


def test_theory_event_persistence_and_immutability(tmp_path: Path):
    store = ImmutableJsonlStore(tmp_path / "theory_events.jsonl")
    tid = new_theory_event_id()
    row = build_theory_event(
        run_id="g2m_test",
        git_commit="abc",
        signal={
            "strategy_name": "gate2_micro_fidelity",
            "strategy_version": "v1",
            "symbol": "EURUSD=X",
            "direction": "long",
            "side": "buy",
            "signal_timestamp_utc": "2026-09-23T12:01:00+00:00",
            "source_candle_timestamp_utc": "2026-09-23T12:00:00+00:00",
            "source_candle": {"open": 1.1, "high": 1.2, "low": 1.0, "close": 1.15},
            "prev_candle": {},
            "timeframe": "1m",
            "pattern": "prev_bar_breakout_1m",
            "expected_entry_side": "ASK",
            "parameters": {},
        },
        quote={"bid": 1.1, "ask": 1.1002, "spread_price": 0.0002, "spread_pips": 2.0},
        expected_entry=1.1002,
        expected_sl=1.0998,
        expected_tp=1.1006,
        expected_risk_usd=0.4,
        distance_meta={"sl_trigger_side": "BID", "tp_trigger_side": "BID"},
        config={"x": 1},
        candle_sequence=[],
        theory_event_id=tid,
    )
    store.append(row, id_key="theory_event_id")
    with pytest.raises(ValueError, match="immutable"):
        store.append(row, id_key="theory_event_id")


def test_direct_theory_execution_mapping():
    m = map_theory_to_execution(
        theory_event_id="tev_abc",
        execution_theory_event_id="tev_abc",
    )
    assert m["mapping_status"] == "EXACT_MATCH"
    assert m["confidence"] == "HIGH"


def test_mapping_error_on_id_mismatch():
    m = map_theory_to_execution(
        theory_event_id="tev_a",
        execution_theory_event_id="tev_b",
    )
    assert m["mapping_status"] == "MAPPING_ERROR"


def test_unmapped_broker_trade():
    m = map_theory_to_execution(theory_event_id=None, execution_theory_event_id="tev_x")
    assert m["mapping_status"] == "UNMAPPED"


def test_duplicate_prevention():
    assert (
        reject_if_duplicate_theory(
            theory_store_has=True, execution_store_has=True, open_experiment_for_symbol=False
        )
        == "theory_event_already_executed"
    )
    assert (
        reject_if_duplicate_theory(
            theory_store_has=False, execution_store_has=False, open_experiment_for_symbol=True
        )
        == "open_experiment_position_for_symbol"
    )
    assert (
        reject_if_duplicate_theory(
            theory_store_has=False, execution_store_has=False, open_experiment_for_symbol=False
        )
        is None
    )


def test_spread_gate():
    assert spread_gate(1.5, 2.0)["ok"] is True
    assert spread_gate(2.5, 2.0)["ok"] is False


def test_stop_distance_validation():
    ok = choose_sl_tp_distance(
        pip_size=0.0001,
        min_stop_distance_price=0.00015,
        spread_price=0.0001,
        freeze_level_price=0.0,
        target_pips=4.0,
        max_pips=8.0,
    )
    assert ok["ok"] is True
    bad = choose_sl_tp_distance(
        pip_size=0.0001,
        min_stop_distance_price=0.002,
        spread_price=0.0001,
        freeze_level_price=0.0,
        target_pips=4.0,
        max_pips=8.0,
    )
    assert bad["ok"] is False


def test_long_bid_ask_handling():
    lvl = plan_symmetric_sl_tp(side="buy", entry_ref=1.1000, distance_price=0.0004, digits=5)
    assert lvl["entry_side"] == "ASK"
    assert lvl["exit_side"] == "BID"
    assert lvl["sl"] < 1.1000 < lvl["tp"]
    assert lvl["sl_trigger_side"] == "BID"
    assert lvl["tp_trigger_side"] == "BID"


def test_short_bid_ask_handling():
    lvl = plan_symmetric_sl_tp(side="sell", entry_ref=1.1000, distance_price=0.0004, digits=5)
    assert lvl["entry_side"] == "BID"
    assert lvl["exit_side"] == "ASK"
    assert lvl["tp"] < 1.1000 < lvl["sl"]
    assert lvl["sl_trigger_side"] == "ASK"
    assert lvl["tp_trigger_side"] == "ASK"


def test_broker_sl_tp_and_cleanup_classification():
    sl = classify_micro_exit(exit_deal={"reason": DEAL_REASON_SL, "price": 1.1})
    assert sl["classification"] == "BROKER_SL"
    tp = classify_micro_exit(exit_deal={"reason": DEAL_REASON_TP, "price": 1.1})
    assert tp["classification"] == "BROKER_TP"
    cu = classify_micro_exit(exit_deal={"reason": 1}, intentional_cleanup=True)
    assert cu["classification"] == EXIT_CLEANUP_MARKET_EXIT
    st = classify_micro_exit(exit_deal={"reason": 1}, intentional_strategy_exit=True)
    assert st["classification"] == EXIT_STRATEGY_MARKET_EXIT


def test_protected_position_isolation():
    positions = [
        {"ticket": 1890205005, "symbol": "EURDKK", "magic": 88002, "comment": "old"},
        {"ticket": 99, "symbol": "EURUSD", "magic": 88002, "comment": "bx_g2m_abc"},
        {"ticket": 100, "symbol": "EURUSD", "magic": 88002, "comment": "bx_g2p_other"},
    ]
    exp = filter_experiment_positions(positions, comment_prefix="bx_g2m_", magic=88002)
    assert len(exp) == 1
    assert exp[0]["ticket"] == 99
    info = assert_protected_eurdkk_intact(positions)
    assert 1890205005 in info["present"]
    # Disappearance of baseline is hard fail
    with pytest.raises(MicroSafetyError):
        assert_protected_eurdkk_intact(
            [{"ticket": 1, "symbol": "EURUSD"}],
            baseline={1890205005},
        )


def test_mirror_account_validation_and_live_rejection():
    assert_mirror_identity(
        {
            "login": EXPECTED_MIRROR_LOGIN,
            "server": "ICMarketsSC-Demo",
            "magic": REQUIRED_MAGIC,
            "port_convention": 8792,
            "server_offset_confident": True,
        }
    )
    with pytest.raises(MicroSafetyError):
        assert_mirror_identity(
            {
                "login": FORBIDDEN_LIVE_LOGIN,
                "server": "ICMarketsSC-Demo",
                "magic": REQUIRED_MAGIC,
                "port_convention": 8792,
            }
        )
    with pytest.raises(MicroSafetyError):
        assert_not_live_port(8791)


def test_live_execution_flag_present():
    # Enabled only after explicit human approval for Mirror runs.
    assert isinstance(LIVE_EXECUTION_ENABLED, bool)


def test_server_offset_10800_floor_minute():
    # Broker wall 15:00 with offset 10800 → UTC 12:00
    raw = int(datetime(2026, 9, 23, 15, 0, tzinfo=timezone.utc).timestamp())
    utc = datetime.fromtimestamp(raw - 10800, tz=timezone.utc)
    assert floor_minute_utc(utc).hour == 12


def test_missing_1m_insufficient_bars():
    bars = _bars(3)
    assert evaluate_completed_bar(symbol="EURUSD=X", completed=bars, pip_size=0.0001) is None


def test_entry_delta_and_early_diagnostic():
    d = entry_delta_pips(symbol="EURUSD=X", theory_entry=1.1000, broker_entry=1.1001)
    assert abs(d - 1.0) < 1e-9
    row = {
        "mapping_status": "EXACT_MATCH",
        "confidence": "HIGH",
        "entry_delta_pips": 0.2,
        "broker_exit_reason": "BROKER_SL",
        "theory_event_id": "tev_1",
    }
    early = early_diagnostic(row)
    assert early["verdict"] == "CONSISTENT_WITH_EXPECTED_FIDELITY"
    assert "N=1" in early["note"]


def test_no_alexg_import_in_micro_package():
    import borex.gate2.micro_fidelity as pkg
    import borex.gate2.micro_fidelity.strategy as strat

    src = Path(strat.__file__).read_text(encoding="utf-8")
    assert "from borex.alexg" not in src
    assert "import alexg" not in src
    assert "strategy9" not in src.lower()
    assert pkg.STRATEGY_NAME == "gate2_micro_fidelity"


def test_controlled_vs_natural_comment_prefix_separation():
    assert FROZEN_CONFIG.comment_prefix == "bx_g2m_"
    assert FROZEN_CONFIG.comment_prefix != "bx_g2p_"
    assert FROZEN_CONFIG.comment_prefix != "bx_g2e_"
