from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from borex.data.loader import load_filter_candles
from borex.data.timeframe import (
    filter_intervals_for_execution,
    interval_to_timedelta,
    validate_higher_timeframe,
)
from borex.models.candle import Candle, SignalAction


@dataclass
class MultiTimeframeContext:
    """
    Contexto MTF: alinea velas de ejecución con múltiples timeframes superiores.

    Solo expone velas ya cerradas (sin look-ahead). Entrada válida solo si
    TODOS los timeframes de filtro confirman la dirección.
    """

    execution_interval: str
    filter_intervals: list[str]
    filter_candles: dict[str, list[Candle]]
    _alignments: dict[str, list[int]] = field(repr=False)

    @property
    def filter_interval(self) -> str:
        """Etiqueta compacta para reportes."""
        return "+".join(self.filter_intervals)

    def filter_index_at(self, execution_index: int, interval: str) -> int | None:
        alignment = self._alignments.get(interval)
        if alignment is None or execution_index < 0 or execution_index >= len(alignment):
            return None
        idx = alignment[execution_index]
        return idx if idx >= 0 else None

    def filter_candle_at(self, execution_index: int, interval: str) -> Candle | None:
        idx = self.filter_index_at(execution_index, interval)
        if idx is None:
            return None
        return self.filter_candles[interval][idx]

    def all_filters_align(self, execution_index: int, action: SignalAction) -> bool:
        for interval in self.filter_intervals:
            candle = self.filter_candle_at(execution_index, interval)
            if candle is None:
                return False
            if action == SignalAction.BUY and not candle.is_bullish:
                return False
            if action == SignalAction.SELL and not candle.is_bearish:
                return False
        return True


def align_timeframes(
    execution: list[Candle],
    filter_candles: list[Candle],
    execution_interval: str,
    filter_interval: str,
) -> list[int]:
    """
    Para cada vela de ejecución, devuelve el índice de la última vela de filtro
    completamente cerrada al momento del cierre de esa vela.
    """
    validate_higher_timeframe(execution_interval, filter_interval)

    exec_td = interval_to_timedelta(execution_interval)
    filter_td = interval_to_timedelta(filter_interval)
    alignment: list[int] = []
    filter_idx = -1

    for exec_c in execution:
        exec_close = pd.Timestamp(exec_c.timestamp) + exec_td

        while filter_idx + 1 < len(filter_candles):
            candidate = filter_candles[filter_idx + 1]
            candidate_close = pd.Timestamp(candidate.timestamp) + filter_td
            if candidate_close <= exec_close:
                filter_idx += 1
            else:
                break

        alignment.append(filter_idx)

    return alignment


def build_full_mtf_context(
    execution: list[Candle],
    execution_interval: str,
    symbol: str,
    period: str,
    cache_mode: str = "auto",
) -> MultiTimeframeContext:
    """Construye contexto MTF con todos los TF superiores (15m→1wk)."""
    filter_intervals = filter_intervals_for_execution(execution_interval)
    filter_candles: dict[str, list[Candle]] = {}
    alignments: dict[str, list[int]] = {}

    for interval in filter_intervals:
        candles = load_filter_candles(
            symbol, period, execution, execution_interval, interval, cache_mode
        )
        filter_candles[interval] = candles
        alignments[interval] = align_timeframes(
            execution, candles, execution_interval, interval
        )

    return MultiTimeframeContext(
        execution_interval=execution_interval,
        filter_intervals=filter_intervals,
        filter_candles=filter_candles,
        _alignments=alignments,
    )
