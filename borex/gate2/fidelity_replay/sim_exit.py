"""Simulator exit evaluation adapter for Level 5 replay.

Does NOT modify MultiMarketEngine. Documents and reuses its OHLC wick rules:

- Walk completed 1m bars in time order (first chronological hit wins).
- Long SL: low <= sl; Long TP: high >= tp.
- Short SL: high >= sl; Short TP: low <= tp.
- Same-bar both-wick: SL preferred over TP (broker-path rule in MultiMarketEngine).
- Fill price at the SL/TP level (before optional spread cost wrapper).
- Spread is NOT modeled as separate bid/ask series on the exit path; Theory
  recorded live bid/ask for entry only. Exit cost wrapper uses half-spread
  adverse to mid via apply_exit_fill when enabled.

See borex/backtest/multi_market_engine.py::_check_exit / _hit_stop / _hit_take_profit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from borex.backtest.costs import TradeCosts, apply_entry_fill, apply_exit_fill, infer_pip_size
from borex.backtest.multi_market_engine import MultiMarketEngine
from borex.backtest.portfolio import PositionSide, Trade
from borex.gate2.micro_fidelity.candles import MinuteCandle
from borex.models.candle import Candle


def _to_engine_candle(c: MinuteCandle) -> Candle:
    return Candle(
        timestamp=c.timestamp_utc,
        open=c.open,
        high=c.high,
        low=c.low,
        close=c.close,
        volume=float(c.tick_count),
    )


def _side(direction: str) -> PositionSide:
    return PositionSide.LONG if direction.lower() in ("long", "buy") else PositionSide.SHORT


@dataclass(frozen=True)
class SimEntryResult:
    mid_price: float
    sim_entry: float
    spread_pips: float
    side: str
    costs: dict[str, float]


@dataclass(frozen=True)
class SimExitResult:
    status: str  # HIT_SL | HIT_TP | NO_HIT_IN_PATH | INSUFFICIENT_PATH
    exit_reason: str | None  # SIM_SL | SIM_TP | None
    exit_price_level: float | None
    exit_price_filled: float | None
    exit_timestamp_utc: str | None
    bar_index: int | None
    both_wick_on_exit_bar: bool
    bars_evaluated: int
    path_insufficient_reason: str | None
    detail: dict[str, Any]


def simulate_entry_from_quote(
    *,
    symbol: str,
    direction: str,
    bid: float,
    ask: float,
    spread_pips: float | None = None,
    slippage_pips: float = 0.0,
) -> SimEntryResult:
    """Borex simulator entry: mid ± half-spread (not raw bid/ask Theory sides)."""
    pip = infer_pip_size(symbol)
    mid = (float(bid) + float(ask)) / 2.0
    if spread_pips is None:
        spread_pips = (float(ask) - float(bid)) / pip if pip > 0 else 0.0
    costs = TradeCosts(
        spread_pips=float(spread_pips),
        slippage_pips=float(slippage_pips),
        pip_size=pip,
    )
    side = _side(direction)
    fill = apply_entry_fill(mid, side, costs)
    return SimEntryResult(
        mid_price=mid,
        sim_entry=float(fill),
        spread_pips=float(spread_pips),
        side=side.value,
        costs={
            "spread_pips": costs.spread_pips,
            "slippage_pips": costs.slippage_pips,
            "half_spread_price": costs.half_spread_price,
            "pip_size": costs.pip_size,
        },
    )


def simulate_exit_on_m1_path(
    *,
    symbol: str,
    direction: str,
    sl: float,
    tp: float,
    bars: Sequence[MinuteCandle],
    path_insufficient_reason: str | None = None,
    apply_spread_on_exit: bool = True,
    spread_pips: float = 0.0,
) -> SimExitResult:
    """Walk M1 OHLC with MultiMarketEngine both-wick→SL-first semantics."""
    if path_insufficient_reason:
        return SimExitResult(
            status="INSUFFICIENT_PATH",
            exit_reason=None,
            exit_price_level=None,
            exit_price_filled=None,
            exit_timestamp_utc=None,
            bar_index=None,
            both_wick_on_exit_bar=False,
            bars_evaluated=0,
            path_insufficient_reason=path_insufficient_reason,
            detail={},
        )
    if not bars:
        return SimExitResult(
            status="INSUFFICIENT_PATH",
            exit_reason=None,
            exit_price_level=None,
            exit_price_filled=None,
            exit_timestamp_utc=None,
            bar_index=None,
            both_wick_on_exit_bar=False,
            bars_evaluated=0,
            path_insufficient_reason="empty_m1_path",
            detail={},
        )

    side = _side(direction)
    # Minimal Trade stand-in for static hit helpers
    trade = Trade(
        side=side,
        entry_index=0,
        entry_price=0.0,
        entry_time=bars[0].timestamp_utc,
        pattern="gate2_fidelity_replay",
        symbol=symbol,
        stop_loss=float(sl),
        take_profit=float(tp),
    )
    pip = infer_pip_size(symbol)
    costs = TradeCosts(spread_pips=float(spread_pips), pip_size=pip)

    for i, mc in enumerate(bars):
        candle = _to_engine_candle(mc)
        sl_hit = MultiMarketEngine._hit_stop(trade, candle, float(sl), wick=True)
        tp_hit = MultiMarketEngine._hit_take_profit(trade, candle)
        if sl_hit and tp_hit:
            level = float(sl)
            filled = apply_exit_fill(level, side, costs) if apply_spread_on_exit else level
            return SimExitResult(
                status="HIT_SL",
                exit_reason="SIM_SL",
                exit_price_level=level,
                exit_price_filled=float(filled),
                exit_timestamp_utc=mc.timestamp_utc.isoformat(),
                bar_index=i,
                both_wick_on_exit_bar=True,
                bars_evaluated=i + 1,
                path_insufficient_reason=None,
                detail={"rule": "both_wick_sl_first", "bar": mc.to_dict()},
            )
        if tp_hit:
            level = float(tp)
            filled = apply_exit_fill(level, side, costs) if apply_spread_on_exit else level
            return SimExitResult(
                status="HIT_TP",
                exit_reason="SIM_TP",
                exit_price_level=level,
                exit_price_filled=float(filled),
                exit_timestamp_utc=mc.timestamp_utc.isoformat(),
                bar_index=i,
                both_wick_on_exit_bar=False,
                bars_evaluated=i + 1,
                path_insufficient_reason=None,
                detail={"rule": "tp_wick", "bar": mc.to_dict()},
            )
        if sl_hit:
            level = float(sl)
            filled = apply_exit_fill(level, side, costs) if apply_spread_on_exit else level
            return SimExitResult(
                status="HIT_SL",
                exit_reason="SIM_SL",
                exit_price_level=level,
                exit_price_filled=float(filled),
                exit_timestamp_utc=mc.timestamp_utc.isoformat(),
                bar_index=i,
                both_wick_on_exit_bar=False,
                bars_evaluated=i + 1,
                path_insufficient_reason=None,
                detail={"rule": "sl_wick", "bar": mc.to_dict()},
            )

    return SimExitResult(
        status="NO_HIT_IN_PATH",
        exit_reason=None,
        exit_price_level=None,
        exit_price_filled=None,
        exit_timestamp_utc=None,
        bar_index=None,
        both_wick_on_exit_bar=False,
        bars_evaluated=len(bars),
        path_insufficient_reason="path_exhausted_without_sl_tp_hit",
        detail={"last_bar": bars[-1].to_dict() if bars else None},
    )


def map_sim_reason_to_broker_label(sim_reason: str | None) -> str | None:
    if sim_reason == "SIM_SL":
        return "BROKER_SL"
    if sim_reason == "SIM_TP":
        return "BROKER_TP"
    return None
