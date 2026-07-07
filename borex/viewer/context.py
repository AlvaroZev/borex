from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pandas as pd

from borex.alexg.aoi2 import build_bidirectional_aoi
from borex.alexg.swings import detect_swings
from borex.backtest.portfolio import Trade
from borex.models.candle import Candle


def _ts_iso(ts: object) -> str:
    return pd.Timestamp(ts).isoformat()


def _ts_unix(ts: object) -> int:
    return int(pd.Timestamp(ts).timestamp())


def _parse_alexg2_pattern(pattern: str) -> dict[str, str]:
    parts = pattern.split("|")
    if len(parts) < 5 or parts[0] != "alexg2":
        return {}
    return {
        "trend": parts[1],
        "setup": parts[2],
        "aoi_kind": parts[3],
        "signal": parts[4],
    }


def _signal_label(meta: dict[str, str]) -> str:
    raw = meta.get("signal", "")
    labels = {
        "rejection": "Wick rejection",
        "bullish_engulfing": "Bullish engulfing",
        "bearish_engulfing": "Bearish engulfing",
        "momentum": "Momentum candle",
        "three_white_soldiers": "Three white soldiers",
        "three_black_crows": "Three black crows",
    }
    return labels.get(raw, raw.replace("_", " ").title() if raw else "Signal")


def _price_precision(symbol: str) -> tuple[int, float]:
    """(decimal places, min tick) for chart axis."""
    sym = symbol.upper()
    if "JPY" in sym:
        return 3, 0.001
    return 5, 0.00001


def _fmt_price(value: float, precision: int) -> str:
    return f"{value:.{precision}f}"


def _aoi_levels_at_signal(
    candles: list[Candle],
    signal_index: int,
    swing_lookback: int = 5,
    tolerance_pct: float = 0.002,
    min_touches: int = 2,
) -> list[dict[str, Any]]:
    window = candles[: signal_index + 1]
    swings = detect_swings(window, swing_lookback)
    zones = build_bidirectional_aoi(
        swings, window, tolerance_pct, min_touches
    )
    return [
        {
            "level": z.level,
            "kind": z.kind,
            "touches": z.touches,
            "recency": z.recency,
        }
        for z in zones[:12]
    ]


def trade_summary(trade: Trade, leverage: float, symbol: str = "") -> dict[str, Any]:
    signal_index = max(0, trade.entry_index - 1)
    meta = _parse_alexg2_pattern(trade.pattern)
    signal_label = _signal_label(meta)
    precision, _ = _price_precision(symbol)
    risk = abs(trade.entry_price - trade.stop_loss) if trade.stop_loss else 0
    reward = abs(trade.take_profit - trade.entry_price) if trade.take_profit else 0
    rr = reward / risk if risk > 0 else 0

    return {
        "id": None,  # filled by caller
        "side": trade.side.value,
        "pattern": trade.pattern,
        "meta": meta,
        "signal_label": signal_label,
        "signal_index": signal_index,
        "entry_index": trade.entry_index,
        "exit_index": trade.exit_index,
        "entry_time": _ts_iso(trade.entry_time),
        "exit_time": _ts_iso(trade.exit_time) if trade.exit_time else None,
        "entry_price": trade.entry_price,
        "exit_price": trade.exit_price,
        "stop_loss": trade.stop_loss,
        "take_profit": trade.take_profit,
        "planned_rr": round(rr, 2),
        "pnl": round(trade.pnl, 2),
        "pnl_pct": round(trade.pnl_pct * leverage, 4),
        "account_pct": round(
            trade.pnl / trade.entry_equity if trade.entry_equity else 0.0, 4
        ),
        "exit_reason": trade.exit_reason,
        "score": trade.score,
    }


