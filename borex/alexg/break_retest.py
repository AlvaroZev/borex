from __future__ import annotations

from dataclasses import dataclass, field

from borex.alexg.aoi import AOI
from borex.models.candle import Candle


@dataclass
class BreakRetestState:
    """Estado del ciclo break → retest → confirmación."""

    bullish_level: float | None = None
    bullish_break_index: int | None = None
    bullish_retest_ready: bool = False

    bearish_level: float | None = None
    bearish_break_index: int | None = None
    bearish_retest_ready: bool = False


@dataclass
class BreakRetestResult:
    bullish_confirmed: bool = False
    bearish_confirmed: bool = False
    broken_level: float | None = None


def body_closes_above(candle: Candle, level: float) -> bool:
    """Break alcista: cuerpo cierra por encima (mechas no cuentan)."""
    return max(candle.open, candle.close) > level


def body_closes_below(candle: Candle, level: float) -> bool:
    """Break bajista: cuerpo cierra por debajo."""
    return min(candle.open, candle.close) < level


def update_break_retest(
    candles: list[Candle],
    index: int,
    zones: list[AOI],
    state: BreakRetestState,
    tolerance_pct: float = 0.002,
) -> BreakRetestResult:
    """
    Actualiza máquina de estados break/retest.
    Prefiere break → retest → entry sobre entrada inmediata en break.
    """
    candle = candles[index]
    result = BreakRetestResult()
    tol = candle.close * tolerance_pct

    resistances = [z for z in zones if z.kind == "resistance"]
    supports = [z for z in zones if z.kind == "support"]

    for zone in resistances:
        if body_closes_above(candle, zone.level):
            state.bullish_level = zone.level
            state.bullish_break_index = index
            state.bullish_retest_ready = False

    for zone in supports:
        if body_closes_below(candle, zone.level):
            state.bearish_level = zone.level
            state.bearish_break_index = index
            state.bearish_retest_ready = False

    if state.bullish_level is not None and state.bullish_break_index is not None:
        if index > state.bullish_break_index:
            retest = (
                candle.low <= state.bullish_level + tol * 2
                and candle.close >= state.bullish_level - tol
            )
            if retest:
                state.bullish_retest_ready = True
            if state.bullish_retest_ready and candle.close > state.bullish_level:
                result.bullish_confirmed = True
                result.broken_level = state.bullish_level
                state.bullish_level = None
                state.bullish_break_index = None
                state.bullish_retest_ready = False

    if state.bearish_level is not None and state.bearish_break_index is not None:
        if index > state.bearish_break_index:
            retest = (
                candle.high >= state.bearish_level - tol * 2
                and candle.close <= state.bearish_level + tol
            )
            if retest:
                state.bearish_retest_ready = True
            if state.bearish_retest_ready and candle.close < state.bearish_level:
                result.bearish_confirmed = True
                result.broken_level = state.bearish_level
                state.bearish_level = None
                state.bearish_break_index = None
                state.bearish_retest_ready = False

    return result
