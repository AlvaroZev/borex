from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from borex_mirror.models import (
    MirrorEvent,
    MirrorPortfolio,
    MirrorTheoryState,
    MirrorTheoryTrade,
    MirrorTrade,
)


class MirrorRepository:
    """Persistence for mirror theory + real books (isolated from borex_live tables)."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def log_event(self, kind: str, message: str, level: str = "info") -> None:
        self.session.add(MirrorEvent(kind=kind, message=message, level=level))

    def get_portfolio(self, initial_capital: float) -> MirrorPortfolio:
        row = self.session.get(MirrorPortfolio, 1)
        if row is None:
            row = MirrorPortfolio(
                id=1, cash=initial_capital, initial_capital=initial_capital
            )
            self.session.add(row)
            self.session.flush()
        return row

    def set_cash(self, cash: float) -> None:
        row = self.get_portfolio(cash)
        row.cash = cash
        row.updated_at = datetime.now(timezone.utc)

    # --- theory (ShadowEngine adapter) ---

    def get_theory_state(self) -> MirrorTheoryState | None:
        return self.session.get(MirrorTheoryState, 1)

    def reset_theory(self) -> None:
        self.session.query(MirrorTheoryTrade).delete()
        row = self.session.get(MirrorTheoryState, 1)
        if row is not None:
            self.session.delete(row)
        self.session.flush()

    def save_theory_state(
        self,
        *,
        strategy: str,
        config_hash: str,
        initial_capital: float,
        cash: float,
        equity: float,
        last_master_ts: str,
        state_blob: bytes,
    ) -> MirrorTheoryState:
        row = self.session.get(MirrorTheoryState, 1)
        if row is None:
            row = MirrorTheoryState(
                id=1,
                strategy=strategy,
                config_hash=config_hash,
                initial_capital=initial_capital,
                cash=cash,
                equity=equity,
                last_master_ts=last_master_ts,
                state_blob=state_blob,
            )
            self.session.add(row)
        else:
            row.strategy = strategy
            row.config_hash = config_hash
            row.initial_capital = initial_capital
            row.cash = cash
            row.equity = equity
            row.last_master_ts = last_master_ts
            row.state_blob = state_blob
            row.updated_at = datetime.now(timezone.utc)
        self.session.flush()
        return row

    def upsert_theory_trade(self, trade: Any) -> MirrorTheoryTrade:
        entry_time = str(trade.entry_time)
        pattern = str(trade.pattern or "")
        row = (
            self.session.query(MirrorTheoryTrade)
            .filter(
                MirrorTheoryTrade.symbol == trade.symbol,
                MirrorTheoryTrade.entry_time == entry_time,
                MirrorTheoryTrade.pattern == pattern,
            )
            .one_or_none()
        )
        if row is None:
            row = MirrorTheoryTrade(
                symbol=trade.symbol,
                side=trade.side.value if hasattr(trade.side, "value") else str(trade.side),
                pattern=pattern,
                entry_index=int(trade.entry_index),
                entry_price=float(trade.entry_price),
                entry_time=entry_time,
                stop_loss=trade.stop_loss,
                take_profit=trade.take_profit,
                margin=float(trade.margin),
                rr_used=float(trade.score),
            )
            self.session.add(row)
        row.entry_index = int(trade.entry_index)
        row.entry_price = float(trade.entry_price)
        row.stop_loss = trade.stop_loss
        row.take_profit = trade.take_profit
        row.margin = float(trade.margin)
        row.rr_used = float(trade.score)
        row.commission = float(getattr(trade, "commission", 0) or 0.0)
        row.status = "open" if trade.is_open else "closed"
        row.exit_index = trade.exit_index
        row.exit_price = trade.exit_price
        row.exit_time = str(trade.exit_time) if trade.exit_time is not None else None
        row.exit_reason = trade.exit_reason or None
        row.pnl = float(trade.pnl or 0.0)
        self.session.flush()
        return row

    def find_theory_trade(
        self, symbol: str, entry_time: str, pattern: str
    ) -> MirrorTheoryTrade | None:
        return (
            self.session.query(MirrorTheoryTrade)
            .filter(
                MirrorTheoryTrade.symbol == symbol,
                MirrorTheoryTrade.entry_time == entry_time,
                MirrorTheoryTrade.pattern == pattern,
            )
            .one_or_none()
        )

    # --- mirror (real) trades ---

    def open_mirror_trade(self, **kwargs: Any) -> MirrorTrade:
        row = MirrorTrade(**kwargs)
        self.session.add(row)
        self.session.flush()
        return row

    def find_mirror_trade(
        self, symbol: str, entry_time: str, pattern: str
    ) -> MirrorTrade | None:
        return (
            self.session.query(MirrorTrade)
            .filter(
                MirrorTrade.symbol == symbol,
                MirrorTrade.entry_time == entry_time,
                MirrorTrade.pattern == pattern,
            )
            .one_or_none()
        )

    def open_mirror_trades(self) -> list[MirrorTrade]:
        return (
            self.session.query(MirrorTrade)
            .filter(MirrorTrade.status == "open")
            .order_by(MirrorTrade.entry_time)
            .all()
        )

    def closed_mirror_trades(self, limit: int = 200) -> list[MirrorTrade]:
        return (
            self.session.query(MirrorTrade)
            .filter(MirrorTrade.status == "closed")
            .order_by(MirrorTrade.id.desc())
            .limit(limit)
            .all()
        )

    def close_mirror_trade(
        self,
        trade_id: int,
        *,
        exit_price: float,
        exit_time: str,
        exit_reason: str,
        pnl: float,
    ) -> None:
        row = self.session.get(MirrorTrade, trade_id)
        if row is None:
            return
        row.status = "closed"
        row.exit_price = exit_price
        row.exit_time = exit_time
        row.exit_reason = exit_reason
        row.pnl = pnl

    def theory_snapshot(self) -> dict[str, Any]:
        state = self.get_theory_state()
        opens = (
            self.session.query(MirrorTheoryTrade)
            .filter(MirrorTheoryTrade.status == "open")
            .order_by(MirrorTheoryTrade.entry_time)
            .all()
        )
        closed = (
            self.session.query(MirrorTheoryTrade)
            .filter(MirrorTheoryTrade.status == "closed")
            .order_by(MirrorTheoryTrade.id.desc())
            .limit(200)
            .all()
        )

        def row(t: MirrorTheoryTrade) -> dict[str, Any]:
            return {
                "id": t.id,
                "symbol": t.symbol,
                "side": t.side,
                "pattern": t.pattern,
                "entry_price": t.entry_price,
                "entry_time": t.entry_time,
                "stop_loss": t.stop_loss,
                "take_profit": t.take_profit,
                "margin": t.margin,
                "rr_used": t.rr_used,
                "status": t.status,
                "exit_price": t.exit_price,
                "exit_time": t.exit_time,
                "exit_reason": t.exit_reason,
                "pnl": t.pnl,
            }

        closed_all = (
            self.session.query(MirrorTheoryTrade)
            .filter(MirrorTheoryTrade.status == "closed")
            .all()
        )
        wr = None
        if closed_all:
            wins = sum(1 for t in closed_all if float(t.pnl or 0) > 0)
            wr = wins / len(closed_all)
        return {
            "active": state is not None,
            "cash": state.cash if state else 0.0,
            "equity": state.equity if state else 0.0,
            "initial_capital": state.initial_capital if state else 0.0,
            "last_master_ts": state.last_master_ts if state else "",
            "win_rate_recent": wr,
            "open_trades": [row(t) for t in opens],
            "closed_trades": [row(t) for t in closed],
        }

    def mirror_snapshot(self) -> dict[str, Any]:
        pf = self.session.get(MirrorPortfolio, 1)
        opens = self.open_mirror_trades()
        closed = self.closed_mirror_trades()

        def row(t: MirrorTrade) -> dict[str, Any]:
            return {
                "id": t.id,
                "theory_trade_id": t.theory_trade_id,
                "symbol": t.symbol,
                "side": t.side,
                "pattern": t.pattern,
                "theory_entry": t.theory_entry,
                "entry_price": t.entry_price,
                "entry_time": t.entry_time,
                "stop_loss": t.stop_loss,
                "take_profit": t.take_profit,
                "sl_pips": t.sl_pips,
                "tp_pips": t.tp_pips,
                "margin": t.margin,
                "rr_used": t.rr_used,
                "mt5_ticket": t.mt5_ticket,
                "volume": t.volume,
                "status": t.status,
                "exit_price": t.exit_price,
                "exit_time": t.exit_time,
                "exit_reason": t.exit_reason,
                "pnl": t.pnl,
                "expected_win_usd": t.expected_win_usd,
                "expected_loss_usd": t.expected_loss_usd,
                "paper": t.paper,
            }

        return {
            "cash": pf.cash if pf else 0.0,
            "initial_capital": pf.initial_capital if pf else 0.0,
            "open_trades": [row(t) for t in opens],
            "closed_trades": [row(t) for t in closed],
        }
