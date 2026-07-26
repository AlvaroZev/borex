from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from borex_live.store.models import (
    BarCursor,
    LiveCandle,
    LiveTrade,
    PendingGhost,
    PortfolioState,
    ServiceEvent,
    ServiceRun,
)


@dataclass
class GhostSnapshot:
    symbol: str
    action: str
    pattern: str
    stop_loss: float
    take_profit: float
    planned_entry: float
    created_index: int
    expires_index: int
    saw_near_sl: bool = False


class StateRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def log_event(self, kind: str, message: str, payload: dict | None = None, level: str = "info") -> None:
        self.session.add(
            ServiceEvent(kind=kind, message=message, payload=payload, level=level)
        )

    def start_run(self, strategy: str, entry_mode: str, config: dict[str, Any]) -> int:
        safe = {
            k: (str(v) if hasattr(v, "__fspath__") else v)
            for k, v in config.items()
        }
        for run in self.session.query(ServiceRun).filter(ServiceRun.active.is_(True)):
            run.active = False
        row = ServiceRun(strategy=strategy, entry_mode=entry_mode, config_json=safe)
        self.session.add(row)
        self.session.flush()
        return int(row.id)

    def get_portfolio(self, initial_capital: float) -> PortfolioState:
        row = self.session.get(PortfolioState, 1)
        if row is None:
            row = PortfolioState(id=1, cash=initial_capital, initial_capital=initial_capital)
            self.session.add(row)
            self.session.flush()
        return row

    def set_cash(self, cash: float) -> None:
        row = self.get_portfolio(cash)
        row.cash = cash
        row.updated_at = datetime.now(timezone.utc)

    def closed_trades(self) -> list[LiveTrade]:
        return (
            self.session.query(LiveTrade)
            .filter(LiveTrade.status == "closed")
            .order_by(LiveTrade.id)
            .all()
        )

    def open_trades(self) -> list[LiveTrade]:
        return (
            self.session.query(LiveTrade)
            .filter(LiveTrade.status == "open")
            .order_by(LiveTrade.id)
            .all()
        )

    def get_open_trade(self, symbol: str) -> LiveTrade | None:
        return (
            self.session.query(LiveTrade)
            .filter(LiveTrade.symbol == symbol, LiveTrade.status == "open")
            .one_or_none()
        )

    def get_open_by_ticket(self, ticket: int) -> LiveTrade | None:
        if not ticket:
            return None
        return (
            self.session.query(LiveTrade)
            .filter(LiveTrade.mt5_ticket == int(ticket), LiveTrade.status == "open")
            .one_or_none()
        )

    def win_rate(self) -> float | None:
        closed = self.closed_trades()
        if not closed:
            return None
        wins = sum(1 for t in closed if t.pnl > 0)
        return wins / len(closed)

    def save_bar_cursor(self, symbol: str, ts: object, bar_index: int) -> None:
        iso = str(ts)
        row = self.session.get(BarCursor, symbol)
        if row is None:
            row = BarCursor(symbol=symbol, last_ts=iso, bar_index=bar_index)
            self.session.add(row)
        else:
            row.last_ts = iso
            row.bar_index = bar_index

    def get_bar_index(self, symbol: str) -> int:
        row = self.session.get(BarCursor, symbol)
        return int(row.bar_index) if row else 0

    def list_pending_ghosts(self) -> list[PendingGhost]:
        return (
            self.session.query(PendingGhost)
            .filter(PendingGhost.status == "waiting")
            .all()
        )

    def upsert_pending_ghost(self, ghost: GhostSnapshot, mt5_ticket: int | None = None) -> PendingGhost:
        existing = (
            self.session.query(PendingGhost)
            .filter(PendingGhost.symbol == ghost.symbol, PendingGhost.status == "waiting")
            .one_or_none()
        )
        if existing is None:
            existing = PendingGhost(
                symbol=ghost.symbol,
                action=ghost.action,
                pattern=ghost.pattern,
                stop_loss=ghost.stop_loss,
                take_profit=ghost.take_profit,
                planned_entry=ghost.planned_entry,
                created_index=ghost.created_index,
                expires_index=ghost.expires_index,
                saw_near_sl=ghost.saw_near_sl,
                mt5_ticket=mt5_ticket,
            )
            self.session.add(existing)
        else:
            existing.action = ghost.action
            existing.pattern = ghost.pattern
            existing.stop_loss = ghost.stop_loss
            existing.take_profit = ghost.take_profit
            existing.planned_entry = ghost.planned_entry
            existing.created_index = ghost.created_index
            existing.expires_index = ghost.expires_index
            existing.saw_near_sl = ghost.saw_near_sl
            if mt5_ticket is not None:
                existing.mt5_ticket = mt5_ticket
        self.session.flush()
        return existing

    def invalidate_pending(self, symbol: str, reason: str) -> None:
        row = (
            self.session.query(PendingGhost)
            .filter(PendingGhost.symbol == symbol, PendingGhost.status == "waiting")
            .one_or_none()
        )
        if row is None:
            return
        row.status = reason
        self.log_event("ghost_invalidated", f"{symbol}: {reason}", {"symbol": symbol})

    def open_live_trade(
        self,
        *,
        symbol: str,
        side: str,
        pattern: str,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        margin: float,
        rr_used: float,
        mt5_ticket: int | None,
        entry_time: str,
        expected_win_usd: float,
        expected_loss_usd: float,
    ) -> LiveTrade:
        trade = LiveTrade(
            symbol=symbol,
            side=side,
            pattern=pattern,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            margin=margin,
            rr_used=rr_used,
            mt5_ticket=mt5_ticket,
            status="open",
            entry_time=entry_time,
            expected_win_usd=expected_win_usd,
            expected_loss_usd=expected_loss_usd,
        )
        self.session.add(trade)
        self.session.flush()
        return trade

    def close_live_trade(
        self,
        trade_id: int,
        *,
        exit_price: float,
        exit_time: str,
        exit_reason: str,
        pnl: float,
    ) -> None:
        trade = self.session.get(LiveTrade, trade_id)
        if trade is None:
            return
        trade.status = "closed"
        trade.exit_price = exit_price
        trade.exit_time = exit_time
        trade.exit_reason = exit_reason
        trade.pnl = pnl

    def record_live_candle(
        self,
        *,
        symbol: str,
        ts: str,
        open_: float,
        high: float,
        low: float,
        close: float,
        volume: float = 0.0,
        interval: str = "1h",
    ) -> bool:
        """Insert live MT5 candle if not already stored. Returns True if new."""
        existing = (
            self.session.query(LiveCandle)
            .filter(LiveCandle.symbol == symbol, LiveCandle.ts == ts)
            .one_or_none()
        )
        if existing is not None:
            return False
        self.session.add(
            LiveCandle(
                symbol=symbol,
                ts=ts,
                open=open_,
                high=high,
                low=low,
                close=close,
                volume=volume,
                interval=interval,
            )
        )
        return True

    def live_candle_symbols(self) -> list[dict[str, Any]]:
        from sqlalchemy import func

        agg = (
            self.session.query(
                LiveCandle.symbol,
                func.count(LiveCandle.id),
                func.min(LiveCandle.ts),
                func.max(LiveCandle.ts),
            )
            .group_by(LiveCandle.symbol)
            .order_by(LiveCandle.symbol)
            .all()
        )
        return [
            {
                "symbol": sym,
                "count": int(cnt),
                "first_ts": first,
                "last_ts": last,
            }
            for sym, cnt, first, last in agg
        ]

    def live_candles_for_symbol(self, symbol: str, limit: int = 5000) -> list[LiveCandle]:
        q = (
            self.session.query(LiveCandle)
            .filter(LiveCandle.symbol == symbol)
            .order_by(LiveCandle.ts.asc())
        )
        rows = q.all()
        if limit and len(rows) > limit:
            return rows[-limit:]
        return rows

    def dashboard_snapshot(self) -> dict[str, Any]:
        pending = self.list_pending_ghosts()
        open_trades = self.open_trades()
        closed = self.closed_trades()
        pf = self.session.get(PortfolioState, 1)
        wr = self.win_rate()
        live_syms = self.live_candle_symbols()
        return {
            "cash": pf.cash if pf else 0.0,
            "initial_capital": pf.initial_capital if pf else 0.0,
            "win_rate": wr,
            "live_candle_count": sum(s["count"] for s in live_syms),
            "live_candle_symbols": live_syms,
            "pending_ghosts": [
                {
                    "id": g.id,
                    "symbol": g.symbol,
                    "action": g.action,
                    "pattern": g.pattern,
                    "stop_loss": g.stop_loss,
                    "take_profit": g.take_profit,
                    "planned_entry": g.planned_entry,
                    "mt5_ticket": g.mt5_ticket,
                    "status": g.status,
                    "expires_index": g.expires_index,
                }
                for g in pending
            ],
            "open_trades": [
                {
                    "id": t.id,
                    "symbol": t.symbol,
                    "side": t.side,
                    "entry_price": t.entry_price,
                    "stop_loss": t.stop_loss,
                    "take_profit": t.take_profit,
                    "margin": t.margin,
                    "rr_used": t.rr_used,
                    "expected_win_usd": t.expected_win_usd,
                    "expected_loss_usd": t.expected_loss_usd,
                    "mt5_ticket": t.mt5_ticket,
                    "entry_time": t.entry_time,
                    "pattern": t.pattern,
                    "floating_pnl": None,
                    "source": "db",
                }
                for t in open_trades
            ],
            "pending_orders_mt5": [],
            "mt5_positions": [],
            "closed_trades": [
                {
                    "id": t.id,
                    "symbol": t.symbol,
                    "side": t.side,
                    "pnl": t.pnl,
                    "exit_reason": t.exit_reason,
                }
                for t in closed[-50:]
            ],
        }

    def export_config_seed(self) -> str:
        return json.dumps(
            {
                "closed": [
                    {
                        "symbol": t.symbol,
                        "pnl": t.pnl,
                        "exit_reason": t.exit_reason,
                    }
                    for t in self.closed_trades()
                ]
            }
        )
