from __future__ import annotations

from dataclasses import dataclass

from borex.backtest.portfolio import PositionSide


@dataclass(frozen=True)
class TradeCosts:
    spread_pips: float = 0.0
    slippage_pips: float = 0.0
    commission_per_trade: float = 0.0
    pip_size: float = 0.0001

    @property
    def half_spread_price(self) -> float:
        return (self.spread_pips / 2.0) * self.pip_size

    @property
    def slippage_price(self) -> float:
        return self.slippage_pips * self.pip_size


def infer_pip_size(symbol: str) -> float:
    """Tamaño de pip según símbolo (forex estándar)."""
    sym = symbol.upper().replace("=X", "")
    if "JPY" in sym:
        return 0.01
    return 0.0001


def apply_entry_fill(
    mid_price: float,
    side: PositionSide,
    costs: TradeCosts,
) -> float:
    """Precio de fill en entrada (peor que mid: spread/2 + slippage)."""
    adverse = costs.half_spread_price + costs.slippage_price
    if side == PositionSide.LONG:
        return mid_price + adverse
    return mid_price - adverse


def apply_exit_fill(
    mid_price: float,
    side: PositionSide,
    costs: TradeCosts,
) -> float:
    """Precio de fill en salida (peor que mid: spread/2 + slippage)."""
    adverse = costs.half_spread_price + costs.slippage_price
    if side == PositionSide.LONG:
        return mid_price - adverse
    return mid_price + adverse


def round_trip_cost_pips(costs: TradeCosts) -> float:
    """Costo total en pips por round-trip (spread + 2× slippage)."""
    return costs.spread_pips + 2.0 * costs.slippage_pips
