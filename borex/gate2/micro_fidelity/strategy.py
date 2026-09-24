"""Deterministic 1m previous-bar breakout strategy for Gate 2 micro fidelity.

No alexg imports. Reconstructible from stored completed 1m candles + frozen params.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Sequence

from borex.gate2.micro_fidelity.candles import MinuteCandle
from borex.gate2.micro_fidelity.config import FROZEN_CONFIG, MicroFidelityConfig


@dataclass(frozen=True)
class MicroFidelitySignal:
    symbol: str
    direction: str  # long | short
    side: str  # buy | sell
    signal_timestamp_utc: datetime
    source_candle_timestamp_utc: datetime
    source_candle: dict[str, Any]
    prev_candle: dict[str, Any]
    lookback_mean_range: float
    breakout_threshold: float
    breakout_distance: float
    expected_entry_side: str  # ASK | BID
    pattern: str
    strategy_name: str
    strategy_version: str
    timeframe: str
    parameters: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["signal_timestamp_utc"] = self.signal_timestamp_utc.isoformat()
        d["source_candle_timestamp_utc"] = self.source_candle_timestamp_utc.isoformat()
        return d


def _mean_range(candles: Sequence[MinuteCandle]) -> float:
    if not candles:
        return 0.0
    return sum(c.range for c in candles) / float(len(candles))


def evaluate_completed_bar(
    *,
    symbol: str,
    completed: Sequence[MinuteCandle],
    pip_size: float,
    config: MicroFidelityConfig = FROZEN_CONFIG,
    bars_since_last_signal: int | None = None,
) -> MicroFidelitySignal | None:
    """Evaluate the newest completed 1m bar. Never uses a forming bar.

    Signal timestamp = source candle open + 1 minute (bar close instant).
    """
    need = int(config.lookback_bars) + 1
    if len(completed) < need:
        return None
    if bars_since_last_signal is not None and bars_since_last_signal < int(config.cooldown_bars):
        return None

    curr = completed[-1]
    prev = completed[-2]
    lookback = completed[-(int(config.lookback_bars) + 1) : -1]
    mean_r = _mean_range(lookback)
    pip = float(pip_size)
    if pip <= 0:
        return None
    threshold = max(float(config.breakout_frac) * mean_r, float(config.min_breakout_pips) * pip)

    direction: str | None = None
    distance = 0.0
    if curr.close > prev.high + threshold:
        direction = "long"
        distance = float(curr.close) - float(prev.high)
    elif curr.close < prev.low - threshold:
        direction = "short"
        distance = float(prev.low) - float(curr.close)
    else:
        return None

    # Signal at bar close: open + 60s
    from datetime import timedelta

    signal_ts = curr.timestamp_utc + timedelta(minutes=1)
    side = "buy" if direction == "long" else "sell"
    entry_side = "ASK" if direction == "long" else "BID"
    params = {
        "lookback_bars": config.lookback_bars,
        "breakout_frac": config.breakout_frac,
        "min_breakout_pips": config.min_breakout_pips,
        "cooldown_bars": config.cooldown_bars,
        "sl_tp_target_pips": config.sl_tp_target_pips,
        "rr": config.rr,
        "timeframe": config.timeframe,
    }
    return MicroFidelitySignal(
        symbol=symbol,
        direction=direction,
        side=side,
        signal_timestamp_utc=signal_ts,
        source_candle_timestamp_utc=curr.timestamp_utc,
        source_candle=curr.to_dict(),
        prev_candle=prev.to_dict(),
        lookback_mean_range=mean_r,
        breakout_threshold=threshold,
        breakout_distance=distance,
        expected_entry_side=entry_side,
        pattern="prev_bar_breakout_1m",
        strategy_name=config.strategy_name,
        strategy_version=config.strategy_version,
        timeframe=config.timeframe,
        parameters=params,
    )


def plan_symmetric_sl_tp(
    *,
    side: str,
    entry_ref: float,
    distance_price: float,
    digits: int,
    rr: float = 1.0,
) -> dict[str, Any]:
    """Symmetric (or RR-scaled) SL/TP from entry reference. Correct sides."""
    d = float(distance_price)
    e = float(entry_ref)
    side_l = side.lower()
    tp_d = d * float(rr)
    if side_l in ("buy", "long"):
        sl = round(e - d, digits)
        tp = round(e + tp_d, digits)
        entry_side, exit_side = "ASK", "BID"
        sl_trigger, tp_trigger = "BID", "BID"
    else:
        sl = round(e + d, digits)
        tp = round(e - tp_d, digits)
        entry_side, exit_side = "BID", "ASK"
        sl_trigger, tp_trigger = "ASK", "ASK"
    return {
        "sl": sl,
        "tp": tp,
        "entry_side": entry_side,
        "exit_side": exit_side,
        "sl_trigger_side": sl_trigger,
        "tp_trigger_side": tp_trigger,
        "sl_distance_price": d,
        "tp_distance_price": tp_d,
    }


def choose_sl_tp_distance(
    *,
    pip_size: float,
    min_stop_distance_price: float,
    spread_price: float,
    freeze_level_price: float,
    target_pips: float,
    max_pips: float,
) -> dict[str, Any]:
    """Pick frozen-target SL/TP distance; fail-closed if broker constraints exceed max."""
    from borex.gate2.exit_classify import choose_trigger_distance

    base = choose_trigger_distance(
        pip_size=pip_size,
        min_stop_distance_price=min_stop_distance_price,
        spread_price=spread_price,
        freeze_level_price=freeze_level_price,
        max_trigger_pips=max_pips,
        buffer_pips=0.5,
    )
    if not base.get("ok"):
        return base
    need = float(base["distance_price"])
    target = float(target_pips) * float(pip_size)
    dist = max(need, target)
    pips = dist / float(pip_size)
    if pips > float(max_pips) + 1e-12:
        return {
            "ok": False,
            "rejection_reason": f"required_sl_tp_{pips:.2f}_pips_gt_max_{float(max_pips):.2f}",
            "distance_price": dist,
            "distance_pips": pips,
        }
    return {
        "ok": True,
        "rejection_reason": None,
        "distance_price": dist,
        "distance_pips": pips,
        "target_pips": target_pips,
        "broker_min_distance_price": need,
    }
