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
    load_ltf_universe,
    load_universe,
    refresh_ltf_bars,
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
        self.ltf_by_symbol: dict[str, dict[str, list[Candle]]] = {}
        self.master_symbol: str = cfg.master_yahoo
        self._live_started_at: pd.Timestamp | None = None
        self._stop = threading.Event()
        self._runtime: dict[str, Any] = {"status": "init"}
        self._strategy: Any = None
        self._last_session_status_hour: str | None = None

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
            execution_interval=self.cfg.interval,
            ltf_intervals=self.cfg.ltf_intervals,
            ltf_confirm_mode=self.cfg.ltf_confirm_mode,
        )
        self._strategy = strategy
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
        self._attach_ltf(strategy, symbols)
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
            # H1-close mode: ghosts wait in DB; cancel any old MT5 limits.
            n_cancel = router.cancel_leftover_broker_pendings()
            if n_cancel:
                logger.info(
                    "Cancelled %d leftover MT5 pending(s); using H1-close market entry",
                    n_cancel,
                )
            self.engine = LiveEngine(strategy, self.cfg, repo, router, spec.entry_mode)
            session.commit()

        self._live_started_at = pd.Timestamp.now(tz="UTC")
        self._runtime.update(
            {
                "status": "running",
                "strategy": self.cfg.strategy,
                "entry_mode": spec.entry_mode.value,
                "ghost_entry": "h1_close_market",
                "same_bar_exit": self.cfg.same_bar_exit,
                "master": self.master_symbol,
                "symbols": symbols,
                "mt5_connected": self.mt5.connected,
                "live_started_at": str(self._live_started_at),
            }
        )
        logger.info(
            "Live service started | %s | entry_mode=%s | ghost=H1-close-market | "
            "same_bar_exit=%s | master=%s",
            self.cfg.strategy,
            spec.entry_mode.value,
            "on" if self.cfg.same_bar_exit else "off",
            self.master_symbol,
        )
        self._log_trading_session_status(reason="startup")

    def _strategy_session_filter(self) -> str:
        """Ablation session pill (alexg5revised+); default all if absent."""
        strat = self._strategy
        if strat is None:
            return "unknown"
        abl = getattr(strat, "ablation", None)
        if abl is None:
            return "all"
        return str(getattr(abl, "session", "all") or "all").lower()

    def _log_trading_session_status(self, *, reason: str = "hourly") -> None:
        """Log whether new setups are allowed under the strategy session filter."""
        from datetime import datetime, timezone

        from borex.alexg.sessions import TradingSession, in_session

        now = datetime.now(timezone.utc)
        filt = self._strategy_session_filter()
        window_hints = {
            "asia": "00:00-09:00 UTC",
            "london": "07:00-16:00 UTC",
            "newyork": "12:00-21:00 UTC",
            "overlap": "12:00-16:00 UTC (London–NY)",
            "all": "no restriction",
        }
        hint = window_hints.get(filt, "")
        if filt == "all":
            active = True
            detail = "IN session — filter=all (new setups anytime)"
        elif filt == "unknown":
            active = True
            detail = "session filter unknown (assuming open)"
        else:
            active = in_session(now, TradingSession(filt))
            detail = (
                "IN session — new setups allowed"
                if active
                else "OUT of session — new setups blocked (queued ghosts may still fill)"
            )
        logger.info(
            "Trading session [%s]: filter=%s%s | now=%s UTC | %s",
            reason,
            filt,
            f" ({hint})" if hint else "",
            now.strftime("%Y-%m-%d %H:%M"),
            detail,
        )
        self._runtime["session_filter"] = filt
        self._runtime["session_window"] = hint
        self._runtime["in_trading_session"] = active
        self._last_session_status_hour = now.strftime("%Y-%m-%d %H")

    def stop(self) -> None:
        self._stop.set()
        self.mt5.disconnect()
        self._runtime["status"] = "stopped"

    def _attach_ltf(self, strategy: Any, symbols: list[str]) -> None:
        """Warm up and attach lower-TF series when the strategy supports it (alexg8)."""
        if not hasattr(strategy, "attach_ltf"):
            self.ltf_by_symbol = {}
            return
        intervals = tuple(
            getattr(strategy, "ltf_intervals", None) or self.cfg.ltf_intervals or ("1m",)
        )
        self.ltf_by_symbol = load_ltf_universe(
            symbols,
            intervals,
            mt5=self.mt5,
            warmup_bars=self.cfg.ltf_warmup_bars,
        )
        strategy.attach_ltf(self.ltf_by_symbol)
        self._runtime["ltf_intervals"] = list(intervals)
        self._runtime["ltf_symbols"] = len(self.ltf_by_symbol)
        logger.info(
            "Attached LTF for %s: %d symbols (%s)",
            self.cfg.strategy,
            len(self.ltf_by_symbol),
            ",".join(intervals),
        )

    def _refresh_ltf(self) -> None:
        strategy = getattr(self, "_strategy", None) or getattr(
            getattr(self, "engine", None), "strategy", None
        )
        if strategy is None or not hasattr(strategy, "attach_ltf"):
            return
        intervals = tuple(
            getattr(strategy, "ltf_intervals", None) or self.cfg.ltf_intervals or ("1m",)
        )
        added = refresh_ltf_bars(
            self.ltf_by_symbol,
            list(self.candles_by_symbol.keys()),
            intervals,
            mt5=self.mt5,
            keep=self.cfg.ltf_warmup_bars,
        )
        if added:
            strategy.attach_ltf(self.ltf_by_symbol)
            logger.debug("Refreshed %d LTF bars", added)

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

        self._refresh_ltf()

        # Reconcile broker positions; do not arm resting ghost limits.
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
            router.cancel_leftover_broker_pendings()
            # If a position somehow exists for a DB ghost (manual / old limit), book it.
            margin = self.engine._margin_for_entry()
            ghost_fills = router.sync_ghost_fills(
                margin=margin,
                rr_used=self.cfg.min_rr * self.cfg.rr_factor,
                expected_win=margin * self.cfg.min_rr * self.cfg.rr_factor,
                expected_loss=margin,
            )
            for _ in ghost_fills:
                repo.set_cash(repo.get_portfolio(self.cfg.capital).cash - margin)
            router.reconcile_open_with_mt5()
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
        snap["account"] = self._account_live_snapshot(
            db_cash=float(snap.get("cash") or 0.0),
            open_trades=open_merged,
            closed_trades=snap.get("closed_trades") or [],
        )
        return snap

    def _account_live_snapshot(
        self,
        *,
        db_cash: float,
        open_trades: list[dict[str, Any]],
        closed_trades: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Prefer MT5 balance/equity; explain DB cash drift when they diverge."""
        out: dict[str, Any] = {
            "db_cash": float(db_cash),
            "mt5_balance": None,
            "mt5_equity": None,
            "mt5_margin": None,
            "mt5_free_margin": None,
            "mt5_floating": None,
            "mt5_login": None,
            "source": "db",
            "display_equity": float(db_cash),
            "mismatch": False,
            "delta_db_minus_mt5": None,
            "diagnosis": [],
        }
        if not (self.mt5.connected and not self.mt5.dry_run):
            out["diagnosis"] = ["MT5 not connected — showing DB paper cash only."]
            return out

        try:
            info = self.mt5._mt5.account_info() if self.mt5._mt5 is not None else None
        except Exception:
            info = None
        if info is None:
            try:
                bal = float(self.mt5.account_balance())
                eq = float(self.mt5.account_equity())
            except Exception:
                out["diagnosis"] = ["Could not read MT5 account_info."]
                return out
            out["mt5_balance"] = bal
            out["mt5_equity"] = eq
            out["mt5_free_margin"] = float(self.mt5.account_free_margin())
        else:
            out["mt5_balance"] = float(getattr(info, "balance", 0.0) or 0.0)
            out["mt5_equity"] = float(getattr(info, "equity", 0.0) or out["mt5_balance"])
            out["mt5_margin"] = float(getattr(info, "margin", 0.0) or 0.0)
            out["mt5_free_margin"] = float(getattr(info, "margin_free", 0.0) or 0.0)
            out["mt5_floating"] = float(getattr(info, "profit", 0.0) or 0.0)
            out["mt5_login"] = int(getattr(info, "login", 0) or 0)

        eq = float(out["mt5_equity"] or 0.0)
        bal = float(out["mt5_balance"] or 0.0)
        out["source"] = "mt5"
        out["display_equity"] = eq if eq > 0 else bal
        delta = float(db_cash) - eq
        out["delta_db_minus_mt5"] = delta
        out["mismatch"] = abs(delta) > max(1.0, 0.002 * max(eq, 1.0))  # >$1 or >0.2%

        if not out["mismatch"]:
            out["diagnosis"] = ["DB cash matches MT5 equity within tolerance."]
            return out

        reasons: list[str] = [
            f"DB cash ${db_cash:.2f} ≠ MT5 equity ${eq:.2f} (Δ ${delta:+.2f})."
        ]
        closed_pnl = sum(float(t.get("pnl") or 0.0) for t in closed_trades)
        zero_broker = [
            t for t in closed_trades
            if str(t.get("exit_reason") or "") == "mt5_closed" and abs(float(t.get("pnl") or 0.0)) < 1e-9
        ]
        open_margin = sum(float(t.get("margin") or 0.0) for t in open_trades if not t.get("stale"))
        reasons.append(f"DB closed PnL sum ${closed_pnl:+.2f} across {len(closed_trades)} trades.")
        if zero_broker:
            reasons.append(
                f"{len(zero_broker)} broker closes booked at pnl=0 (old reconcile) — "
                "DB never applied real MT5 deal PnL."
            )
        if open_margin > 0:
            reasons.append(f"DB still locks ${open_margin:.2f} margin on open/stale rows.")
        # Expected rough reconstruct: initial + closed pnl - open margin
        initial = float(self.cfg.capital)
        expected_db = initial + closed_pnl - open_margin
        reasons.append(
            f"DB reconstruct ≈ initial ${initial:.2f} + closed ${closed_pnl:+.2f} "
            f"- open margin ${open_margin:.2f} = ${expected_db:.2f}."
        )
        reasons.append(
            "UI uses MT5 equity for risk/display; DB cash is paper bookkeeping only."
        )
        out["diagnosis"] = reasons
        return out

    def markets_payload(self) -> dict[str, Any]:
        """
        Viewer-style all-markets snapshot.

        Ghost rows include a *display-only* what-if: entry assumed at ghost SL,
        with true-SL protective levels (does not change live entry logic).
        Open rows include expected PnL if SL or TP is hit.
        """
        from borex.backtest.margin_stops import margin_stop_out_prices, resolve_rr
        from borex.backtest.portfolio import PositionSide

        dash = self.dashboard_payload()
        account = dash.get("account") or {}
        wr = dash.get("win_rate")
        rr = resolve_rr(
            rr_mode="dynamic",
            fixed_rr=self.cfg.min_rr,
            winrate=wr,
            rr_factor=self.cfg.rr_factor,
        )
        live_equity = float(account.get("display_equity") or 0.0)
        if live_equity <= 0:
            live_equity = float(dash.get("cash") or self.cfg.capital)
        risk_usd = live_equity * self.cfg.position_size_pct

        ghosts_out: list[dict[str, Any]] = []
        for g in dash.get("pending_ghosts") or []:
            side_raw = str(g.get("action") or "buy").lower()
            side = PositionSide.LONG if side_raw in ("buy", "long") else PositionSide.SHORT
            ghost_sl = float(g.get("stop_loss") or 0.0)
            hyp_entry = ghost_sl  # display-only: assume fill at ghost trigger
            hyp_sl, hyp_tp = (None, None)
            if hyp_entry > 0:
                hyp_sl, hyp_tp = margin_stop_out_prices(
                    hyp_entry, side, self.cfg.leverage, rr
                )

            last = None
            series = self.candles_by_symbol.get(g["symbol"]) or []
            if series:
                last = float(series[-1].close)
            dist = None
            dist_pct = None
            if last is not None and ghost_sl > 0:
                dist = abs(last - ghost_sl)
                dist_pct = dist / last * 100.0

            ghosts_out.append(
                {
                    "symbol": g["symbol"],
                    "side": "buy" if side == PositionSide.LONG else "sell",
                    "pattern": g.get("pattern") or "",
                    "ghost_sl": ghost_sl,
                    "planned_entry": g.get("planned_entry"),
                    "last_price": last,
                    "dist_to_sl": dist,
                    "dist_to_sl_pct": dist_pct,
                    # Display-only what-if (entry = ghost SL)
                    "hyp_entry": hyp_entry,
                    "hyp_sl": hyp_sl,
                    "hyp_tp": hyp_tp,
                    "rr": rr,
                    "risk_usd": risk_usd,
                    "expected_loss_usd": risk_usd,
                    "expected_win_usd": risk_usd * rr,
                    "expires_index": g.get("expires_index"),
                    "status": g.get("status"),
                }
            )

        opens_out: list[dict[str, Any]] = []
        for t in dash.get("open_trades") or []:
            exp_loss = t.get("expected_loss_usd")
            exp_win = t.get("expected_win_usd")
            if exp_loss is None:
                exp_loss = t.get("margin") or risk_usd
            if exp_win is None:
                rr_used = float(t.get("rr_used") or rr or 0.0)
                exp_win = float(exp_loss) * rr_used
            opens_out.append(
                {
                    "symbol": t.get("symbol"),
                    "side": t.get("side"),
                    "entry_price": t.get("entry_price"),
                    "stop_loss": t.get("stop_loss"),
                    "take_profit": t.get("take_profit"),
                    "volume": t.get("volume"),
                    "floating_pnl": t.get("floating_pnl"),
                    "mt5_ticket": t.get("mt5_ticket"),
                    "margin": t.get("margin"),
                    "rr_used": t.get("rr_used"),
                    "pattern": t.get("pattern") or "",
                    "stale": bool(t.get("stale")),
                    "pnl_if_sl": -abs(float(exp_loss or 0.0)),
                    "pnl_if_tp": float(exp_win or 0.0),
                    "source": t.get("source"),
                }
            )

        ghosts_out.sort(key=lambda x: (x.get("dist_to_sl_pct") is None, x.get("dist_to_sl_pct") or 9e9))
        opens_out.sort(key=lambda x: str(x.get("symbol") or ""))

        return {
            "runtime": dash.get("runtime"),
            "entry_mode": dash.get("entry_mode"),
            "cash": dash.get("cash"),
            "account": account,
            "win_rate": wr,
            "rr": rr,
            "leverage": self.cfg.leverage,
            "risk_usd": risk_usd,
            "position_size_pct": self.cfg.position_size_pct,
            "note": (
                "Ghost hyp_entry/hyp_sl/hyp_tp assume fill at ghost SL for display only; "
                "live still market-enters on H1-close confirmation. "
                "Header equity/risk use MT5 when connected."
            ),
            "ghosts": ghosts_out,
            "open_positions": opens_out,
            "closed_trades": dash.get("closed_trades") or [],
        }

    def run_loop(self, poll_seconds: int = 30) -> None:
        self.start()
        try:
            while not self._stop.is_set():
                try:
                    now = pd.Timestamp.now(tz="UTC")
                    hour_key = now.strftime("%Y-%m-%d %H")
                    if hour_key != self._last_session_status_hour:
                        self._log_trading_session_status(reason="hourly")
                    self.process_once()
                except Exception as exc:
                    logger.exception("live loop error")
                    # Only recycle DB pool on connection/timeout style failures
                    msg = str(exc).lower()
                    if any(
                        k in msg
                        for k in (
                            "timeout",
                            "ssl connection",
                            "connection refused",
                            "server closed",
                            "terminating connection",
                            "could not connect",
                            "operationalerror",
                        )
                    ):
                        try:
                            bind = getattr(self.session_factory, "bind", None) or self.session_factory.kw.get(
                                "bind"
                            )
                            if bind is not None:
                                bind.dispose()
                                logger.warning("Disposed DB pool after connection error; will retry next poll")
                        except Exception:
                            logger.exception("failed to dispose DB engine after loop error")
                self._stop.wait(poll_seconds)
        finally:
            self.stop()

    def seconds_to_next_h1_close(self) -> float:
        now = pd.Timestamp.now(tz="UTC")
        next_close = now.ceil("h")
        return max(1.0, (next_close - now).total_seconds())
