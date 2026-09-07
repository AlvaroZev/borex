from __future__ import annotations

import logging
from typing import Any

from borex.backtest.costs import infer_pip_size
from borex.backtest.portfolio import PositionSide

from borex_live.mt5.client import dollar_exit_reason
from borex_mirror.config import MirrorConfig
from borex_mirror.repository import MirrorRepository

logger = logging.getLogger(__name__)


def _pip_distances(
    entry: float,
    stop_loss: float | None,
    take_profit: float | None,
    symbol: str,
) -> tuple[float, float, float, float]:
    """Return (sl_dist_price, tp_dist_price, sl_pips, tp_pips)."""
    pip = infer_pip_size(symbol)
    sl_dist = abs(float(entry) - float(stop_loss)) if stop_loss is not None else 0.0
    tp_dist = abs(float(entry) - float(take_profit)) if take_profit is not None else 0.0
    sl_pips = sl_dist / pip if pip > 0 else 0.0
    tp_pips = tp_dist / pip if pip > 0 else 0.0
    return sl_dist, tp_dist, sl_pips, tp_pips


def levels_from_fill(
    side: str,
    fill: float,
    sl_dist: float,
    tp_dist: float,
) -> tuple[float | None, float | None]:
    """Apply theory pip/price distances from the actual broker fill."""
    if fill <= 0:
        return None, None
    if side.lower() in ("buy", "long"):
        sl = fill - sl_dist if sl_dist > 0 else None
        tp = fill + tp_dist if tp_dist > 0 else None
    else:
        sl = fill + sl_dist if sl_dist > 0 else None
        tp = fill - tp_dist if tp_dist > 0 else None
    return sl, tp