def build_trade_chart(
    candles: list[Candle],
    trade: Trade,
    trade_id: int,
    leverage: float,
    symbol: str = "",
    bars_before: int = 55,
    bars_after: int = 20,
) -> dict[str, Any]:
    signal_index = max(0, trade.entry_index - 1)
    exit_index = trade.exit_index if trade.exit_index is not None else signal_index
    start = max(0, signal_index - bars_before)
    end = min(len(candles), exit_index + bars_after + 1)
    slice_candles = candles[start:end]

    meta = _parse_alexg2_pattern(trade.pattern)
    signal_label = _signal_label(meta)
    precision, min_move = _price_precision(symbol)
    signal_candle = candles[signal_index]

    ohlc = [
        {
            "time": _ts_unix(c.timestamp),
            "open": c.open,
            "high": c.high,
            "low": c.low,
            "close": c.close,
        }
        for c in slice_candles
    ]

    def abs_index(local_i: int) -> int:
        return start + local_i

    markers: list[dict[str, Any]] = []
    for i, c in enumerate(slice_candles):
        idx = abs_index(i)
        if idx == signal_index:
            markers.append(
                {
                    "time": _ts_unix(c.timestamp),
                    "position": "aboveBar" if trade.side.value == "short" else "belowBar",
                    "color": "#fbbf24",
                    "shape": "circle",
                    "text": signal_label,
                }
            )
        if idx == trade.entry_index:
            markers.append(
                {
                    "time": _ts_unix(c.timestamp),
                    "position": "belowBar" if trade.side.value == "long" else "aboveBar",
                    "color": "#22c55e" if trade.side.value == "long" else "#ef4444",
                    "shape": "arrowUp" if trade.side.value == "long" else "arrowDown",
                    "text": "Entry",
                }
            )
        if trade.exit_index is not None and idx == trade.exit_index:
            win = trade.pnl > 0
            markers.append(
                {
                    "time": _ts_unix(c.timestamp),
                    "position": "aboveBar" if trade.side.value == "long" else "belowBar",
                    "color": "#22c55e" if win else "#ef4444",
                    "shape": "square",
                    "text": trade.exit_reason.replace("_", " ").title(),
                }
            )

    aoi_levels = _aoi_levels_at_signal(candles, signal_index)

    summary = trade_summary(trade, leverage, symbol)
    summary["id"] = trade_id
    summary["chart_start_index"] = start
    summary["chart_end_index"] = end - 1
    summary["signal_time"] = _ts_iso(signal_candle.timestamp)
    summary["signal_candle"] = {
        "open": signal_candle.open,
        "high": signal_candle.high,
        "low": signal_candle.low,
        "close": signal_candle.close,
    }

    levels: list[dict[str, Any]] = []
    if trade.stop_loss is not None:
        levels.append(
            {
                "price": trade.stop_loss,
                "color": "#ef4444",
                "title": f"SL {_fmt_price(trade.stop_loss, precision)}",
            }
        )
    if trade.take_profit is not None:
        levels.append(
            {
                "price": trade.take_profit,
                "color": "#22c55e",
                "title": f"TP {_fmt_price(trade.take_profit, precision)}",
            }
        )
    for z in aoi_levels:
        color = "#3b82f6" if z["kind"] == "support" else "#a855f7"
        levels.append(
            {
                "price": z["level"],
                "color": color,
                "title": f"{z['kind']} {_fmt_price(z['level'], precision)} ({z['touches']}x)",
                "lineStyle": 2,
            }
        )

    return {
        "trade": summary,
        "candles": ohlc,
        "markers": markers,
        "levels": levels,
        "aoi_levels": aoi_levels,
        "price_precision": precision,
        "price_min_move": min_move,
    }


@dataclass
class ViewerSession:
    symbol: str
    timeframe: str
    strategy_name: str
    leverage: float
    candles: list[Candle]
    trades: list[Trade]
    summary_text: str
    total_return_pct: float
    win_rate: float
    total_trades: int

    def trade_summaries(self) -> list[dict[str, Any]]:
        out = []
        for i, t in enumerate(self.trades):
            s = trade_summary(t, self.leverage, self.symbol)
            s["id"] = i
            out.append(s)
        return out

    def trade_chart(self, trade_id: int) -> dict[str, Any]:
        if trade_id < 0 or trade_id >= len(self.trades):
            raise IndexError(trade_id)
        return build_trade_chart(
            self.candles,
            self.trades[trade_id],
            trade_id,
            self.leverage,
            self.symbol,
        )

    def to_json(self) -> str:
        payload = {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "strategy": self.strategy_name,
            "leverage": self.leverage,
            "summary": self.summary_text,
            "total_return_pct": self.total_return_pct,
            "win_rate": self.win_rate,
            "total_trades": self.total_trades,
            "trades": self.trade_summaries(),
        }
        return json.dumps(payload)
