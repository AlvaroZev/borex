from __future__ import annotations

from dataclasses import dataclass

from borex.backtest.portfolio import PositionSide

# Standard FX contract size used to map margin×leverage → lot-equivalents.
DEFAULT_LOT_NOTIONAL = 100_000.0
# ICMarkets Raw (USD): ~$3.50 per side per lot → $7.00 round-turn.
DEFAULT_COMMISSION_PER_LOT = 7.0
# Observed floor on demo deals (0.01 lot charged $0.04/side, not $0.035).
DEFAULT_MIN_COMMISSION_PER_SIDE = 0.04


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


# IC Markets Raw session averages (pips). Weekend last-quote is often much wider.
_ICMARKETS_RAW_SPREAD_PIPS: dict[str, float] = {
    "EURUSD": 0.10,
    "GBPUSD": 0.20,
    "USDJPY": 0.10,
    "AUDUSD": 0.20,
    "USDCAD": 0.30,
    "USDCHF": 0.20,
    "NZDUSD": 0.30,
    "EURGBP": 0.20,
    "EURJPY": 0.30,
    "GBPJPY": 0.60,
    "EURCHF": 0.25,
    "AUDJPY": 0.40,
    "EURAUD": 0.40,
    "EURCAD": 0.40,
    "EURNZD": 0.50,
    "GBPAUD": 0.60,
    "GBPCAD": 0.60,
    "GBPCHF": 0.50,
    "AUDCAD": 0.40,
    "AUDCHF": 0.40,
    "AUDNZD": 0.50,
    "CADJPY": 0.40,
    "CADCHF": 0.40,
    "NZDJPY": 0.50,
    "NZDCAD": 0.50,
    "NZDCHF": 0.50,
    "CHFJPY": 0.40,
    "USDSGD": 0.80,
    "EURSGD": 1.00,
    "GBPSGD": 1.20,
    "AUDSGD": 1.00,
    "SGDJPY": 1.20,
    "USDSEK": 2.50,
    "USDNOK": 2.50,
    "USDDKK": 1.50,
    "EURSEK": 2.50,
    "EURNOK": 2.50,
    "EURDKK": 1.20,
    "GBPSEK": 3.00,
    "GBPNOK": 3.00,
    "GBPDKK": 2.00,
    "NOKSEK": 2.00,
    "NOKJPY": 2.00,
    "SEKJPY": 2.00,
    "USDCNH": 1.50,
    "USDHKD": 1.50,
    "USDPLN": 2.50,
    "EURPLN": 2.50,
    "EURHKD": 2.00,
    "USDCZK": 3.00,
    "USDHUF": 4.00,
    "USDTHB": 4.00,
    "USDMXN": 6.00,
    "USDTRY": 12.00,
    "USDZAR": 10.00,
    "EURTRY": 14.00,
    "EURZAR": 12.00,
    "GBPTRY": 16.00,
}


def typical_icmarkets_raw_spread_pips(symbol: str) -> float:
    """Session-typical Raw spread in pips when the live quote is unusable."""
    key = symbol.upper().replace("=X", "").replace("/", "")
    if key in _ICMARKETS_RAW_SPREAD_PIPS:
        return _ICMARKETS_RAW_SPREAD_PIPS[key]
    exotic = {"TRY", "ZAR", "MXN", "HUF", "CZK", "THB", "CNH"}
    scandi = {"SEK", "NOK", "DKK"}
    base, quote = key[:3], key[3:6]
    if base in exotic or quote in exotic:
        return 8.0
    if base in scandi or quote in scandi:
        return 2.5
    if "SGD" in key or "HKD" in key or "PLN" in key:
        return 1.5
    return 0.80


def clamp_spread_pips(quoted: float, typical: float) -> float:
    """Keep a live quote if it looks like session; else the typical Raw value."""
    q = float(quoted or 0.0)
    typ = max(0.10, float(typical or 0.0))
    if q < 0.05:
        return typ
    # Weekend / rollover quotes can be 10–50× session.
    if q > max(3.0 * typ, typ + 4.0):
        return typ
    return q


