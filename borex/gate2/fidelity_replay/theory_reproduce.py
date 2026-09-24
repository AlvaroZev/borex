"""Causal Theory reproduction from a stored Theory event's own candles."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from borex.backtest.costs import infer_pip_size
from borex.gate2.fidelity_replay.provenance import causal_candles_for_theory, parse_utc
from borex.gate2.micro_fidelity.config import FROZEN_CONFIG, MicroFidelityConfig
from borex.gate2.micro_fidelity.strategy import (
    evaluate_completed_bar,
    plan_symmetric_sl_tp,
)


def _digits_for_symbol(symbol: str) -> int:
    return 3 if "JPY" in symbol.upper().replace("=X", "") else 5


@dataclass(frozen=True)
class TheoryReproduction:
    theory_event_id: str
    ok: bool
    signal_match: bool
    latest_permitted_market_ts: str | None
    used_future_candle: bool
    replay_direction: str | None
    replay_signal_timestamp_utc: str | None
    replay_source_candle_timestamp_utc: str | None
    replay_entry: float | None
    replay_sl: float | None
    replay_tp: float | None
    differences: list[str]
    detail: dict[str, Any]


def reproduce_theory_event(
    theory: dict[str, Any],
    *,
    config: MicroFidelityConfig = FROZEN_CONFIG,
) -> TheoryReproduction:
    """Re-evaluate frozen strategy on the event's causal candle_sequence only."""
    teid = str(theory.get("theory_event_id") or "")
    symbol = str(theory.get("symbol") or "")
    source_ts = parse_utc(theory.get("source_candle_timestamp_utc"))
    candles = causal_candles_for_theory(theory)
    latest = candles[-1].timestamp_utc.isoformat() if candles else None

    # Look-ahead guard: every candle must be <= source candle
    used_future = False
    if source_ts is not None:
        for c in candles:
            if c.timestamp_utc > source_ts:
                used_future = True
                break

    differences: list[str] = []
    if used_future:
        return TheoryReproduction(
            theory_event_id=teid,
            ok=False,
            signal_match=False,
            latest_permitted_market_ts=latest,
            used_future_candle=True,
            replay_direction=None,
            replay_signal_timestamp_utc=None,
            replay_source_candle_timestamp_utc=None,
            replay_entry=None,
            replay_sl=None,
            replay_tp=None,
            differences=["future_candle_in_causal_set"],
            detail={"candle_count": len(candles)},
        )

    pip = infer_pip_size(symbol)
    signal = evaluate_completed_bar(
        symbol=symbol,
        completed=candles,
        pip_size=pip,
        config=config,
        bars_since_last_signal=None,  # cooldown not re-applied; event already existed
    )

    if signal is None:
        differences.append("strategy_returned_no_signal")
        return TheoryReproduction(
            theory_event_id=teid,
            ok=False,
            signal_match=False,
            latest_permitted_market_ts=latest,
            used_future_candle=False,
            replay_direction=None,
            replay_signal_timestamp_utc=None,
            replay_source_candle_timestamp_utc=None,
            replay_entry=None,
            replay_sl=None,
            replay_tp=None,
            differences=differences,
            detail={"candle_count": len(candles)},
        )

    # Entry / SL / TP from stored bid/ask + stored distance (exact Theory plan)
    bid = float(theory.get("bid") or 0.0)
    ask = float(theory.get("ask") or 0.0)
    side = signal.side
    entry = ask if side == "buy" else bid
    dist_info = theory.get("distance_selection") or {}
    dist = float(dist_info.get("distance_price") or (float(config.sl_tp_target_pips) * pip))
    plan = plan_symmetric_sl_tp(
        side=side,
        entry_ref=entry,
        distance_price=dist,
        digits=_digits_for_symbol(symbol),
        rr=float(config.rr),
    )

    orig_dir = str(theory.get("direction") or "")
    orig_sig = parse_utc(theory.get("signal_timestamp_utc"))
    orig_src = parse_utc(theory.get("source_candle_timestamp_utc"))
    orig_entry = float(theory.get("expected_entry") or 0.0)
    orig_sl = float(theory.get("expected_sl") or 0.0)
    orig_tp = float(theory.get("expected_tp") or 0.0)

    if signal.direction != orig_dir:
        differences.append(f"direction:{signal.direction}!={orig_dir}")
    if orig_sig and signal.signal_timestamp_utc != orig_sig:
        differences.append(
            f"signal_ts:{signal.signal_timestamp_utc.isoformat()}!={orig_sig.isoformat()}"
        )
    if orig_src and signal.source_candle_timestamp_utc != orig_src:
        differences.append(
            f"source_ts:{signal.source_candle_timestamp_utc.isoformat()}!={orig_src.isoformat()}"
        )
    if abs(entry - orig_entry) > 1e-9:
        differences.append(f"entry:{entry}!={orig_entry}")
    if abs(float(plan["sl"]) - orig_sl) > 1e-9:
        differences.append(f"sl:{plan['sl']}!={orig_sl}")
    if abs(float(plan["tp"]) - orig_tp) > 1e-9:
        differences.append(f"tp:{plan['tp']}!={orig_tp}")

    signal_match = len(differences) == 0
    return TheoryReproduction(
        theory_event_id=teid,
        ok=signal_match,
        signal_match=signal_match,
        latest_permitted_market_ts=latest,
        used_future_candle=False,
        replay_direction=signal.direction,
        replay_signal_timestamp_utc=signal.signal_timestamp_utc.isoformat(),
        replay_source_candle_timestamp_utc=signal.source_candle_timestamp_utc.isoformat(),
        replay_entry=float(entry),
        replay_sl=float(plan["sl"]),
        replay_tp=float(plan["tp"]),
        differences=differences,
        detail={
            "candle_count": len(candles),
            "pattern": signal.pattern,
            "breakout_threshold": signal.breakout_threshold,
            "breakout_distance": signal.breakout_distance,
        },
    )
