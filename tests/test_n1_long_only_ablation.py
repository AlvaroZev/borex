"""Unit tests for N1 long-only ablation one-bit filter (no simulator changes)."""

from __future__ import annotations

from types import SimpleNamespace

from scripts.run_n1_long_only_ablation import filter_long_only, remap_tape_by_time


def test_filter_long_only_drops_sells_keeps_buys():
    tape = [
        {"action": "buy", "symbol": "EURUSD=X", "index": 1},
        {"action": "sell", "symbol": "EURUSD=X", "index": 2},
        {"action": "BUY", "symbol": "GBPUSD=X", "index": 3},
        {"action": "Sell", "symbol": "GBPUSD=X", "index": 4},
        {"action": "hold", "symbol": "USDJPY=X", "index": 5},
    ]
    out = filter_long_only(tape)
    assert [r["action"] for r in out] == ["buy", "BUY"]
    assert all(str(r["action"]).lower() != "sell" for r in out)


def test_remap_tape_by_time_rekeys_index():
    bars = [
        SimpleNamespace(timestamp="2026-06-01T00:00:00+00:00"),
        SimpleNamespace(timestamp="2026-06-01T01:00:00+00:00"),
        SimpleNamespace(timestamp="2026-06-01T02:00:00+00:00"),
    ]
    candles = {"EURUSD=X": bars}
    tape = [
        {
            "symbol": "EURUSD=X",
            "action": "buy",
            "index": 9999,
            "time_unix": 1780275600,  # 2026-06-01 01:00:00 UTC
        },
        {
            "symbol": "MISSING=X",
            "action": "buy",
            "index": 1,
            "time_unix": 1780275600,
        },
    ]
    remapped, meta = remap_tape_by_time(tape, candles)
    assert meta["remapped"] == 1
    assert meta["missed_symbol"] == 1
    assert remapped[0]["index"] == 1
    assert remapped[0]["action"] == "buy"
