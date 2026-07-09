from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Any
from borex.alexg.aoi2 import build_bidirectional_aoi
from borex.alexg.multi_market import (
    MultiMarketContext,
    align_symbols_to_timeline,
    pick_master_symbol,
)
from borex.alexg.strategy3 import AlexG3Strategy
from borex.alexg.swings import detect_swings
from borex.models.candle import Candle
from borex.viewer.context import _parse_alexg2_pattern, _price_precision, _signal_label


def _ts_iso(ts: object) -> str:
    import pandas as pd

    if isinstance(ts, (int, float)) and ts < 10**12:
        return pd.Timestamp(ts, unit="s").isoformat()
    return pd.Timestamp(ts).isoformat()


def _ts_unix(ts: object) -> int:
    import pandas as pd

    return int(pd.Timestamp(ts).timestamp())


def _fmt_price(value: float, precision: int) -> str:
    return f"{value:.{precision}f}"


def strategy_params(strategy: AlexG3Strategy) -> dict[str, Any]:
    params = {
        "min_rr": strategy.min_rr,
        "tp_fraction": strategy.tp_fraction,
        "swing_lookback": strategy.swing_lookback,
        "aoi_tolerance_pct": strategy.aoi_tolerance_pct,
        "min_aoi_touches": strategy.min_aoi_touches,
        "min_bars": strategy.min_bars,
        "signal_cooldown": strategy.signal_cooldown,
        "strength_lookback": strategy.strength_lookback,
        "min_currency_edge": strategy.min_currency_edge,
        "min_confirming_pairs": strategy.min_confirming_pairs,
        "require_currency_filter": strategy.require_currency_filter,
        "filter_false_positives": strategy.filter_false_positives,
        "disabled_signals": strategy.disabled_signals,
        "name": strategy.name,
    }
    sl_wait = getattr(strategy, "sl_wait_max_bars", None)
    if sl_wait is not None:
        params["sl_wait_max_bars"] = sl_wait
    return params


def latest_aoi_levels(
    candles: list[Candle],
    swing_lookback: int = 5,
    tolerance_pct: float = 0.002,
    min_touches: int = 2,
) -> list[dict[str, Any]]:
    if len(candles) < 80:
        return []
    swings = detect_swings(candles, swing_lookback)
    zones = build_bidirectional_aoi(swings, candles, tolerance_pct, min_touches)
    return [
        {
            "level": z.level,
            "kind": z.kind,
            "touches": z.touches,
            "recency": z.recency,
        }
        for z in zones[:12]
    ]


def _decision_from_signal(symbol: str, signal, candles: list[Candle]) -> dict[str, Any]:
    meta = _parse_alexg2_pattern(signal.pattern)
    candle = candles[signal.index]
    precision, _ = _price_precision(symbol)
    return {
        "time": _ts_iso(candle.timestamp),
        "time_unix": _ts_unix(candle.timestamp),
        "index": signal.index,
        "symbol": symbol,
        "action": signal.action.value,
        "trend": meta.get("trend", ""),
        "setup": meta.get("setup", ""),
        "aoi_kind": meta.get("aoi_kind", ""),
        "signal": meta.get("signal", ""),
        "signal_label": _signal_label(meta),
        "currency_bias": meta.get("currency_bias", ""),
        "strength": "",
        "pattern": signal.pattern,
        "stop_loss": signal.stop_loss,
        "take_profit": signal.take_profit,
        "price": signal.price,
        "price_fmt": _fmt_price(signal.price, precision),
    }


