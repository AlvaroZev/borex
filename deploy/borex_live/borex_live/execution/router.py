from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from borex.alexg.strategy4 import _PendingSetup
from borex.models.candle import Signal, SignalAction

from borex_live.entry_mode import EntryMode
from borex_live.mt5.client import Mt5Client
from borex_live.mt5.symbols import mt5_to_yahoo
from borex_live.store.repository import GhostSnapshot, StateRepository

logger = logging.getLogger(__name__)


def read_pending_snapshot(strategy: Any) -> dict[str, GhostSnapshot]:
    pending = getattr(strategy, "_pending", None)
    if not isinstance(pending, dict):
        return {}
    out: dict[str, GhostSnapshot] = {}
    for symbol, p in pending.items():
        if not isinstance(p, _PendingSetup):
            continue
        out[symbol] = GhostSnapshot(
            symbol=symbol,
            action=p.action.value if hasattr(p.action, "value") else str(p.action),
            pattern=p.pattern,
            stop_loss=p.stop_loss,
            take_profit=p.take_profit,
            planned_entry=p.planned_entry,
            created_index=p.created_index,
            expires_index=p.expires_index,
            saw_near_sl=p.saw_near_sl,
        )
    return out


def restore_pending_to_strategy(strategy: Any, ghosts: list[GhostSnapshot]) -> None:
    if not hasattr(strategy, "_pending"):
        return
    for g in ghosts:
        action = SignalAction.BUY if g.action in ("buy", "BUY") else SignalAction.SELL
        strategy._pending[g.symbol] = _PendingSetup(
            action=action,
            pattern=g.pattern,
            stop_loss=g.stop_loss,
            take_profit=g.take_profit,
            planned_entry=g.planned_entry,
            created_index=g.created_index,
            expires_index=g.expires_index,
            saw_near_sl=g.saw_near_sl,
        )


def late_entry_stops(ghost: GhostSnapshot) -> tuple[float, float]:
    """Protective SL/TP once the ghost trigger fills at ghost.stop_loss."""
    fill = float(ghost.stop_loss)
    risk = abs(float(ghost.planned_entry) - float(ghost.stop_loss))
    reward = abs(float(ghost.take_profit) - float(ghost.planned_entry))
    if ghost.action.lower() in ("buy", "long"):
        return fill - risk, fill + reward
    return fill + risk, fill - reward