class MirrorExecutor:
    """Mirror a theory open into MT5 (or paper) at candle close."""

    def __init__(self, cfg: MirrorConfig, mt5: Any) -> None:
        self.cfg = cfg
        self.mt5 = mt5
        if mt5 is not None:
            mt5.MAGIC = int(cfg.mt5_magic)
            mt5.COMMENT_PREFIXES = ("bx_m|",)

    def mirror_open(
        self,
        repo: MirrorRepository,
        theory_trade: Any,
        *,
        execute_mt5: bool,
    ) -> None:
        symbol = theory_trade.symbol
        side = (
            theory_trade.side.value
            if hasattr(theory_trade.side, "value")
            else str(theory_trade.side)
        )
        if side.lower() in ("long",):
            side = "buy"
        elif side.lower() in ("short",):
            side = "sell"
        entry_time = str(theory_trade.entry_time)
        pattern = str(theory_trade.pattern or "")
        if repo.find_mirror_trade(symbol, entry_time, pattern) is not None:
            return

        theory_entry = float(theory_trade.entry_price)
        sl_dist, tp_dist, sl_pips, tp_pips = _pip_distances(
            theory_entry,
            theory_trade.stop_loss,
            theory_trade.take_profit,
            symbol,
        )
        margin = float(theory_trade.margin)
        rr = float(theory_trade.score or self.cfg.min_rr)
        theory_row = repo.find_theory_trade(symbol, entry_time, pattern)

        paper = not execute_mt5 or self.cfg.dry_run or not getattr(self.mt5, "connected", False)
        fill = theory_entry
        ticket: int | None = None
        volume: float | None = None
        closed_now = ""
        sl, tp = levels_from_fill(side, fill, sl_dist, tp_dist)

        if not paper:
            result = self.mt5.place_market_with_sltp(
                symbol,
                side,
                self.cfg.position_size_pct,  # unused when risk_money set
                float(sl or 0.0),
                float(tp or 0.0),
                comment=f"bx_m|{pattern[:18]}",
                risk_money=margin,
                rr=rr,
            )
            if not result.ok:
                repo.log_event(
                    "mirror_open_failed",
                    f"{symbol} {side}: {result.message}",
                    level="error",
                )
                logger.error(
                    "Mirror open failed %s %s: %s (retcode=%s)",
                    symbol,
                    side,
                    result.message,
                    result.retcode,
                )
                # Still book a failed attempt? No — keep 1:1 by booking paper
                # at theory levels when broker rejects, so theory/real counts match.
                paper = True
                fill = theory_entry
                sl, tp = levels_from_fill(side, fill, sl_dist, tp_dist)
                ticket = None
                volume = None
                repo.log_event(
                    "mirror_open_paper_fallback",
                    f"{symbol}: broker reject → paper fill at theory entry",
                    level="warning",
                )
            else:
                ticket = result.ticket
                pos = self.mt5.position_for_ghost(symbol)
                if pos is not None:
                    fill = float(pos.price_open)
                    ticket = int(pos.ticket)
                    volume = float(pos.volume)
                    want_loss = margin
                    want_win = margin * rr
                    # Dollar SL/TP from the *filled* volume so broker PnL at
                    # those prices is the intended 1R / RR, even if fill slipped.
                    d_sl, d_tp = self.mt5.stops_for_dollar_targets(
                        symbol, side, fill, volume, want_loss, want_win
                    )
                    sl, tp = d_sl, d_tp
                    if sl is None or tp is None:
                        sl, tp = levels_from_fill(side, fill, sl_dist, tp_dist)
                    if sl is not None and tp is not None and ticket:
                        mod = self.mt5.modify_position_sltp(
                            int(ticket), float(sl), float(tp)
                        )
                        if not mod.ok:
                            repo.log_event(
                                "mirror_sltp_failed",
                                f"{symbol} ticket={ticket}: {mod.message}",
                                level="error",
                            )
                            logger.error(
                                "Mirror SL/TP modify failed %s ticket=%s: %s (retcode=%s)",
                                symbol,
                                ticket,
                                mod.message,
                                mod.retcode,
                            )
                    pos = self.mt5.position_by_ticket(int(ticket)) or pos
                    fill = float(pos.price_open)
                    volume = float(pos.volume)
                    # Persist what the broker actually has, not just what we asked.
                    if pos.sl:
                        sl = float(pos.sl)
                    if pos.tp:
                        tp = float(pos.tp)
                    pip = infer_pip_size(symbol)
                    if pip > 0:
                        sl_pips = abs(fill - float(sl or fill)) / pip
                        tp_pips = abs(float(tp or fill) - fill) / pip
                    hit = dollar_exit_reason(
                        float(pos.profit), want_win, want_loss
                    )
                    if hit:
                        close_res = self.mt5.close_position(
                            symbol, ticket=int(ticket)
                        )
                        if close_res.ok:
                            closed_now = hit
                            repo.log_event(
                                "mirror_dollar_close",
                                f"{symbol} ticket={ticket} {hit} float={pos.profit:.2f}",
                            )
                            logger.info(
                                "Mirror closed %s at fill because %s (float=%.2f want=%.2f/%.2f)",
                                symbol,
                                hit,
                                pos.profit,
                                want_win,
                                want_loss,
                            )
                        else:
                            logger.error(
                                "Mirror dollar-close failed %s: %s",
                                symbol,
                                close_res.message,
                            )
                else:
                    fill = theory_entry
                    sl, tp = levels_from_fill(side, fill, sl_dist, tp_dist)

        repo.open_mirror_trade(
            theory_trade_id=theory_row.id if theory_row else None,
            symbol=symbol,
            side=side,
            pattern=pattern,
            theory_entry=theory_entry,
            entry_price=fill,
            entry_time=entry_time,
            stop_loss=sl,
            take_profit=tp,
            sl_pips=sl_pips,
            tp_pips=tp_pips,
            margin=margin,
            rr_used=rr,
            mt5_ticket=ticket,
            volume=volume,
            status="open",
            expected_win_usd=margin * rr,
            expected_loss_usd=margin,
            paper=paper,
        )
        row = repo.find_mirror_trade(symbol, entry_time, pattern)
        if not paper:
            # Lock paper cash like live (for UI consistency)
            pf = repo.get_portfolio(self.cfg.capital)
            repo.set_cash(float(pf.cash) - margin)
        if closed_now and row is not None and ticket:
            deal = self.mt5.closed_deal_profit(int(ticket))
            pnl = float(deal) if deal is not None else float(margin * rr)
            repo.close_mirror_trade(
                int(row.id),
                exit_price=float(tp or fill),
                exit_time=entry_time,
                exit_reason=f"mt5:{closed_now}",
                pnl=pnl,
            )
            pf = repo.get_portfolio(self.cfg.capital)
            repo.set_cash(float(pf.cash) + float(margin) + pnl)
        logger.info(
            "Mirrored %s %s theory@%.5f fill@%.5f ticket=%s paper=%s sl_pips=%.1f tp_pips=%.1f",
            symbol,
            side,
            theory_entry,
            fill,
            ticket,
            paper,
            sl_pips,
            tp_pips,
        )

    def mirror_close(
        self,
        repo: MirrorRepository,
        theory_trade: Any,
        *,
        execute_mt5: bool,
    ) -> None:
        symbol = theory_trade.symbol
        entry_time = str(theory_trade.entry_time)
        pattern = str(theory_trade.pattern or "")
        row = repo.find_mirror_trade(symbol, entry_time, pattern)
        if row is None or row.status != "open":
            return

        exit_price = float(theory_trade.exit_price or row.entry_price)
        exit_time = str(theory_trade.exit_time or "")
        exit_reason = str(theory_trade.exit_reason or "theory_close")
        pnl = float(theory_trade.pnl or 0.0)

        # Prefer broker deal PnL when available
        if (
            execute_mt5
            and not row.paper
            and row.mt5_ticket
            and getattr(self.mt5, "connected", False)
            and not self.cfg.dry_run
        ):
            # Close if still open on broker (theory exited before SL/TP)
            pos = self.mt5.position_for_ghost(symbol)
            if pos is not None and int(pos.ticket) == int(row.mt5_ticket):
                close_res = self.mt5.close_position(symbol, ticket=int(row.mt5_ticket))
                if not close_res.ok:
                    logger.error(
                        "Mirror force-close failed %s: %s", symbol, close_res.message
                    )
            deal = self.mt5.closed_deal_profit(int(row.mt5_ticket))
            if deal is not None:
                pnl = float(deal)
                exit_reason = f"mt5:{exit_reason}"

        # Mark-to-market from fill if paper / no deal
        if row.paper or (execute_mt5 is False):
            side = PositionSide.LONG if row.side == "buy" else PositionSide.SHORT
            if row.entry_price > 0 and row.margin > 0:
                if side == PositionSide.LONG:
                    pct = (exit_price - row.entry_price) / row.entry_price
                else:
                    pct = (row.entry_price - exit_price) / row.entry_price
                pnl = max(-row.margin, row.margin * pct * self.cfg.leverage)
                # Prefer theory pnl for paper parity with theory book
                pnl = float(theory_trade.pnl or pnl)

        repo.close_mirror_trade(
            int(row.id),
            exit_price=exit_price,
            exit_time=exit_time,
            exit_reason=exit_reason,
            pnl=pnl,
        )
        pf = repo.get_portfolio(self.cfg.capital)
        if not row.paper:
            repo.set_cash(float(pf.cash) + float(row.margin) + pnl)
        logger.info(
            "Mirror closed %s %s pnl=%.2f reason=%s",
            symbol,
            row.side,
            pnl,
            exit_reason,
        )
