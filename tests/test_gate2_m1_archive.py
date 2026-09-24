"""Tests for continuous M1 archive (Level 5b evidence capture)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from borex.gate2.micro_fidelity.candles import MinuteCandle
from borex.gate2.micro_fidelity.m1_archive import (
    ContinuousM1Archive,
    load_trade_windows,
    parse_utc,
)


def _bar(t0: datetime, i: int, px: float = 1.1) -> MinuteCandle:
    return MinuteCandle(
        timestamp_utc=t0 + timedelta(minutes=i),
        open=px,
        high=px + 0.0002,
        low=px - 0.0002,
        close=px + 0.0001,
        tick_count=5,
        completed=True,
    )


def test_continuous_ingest_no_gaps(tmp_path: Path):
    t0 = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
    arch = ContinuousM1Archive(run_dir=tmp_path, symbols=["EURUSD=X"])
    bars = [_bar(t0, i) for i in range(5)]
    st = arch.ingest_completed("EURUSD=X", bars)
    assert st.candles_appended == 5
    assert st.gaps_recorded == 0
    rep = arch.continuity_report("EURUSD=X")
    assert rep["continuous"] is True
    assert rep["n"] == 5


def test_gap_recorded_on_missing_minute(tmp_path: Path):
    t0 = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
    arch = ContinuousM1Archive(run_dir=tmp_path, symbols=["EURUSD=X"])
    arch.ingest_completed("EURUSD=X", [_bar(t0, 0), _bar(t0, 1)])
    st = arch.ingest_completed("EURUSD=X", [_bar(t0, 4)])  # skip 2,3
    assert st.gaps_recorded == 1
    assert st.candles_appended == 1
    gaps = (tmp_path / "m1_archive" / "gaps.jsonl").read_text(encoding="utf-8")
    assert "missing_completed_m1_between_persisted_bars" in gaps


def test_duplicates_skipped(tmp_path: Path):
    t0 = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
    arch = ContinuousM1Archive(run_dir=tmp_path, symbols=["EURUSD=X"])
    arch.ingest_completed("EURUSD=X", [_bar(t0, 0)])
    st = arch.ingest_completed("EURUSD=X", [_bar(t0, 0), _bar(t0, 1)])
    assert st.duplicates_skipped == 1
    assert st.candles_appended == 1
    assert arch.continuity_report("EURUSD=X")["n"] == 2


def test_fetch_failed_fail_closed(tmp_path: Path):
    arch = ContinuousM1Archive(run_dir=tmp_path, symbols=["EURUSD=X"])
    st = arch.ingest_completed("EURUSD=X", [], fetch_failed=True)
    assert st.gaps_recorded == 1
    assert "fetch_failed_fail_closed" in (tmp_path / "m1_archive" / "gaps.jsonl").read_text(
        encoding="utf-8"
    )


def test_trade_window_index_and_path(tmp_path: Path):
    t0 = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
    arch = ContinuousM1Archive(
        run_dir=tmp_path,
        symbols=["AUDUSD=X"],
        pre_signal_buffer_minutes=5,
        post_exit_tail_minutes=2,
    )
    arch.ingest_completed("AUDUSD=X", [_bar(t0, i, 0.70) for i in range(20)])
    sig = t0 + timedelta(minutes=10)
    arch.open_trade_window(
        theory_event_id="tev_x",
        execution_id="exe_x",
        broker_ticket=123,
        symbol="AUDUSD=X",
        signal_time_utc=sig,
        source_candle_utc=sig - timedelta(minutes=1),
    )
    exit_ts = sig + timedelta(minutes=5)
    arch.close_trade_window(
        theory_event_id="tev_x",
        exit_observed_utc=exit_ts,
        broker_ticket=123,
        execution_id="exe_x",
    )
    windows = load_trade_windows(tmp_path / "m1_archive")
    assert windows["tev_x"]["status"] == "CLOSED"
    assert windows["tev_x"]["broker_ticket"] == 123
    start = parse_utc(windows["tev_x"]["window_start_utc"])
    end = parse_utc(windows["tev_x"]["window_end_utc"])
    path = arch.path_for_window(symbol="AUDUSD=X", start_utc=start, end_utc=end)
    assert len(path) >= 5
    assert all(r["symbol"] == "AUDUSD=X" for r in path)


def test_no_fabricated_bid_ask_without_quote(tmp_path: Path):
    t0 = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
    arch = ContinuousM1Archive(run_dir=tmp_path, symbols=["EURUSD=X"])
    arch.ingest_completed("EURUSD=X", [_bar(t0, 0)])
    row = next(arch.iter_symbol_candles("EURUSD=X"))
    assert row["bid"] is None
    assert row["ask"] is None
    assert row["data_source"] == "mt5_fetch_bars"


def test_quote_attached_when_provided(tmp_path: Path):
    t0 = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
    arch = ContinuousM1Archive(run_dir=tmp_path, symbols=["EURUSD=X"])
    arch.ingest_completed(
        "EURUSD=X",
        [_bar(t0, 0)],
        quote={"bid": 1.1, "ask": 1.1001, "spread_pips": 1.0},
    )
    row = next(arch.iter_symbol_candles("EURUSD=X"))
    assert row["bid"] == 1.1
    assert row["ask"] == 1.1001
