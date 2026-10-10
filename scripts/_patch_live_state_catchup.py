#!/usr/bin/env python3
"""Patch borex_live for 1-month pretend-fill state catch-up on :8791."""

from __future__ import annotations

from pathlib import Path

LIVE = Path(r"C:\Users\azeva\OneDrive\Documentos\work\trading\borex_live")


def must_replace(text: str, old: str, new: str, label: str, *, optional: bool = False) -> str:
    if old not in text:
        if optional:
            print("skip", label)
            return text
        raise SystemExit(f"MISSING anchor: {label}")
    print("apply", label)
    return text.replace(old, new, 1)


def patch_router() -> None:
    p = LIVE / "borex_live" / "execution" / "router.py"
    t = p.read_text(encoding="utf-8")
    t = must_replace(
        t,
        """@dataclass
class ExecutionRouter:
    entry_mode: EntryMode
    mt5: Mt5Client
    repo: StateRepository
    default_lot: float = 0.01
    dry_run: bool = False
    leverage: float = 5000.0
""",
        """@dataclass
class ExecutionRouter:
    entry_mode: EntryMode
    mt5: Mt5Client
    repo: StateRepository
    default_lot: float = 0.01
    dry_run: bool = False
    leverage: float = 5000.0
    # False = pretend broker fills in DB (same open/close path, no terminal orders).
    execute_mt5: bool = True
""",
        "router.dataclass", optional=True,
    )
    t = must_replace(
        t,
        """    def force_close_position(self, symbol: str, ticket: int = 0) -> None:
        \"\"\"Market-close a live MT5 position (session-end flatten).\"\"\"
        if self.dry_run:
            return
""",
        """    def force_close_position(self, symbol: str, ticket: int = 0) -> None:
        \"\"\"Market-close a live MT5 position (session-end flatten).\"\"\"
        if self.dry_run or not self.execute_mt5:
            return
""",
        "router.force_close", optional=True,
    )
    t = must_replace(
        t,
        """    def reconcile_open_with_mt5(self) -> list[str]:
        \"\"\"Close DB opens that no longer exist on MT5 (broker SL/TP/manual).\"\"\"
        if self.dry_run or not self.mt5.connected:
            return []
        closed: list[str] = []
        live = [p for p in self.mt5.open_positions() if self.mt5.owns_position(p)]
        live_tickets = {int(p.ticket) for p in live}
        live_syms = {mt5_to_yahoo(p.symbol) for p in live}

        for trade in list(self.repo.open_trades()):
            ticket = int(trade.mt5_ticket) if trade.mt5_ticket else 0
            still_open = False
""",
        """    def reconcile_open_with_mt5(self) -> list[str]:
        \"\"\"Close DB opens that no longer exist on MT5 (broker SL/TP/manual).\"\"\"
        if self.dry_run or not self.mt5.connected or not self.execute_mt5:
            return []
        closed: list[str] = []
        live = [p for p in self.mt5.open_positions() if self.mt5.owns_position(p)]
        live_tickets = {int(p.ticket) for p in live}
        live_syms = {mt5_to_yahoo(p.symbol) for p in live}

        for trade in list(self.repo.open_trades()):
            ticket = int(trade.mt5_ticket) if trade.mt5_ticket else 0
            # Pretend/catch-up fills (ticket<=0) are managed by bar exits, not MT5.
            if ticket <= 0:
                continue
            still_open = False
""",
        "router.reconcile",
    )
    t = must_replace(
        t,
        """        side = "buy" if signal.action == SignalAction.BUY else "sell"
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
""",
        """        side = "buy" if signal.action == SignalAction.BUY else "sell"
        if self.dry_run or not self.execute_mt5:
            self.repo.log_event(
                "immediate_order_pretend",
                f"{symbol} {side}",
                {"ok": True, "ticket": -2, "execute_mt5": False},
            )
            return -2
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
""",
        "router.immediate",
    )
    t = must_replace(
        t,
        """    def handle_entry_fill(
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
        \"\"\"Market entry on closed-bar ghost fill (or immediate signal).\"\"\"
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
""",
        """    def handle_entry_fill(
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
        entry_price: float | None = None,
    ) -> None:
        \"\"\"Market entry on closed-bar ghost fill (or immediate signal).\"\"\"
        if any(t.symbol == symbol for t in self.repo.open_trades()):
            self.repo.invalidate_pending(symbol, "filled")
            return

        side = "buy" if signal.action == SignalAction.BUY else "sell"
        ticket = mt5_ticket
        fill_price = float(entry_price) if entry_price is not None else float(signal.price)

        # Same open path; execute_mt5 gates only the terminal send.
        if ticket is None and (self.dry_run or not self.execute_mt5):
            ticket = -2  # pretend broker fill (kept as a live_trades row)
            self.repo.log_event(
                "trade_opened_pretend",
                f"{symbol} {side} pretend-broker fill @ {fill_price}",
                {"sl": sl, "tp": tp, "risk": margin, "execute_mt5": False},
            )
            logger.info(
                "Pretend broker fill %s %s @ %.5f risk=%.2f (execute_mt5=off)",
                symbol,
                side,
                fill_price,
                margin,
            )
        elif ticket is None:
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
        elif self.execute_mt5 and not self.dry_run and int(ticket) > 0:
            self.mt5.modify_position_sltp(int(ticket), sl, tp)

        self.repo.invalidate_pending(symbol, "filled")
        entry_price = fill_price
""",
        "router.handle_entry_fill",
    )
    t = must_replace(
        t,
        "            if ticket is not None and not self.dry_run:\n                modified = self.mt5.modify_position_sltp(\n",
        "            if ticket is not None and int(ticket) > 0 and self.execute_mt5 and not self.dry_run:\n                modified = self.mt5.modify_position_sltp(\n",
        "router.modify_after_fill",
    )
    p.write_text(t, encoding="utf-8")
    print("patched", p)


