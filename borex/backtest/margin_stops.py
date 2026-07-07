from __future__ import annotations

from borex.backtest.portfolio import PositionSide


def tp_from_sl_rr(
    entry: float,
    stop_loss: float,
    side: PositionSide,
    rr: float,
) -> float:
    """TP at risk × rr from structural/margin SL."""
    risk = abs(entry - stop_loss)
    if risk <= 0 or rr <= 0:
        return entry
    if side == PositionSide.LONG:
        return entry + risk * rr
    return entry - risk * rr


def rr_from_winrate(winrate: float | None, default_rr: float = 2.0) -> float:
    """
    RR needed for break-even given historical win rate: RR = 1 / winrate.
    Example: 50% winrate → RR 2. Before any trades, uses default_rr.
    """
    if winrate is None or winrate <= 0:
        return default_rr
    return 1.0 / winrate


def tighten_sl_to_margin_stop(
    entry: float,
    stop_loss: float | None,
    side: PositionSide,
    leverage: float,
) -> float:
    """Never place SL farther than the margin wipe distance."""
    move = margin_stop_move_pct(leverage)
    if side == PositionSide.LONG:
        cap = entry * (1.0 - move)
        if stop_loss is None:
            return cap
        return max(stop_loss, cap)
    cap = entry * (1.0 + move)
    if stop_loss is None:
        return cap
    return min(stop_loss, cap)


def margin_stop_move_pct(leverage: float) -> float:
    """Price move (fraction) that wipes the posted margin at given broker leverage."""
    if leverage <= 0:
        return 0.0
    return 1.0 / leverage


def margin_stop_out_prices(
    entry: float,
    side: PositionSide,
    leverage: float,
    rr: float = 2.0,
) -> tuple[float, float]:
    """
    True SL: stop at margin liquidation (lose exactly posted margin).
    TP: reward = risk × rr from that stop distance.
    """
    move = margin_stop_move_pct(leverage)
    if side == PositionSide.LONG:
        sl = entry * (1.0 - move)
        risk = entry - sl
        tp = entry + risk * rr
    else:
        sl = entry * (1.0 + move)
        risk = sl - entry
        tp = entry - risk * rr
    return sl, tp
