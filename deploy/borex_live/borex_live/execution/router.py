from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from borex.alexg.ghost_entry import PendingSetup
from borex.backtest.margin_stops import margin_stop_out_prices
from borex.backtest.portfolio import PositionSide
from borex.models.candle import Signal, SignalAction

from borex_live.entry_mode import EntryMode
from borex_live.mt5.client import Mt5Client, dollar_exit_reason
from borex_live.mt5.symbols import mt5_to_yahoo
from borex_live.store.repository import GhostSnapshot, StateRepository

logger = logging.getLogger(__name__)


def read_pending_snapshot(strategy: Any) -> dict[str, GhostSnapshot]:
    pending = getattr(strategy, "_pending", None)
    if not isinstance(pending, dict):
        return {}
    out: dict[str, GhostSnapshot] = {}
    for symbol, p in pending.items():
        if not isinstance(p, PendingSetup):
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
        strategy._pending[g.symbol] = PendingSetup(
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
    leverage: float = 5000.0

    def sync_ghost_pending_orders(
        self,
        before: dict[str, GhostSnapshot],
        after: dict[str, GhostSnapshot],
    ) -> None:
        """
        Keep DB ghosts in sync with strategy._pending.

        Does NOT place MT5 buy/sell limits — fills are decided on closed H1
        bars (same as backtest), then executed as market orders.
        """
        if self.entry_mode != EntryMode.GHOST:
            return

        for symbol, ghost in after.items():
            if symbol in before:
                # Refresh stored levels / expiry while ghost is waiting
                self.repo.upsert_pending_ghost(ghost, mt5_ticket=None)
                continue
            self._queue_ghost_db_only(symbol, ghost)

        for symbol in before:
            if symbol not in after:
                self._clear_ghost_db(symbol, "strategy_removed")

    def _clear_ghost_db(self, symbol: str, reason: str) -> None:
        rows = [g for g in self.repo.list_pending_ghosts() if g.symbol == symbol]
        for row in rows:
            if row.mt5_ticket:
                self.mt5.cancel_order(int(row.mt5_ticket))
            self.repo.invalidate_pending(symbol, reason)
            logger.info("Cleared ghost %s (%s)", symbol, reason)

    def ensure_broker_pendings(self) -> int:
        """Legacy no-op: H1-close mode does not arm resting MT5 limits."""
        return 0

    def cancel_leftover_broker_pendings(self) -> int:
        """
        Cancel any old borex magic pendings left from the previous limit mode.
        Keeps DB ghosts waiting for H1-close market entry.
        """
        if self.entry_mode != EntryMode.GHOST:
            return 0
        cancelled = 0
        for row in self.repo.list_pending_ghosts():
            ticket = int(row.mt5_ticket) if row.mt5_ticket else 0
            if ticket > 0 and self.mt5.pending_order_exists(ticket):
                self.mt5.cancel_order(ticket)
                cancelled += 1
                logger.info(
                    "Cancelled leftover MT5 pending %s ticket=%s (H1-close mode)",
                    row.symbol,
                    ticket,
                )
            # Clear ticket so we never treat a limit as the fill source
            if ticket:
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
                self.repo.upsert_pending_ghost(ghost, mt5_ticket=None)
        # Also sweep any magic-tagged pendings not in DB
        for o in self.mt5.pending_orders():
            if int(o.get("magic", 0) or 0) != self.mt5.MAGIC:
                continue
            self.mt5.cancel_order(int(o["ticket"]))
            cancelled += 1
            logger.info(
                "Cancelled orphan borex pending ticket=%s %s",
                o["ticket"],
                o.get("symbol"),
            )
        return cancelled

    def force_close_position(self, symbol: str, ticket: int = 0) -> None:
        """Market-close a live MT5 position (session-end flatten)."""
        if self.dry_run:
            return
        result = self.mt5.close_position(symbol, ticket=ticket or None)
        if not result.ok:
            self.repo.log_event(
                "force_close_failed",
                f"{symbol}: {result.message}",
                {"ticket": ticket, "retcode": result.retcode},
                level="error",
            )
            logger.error("Force close failed %s: %s", symbol, result.message)
        else:
            logger.info("Force closed %s ticket=%s", symbol, result.ticket)

    def _queue_ghost_db_only(self, symbol: str, ghost: GhostSnapshot) -> None:
        self.repo.upsert_pending_ghost(ghost, mt5_ticket=None)
        self.repo.log_event(
            "ghost_queued",
            f"{symbol} {ghost.action} wait SL={ghost.stop_loss} (no MT5 limit; H1 close)",
            {
                "planned_entry": ghost.planned_entry,
                "take_profit": ghost.take_profit,
                "expires_index": ghost.expires_index,
            },
        )
        logger.info(
            "Queued ghost %s %s @ SL %.5f (DB only — enter on closed H1 tag)",
            symbol,
            ghost.action,
            ghost.stop_loss,
        )

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
            side = "buy" if pos.side == "long" else "sell"
            d_sl, d_tp = self.mt5.stops_for_dollar_targets(
                row.symbol,
                side,
                float(pos.price_open),
                float(pos.volume),
                expected_loss,
                expected_win,
            )
            use_sl = d_sl if d_sl is not None else (pos.sl if pos.sl else sl)
            use_tp = d_tp if d_tp is not None else (pos.tp if pos.tp else tp)
            if use_sl and use_tp and not self.dry_run:
                self.mt5.modify_position_sltp(pos.ticket, use_sl, use_tp)
                refreshed = self.mt5.position_by_ticket(int(pos.ticket))
                if refreshed is not None:
                    if refreshed.sl:
                        use_sl = float(refreshed.sl)
                    if refreshed.tp:
                        use_tp = float(refreshed.tp)

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
        live = [p for p in self.mt5.open_positions() if self.mt5.owns_position(p)]
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
                pos = next(
                    (p for p in live if ticket and int(p.ticket) == ticket),
                    None,
                )
                if pos is None:
                    pos = next(
                        (p for p in live if mt5_to_yahoo(p.symbol) == trade.symbol),
                        None,
                    )
                if pos is not None:
                    if self._enforce_dollar_sltp(trade, pos):
                        closed.append(trade.symbol)
                continue
            deal_pnl = self.mt5.closed_deal_profit(ticket) if ticket else None
            if deal_pnl is None:
                # Fall back to intended 1% risk loss if we cannot read the deal.
                deal_pnl = -float(trade.expected_loss_usd or trade.margin or 0.0)
            self.repo.close_live_trade(
                int(trade.id),
                exit_price=float(trade.entry_price),
                exit_time="",
                exit_reason="mt5_closed",
                pnl=float(deal_pnl),
            )
            pf = self.repo.get_portfolio(0)
            self.repo.set_cash(float(pf.cash) + float(trade.margin) + float(deal_pnl))
            self.repo.log_event(
                "trade_closed_broker",
                f"{trade.symbol} ticket={ticket} pnl={deal_pnl:.2f}",
                {"pnl": deal_pnl},
            )
            logger.info(
                "Reconciled closed on MT5: %s ticket=%s pnl=%.2f",
                trade.symbol,
                ticket,
                deal_pnl,
            )
            closed.append(trade.symbol)
        return closed

    def _enforce_dollar_sltp(self, trade: Any, pos: Any) -> bool:
        """Close if broker PnL already matches intended $; else re-anchor SL/TP."""
        want_win = float(trade.expected_win_usd or 0.0)
        want_loss = float(trade.expected_loss_usd or trade.margin or 0.0)
        hit = dollar_exit_reason(float(pos.profit), want_win, want_loss)
        if hit:
            close_res = self.mt5.close_position(
                trade.symbol, ticket=int(trade.mt5_ticket or pos.ticket)
            )
            if not close_res.ok:
                logger.error(
                    "Dollar-close failed %s ticket=%s: %s",
                    trade.symbol,
                    trade.mt5_ticket,
                    close_res.message,
                )
                return False
            deal_pnl = self.mt5.closed_deal_profit(int(trade.mt5_ticket or pos.ticket))
            if deal_pnl is None:
                deal_pnl = float(pos.profit)
            self.repo.close_live_trade(
                int(trade.id),
                exit_price=float(pos.price_open),
                exit_time="",
                exit_reason=f"mt5:{hit}",
                pnl=float(deal_pnl),
            )
            pf = self.repo.get_portfolio(0)
            self.repo.set_cash(float(pf.cash) + float(trade.margin) + float(deal_pnl))
            self.repo.log_event(
                "trade_closed_dollar",
                f"{trade.symbol} ticket={trade.mt5_ticket} {hit} pnl={deal_pnl:.2f}",
                {"pnl": deal_pnl, "reason": hit},
            )
            logger.info(
                "Dollar-closed %s ticket=%s %s pnl=%.2f",
                trade.symbol,
                trade.mt5_ticket,
                hit,
                deal_pnl,
            )
            return True

        fill = float(pos.price_open or trade.entry_price or 0.0)
        volume = float(pos.volume or 0.0)
        sl, tp = self.mt5.stops_for_dollar_targets(
            trade.symbol, str(trade.side), fill, volume, want_loss, want_win
        )
        if sl is None or tp is None or self.dry_run:
            return False
        broker_sl = float(pos.sl or 0.0)
        broker_tp = float(pos.tp or 0.0)
        missing = broker_sl <= 0 or broker_tp <= 0
        tp_dist = abs(fill - tp)
        drifted = abs(broker_tp - tp) > max(tp_dist * 0.15, fill * 1e-5)
        if not (missing or drifted):
            return False
        ticket = int(trade.mt5_ticket or pos.ticket)
        mod = self.mt5.modify_position_sltp(ticket, float(sl), float(tp))
        if not mod.ok:
            logger.warning(
                "SL/TP retry failed %s ticket=%s: %s",
                trade.symbol,
                ticket,
                mod.message,
            )
            return False
        trade.stop_loss = sl
        trade.take_profit = tp
        self.repo.log_event(
            "sltp_reanchor",
            f"{trade.symbol} ticket={ticket} sl={sl} tp={tp}",
            {"sl": sl, "tp": tp},
        )
        return False

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
                "Placed MT5 order %s %s @ %.5f (ticket=%s, sl=%.5f tp=%.5f, %s)",
                symbol,
                side,
                ghost.stop_loss,
                ticket,
                prot_sl,
                prot_tp,
                result.message or "ok",
            )
        else:
            logger.error(
                "Failed MT5 order %s %s @ %.5f: %s (retcode=%s) prot_sl=%.5f prot_tp=%.5f",
                symbol,
                side,
                ghost.stop_loss,
                result.message,
                result.retcode,
                prot_sl,
                prot_tp,
            )

    def handle_immediate_signal(
        self,
        symbol: str,
        signal: Signal,
        sl: float,
        tp: float,
        *,
        margin: float,
        rr_used: float,
    ) -> int | None:
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
            risk_money=margin,
            rr=rr_used,
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
        """Market entry on closed-bar ghost fill (or immediate signal)."""
        if any(t.symbol == symbol for t in self.repo.open_trades()):
            self.repo.invalidate_pending(symbol, "filled")
            return

        side = "buy" if signal.action == SignalAction.BUY else "sell"
        ticket = mt5_ticket

        # H1-close ghost mode: always market-enter now (no resting limits).
        # Volume is sized so SL ≈ risk_money (1% of equity by default).
        if ticket is None:
            result = self.mt5.place_market_with_sltp(
                symbol,
                side,
                self.default_lot,
                sl,
                tp,
                comment=f"bx_h1|{signal.pattern[:18]}",
                risk_money=margin,
                rr=rr_used,
            )
            if not result.ok:
                self.repo.log_event(
                    "entry_failed",
                    f"{symbol}: {result.message}",
                    {"retcode": result.retcode, "sl": sl, "tp": tp, "risk": margin},
                    level="error",
                )
                logger.error(
                    "H1-close market entry failed %s %s: %s (retcode=%s)",
                    symbol,
                    side,
                    result.message,
                    result.retcode,
                )
                return
            ticket = result.ticket
        elif not self.dry_run:
            self.mt5.modify_position_sltp(int(ticket), sl, tp)

        self.repo.invalidate_pending(symbol, "filled")
        entry_price = float(signal.price)
        pos = self.mt5.position_for_ghost(symbol)
        volume = None
        if pos is not None:
            entry_price = pos.price_open
            ticket = pos.ticket
            volume = pos.volume
            want_sl, want_tp = self.mt5.stops_for_dollar_targets(
                symbol,
                side,
                float(entry_price),
                float(volume),
                expected_loss,
                expected_win,
            )
            if want_sl is None or want_tp is None:
                actual_side = PositionSide.LONG if side == "buy" else PositionSide.SHORT
                want_sl, want_tp = margin_stop_out_prices(
                    entry_price,
                    actual_side,
                    self.leverage,
                    rr_used,
                )
            want_sl = self.mt5.normalize_price(symbol, want_sl)
            want_tp = self.mt5.normalize_price(symbol, want_tp)
            if ticket is not None and not self.dry_run:
                modified = self.mt5.modify_position_sltp(
                    int(ticket), want_sl, want_tp
                )
                if modified.ok:
                    sl, tp = want_sl, want_tp
                else:
                    self.repo.log_event(
                        "sltp_reanchor_failed",
                        f"{symbol}: could not anchor SL/TP to actual fill",
                        {
                            "ticket": ticket,
                            "entry": entry_price,
                            "sl": want_sl,
                            "tp": want_tp,
                            "retcode": modified.retcode,
                        },
                        level="error",
                    )
            refreshed = self.mt5.position_by_ticket(int(ticket)) if ticket else None
            if refreshed is not None:
                if refreshed.sl:
                    sl = float(refreshed.sl)
                if refreshed.tp:
                    tp = float(refreshed.tp)

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
            f"{symbol} {side} ticket={ticket} (H1-close market)",
            {
                "sl": sl,
                "tp": tp,
                "rr": rr_used,
                "risk": margin,
                "volume": volume,
                "signal_price": float(signal.price),
            },
        )
        logger.info(
            "Opened %s %s @ %.5f ticket=%s vol=%s risk=%.2f (signal/ghost SL was %.5f)",
            symbol,
            side,
            entry_price,
            ticket,
            f"{volume:.2f}" if volume is not None else "?",
            margin,
            float(signal.price),
        )
