#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path

LIVE = Path(r"C:\Users\azeva\OneDrive\Documentos\work\trading\borex_live")


def must_replace(text: str, old: str, new: str, label: str, *, optional: bool = False) -> str:
    if old not in text:
        if optional:
            print("skip", label)
            return text
        raise SystemExit(f"MISSING: {label}")
    print("apply", label)
    return text.replace(old, new, 1)


CATCHUP_FN = r'''
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
            "State catch-up: pretend-fill %d H1 bars [%s -> %s] execute_mt5=off "
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
            for mi in range(start_i, len(master)):
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
            for mi in range(start_i, len(master)):
                mts = str(master[mi].timestamp)
                for sym, series in self.candles_by_symbol.items():
                    if not series:
                        continue
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


def patch_service() -> None:
    p = LIVE / "borex_live" / "service.py"
    t = p.read_text(encoding="utf-8")

    t = must_replace(
        t,
        """        if self.cfg.master_yahoo not in symbols:
            symbols = [self.cfg.master_yahoo] + list(symbols)

        self.candles_by_symbol = load_universe(symbols, self.cfg, self.mt5)
""",
        """        if self.cfg.master_yahoo not in symbols:
            symbols = [self.cfg.master_yahoo] + list(symbols)

        # Warmup depth ends ~state_catchup_days ago; load that plus the catch-up window.
        catchup_days = int(getattr(self.cfg, "state_catchup_days", 0) or 0)
        if catchup_days > 0:
            extra = int(catchup_days) * 24 + 48
            self.cfg.warmup_bars = int(self.cfg.warmup_bars) + extra
            logger.info(
                "State catch-up enabled: +%d H1 bars (days=%d) -> load_bars=%d",
                extra,
                catchup_days,
                self.cfg.warmup_bars,
            )

        self.candles_by_symbol = load_universe(symbols, self.cfg, self.mt5)
""",
        "service.load_extend",
        optional=True,
    )

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
        optional=True,
    )

    t = must_replace(
        t,
        "    def _replay_recent_history(self) -> None:\n",
        CATCHUP_FN + "    def _replay_recent_history(self) -> None:\n",
        "service.insert_catchup",
        optional=True,
    )

    # Inject execute_mt5 into every ExecutionRouter construction that lacks it.
    needle = "leverage=self.cfg.leverage,\n            )"
    repl = (
        "leverage=self.cfg.leverage,\n"
        "                execute_mt5=bool(getattr(self.cfg, \"execute_mt5\", True)),\n"
        "            )"
    )
    count = t.count(needle)
    if count:
        t = t.replace(needle, repl)
        print(f"injected execute_mt5 into {count} ExecutionRouter call(s)")
    else:
        print("no bare ExecutionRouter leverage endings left (maybe already injected)")

    # Legacy replay: force execute_mt5=False + paper_fills=False
    t = must_replace(
        t,
        """                dry_run=True,
                leverage=self.cfg.leverage,
                execute_mt5=bool(getattr(self.cfg, "execute_mt5", True)),
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
        """                dry_run=True,
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
        "service.legacy_replay",
        optional=True,
    )
    # If inject didn't happen yet, try original form
    t = must_replace(
        t,
        """                dry_run=True,
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
        """                dry_run=True,
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
        "service.legacy_replay_orig",
        optional=True,
    )

    p.write_text(t, encoding="utf-8")
    print("wrote", p)


def patch_cli_and_start() -> None:
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
        "cli.args",
        optional=True,
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
        "cli.build",
        optional=True,
    )
    p.write_text(t, encoding="utf-8")
    print("wrote", p)

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
        "start.catchup",
        optional=True,
    )
    t = t.replace(
        '"label": "ALEXG10_POSTFIX_AFIX_FULL_LIVE_8791"',
        '"label": "ALEXG10_POSTFIX_AFIX_FULL_LIVE_8791_STATE_CATCHUP_30D"',
    )
    p.write_text(t, encoding="utf-8")
    print("wrote", p)


def main() -> int:
    patch_service()
    patch_cli_and_start()
    # sanity
    svc = (LIVE / "borex_live" / "service.py").read_text(encoding="utf-8")
    assert "_state_catchup_month" in svc
    assert "state_catchup_days" in svc
    eng = (LIVE / "borex_live" / "engine" / "live_engine.py").read_text(encoding="utf-8")
    assert "_mt5_available_cash" in eng
    assert "paper_fills" in eng
    rtr = (LIVE / "borex_live" / "execution" / "router.py").read_text(encoding="utf-8")
    assert "execute_mt5: bool" in rtr
    assert "trade_opened_pretend" in rtr
    print("SANITY_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
