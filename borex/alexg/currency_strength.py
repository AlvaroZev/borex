from __future__ import annotations

from collections import defaultdict

from borex.models.candle import Candle, SignalAction


def parse_pair(symbol: str) -> tuple[str, str]:
    """EURUSD=X → (EUR, USD)."""
    raw = symbol.upper().replace("=X", "").replace("/", "")
    if len(raw) != 6:
        raise ValueError(f"Par FX no reconocido: {symbol}")
    return raw[:3], raw[3:]


def pair_return(candles: list[Candle], index: int, lookback: int) -> float | None:
    if index < lookback or lookback <= 0:
        return None
    prev = candles[index - lookback].close
    if prev <= 0:
        return None
    return (candles[index].close - prev) / prev


def compute_currency_strengths(
    symbol_indices: dict[str, int],
    candles_by_symbol: dict[str, list[Candle]],
    lookback: int = 24,
) -> dict[str, float]:
    """
    Strength score per currency from cross-market returns.

    Pair up → base strengthens, quote weakens.
    EUR/USD +0.5% → EUR +0.5%, USD -0.5% (averaged across all pairs).
    """
    buckets: dict[str, list[float]] = defaultdict(list)

    for symbol, idx in symbol_indices.items():
        candles = candles_by_symbol.get(symbol)
        if not candles:
            continue
        try:
            base, quote = parse_pair(symbol)
        except ValueError:
            continue
        ret = pair_return(candles, idx, lookback)
        if ret is None:
            continue
        buckets[base].append(ret)
        buckets[quote].append(-ret)

    return {ccy: sum(vals) / len(vals) for ccy, vals in buckets.items() if vals}


def strength_ranking(strengths: dict[str, float]) -> tuple[str | None, str | None]:
    if not strengths:
        return None, None
    ranked = sorted(strengths.items(), key=lambda x: x[1])
    return ranked[-1][0], ranked[0][0]


def trade_aligns_with_strength(
    base: str,
    quote: str,
    action: SignalAction,
    strengths: dict[str, float],
    min_edge: float,
) -> bool:
    """Long base = expect base stronger than quote; short base = opposite."""
    base_s = strengths.get(base, 0.0)
    quote_s = strengths.get(quote, 0.0)
    edge = base_s - quote_s
    if action == SignalAction.BUY:
        return edge >= min_edge
    if action == SignalAction.SELL:
        return -edge >= min_edge
    return False


def count_confirming_pairs(
    symbol_indices: dict[str, int],
    candles_by_symbol: dict[str, list[Candle]],
    currency: str,
    direction: str,
    lookback: int = 24,
) -> int:
    """How many pairs show this currency strengthening ('up') or weakening ('down')."""
    ccy = currency.upper()
    count = 0
    for symbol, idx in symbol_indices.items():
        candles = candles_by_symbol.get(symbol)
        if not candles:
            continue
        try:
            base, quote = parse_pair(symbol)
        except ValueError:
            continue
        ret = pair_return(candles, idx, lookback)
        if ret is None:
            continue
        if base == ccy and direction == "up" and ret > 0:
            count += 1
        elif base == ccy and direction == "down" and ret < 0:
            count += 1
        elif quote == ccy and direction == "up" and ret < 0:
            count += 1
        elif quote == ccy and direction == "down" and ret > 0:
            count += 1
    return count