@dataclass
class MarketAnalysis:
    master_symbol: str
    symbols: list[str]
    master_timeline_unix: list[int] = field(default_factory=list)
    symbol_ts_unix: dict[str, dict[int, int]] = field(default_factory=dict)
    decisions_by_symbol: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    all_decisions: list[dict[str, Any]] = field(default_factory=list)
    aoi_by_symbol: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    aligned_candles: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    bar_counts: dict[str, int] = field(default_factory=dict)
    date_ranges: dict[str, dict[str, str]] = field(default_factory=dict)
    timeframe: str = ""
    saved_at: str = ""
    source_path: str = ""

    @property
    def total_decisions(self) -> int:
        return len(self.all_decisions)

    def overview(self) -> dict[str, Any]:
        timeline_start = self.master_timeline_unix[0] if self.master_timeline_unix else None
        timeline_end = self.master_timeline_unix[-1] if self.master_timeline_unix else None
        return {
            "master_symbol": self.master_symbol,
            "symbols": self.symbols,
            "bar_counts": self.bar_counts,
            "date_ranges": self.date_ranges,
            "timeline_start": timeline_start,
            "timeline_end": timeline_end,
            "timeline_bar_count": len(self.master_timeline_unix),
            "decision_counts": {
                sym: len(self.decisions_by_symbol.get(sym, []))
                for sym in self.symbols
            },
            "total_decisions": len(self.all_decisions),
            "decisions": self.all_decisions,
            "saved_at": self.saved_at,
            "source_path": self.source_path,
            "from_cache": bool(self.source_path),
        }

    def _aligned_candles(
        self,
        symbol: str,
        candles: list[Candle],
    ) -> list[dict[str, Any]]:
        ts_lookup = self.symbol_ts_unix.get(symbol, {})
        out: list[dict[str, Any]] = []
        for t in self.master_timeline_unix:
            idx = ts_lookup.get(t)
            if idx is None:
                continue
            c = candles[idx]
            out.append(
                {
                    "time": t,
                    "open": c.open,
                    "high": c.high,
                    "low": c.low,
                    "close": c.close,
                }
            )
        return out

    def populate_aligned_candles(
        self, candles_by_symbol: dict[str, list[Candle]]
    ) -> None:
        for sym in self.symbols:
            candles = candles_by_symbol.get(sym)
            if candles:
                self.aligned_candles[sym] = self._aligned_candles(sym, candles)

    def market_chart(
        self,
        symbol: str,
        candles: list[Candle] | None = None,
        show_aoi: bool = True,
    ) -> dict[str, Any]:
        precision, min_move = _price_precision(symbol)
        decisions = self.decisions_by_symbol.get(symbol, [])
        if symbol in self.aligned_candles:
            ohlc = self.aligned_candles[symbol]
        elif candles:
            ohlc = self._aligned_candles(symbol, candles)
        else:
            ohlc = []

        markers: list[dict[str, Any]] = []
        for d in decisions:
            is_long = d["action"] == "buy"
            label = d["signal_label"][:24]
            markers.append(
                {
                    "time": d["time_unix"],
                    "position": "belowBar" if is_long else "aboveBar",
                    "color": "#fbbf24",
                    "shape": "circle",
                    "text": label,
                }
            )

        levels: list[dict[str, Any]] = []
        if show_aoi:
            for z in self.aoi_by_symbol.get(symbol, []):
                color = "#3b82f6" if z["kind"] == "support" else "#a855f7"
                levels.append(
                    {
                        "price": z["level"],
                        "color": color,
                        "title": (
                            f"{z['kind']} {_fmt_price(z['level'], precision)} "
                            f"({z['touches']}x)"
                        ),
                        "lineStyle": 2,
                    }
                )

        start = candles[0].timestamp if candles else None
        end = candles[-1].timestamp if candles else None
        if ohlc and candles is None:
            start = ohlc[0]["time"]
            end = ohlc[-1]["time"]
        return {
            "symbol": symbol,
            "bar_count": len(ohlc),
            "aligned_bar_count": len(ohlc),
            "start_time": _ts_iso(start) if start is not None else None,
            "end_time": _ts_iso(end) if end is not None else None,
            "timeline_start": self.master_timeline_unix[0] if self.master_timeline_unix else None,
            "timeline_end": self.master_timeline_unix[-1] if self.master_timeline_unix else None,
            "candles": ohlc,
            "decisions": decisions,
            "markers": markers,
            "levels": levels,
            "aoi_levels": self.aoi_by_symbol.get(symbol, []),
            "price_precision": precision,
            "price_min_move": min_move,
        }


