from __future__ import annotations

import logging
import threading
import time
from dataclasses import asdict
from typing import Any

import pandas as pd

from borex_live.config import LiveServiceConfig
from borex_live.paths import ensure_borex_main_on_path

ensure_borex_main_on_path()
from borex.alexg.multi_market import default_forex_universe  # noqa: E402
from borex.models.candle import Candle  # noqa: E402

from borex_live.config import LiveServiceConfig
from borex_live.data.feed import (
    _drop_forming_bar,
    _interval_seconds,
    bootstrap_candles,
    load_universe,
)
from borex_live.engine.live_engine import LiveEngine
from borex_live.entry_mode import EntryMode
from borex_live.execution.router import ExecutionRouter, restore_pending_to_strategy
from borex_live.mt5.client import Mt5Client
from borex_live.store.models import init_db
from borex_live.store.repository import GhostSnapshot, StateRepository
from borex_live.strategy_registry import create_strategy

logger = logging.getLogger(__name__)


class LiveService:
    def __init__(self, cfg: LiveServiceConfig) -> None:
        self.cfg = cfg
        self.session_factory = init_db(cfg.database_url)
        self.mt5 = Mt5Client(
            path=cfg.mt5_path,
            login=cfg.mt5_login,
            password=cfg.mt5_password,
            server=cfg.mt5_server,
            dry_run=cfg.dry_run,
        )
        self.candles_by_symbol: dict[str, list[Candle]] = {}
        self.live_candles_by_symbol: dict[str, list[Candle]] = {}
        self.master_symbol: str = cfg.master_yahoo
        self._live_started_at: pd.Timestamp | None = None
        self._stop = threading.Event()
        self._runtime: dict[str, Any] = {"status": "init"}

    @property
    def runtime(self) -> dict[str, Any]:
        return self._runtime

    def _session(self):
        return self.session_factory()

    def start(self) -> None:
        if self.cfg.borex_main_root:
            import os

            os.environ["BOREX_MAIN_ROOT"] = str(self.cfg.borex_main_root)

        login, password, server = self.cfg.mt5_credentials()
        self.mt5.login = login
        self.mt5.password = password
        self.mt5.server = server
        self.mt5.connect()

        strategy, spec = create_strategy(
            self.cfg.strategy,
            min_rr=self.cfg.min_rr,
            second_signal=self.cfg.second_signal,
        )
        if self.cfg.dry_run and not self.mt5.dry_run:
            self.mt5.dry_run = True

        if self.cfg.symbols:
            symbols = list(self.cfg.symbols)
        elif self.mt5.connected and not self.mt5.dry_run:
            symbols = self.mt5.list_forex_yahoo_symbols()
            logger.info("Using all MT5 Forex pairs (%d)", len(symbols))
        else:
            symbols = default_forex_universe()
        if self.cfg.master_yahoo not in symbols:
            symbols = [self.cfg.master_yahoo] + list(symbols)

        self.candles_by_symbol = load_universe(symbols, self.cfg, self.mt5)
        symbols = list(self.candles_by_symbol.keys())
        # Prefer configured master if it has bars; else densest series; else configured master.
        preferred = self.cfg.master_yahoo
        if preferred in self.candles_by_symbol and self.candles_by_symbol[preferred]:
            self.master_symbol = preferred
        else:
            ranked = sorted(
                self.candles_by_symbol.items(),
                key=lambda kv: len(kv[1]),
                reverse=True,
            )
            self.master_symbol = ranked[0][0] if ranked and ranked[0][1] else preferred
            if self.master_symbol != preferred:
                logger.warning(
                    "Master %s has no warmup bars; using %s (%d bars)",
                    preferred,
                    self.master_symbol,
                    len(self.candles_by_symbol.get(self.master_symbol, [])),
                )
        self.live_candles_by_symbol = {sym: [] for sym in symbols}

        with self._session() as session:
            repo = StateRepository(session)
            # Restore previously processed live MT5 candles (not warmup)
            for meta in repo.live_candle_symbols():
                rows = repo.live_candles_for_symbol(meta["symbol"])
                self.live_candles_by_symbol[meta["symbol"]] = [
                    Candle(
                        timestamp=r.ts,
                        open=r.open,
                        high=r.high,
                        low=r.low,
                        close=r.close,
                        volume=r.volume,
                    )
                    for r in rows
                ]
            repo.start_run(
                self.cfg.strategy,
                spec.entry_mode.value,
                asdict(self.cfg),
            )
            pf = repo.get_portfolio(self.cfg.capital)
            if pf.cash <= 0:
                repo.set_cash(self.cfg.capital)

            ghosts = [
                GhostSnapshot(
                    symbol=g.symbol,
                    action=g.action,
                    pattern=g.pattern,
                    stop_loss=g.stop_loss,
                    take_profit=g.take_profit,
                    planned_entry=g.planned_entry,
                    created_index=g.created_index,
                    expires_index=g.expires_index,
                    saw_near_sl=g.saw_near_sl,
                )
                for g in repo.list_pending_ghosts()
            ]
            restore_pending_to_strategy(strategy, ghosts)

            router = ExecutionRouter(
                entry_mode=spec.entry_mode,
                mt5=self.mt5,
                repo=repo,
                default_lot=self.cfg.default_lot,
                dry_run=self.mt5.dry_run,
            )
            # Restore ghosts into strategy, then make sure each has an MT5 pending
            # limit parked at the ghost SL so fills happen automatically.
            n_pending = router.ensure_broker_pendings()
            if n_pending:
                logger.info("Armed %d MT5 pending ghost orders at SL", n_pending)
            self.engine = LiveEngine(strategy, self.cfg, repo, router, spec.entry_mode)
            session.commit()

        self._live_started_at = pd.Timestamp.now(tz="UTC")
        self._runtime.update(
            {
                "status": "running",
                "strategy": self.cfg.strategy,
                "entry_mode": spec.entry_mode.value,
                "master": self.master_symbol,
                "symbols": symbols,
                "mt5_connected": self.mt5.connected,
                "live_started_at": str(self._live_started_at),
            }
        )
        logger.info(
            "Live service started | %s | entry_mode=%s | master=%s",
            self.cfg.strategy,
            spec.entry_mode.value,
            self.master_symbol,
        )

    def stop(self) -> None:
        self._stop.set()
        self.mt5.disconnect()
        self._runtime["status"] = "stopped"

    def _master_index(self) -> int:
        return len(self.candles_by_symbol[self.master_symbol]) - 1

    def process_once(self) -> dict[str, Any]:
        """Process latest closed bars (call every loop or on H1 close)."""
        master_candles = self.candles_by_symbol[self.master_symbol]
        if len(master_candles) < self.strategy_min_bars():
            return {"skipped": "warmup"}

        new_bars = 0
        new_live: list[tuple[str, Candle]] = []
        if self.mt5.dry_run:
            return {"skipped": "dry_run_no_live_bars"}

        # Always keep ghost SL limits on the broker and book early fills.
        ghost_fills: list[str] = []
        with self._session() as session:
            repo = StateRepository(session)
            router = ExecutionRouter(
                entry_mode=self.engine.entry_mode,
                mt5=self.mt5,
                repo=repo,
                default_lot=self.cfg.default_lot,
                dry_run=self.mt5.dry_run,
            )
            self.engine.repo = repo
            self.engine.router = router
            router.ensure_broker_pendings()
            margin = self.engine._margin_for_entry()
            ghost_fills = router.sync_ghost_fills(
                margin=margin,
                rr_used=self.cfg.min_rr * self.cfg.rr_factor,
                expected_win=margin * self.cfg.min_rr * self.cfg.rr_factor,
                expected_loss=margin,
            )
            for _ in ghost_fills:
                repo.set_cash(repo.get_portfolio(self.cfg.capital).cash - margin)
            session.commit()

        started = self._live_started_at or pd.Timestamp.now(tz="UTC")
        interval_sec = _interval_seconds(self.cfg.interval)

        for sym in self.candles_by_symbol:
            bars = self.mt5.fetch_bars(sym, self.cfg.interval, count=5)
            if not bars:
                continue
            closed_bars = _drop_forming_bar(bars, self.cfg.interval)
            if not closed_bars:
                continue

            series = self.candles_by_symbol[sym]
            live_series = self.live_candles_by_symbol.setdefault(sym, [])
            live_ts = {str(c.timestamp) for c in live_series}

            for closed in closed_bars:
                open_ts = pd.Timestamp(closed.timestamp)
                if open_ts.tzinfo is None:
                    open_ts = open_ts.tz_localize("UTC")
                else:
                    open_ts = open_ts.tz_convert("UTC")
                close_ts = open_ts + pd.Timedelta(seconds=interval_sec)

                # Live page: only bars that *closed* after this service start
                if close_ts <= started:
                    continue
                if str(closed.timestamp) in live_ts:
                    continue

                if not series or series[-1].timestamp < closed.timestamp:
                    series.append(closed)
                elif series[-1].timestamp == closed.timestamp:
                    series[-1] = closed  # refresh OHLC after finalize

                live_series.append(closed)
                live_ts.add(str(closed.timestamp))
                new_live.append((sym, closed))
                new_bars += 1
                logger.info(
                    "live bar %s %s (closed %s) O=%.5f C=%.5f",
                    sym,
                    closed.timestamp,
                    close_ts,
                    closed.open,
                    closed.close,
                )

        if new_bars == 0:
            out = {"skipped": "no_new_bar", "ghost_fills": len(ghost_fills)}
            if ghost_fills:
                self._runtime["last_tick"] = out
            return out

        master_i = self._master_index()
        with self._session() as session:
            repo = StateRepository(session)
            for sym, candle in new_live:
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
            router = ExecutionRouter(
                entry_mode=self.engine.entry_mode,
                mt5=self.mt5,
                repo=repo,
                default_lot=self.cfg.default_lot,
                dry_run=self.mt5.dry_run,
            )
            self.engine.repo = repo
            self.engine.router = router
            result = self.engine.step_master_bar(
                master_i,
                self.candles_by_symbol,
                self.master_symbol,
            )
            for sym in self.candles_by_symbol:
                c = self.candles_by_symbol[sym][-1]
                repo.save_bar_cursor(sym, c.timestamp, len(self.candles_by_symbol[sym]) - 1)
            session.commit()
            snap = repo.dashboard_snapshot()

        out = {
            "master_index": result.master_index,
            "signals": len(result.signals),
            "exits": len(result.exits),
            "new_live_bars": len(new_live),
            "ghost_fills": len(ghost_fills),
            "dashboard": snap,
        }
        self._runtime["last_tick"] = out
        self._runtime["live_candle_count"] = sum(
            len(v) for v in self.live_candles_by_symbol.values()
        )
        return out

    def live_candles_payload(self, symbol: str | None = None) -> dict[str, Any]:
        """API helper: only MT5 live-ingested candles (no warmup/backdata)."""
        symbols = list(self.live_candles_by_symbol.keys())
        if symbol:
            series = self.live_candles_by_symbol.get(symbol, [])
            return {
                "symbol": symbol,
                "interval": self.cfg.interval,
                "count": len(series),
                "candles": [
                    {
                        "time": int(pd.Timestamp(c.timestamp).timestamp()),
                        "ts": str(c.timestamp),
                        "open": c.open,
                        "high": c.high,
                        "low": c.low,
                        "close": c.close,
                        "volume": c.volume,
                    }
                    for c in series
                ],
            }
        return {
            "interval": self.cfg.interval,
            "symbols": [
                {
                    "symbol": sym,
                    "count": len(self.live_candles_by_symbol.get(sym, [])),
                    "last_ts": (
                        str(self.live_candles_by_symbol[sym][-1].timestamp)
                        if self.live_candles_by_symbol.get(sym)
                        else None
                    ),
                }
                for sym in symbols
            ],
            "total": sum(len(v) for v in self.live_candles_by_symbol.values()),
        }

    def strategy_min_bars(self) -> int:
        return int(getattr(self.engine.strategy, "min_bars", 80))

    def dashboard_payload(self) -> dict[str, Any]:
        """DB snapshot + live MT5 positions/pendings (source of truth for UI)."""
        from borex_live.execution.router import late_entry_stops
        from borex_live.mt5.symbols import mt5_to_yahoo
        from borex_live.store.repository import GhostSnapshot

        with self._session() as session:
            repo = StateRepository(session)
            # Reconcile filled/closed vs broker before rendering.
            if self.mt5.connected and not self.mt5.dry_run and getattr(self, "engine", None):
                router = ExecutionRouter(
                    entry_mode=self.engine.entry_mode,
                    mt5=self.mt5,
                    repo=repo,
                    default_lot=self.cfg.default_lot,
                    dry_run=self.mt5.dry_run,
                )
                margin = self.engine._margin_for_entry()
                fills = router.sync_ghost_fills(
                    margin=margin,
                    rr_used=self.cfg.min_rr * self.cfg.rr_factor,
                    expected_win=margin * self.cfg.min_rr * self.cfg.rr_factor,
                    expected_loss=margin,
                )
                for _ in fills:
                    repo.set_cash(repo.get_portfolio(self.cfg.capital).cash - margin)
                router.reconcile_open_with_mt5()
            snap = repo.dashboard_snapshot()
            session.commit()

        # Live broker state (always prefer MT5 for open display)
        mt5_rows: list[dict[str, Any]] = []
        pending_mt5: list[dict[str, Any]] = []
        if self.mt5.connected and not self.mt5.dry_run:
            for p in self.mt5.open_positions():
                if p.magic != self.mt5.MAGIC and "borex" not in (p.comment or "").lower():
                    # Still show account positions so the UI matches MT5 Trade
                    pass
                yahoo = mt5_to_yahoo(p.symbol)
                mt5_rows.append(
                    {
                        "symbol": yahoo,
                        "mt5_symbol": p.symbol,
                        "side": "buy" if p.side == "long" else "sell",
                        "entry_price": p.price_open,
                        "stop_loss": p.sl,
                        "take_profit": p.tp,
                        "volume": p.volume,
                        "floating_pnl": p.profit,
                        "mt5_ticket": p.ticket,
                        "magic": p.magic,
                        "comment": p.comment,
                        "source": "mt5",
                    }
                )
            for o in self.mt5.pending_orders():
                yahoo = mt5_to_yahoo(str(o["symbol"]))
                otype = int(o.get("type") or 0)
                side = "buy" if otype in (2, 4) else "sell"  # LIMIT/STOP buy vs sell
                pending_mt5.append(
                    {
                        "symbol": yahoo,
                        "mt5_symbol": o["symbol"],
                        "side": side,
                        "trigger_price": o["price"],
                        "stop_loss": o.get("sl"),
                        "take_profit": o.get("tp"),
                        "volume": o.get("volume"),
                        "mt5_ticket": o["ticket"],
                        "magic": o.get("magic"),
                        "source": "mt5",
                    }
                )

        db_by_sym = {t["symbol"]: t for t in snap.get("open_trades") or []}
        db_by_ticket = {
            int(t["mt5_ticket"]): t
            for t in (snap.get("open_trades") or [])
            if t.get("mt5_ticket")
        }

        open_merged: list[dict[str, Any]] = []
        seen_tickets: set[int] = set()
        for row in mt5_rows:
            ticket = int(row["mt5_ticket"])
            seen_tickets.add(ticket)
            db = db_by_ticket.get(ticket) or db_by_sym.get(row["symbol"]) or {}
            open_merged.append(
                {
                    **db,
                    **row,
                    "id": db.get("id"),
                    "expected_win_usd": db.get("expected_win_usd"),
                    "expected_loss_usd": db.get("expected_loss_usd"),
                    "margin": db.get("margin"),
                    "rr_used": db.get("rr_used"),
                    "pattern": db.get("pattern") or row.get("comment") or "",
                }
            )
        # DB orphans (closed on broker but still open in DB) — keep visible but flagged
        for t in snap.get("open_trades") or []:
            ticket = int(t["mt5_ticket"]) if t.get("mt5_ticket") else 0
            if ticket and ticket in seen_tickets:
                continue
            if any(r["symbol"] == t["symbol"] for r in open_merged):
                continue
            open_merged.append({**t, "floating_pnl": None, "stale": True, "source": "db_only"})

        # Enrich pending ghosts with protective levels + MT5 pending match
        enriched_ghosts = []
        mt5_by_ticket = {int(o["mt5_ticket"]): o for o in pending_mt5}
        for g in snap.get("pending_ghosts") or []:
            try:
                prot_sl, prot_tp = late_entry_stops(
                    GhostSnapshot(
                        symbol=g["symbol"],
                        action=g["action"],
                        pattern=g.get("pattern") or "",
                        stop_loss=g["stop_loss"],
                        take_profit=g["take_profit"],
                        planned_entry=g["planned_entry"],
                        created_index=0,
                        expires_index=g.get("expires_index") or 0,
                    )
                )
            except Exception:
                prot_sl, prot_tp = g.get("stop_loss"), g.get("take_profit")
            ticket = g.get("mt5_ticket")
            mt5 = mt5_by_ticket.get(int(ticket)) if ticket else None
            enriched_ghosts.append(
                {
                    **g,
                    "trigger_price": g["stop_loss"],
                    "protect_sl": prot_sl,
                    "protect_tp": prot_tp,
                    "mt5_live": mt5 is not None,
                    "mt5_pending": mt5,
                }
            )

        snap["open_trades"] = open_merged
        snap["mt5_positions"] = mt5_rows
        snap["pending_orders_mt5"] = pending_mt5
        snap["pending_ghosts"] = enriched_ghosts
        snap["runtime"] = self.runtime
        snap["entry_mode"] = self.runtime.get("entry_mode")
        return snap

    def run_loop(self, poll_seconds: int = 30) -> None:
        self.start()
        try:
            while not self._stop.is_set():
                try:
                    self.process_once()
                except Exception:
                    logger.exception("live loop error")
                    # Drop broken pooled DB connections (Railway idle kill, etc.)
                    try:
                        bind = getattr(self.session_factory, "bind", None) or self.session_factory.kw.get(
                            "bind"
                        )
                        if bind is not None:
                            bind.dispose()
                    except Exception:
                        logger.exception("failed to dispose DB engine after loop error")
                self._stop.wait(poll_seconds)
        finally:
            self.stop()

    def seconds_to_next_h1_close(self) -> float:
        now = pd.Timestamp.now(tz="UTC")
        next_close = now.ceil("h")
        return max(1.0, (next_close - now).total_seconds())