@dataclass
class ExecutionRouter:
    entry_mode: EntryMode
    mt5: Mt5Client
    repo: StateRepository
    default_lot: float = 0.01
    dry_run: bool = False

    def sync_ghost_pending_orders(
        self,
        before: dict[str, GhostSnapshot],
        after: dict[str, GhostSnapshot],
    ) -> None:
        if self.entry_mode != EntryMode.GHOST:
            return

        for symbol, ghost in after.items():
            if symbol in before:
                continue
            self._place_ghost_pending(symbol, ghost)

        for symbol in before:
            if symbol not in after:
                self._cancel_ghost_pending(symbol, "strategy_removed")

    def ensure_broker_pendings(self) -> int:
        """Place any waiting ghosts that have no live MT5 pending ticket."""
        if self.entry_mode != EntryMode.GHOST:
            return 0
        placed = 0
        for row in self.repo.list_pending_ghosts():
            ticket = int(row.mt5_ticket) if row.mt5_ticket else 0
            if ticket and self.mt5.pending_order_exists(ticket):
                continue
            # Already filled into a position — leave sync_ghost_fills to book it.
            if self.mt5.position_for_ghost(row.symbol) is not None:
                continue
            ghost = GhostSnapshot(
                symbol=row.symbol,
                action=row.action,
                pattern=row.pattern,
                stop_loss=row.stop_loss,
                take_profit=row.take_profit,
                planned_entry=row.planned_entry,
                created_index=row.created_index,
                expires_index=row.expires_index,
                saw_near_sl=row.saw_near_sl,
            )
            self._place_ghost_pending(row.symbol, ghost)
            placed += 1
        return placed

    def sync_ghost_fills(
        self,
        *,
        margin: float,
        rr_used: float,
        expected_win: float,
        expected_loss: float,
    ) -> list[str]:
        """
        Detect MT5 pendings that filled (position opened) and book them in DB
        without waiting for the next H1 strategy confirmation.
        """
        if self.entry_mode != EntryMode.GHOST:
            return []
        filled: list[str] = []
        open_syms = {t.symbol for t in self.repo.open_trades()}
        for row in self.repo.list_pending_ghosts():
            if row.symbol in open_syms:
                continue
            pos = self.mt5.position_for_ghost(row.symbol)
            ticket = int(row.mt5_ticket) if row.mt5_ticket else 0
            still_pending = ticket > 0 and self.mt5.pending_order_exists(ticket)

            if pos is None:
                continue
            # Position exists → pending filled (or market path on touch)
            if still_pending:
                # rare: both exist; prefer cancel leftover pending
                self.mt5.cancel_order(ticket)

            sl, tp = late_entry_stops(
                GhostSnapshot(
                    symbol=row.symbol,
                    action=row.action,
                    pattern=row.pattern,
                    stop_loss=row.stop_loss,
                    take_profit=row.take_profit,
                    planned_entry=row.planned_entry,
                    created_index=row.created_index,
                    expires_index=row.expires_index,
                    saw_near_sl=row.saw_near_sl,
                )
            )
            # Prefer broker SL/TP if already set; otherwise attach ours.
            use_sl = pos.sl if pos.sl else sl
            use_tp = pos.tp if pos.tp else tp
            if (not pos.sl or not pos.tp) and (sl or tp):
                self.mt5.modify_position_sltp(pos.ticket, use_sl, use_tp)

            side = "buy" if pos.side == "long" else "sell"
            self.repo.invalidate_pending(row.symbol, "filled")
            self.repo.open_live_trade(
                symbol=row.symbol,
                side=side,
                pattern=row.pattern,
                entry_price=pos.price_open,
                stop_loss=use_sl,
                take_profit=use_tp,
                margin=margin,
                rr_used=rr_used,
                mt5_ticket=pos.ticket,
                entry_time="",
                expected_win_usd=expected_win,
                expected_loss_usd=expected_loss,
            )
            self.repo.log_event(
                "ghost_pending_filled",
                f"{row.symbol} auto-filled ticket={pos.ticket} @ {pos.price_open}",
                {"sl": use_sl, "tp": use_tp},
            )
            logger.info(
                "Ghost pending filled on MT5: %s ticket=%s @ %s",
                row.symbol,
                pos.ticket,
                pos.price_open,
            )
            filled.append(row.symbol)
        return filled

    def reconcile_open_with_mt5(self) -> list[str]:
        """Close DB opens that no longer exist on MT5 (broker SL/TP/manual)."""
        if self.dry_run or not self.mt5.connected:
            return []
        closed: list[str] = []
        live = self.mt5.open_positions()
        live_tickets = {int(p.ticket) for p in live}
        live_syms = {mt5_to_yahoo(p.symbol) for p in live}

        for trade in list(self.repo.open_trades()):
            ticket = int(trade.mt5_ticket) if trade.mt5_ticket else 0
            still_open = False
            if ticket and ticket in live_tickets:
                still_open = True
            elif trade.symbol in live_syms and not ticket:
                still_open = True
            if still_open:
                continue
            # Gone from broker → mark closed (unknown exit; use last entry as proxy)
            self.repo.close_live_trade(
                int(trade.id),
                exit_price=float(trade.entry_price),
                exit_time="",
                exit_reason="mt5_closed",
                pnl=0.0,
            )
            self.repo.log_event(
                "trade_closed_broker",
                f"{trade.symbol} ticket={ticket} missing on MT5",
            )
            logger.info("Reconciled closed on MT5: %s ticket=%s", trade.symbol, ticket)
            closed.append(trade.symbol)
        return closed

    def _place_ghost_pending(self, symbol: str, ghost: GhostSnapshot) -> None:
        side = ghost.action.lower()
        if side in ("long",):
            side = "buy"
        elif side in ("short",):
            side = "sell"
        prot_sl, prot_tp = late_entry_stops(ghost)
        result = self.mt5.place_pending_ghost(
            symbol,
            side,
            ghost.stop_loss,
            self.default_lot,
            sl=prot_sl,
            tp=prot_tp,
            comment=f"bx_g|{ghost.pattern[:20]}",
        )
        ticket = result.ticket if result.ok else None
        self.repo.upsert_pending_ghost(ghost, mt5_ticket=ticket)
        level = "info" if result.ok else "error"
        self.repo.log_event(
            "ghost_pending_placed",
            f"{symbol} {side} limit @ {ghost.stop_loss} sl={prot_sl} tp={prot_tp}",
            {
                "ticket": ticket,
                "ok": result.ok,
                "message": result.message,
                "retcode": result.retcode,
            },
            level=level,
        )
        if result.ok:
            logger.info(
                "Placed MT5 pending %s %s @ %.5f (ticket=%s)",
                symbol,
                side,
                ghost.stop_loss,
                ticket,
            )
        else:
            logger.error(
                "Failed MT5 pending %s %s @ %.5f: %s (retcode=%s)",
                symbol,
                side,
                ghost.stop_loss,
                result.message,
                result.retcode,
            )

    def _cancel_ghost_pending(self, symbol: str, reason: str) -> None:
        rows = [g for g in self.repo.list_pending_ghosts() if g.symbol == symbol]
        for row in rows:
            if row.mt5_ticket:
                self.mt5.cancel_order(int(row.mt5_ticket))
            self.repo.invalidate_pending(symbol, reason)
            logger.info("Cancelled MT5 pending for %s (%s)", symbol, reason)

    def handle_immediate_signal(self, symbol: str, signal: Signal, sl: float, tp: float) -> int | None:
        if self.entry_mode != EntryMode.IMMEDIATE:
            return None
        side = "buy" if signal.action == SignalAction.BUY else "sell"
        result = self.mt5.place_market_with_sltp(
            symbol,
            side,
            self.default_lot,
            sl,
            tp,
            comment=f"bx_i|{signal.pattern[:20]}",
        )
        self.repo.log_event(
            "immediate_order",
            f"{symbol} {side}",
            {"ok": result.ok, "ticket": result.ticket, "message": result.message},
        )
        return result.ticket if result.ok else None

    def resolve_ghost_entry_ticket(self, symbol: str) -> int | None:
        """If the broker pending already filled, return the position ticket."""
        pos = self.mt5.position_for_ghost(symbol)
        return int(pos.ticket) if pos else None

    def handle_entry_fill(
        self,
        symbol: str,
        signal: Signal,
        sl: float,
        tp: float,
        *,
        margin: float,
        rr_used: float,
        expected_win: float,
        expected_loss: float,
        mt5_ticket: int | None = None,
    ) -> None:
        """Market entry (ghost fill adopt/fallback, immediate, or flip)."""
        # Already booked (e.g. sync_ghost_fills)
        if any(t.symbol == symbol for t in self.repo.open_trades()):
            self.repo.invalidate_pending(symbol, "filled")
            return

        side = "buy" if signal.action == SignalAction.BUY else "sell"
        ticket = mt5_ticket
        if ticket is None and self.entry_mode == EntryMode.GHOST:
            ticket = self.resolve_ghost_entry_ticket(symbol)

        if ticket is None:
            result = self.mt5.place_market_with_sltp(
                symbol,
                side,
                self.default_lot,
                sl,
                tp,
                comment=f"bx_e|{signal.pattern[:20]}",
            )
            if not result.ok:
                self.repo.log_event(
                    "entry_failed",
                    f"{symbol}: {result.message}",
                    level="error",
                )
                return
            ticket = result.ticket
        elif not self.dry_run:
            self.mt5.modify_position_sltp(int(ticket), sl, tp)

        self.repo.invalidate_pending(symbol, "filled")
        entry_price = float(signal.price)
        pos = self.mt5.position_for_ghost(symbol)
        if pos is not None:
            entry_price = pos.price_open
            ticket = pos.ticket

        self.repo.open_live_trade(
            symbol=symbol,
            side=side,
            pattern=signal.pattern,
            entry_price=entry_price,
            stop_loss=sl,
            take_profit=tp,
            margin=margin,
            rr_used=rr_used,
            mt5_ticket=ticket,
            entry_time=str(signal.timestamp),
            expected_win_usd=expected_win,
            expected_loss_usd=expected_loss,
        )
        self.repo.log_event(
            "trade_opened",
            f"{symbol} {side} ticket={ticket}",
            {"sl": sl, "tp": tp, "rr": rr_used},
        )