def scan_alexg3_decisions(
    candles_by_symbol: dict[str, list[Candle]],
    strategy: AlexG3Strategy,
    master_symbol: str | None = None,
) -> MarketAnalysis:
    if not candles_by_symbol:
        raise ValueError("No candle data loaded")

    master = master_symbol or pick_master_symbol(candles_by_symbol)
    master_candles = candles_by_symbol[master]
    symbols = sorted(candles_by_symbol.keys())
    ts_maps = align_symbols_to_timeline(master_candles, candles_by_symbol)
    master_timeline_unix = [_ts_unix(c.timestamp) for c in master_candles]
    symbol_ts_unix = {
        sym: {_ts_unix(ts): idx for ts, idx in ts_map.items()}
        for sym, ts_map in ts_maps.items()
    }

    scanner = type(strategy)(**strategy_params(strategy))
    min_bars = scanner.min_bars

    decisions_by_symbol: dict[str, list[dict[str, Any]]] = {s: [] for s in symbols}
    all_decisions: list[dict[str, Any]] = []

    scan_end = len(master_candles)
    scan_range = max(0, scan_end - min_bars)
    progress_every = max(1, scan_range // 20)  # ~5% steps

    print(
        f"[alexg3-scan] start | master={master} | pairs={len(symbols)} | "
        f"bars={scan_range:,} (idx {min_bars}→{scan_end - 1})",
        flush=True,
        file=sys.stderr,
    )

    for master_i in range(min_bars, scan_end):
        ctx = MultiMarketContext.at_master_bar(
            master_i,
            master_candles,
            candles_by_symbol,
            ts_maps,
            strength_lookback=scanner.strength_lookback,
            min_currency_edge=scanner.min_currency_edge,
            min_confirming_pairs=scanner.min_confirming_pairs,
        )

        for sym in symbols:
            idx = ctx.indices.get(sym)
            if idx is None or idx < min_bars:
                continue
            scanner.set_context(sym, ctx)
            signal = scanner.on_bar(idx, candles_by_symbol[sym], None)
            if signal is None:
                continue
            row = _decision_from_signal(sym, signal, candles_by_symbol[sym])
            strength_tag = signal.pattern.split("|")[-1] if "|" in signal.pattern else ""
            row["strength"] = strength_tag
            decisions_by_symbol[sym].append(row)
            all_decisions.append(row)

        done = master_i - min_bars + 1
        if done % progress_every == 0 or master_i == scan_end - 1:
            pct = (done / scan_range * 100) if scan_range else 100.0
            print(
                f"[alexg3-scan] bar {master_i}/{scan_end - 1} ({pct:.0f}%) | "
                f"signals={len(all_decisions)} | "
                f"last={master_candles[master_i].timestamp}",
                flush=True,
                file=sys.stderr,
            )

    all_decisions.sort(key=lambda r: (r["time_unix"], r["symbol"]))

    aoi_by_symbol: dict[str, list[dict[str, Any]]] = {}
    bar_counts: dict[str, int] = {}
    date_ranges: dict[str, dict[str, str]] = {}
    for sym, candles in candles_by_symbol.items():
        bar_counts[sym] = len(candles)
        if candles:
            date_ranges[sym] = {
                "start": _ts_iso(candles[0].timestamp),
                "end": _ts_iso(candles[-1].timestamp),
            }
        aoi_by_symbol[sym] = latest_aoi_levels(
            candles,
            swing_lookback=scanner.swing_lookback,
            tolerance_pct=scanner.aoi_tolerance_pct,
            min_touches=scanner.min_aoi_touches,
        )

    analysis = MarketAnalysis(
        master_symbol=master,
        symbols=symbols,
        master_timeline_unix=master_timeline_unix,
        symbol_ts_unix=symbol_ts_unix,
        decisions_by_symbol=decisions_by_symbol,
        all_decisions=all_decisions,
        aoi_by_symbol=aoi_by_symbol,
        bar_counts=bar_counts,
        date_ranges=date_ranges,
    )
    analysis.populate_aligned_candles(candles_by_symbol)
    return analysis