def patch_live_engine() -> None:
    p = LIVE / "borex_live" / "engine" / "live_engine.py"
    t = p.read_text(encoding="utf-8")
    t = must_replace(
        t,
        """    def _margin_for_entry(self) -> float:
        \"\"\"Posted margin so SL price loss (+ commission) ≈ balance × position_size_pct.

        Prefer MT5 balance over equity so open floating PnL does not change risk.
        \"\"\"
        bal = 0.0
        mt5 = getattr(getattr(self, "router", None), "mt5", None)
        if mt5 is not None and getattr(mt5, "connected", False) and not getattr(mt5, "dry_run", True):
            try:
                bal = float(mt5.account_balance() or 0.0)
                if bal <= 0:
                    bal = float(mt5.account_equity() or 0.0)
            except Exception:
                bal = 0.0
        if bal <= 0:
            bal = self._cash()
""",
        """    def _mt5_available_cash(self) -> float:
        \"\"\"Broker free margin, minus pretend-open locks not visible to MT5.\"\"\"
        mt5 = getattr(getattr(self, "router", None), "mt5", None)
        free = 0.0
        if mt5 is not None and getattr(mt5, "connected", False) and not getattr(mt5, "dry_run", True):
            try:
                free = float(mt5.account_free_margin() or 0.0)
            except Exception:
                free = 0.0
            if free <= 0:
                try:
                    free = float(mt5.account_balance() or 0.0)
                except Exception:
                    free = 0.0
        if free <= 0:
            return self._cash()
        pretend_locked = 0.0
        for trade in self.repo.open_trades():
            ticket = int(trade.mt5_ticket) if trade.mt5_ticket else 0
            if ticket <= 0:
                pretend_locked += float(trade.margin or 0.0)
        return max(0.0, free - pretend_locked)

    def _margin_for_entry(self) -> float:
        \"\"\"Posted margin so SL price loss (+ commission) ≈ balance × position_size_pct.

        Prefer MT5 balance over equity so open floating PnL does not change risk.
        \"\"\"
        bal = 0.0
        mt5 = getattr(getattr(self, "router", None), "mt5", None)
        if mt5 is not None and getattr(mt5, "connected", False) and not getattr(mt5, "dry_run", True):
            try:
                bal = float(mt5.account_balance() or 0.0)
                if bal <= 0:
                    bal = float(mt5.account_equity() or 0.0)
            except Exception:
                bal = 0.0
        if bal <= 0:
            bal = self._cash()
""",
        "engine.margin_helpers",
    )
    t = must_replace(
        t,
        """    def step_master_bar(
        self,
        master_index: int,
        candles_by_symbol: dict[str, list[Candle]],
        master_symbol: str,
        *,
        allow_broker_orders: bool = True,
        sync_pending: bool = True,
    ) -> LiveStepResult:
""",
        """    def step_master_bar(
        self,
        master_index: int,
        candles_by_symbol: dict[str, list[Candle]],
        master_symbol: str,
        *,
        allow_broker_orders: bool = True,
        sync_pending: bool = True,
        paper_fills: bool = False,
    ) -> LiveStepResult:
""",
        "engine.step_sig",
    )
    t = must_replace(
        t,
        """            # same_bar_exit=False: do not evaluate SL/TP on the entry bar
            # (entry is booked after that H1 bar has already closed).
            if not allow_broker_orders:
                continue
""",
        """            # same_bar_exit=False: do not evaluate SL/TP on the entry bar
            # (entry is booked after that H1 bar has already closed).
            # paper_fills: same exit path as live, without MT5 (pretend broker).
            if not allow_broker_orders and not paper_fills:
                continue
""",
        "engine.exit_gate",
    )
    t = must_replace(
        t,
        """        candidates.sort(key=lambda x: x[1].score, reverse=True)
        fired: list[tuple[str, Signal]] = []
        if not allow_broker_orders:
            return LiveStepResult(master_index=master_index, signals=fired, exits=exits)
        open_count = len(self.repo.open_trades())
""",
        """        candidates.sort(key=lambda x: x[1].score, reverse=True)
        fired: list[tuple[str, Signal]] = []
        if not allow_broker_orders and not paper_fills:
            return LiveStepResult(master_index=master_index, signals=fired, exits=exits)
        open_count = len(self.repo.open_trades())
""",
        "engine.entry_gate",
    )
    t = must_replace(
        t,
        """        sl, tp, rr = self._compute_sltp(signal, exec_price, side)
        margin = self._margin_for_entry()
        expected_loss = margin
        expected_win = margin * rr

        if self.entry_mode == EntryMode.IMMEDIATE:
            before = {t.symbol for t in self.repo.open_trades()}
            ticket = self.router.handle_immediate_signal(
                symbol,
                signal,
                sl,
                tp,
                margin=margin,
                rr_used=rr,
            )
            self.router.handle_entry_fill(
                symbol,
                signal,
                sl,
                tp,
                margin=margin,
                rr_used=rr,
                expected_win=expected_win,
                expected_loss=expected_loss,
                mt5_ticket=ticket,
            )
            after = {t.symbol for t in self.repo.open_trades()}
            self._deduct_entry_cash(symbol, before, after, margin)
            return

        # GHOST fill on closed H1: market enter with true_sl protective stops
        # (matches backtest true_sl + same_bar_exit=False entry timing).
        before = {t.symbol for t in self.repo.open_trades()}
        self.router.handle_entry_fill(
            symbol,
            signal,
            sl,
            tp,
            margin=margin,
            rr_used=rr,
            expected_win=expected_win,
            expected_loss=expected_loss,
        )
        after = {t.symbol for t in self.repo.open_trades()}
        self._deduct_entry_cash(symbol, before, after, margin)
""",
        """        sl, tp, rr = self._compute_sltp(signal, exec_price, side)
        margin = self._margin_for_entry()
        expected_loss = margin
        expected_win = margin * rr

        # Always consult MT5 available cash (minus pretend locks) before opening.
        available = self._mt5_available_cash()
        if margin > available * 0.95:
            self.repo.log_event(
                "entry_skipped_cash",
                f"{symbol}: margin {margin:.2f} > available {available:.2f}",
                {"margin": margin, "available": available},
            )
            logger.info(
                "Skip open %s: need margin=%.2f available_cash=%.2f",
                symbol,
                margin,
                available,
            )
            return

        if self.entry_mode == EntryMode.IMMEDIATE:
            before = {t.symbol for t in self.repo.open_trades()}
            ticket = self.router.handle_immediate_signal(
                symbol,
                signal,
                sl,
                tp,
                margin=margin,
                rr_used=rr,
            )
            self.router.handle_entry_fill(
                symbol,
                signal,
                sl,
                tp,
                margin=margin,
                rr_used=rr,
                expected_win=expected_win,
                expected_loss=expected_loss,
                mt5_ticket=ticket,
                entry_price=float(exec_price),
            )
            after = {t.symbol for t in self.repo.open_trades()}
            self._deduct_entry_cash(symbol, before, after, margin)
            return

        # GHOST fill on closed H1: market enter with true_sl protective stops
        # (matches backtest true_sl + same_bar_exit=False entry timing).
        before = {t.symbol for t in self.repo.open_trades()}
        self.router.handle_entry_fill(
            symbol,
            signal,
            sl,
            tp,
            margin=margin,
            rr_used=rr,
            expected_win=expected_win,
            expected_loss=expected_loss,
            entry_price=float(exec_price),
        )
        after = {t.symbol for t in self.repo.open_trades()}
        self._deduct_entry_cash(symbol, before, after, margin)
""",
        "engine.process_entry",
    )
    p.write_text(t, encoding="utf-8")
    print("patched", p)


