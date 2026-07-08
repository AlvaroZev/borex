from __future__ import annotations

from dataclasses import dataclass, field

from borex.alexg.currency_strength import (
    compute_currency_strengths,
    count_confirming_pairs,
    parse_pair,
    strength_ranking,
    trade_aligns_with_strength,
)
from borex.data.symbols import FOREX_PAIRS
from borex.models.candle import Candle, SignalAction


def default_forex_universe() -> list[str]:
    return list(FOREX_PAIRS)


def build_timestamp_index(candles: list[Candle]) -> dict[object, int]:
    return {c.timestamp: i for i, c in enumerate(candles)}


def align_symbols_to_timeline(
    master_candles: list[Candle],
    candles_by_symbol: dict[str, list[Candle]],
) -> dict[str, dict[object, int]]:
    """Map each symbol's timestamp → bar index."""
    out: dict[str, dict[object, int]] = {}
    for symbol, candles in candles_by_symbol.items():
        out[symbol] = build_timestamp_index(candles)
    return out


@dataclass
class MultiMarketContext:
    """Cross-market state at one point in time (shared across pairs)."""

    master_index: int
    timestamp: object
    indices: dict[str, int] = field(default_factory=dict)
    strengths: dict[str, float] = field(default_factory=dict)
    strongest: str | None = None
    weakest: str | None = None
    strength_lookback: int = 24
    min_currency_edge: float = 0.0
    min_confirming_pairs: int = 1
    candles_by_symbol: dict[str, list[Candle]] = field(default_factory=dict)

    @classmethod
    def at_master_bar(
        cls,
        master_index: int,
        master_candles: list[Candle],
        candles_by_symbol: dict[str, list[Candle]],
        ts_maps: dict[str, dict[object, int]],
        strength_lookback: int = 24,
        min_currency_edge: float = 0.0,
        min_confirming_pairs: int = 1,
    ) -> MultiMarketContext:
        ts = master_candles[master_index].timestamp
        indices: dict[str, int] = {}
        for symbol, ts_map in ts_maps.items():
            idx = ts_map.get(ts)
            if idx is not None:
                indices[symbol] = idx

        strengths = compute_currency_strengths(
            indices, candles_by_symbol, strength_lookback
        )
        strongest, weakest = strength_ranking(strengths)

        return cls(
            master_index=master_index,
            timestamp=ts,
            indices=indices,
            strengths=strengths,
            strongest=strongest,
            weakest=weakest,
            strength_lookback=strength_lookback,
            min_currency_edge=min_currency_edge,
            min_confirming_pairs=min_confirming_pairs,
            candles_by_symbol=candles_by_symbol,
        )

    def allows_trade(self, symbol: str, action: SignalAction) -> bool:
        if symbol not in self.indices:
            return False
        try:
            base, quote = parse_pair(symbol)
        except ValueError:
            return False

        if not trade_aligns_with_strength(
            base, quote, action, self.strengths, self.min_currency_edge
        ):
            return False

        if self.min_confirming_pairs <= 1:
            return True

        if action == SignalAction.BUY:
            base_ok = count_confirming_pairs(
                self.indices,
                self.candles_by_symbol,
                base,
                "up",
                self.strength_lookback,
            )
            quote_ok = count_confirming_pairs(
                self.indices,
                self.candles_by_symbol,
                quote,
                "down",
                self.strength_lookback,
            )
            return (
                base_ok >= self.min_confirming_pairs
                or quote_ok >= self.min_confirming_pairs
            )

        base_ok = count_confirming_pairs(
            self.indices,
            self.candles_by_symbol,
            base,
            "down",
            self.strength_lookback,
        )
        quote_ok = count_confirming_pairs(
            self.indices,
            self.candles_by_symbol,
            quote,
            "up",
            self.strength_lookback,
        )
        return (
            base_ok >= self.min_confirming_pairs
            or quote_ok >= self.min_confirming_pairs
        )

    def strength_summary(self) -> str:
        if not self.strengths:
            return "neutral"
        parts = sorted(self.strengths.items(), key=lambda x: -x[1])[:4]
        return "|".join(f"{c}:{v:+.4f}" for c, v in parts)


def load_universe_candles(
    symbols: list[str],
    period: str,
    interval: str,
    cache_mode: str,
    loader,
) -> dict[str, list[Candle]]:
    """Load OHLCV for each symbol; skip failures."""
    out: dict[str, list[Candle]] = {}
    for sym in symbols:
        try:
            out[sym] = loader(sym, period, interval, cache_mode=cache_mode)
        except Exception:
            continue
    return out


def pick_master_symbol(
    candles_by_symbol: dict[str, list[Candle]],
    preferred: str = "EURUSD=X",
) -> str:
    if preferred in candles_by_symbol:
        return preferred
    if not candles_by_symbol:
        raise ValueError("No hay datos para ningún símbolo")
    return max(candles_by_symbol.items(), key=lambda kv: len(kv[1]))[0]


def intersect_timeline(
    master_candles: list[Candle],
    candles_by_symbol: dict[str, list[Candle]],
    min_symbols: int = 3,
) -> list[int]:
    """Master bar indices where at least min_symbols have a bar at the same timestamp."""
    ts_sets = []
    for candles in candles_by_symbol.values():
        ts_sets.append({c.timestamp for c in candles})
    if not ts_sets:
        return list(range(len(master_candles)))

    common = set.intersection(*ts_sets) if len(ts_sets) > 1 else ts_sets[0]
    if len(common) < 50:
        return list(range(len(master_candles)))

    valid: list[int] = []
    for i, c in enumerate(master_candles):
        if c.timestamp in common:
            valid.append(i)
    return valid