def spread_pips_from_quote(
    *,
    bid: float,
    ask: float,
    symbol: str,
    points: int = 0,
    point_size: float = 0.0,
) -> float:
    """Bid/ask or MT5 point-spread → pips."""
    pip = infer_pip_size(symbol)
    if ask > 0 and bid > 0 and ask >= bid:
        return (float(ask) - float(bid)) / pip
    if points > 0 and point_size > 0 and pip > 0:
        return float(points) * float(point_size) / pip
    return 0.0


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


def lots_from_notional(notional: float, lot_notional: float = DEFAULT_LOT_NOTIONAL) -> float:
    if lot_notional <= 0:
        return 0.0
    return max(0.0, float(notional) / float(lot_notional))


def lots_from_margin(
    margin: float,
    leverage: float,
    lot_notional: float = DEFAULT_LOT_NOTIONAL,
) -> float:
    return lots_from_notional(float(margin) * float(leverage), lot_notional)


def commission_for_lots(
    lots: float,
    *,
    commission_per_lot: float = DEFAULT_COMMISSION_PER_LOT,
    commission_per_trade: float = 0.0,
    min_commission_per_side: float = DEFAULT_MIN_COMMISSION_PER_SIDE,
) -> float:
    """
    Round-turn commission for a volume in lots.

    ICMarkets Raw demo: ~$3.50/side/lot ($7 RT), with ~$0.04 minimum per side
    (so 0.01 lots pay $0.08 RT instead of $0.07).
    """
    vol = max(0.0, float(lots))
    flat = float(commission_per_trade)
    if vol <= 0:
        return flat
    per_side_rate = max(0.0, float(commission_per_lot)) / 2.0
    floor = max(0.0, float(min_commission_per_side))
    per_side = max(floor, per_side_rate * vol)
    # Broker posts commission to 2 decimals per deal/side.
    per_side = round(per_side + 1e-12, 2)
    return flat + 2.0 * per_side


def commission_for_margin(
    margin: float,
    leverage: float,
    *,
    commission_per_lot: float = 0.0,
    commission_per_trade: float = 0.0,
    lot_notional: float = DEFAULT_LOT_NOTIONAL,
    min_commission_per_side: float = DEFAULT_MIN_COMMISSION_PER_SIDE,
) -> float:
    """Round-turn commission for a margin×leverage position."""
    lots = lots_from_margin(margin, leverage, lot_notional)
    return commission_for_lots(
        lots,
        commission_per_lot=commission_per_lot,
        commission_per_trade=commission_per_trade,
        min_commission_per_side=min_commission_per_side,
    )


def margin_for_risk_net_commission(
    risk_money: float,
    leverage: float,
    *,
    commission_per_lot: float = 0.0,
    lot_notional: float = DEFAULT_LOT_NOTIONAL,
    min_commission_per_side: float = DEFAULT_MIN_COMMISSION_PER_SIDE,
    max_iters: int = 8,
) -> float:
    """
    Margin so that (price loss at true-SL) + (round-turn commission) ≈ risk_money.

    Uses a few fixed-point iterations because the $0.04/side floor makes the
    commission piecewise-linear in lot size.
    """
    risk = max(0.0, float(risk_money))
    if risk <= 0:
        return 0.0
    c = max(0.0, float(commission_per_lot))
    if c <= 0 or leverage <= 0 or lot_notional <= 0:
        return risk
    # Smooth (no floor) closed form as initial guess.
    denom = 1.0 + (float(leverage) * c / float(lot_notional))
    margin = risk / denom if denom > 0 else risk
    for _ in range(max_iters):
        comm = commission_for_margin(
            margin,
            leverage,
            commission_per_lot=c,
            lot_notional=lot_notional,
            min_commission_per_side=min_commission_per_side,
        )
        # price_loss == margin at true-SL; want margin + comm == risk
        margin = max(0.0, risk - comm)
        # Re-apply linear term so we don't oscillate when floor is inactive
        lots = lots_from_margin(margin, leverage, lot_notional)
        smooth_comm = lots * c
        floor_comm = 2.0 * max(0.0, float(min_commission_per_side)) if lots > 0 else 0.0
        if smooth_comm >= floor_comm - 1e-12:
            margin = risk / denom
            break
    # Final clamp
    comm = commission_for_margin(
        margin,
        leverage,
        commission_per_lot=c,
        lot_notional=lot_notional,
        min_commission_per_side=min_commission_per_side,
    )
    if margin + comm > risk + 1e-9:
        margin = max(0.0, risk - comm)
    return margin