def patch_service() -> None:
    p = LIVE / "borex_live" / "service.py"
    t = p.read_text(encoding="utf-8")

    # Extend loaded history when state catch-up is enabled.
    t = must_replace(
        t,
        """        if self.cfg.symbols:
            symbols = list(self.cfg.symbols)
        elif self.mt5.connected and not self.mt5.dry_run:
            symbols = self.mt5.list_forex_yahoo_symbols()
            logger.info("Using all MT5 Forex pairs (%d)", len(symbols))
        else:
            symbols = default_forex_universe()
        if self.cfg.master_yahoo not in symbols:
            symbols = [self.cfg.master_yahoo] + list(symbols)

        self.candles_by_symbol = load_universe(symbols, self.cfg, self.mt5)
""",
        """        if self.cfg.symbols:
            symbols = list(self.cfg.symbols)
        elif self.mt5.connected and not self.mt5.dry_run:
            symbols = self.mt5.list_forex_yahoo_symbols()
            logger.info("Using all MT5 Forex pairs (%d)", len(symbols))
        else:
            symbols = default_forex_universe()
        if self.cfg.master_yahoo not in symbols:
            symbols = [self.cfg.master_yahoo] + list(symbols)

        # Warmup depth ends ~state_catchup_days ago; load that plus the catch-up window.
        catchup_days = int(getattr(self.cfg, "state_catchup_days", 0) or 0)
        if catchup_days > 0:
            extra = int(catchup_days) * 24 + 48
            self.cfg.warmup_bars = int(self.cfg.warmup_bars) + extra
            logger.info(
                "State catch-up enabled: +%d H1 bars (days=%d) → load_bars=%d",
                extra,
                catchup_days,
                self.cfg.warmup_bars,
            )

        self.candles_by_symbol = load_universe(symbols, self.cfg, self.mt5)
""",
        "service.load_extend",
    )

    # Router construction sites need execute_mt5=self.cfg.execute_mt5
    # Replace ExecutionRouter( blocks carefully - add execute_mt5 kwarg via helper pattern.
    if "execute_mt5=self.cfg.execute_mt5" not in t and "execute_mt5=getattr" not in t:
        t = t.replace(
            "leverage=self.cfg.leverage,\n            )",
            "leverage=self.cfg.leverage,\n"
            "                execute_mt5=bool(getattr(self.cfg, \"execute_mt5\", True)),\n"
            "            )",
        )
        # The above may hit multiple - count
        if t.count("execute_mt5=bool(getattr(self.cfg, \"execute_mt5\", True)),") < 1:
            raise SystemExit("failed to inject execute_mt5 into ExecutionRouter calls")

    t = must_replace(
        t,
        """        self._log_trading_session_status(reason="startup")
        self._replay_recent_history()
        self._runtime["status"] = "running"
        logger.info("Startup replay complete; live polling is active")
""",
        """        self._log_trading_session_status(reason="startup")
        if int(getattr(self.cfg, "state_catchup_days", 0) or 0) > 0:
            self._state_catchup_month()
        else:
            self._replay_recent_history()
        # After catch-up / ghost replay, real MT5 execution is enabled for new bars.
        self.cfg.execute_mt5 = True
        self._runtime["execute_mt5"] = True
        self._runtime["status"] = "running"
        logger.info("Startup replay complete; live polling is active (execute_mt5=on)")
""",
        "service.startup_branch",
    )

    # Insert _state_catchup_month before _replay_recent_history
    catchup_fn = '''
    def _state_catchup_month(self) -> None:
        """Warm indicators to T-N, then pretend-fill the last N days via the live path.

        Same LiveEngine.step_master_bar / ExecutionRouter.handle_entry_fill code as
        live; only execute_mt5=False so fills are booked as broker-like DB trades.
        """
        if not getattr(self, "engine", None):
            return
        days = int(getattr(self.cfg, "state_catchup_days", 0) or 0)
        if days <= 0:
            return
        master = self.candles_by_symbol.get(self.master_symbol) or []
        min_bars = self.strategy_min_bars()
        if len(master) <= min_bars:
            logger.warning("State catch-up skipped: not enough master bars")
            return

        tip_ts = pd.Timestamp(master[-1].timestamp)
        if tip_ts.tzinfo is None:
            tip_ts = tip_ts.tz_localize("UTC")
        else:
            tip_ts = tip_ts.tz_convert("UTC")
        start_ts = tip_ts - pd.Timedelta(days=int(days))

        start_i = min_bars
        for i, c in enumerate(master):
            ts = pd.Timestamp(c.timestamp)
            if ts.tzinfo is None:
                ts = ts.tz_localize("UTC")
            else:
                ts = ts.tz_convert("UTC")
            if ts >= start_ts:
                start_i = max(min_bars, i)
                break

        n = len(master) - start_i
        self._runtime["status"] = "state_catchup"
        self._runtime["execute_mt5"] = False
        self.cfg.execute_mt5 = False
        logger.info(
            "State catch-up: pretend-fill %d H1 bars [%s → %s] execute_mt5=off "
            "(warmup history before this is indicator-only)",
            n,
            master[start_i].timestamp,
            master[-1].timestamp,
        )

        opens = 0
        closes = 0
        with self._session() as session:
            repo = StateRepository(session)
            router = ExecutionRouter(
                entry_mode=self.engine.entry_mode,
                mt5=self.mt5,
                repo=repo,
                default_lot=self.cfg.default_lot,
                dry_run=False,
                leverage=self.cfg.leverage,
                execute_mt5=False,
            )
            self.engine.repo = repo
            self.engine.router = router
            # Mark catch-up candles as live history so dashboard shows the path.
            for mi in range(start_i, len(master)):
                for sym, series in self.candles_by_symbol.items():
                    if mi >= len(series):
                        continue
                    c = series[mi]
                    # Align by timestamp when lengths differ.
                    # Prefer index when series shares master timeline length.
                result = self.engine.step_master_bar(
                    mi,
                    self.candles_by_symbol,
                    self.master_symbol,
                    allow_broker_orders=True,
                    sync_pending=True,
                    paper_fills=True,
                )
                opens += len(result.signals)
                closes += len(result.exits)
                if (mi - start_i) % 120 == 0:
                    logger.info(
                        "State catch-up progress %d/%d opens=%d closes=%d cash=%.2f",
                        mi - start_i,
                        n,
                        opens,
                        closes,
                        float(repo.get_portfolio(self.cfg.capital).cash),
                    )
                    session.commit()
            # Persist catch-up master bars into live_candles for UI history.
            for mi in range(start_i, len(master)):
                for sym, series in self.candles_by_symbol.items():
                    if not series:
                        continue
                    # Find bar at master timestamp when possible
                    mts = str(master[mi].timestamp)
                    candle = None
                    if mi < len(series) and str(series[mi].timestamp) == mts:
                        candle = series[mi]
                    else:
                        for c in series:
                            if str(c.timestamp) == mts:
                                candle = c
                                break
                    if candle is None:
                        continue
                    repo.record_live_candle(
                        symbol=sym,
                        ts=str(candle.timestamp),
                        open_=candle.open,
                        high=candle.high,
                        low=candle.low,
                        close=candle.close,
                        volume=candle.volume,
                        interval=self.cfg.interval,
                    )
                    live_series = self.live_candles_by_symbol.setdefault(sym, [])
                    if not live_series or str(live_series[-1].timestamp) != str(candle.timestamp):
                        live_series.append(candle)
            session.commit()
            snap = repo.dashboard_snapshot()

        self._runtime["state_catchup"] = {
            "days": days,
            "bars": n,
            "start": str(master[start_i].timestamp),
            "end": str(master[-1].timestamp),
            "signals": opens,
            "exits": closes,
            "cash": snap.get("cash"),
            "open_trades": len(snap.get("open_trades") or []),
            "closed_trades": len(snap.get("closed_trades") or []),
        }
        logger.info(
            "State catch-up done | bars=%d signals=%d exits=%d cash=%.2f open=%d closed=%d",
            n,
            opens,
            closes,
            float(snap.get("cash") or 0.0),
            len(snap.get("open_trades") or []),
            len(snap.get("closed_trades") or []),
        )

'''
    t = must_replace(
        t,
        "    def _replay_recent_history(self) -> None:\n",
        catchup_fn + "    def _replay_recent_history(self) -> None:\n",
        "service.insert_catchup",
    )

    # Also pass execute_mt5 in _replay_recent_history router (legacy path)
    t = must_replace(
        t,
        """            router = ExecutionRouter(
                entry_mode=self.engine.entry_mode,
                mt5=self.mt5,
                repo=repo,
                default_lot=self.cfg.default_lot,
                dry_run=True,
                leverage=self.cfg.leverage,
            )
            self.engine.repo = repo
            self.engine.router = router
            for mi in range(start_i, len(master)):
                self.engine.step_master_bar(
                    mi,
                    self.candles_by_symbol,
                    self.master_symbol,
                    allow_broker_orders=False,
                    sync_pending=False,
                )
""",
        """            router = ExecutionRouter(
                entry_mode=self.engine.entry_mode,
                mt5=self.mt5,
                repo=repo,
                default_lot=self.cfg.default_lot,
                dry_run=True,
                leverage=self.cfg.leverage,
                execute_mt5=False,
            )
            self.engine.repo = repo
            self.engine.router = router
            for mi in range(start_i, len(master)):
                self.engine.step_master_bar(
                    mi,
                    self.candles_by_symbol,
                    self.master_symbol,
                    allow_broker_orders=False,
                    sync_pending=False,
                    paper_fills=False,
                )
""",
        "service.legacy_replay_router",
    )

    p.write_text(t, encoding="utf-8")
    print("patched", p)


