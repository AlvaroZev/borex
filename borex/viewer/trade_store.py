from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from borex.backtest.portfolio import Trade

TRADES_FILE = "trades.csv"

TRADE_FIELDS = [
    "id",
    "symbol",
    "side",
    "pattern",
    "signal",
    "setup",
    "aoi_kind",
    "trend",
    "entry_index",
    "exit_index",
    "entry_time",
    "exit_time",
    "entry_price",
    "exit_price",
    "stop_loss",
    "take_profit",
    "planned_rr",
    "entry_cash",
    "entry_equity",
    "margin",
    "notional",
    "pnl",
    "pnl_pct",
    "account_pct",
    "margin_return_pct",
    "exit_reason",
    "score",
]


def trade_row(trade: Trade, trade_id: int, leverage: float) -> dict[str, Any]:
    parts = trade.pattern.split("|")
    signal = parts[6] if len(parts) > 6 else ""
    setup = parts[4] if len(parts) > 4 else ""
    aoi = parts[5] if len(parts) > 5 else ""
    trend = parts[3] if len(parts) > 3 else ""
    risk = abs(trade.entry_price - trade.stop_loss) if trade.stop_loss else 0
    reward = abs(trade.take_profit - trade.entry_price) if trade.take_profit else 0
    rr = reward / risk if risk > 0 else 0
    entry_cap = trade.entry_cash or trade.entry_equity
    return {
        "id": trade_id,
        "symbol": trade.symbol,
        "side": trade.side.value,
        "pattern": trade.pattern,
        "signal": signal,
        "setup": setup,
        "aoi_kind": aoi,
        "trend": trend,
        "entry_index": trade.entry_index,
        "exit_index": trade.exit_index,
        "entry_time": str(trade.entry_time),
        "exit_time": str(trade.exit_time),
        "entry_price": trade.entry_price,
        "exit_price": trade.exit_price,
        "stop_loss": trade.stop_loss,
        "take_profit": trade.take_profit,
        "planned_rr": round(rr, 4),
        "entry_cash": round(entry_cap, 4),
        "entry_equity": round(trade.entry_equity, 4),
        "margin": round(trade.margin, 4),
        "notional": round(trade.margin * leverage, 4),
        "pnl": round(trade.pnl, 4),
        "pnl_pct": trade.pnl_pct,
        "account_pct": (trade.pnl / entry_cap) if entry_cap else 0,
        "margin_return_pct": (trade.pnl / trade.margin) if trade.margin else 0,
        "exit_reason": trade.exit_reason,
        "score": trade.score,
    }


def save_trades_csv(
    trades: list[Trade],
    path: Path,
    *,
    leverage: float = 1.0,
) -> Path:
    """Write closed trades to CSV (file path or directory/trades.csv)."""
    path = Path(path)
    if path.suffix.lower() != ".csv":
        path = path / TRADES_FILE
    path.parent.mkdir(parents=True, exist_ok=True)

    rows = [trade_row(t, i, leverage) for i, t in enumerate(trades, 1)]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=TRADE_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return path
