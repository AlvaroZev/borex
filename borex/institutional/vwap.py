from __future__ import annotations

from dataclasses import dataclass

from borex.models.candle import Candle


@dataclass(frozen=True)
class VwapState:
    vwap: float
    deviation_pct: float  # (close - vwap) / vwap


def typical_price(candle: Candle) -> float:
    return (candle.high + candle.low + candle.close) / 3.0


def rolling_vwap(candles: list[Candle], index: int, period: int = 20) -> float | None:
    """VWAP rolling sobre `period` velas. Sin volumen usa peso uniforme."""
    start = max(0, index - period + 1)
    window = candles[start : index + 1]
    if not window:
        return None

    total_vol = sum(c.volume for c in window)
    if total_vol <= 0:
        return sum(typical_price(c) for c in window) / len(window)

    weighted = sum(typical_price(c) * c.volume for c in window)
    return weighted / total_vol


def vwap_state(
    candles: list[Candle],
    index: int,
    period: int = 20,
) -> VwapState | None:
    vwap = rolling_vwap(candles, index, period)
    if vwap is None or vwap <= 0:
        return None
    close = candles[index].close
    deviation = (close - vwap) / vwap
    return VwapState(vwap=vwap, deviation_pct=deviation)


def vwap_discount(state: VwapState, threshold: float = -0.001) -> bool:
    """Precio por debajo de VWAP (zona de acumulación institucional)."""
    return state.deviation_pct <= threshold


def vwap_premium(state: VwapState, threshold: float = 0.001) -> bool:
    """Precio por encima de VWAP (zona de distribución institucional)."""
    return state.deviation_pct >= threshold