def patch_mt5service() -> None:
    p = LIVE / "mt5service.py"
    t = p.read_text(encoding="utf-8")
    t = must_replace(
        t,
        """    p.add_argument(
        "--catchup-bars",
        type=int,
        default=500,
        help="Bars fetched each poll to recover from laptop sleep (default 500)",
    )
""",
        """    p.add_argument(
        "--catchup-bars",
        type=int,
        default=500,
        help="Bars fetched each poll to recover from laptop sleep (default 500)",
    )
    p.add_argument(
        "--state-catchup-days",
        type=int,
        default=0,
        help="Pretend-fill last N days via live path (execute_mt5=off), then enable MT5",
    )
    p.add_argument(
        "--execute-mt5",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Send orders to MT5 (default on). Catch-up forces off until cutover.",
    )
""",
        "cli.catchup_days",
    )
    t = must_replace(
        t,
        """        warmup_bars=args.warmup_bars,
        catchup_bars=args.catchup_bars,
        database_url=db,
""",
        """        warmup_bars=args.warmup_bars,
        catchup_bars=args.catchup_bars,
        state_catchup_days=int(args.state_catchup_days),
        execute_mt5=bool(args.execute_mt5),
        database_url=db,
""",
        "cli.build_config",
    )
    p.write_text(t, encoding="utf-8")
    print("patched", p)


def patch_start_script() -> None:
    p = LIVE / "scripts" / "start_alexg10_postfix_live_8791.py"
    t = p.read_text(encoding="utf-8")
    t = must_replace(
        t,
        '''        "--warmup-bars",
        "10000",
        "--position-size",
''',
        '''        "--warmup-bars",
        "10000",
        "--state-catchup-days",
        "30",
        "--position-size",
''',
        "start.state_catchup",
    )
    # Update manifest label
    t = t.replace(
        '"label": "ALEXG10_POSTFIX_AFIX_FULL_LIVE_8791"',
        '"label": "ALEXG10_POSTFIX_AFIX_FULL_LIVE_8791_STATE_CATCHUP_30D"',
    )
    p.write_text(t, encoding="utf-8")
    print("patched", p)


def main() -> int:
    patch_router()
    patch_live_engine()
    patch_service()
    patch_mt5service()
    patch_start_script()
    print("ALL_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
