from __future__ import annotations

import logging
import threading
import time
from typing import Any

import pandas as pd

from borex_live.paths import ensure_borex_main_on_path

ensure_borex_main_on_path()
from borex.alexg.multi_market import default_forex_universe  # noqa: E402
from borex.models.candle import Candle  # noqa: E402

from borex_live.data.feed import load_universe
from borex_live.mt5.client import Mt5Client, dollar_exit_reason
from borex_mirror.config import MirrorConfig
from borex_mirror.engine import MirrorEngine
from borex_mirror.models import init_db
from borex_mirror.repository import MirrorRepository

logger = logging.getLogger(__name__)


class MirrorService:
    """Live or bulk theory→MT5 mirror with shared bar processor."""

    def __init__(self, cfg: MirrorConfig) -> None:
        self.cfg = cfg
        self.session_factory = init_db(cfg.database_url)
        self.mt5 = Mt5Client(
            path=cfg.mt5_path,
            login=cfg.mt5_login,
            password=cfg.mt5_password,
            server=cfg.mt5_server,
            dry_run=cfg.dry_run,
        )
        self.mt5.MAGIC = int(cfg.mt5_magic)
        self.mt5.COMMENT_PREFIXES = ("bx_m|",)
        self.candles_by_symbol: dict[str, list[Candle]] = {}
        self.master_symbol = cfg.master_yahoo
        self.engine: MirrorEngine | None = None
        self._stop = threading.Event()
        self._runtime: dict[str, Any] = {"status": "init", "mode": "mirror"}
        self._last_backup_at: float = 0.0
        self._backup_lock = threading.Lock()

    @property
    def runtime(self) -> dict[str, Any]:
        return self._runtime

    def _session(self):
        return self.session_factory()

    def _apply_startup_capital(self) -> None:
        explicit = float(self.cfg.capital or 0.0)
        if explicit > 0:
            self._runtime["capital"] = explicit
            self._runtime["capital_source"] = "cli"
            return
        fetched = 0.0
        source = "fallback"
        if self.mt5.connected and not self.mt5.dry_run:
            try:
                fetched = float(self.mt5.account_balance() or 0.0)
                if fetched > 0:
                    source = "mt5_balance"
            except Exception:
                logger.exception("MT5 balance read failed")
        if fetched <= 0:
            try:
                with self._session() as session:
                    pf = MirrorRepository(session).get_portfolio(0.0)
                    fetched = float(pf.initial_capital or pf.cash or 0.0)
                    if fetched > 0:
                        source = "db"
            except Exception:
                pass
        if fetched <= 0:
            fetched = 1000.0
            source = "fallback"
        self.cfg.capital = fetched
        self._runtime["capital"] = fetched
        self._runtime["capital_source"] = source
        logger.info("Mirror capital $%.2f from %s", fetched, source)

    def start(self, *, init_engine: bool = True) -> None:
        if self.cfg.borex_main_root:
            import os

            os.environ["BOREX_MAIN_ROOT"] = str(self.cfg.borex_main_root)

        login, password, server = self.cfg.mt5_credentials()
        self.mt5.login = login
        self.mt5.password = password
        self.mt5.server = server
        # Always attach for history when not a pure --dry-run (paper execute still needs MT5 bars).
        if not self.cfg.dry_run:
            self.mt5.connect()
        self._apply_startup_capital()

        live_cfg = self.cfg.to_live_cfg()
        if self.cfg.symbols:
            symbols = list(self.cfg.symbols)
        elif self.mt5.connected and not self.mt5.dry_run:
            symbols = self.mt5.list_forex_yahoo_symbols()
        else:
            symbols = default_forex_universe()
        if self.cfg.master_yahoo not in symbols:
            symbols = [self.cfg.master_yahoo] + list(symbols)

        self.candles_by_symbol = load_universe(symbols, live_cfg, self.mt5)
        symbols = list(self.candles_by_symbol.keys())
        preferred = self.cfg.master_yahoo
        if preferred in self.candles_by_symbol and self.candles_by_symbol[preferred]:
            self.master_symbol = preferred
        else:
            ranked = sorted(
                self.candles_by_symbol.items(), key=lambda kv: len(kv[1]), reverse=True
            )
            self.master_symbol = ranked[0][0] if ranked and ranked[0][1] else preferred

        self.engine = MirrorEngine(self.cfg, self.mt5)
        if not init_engine:
            self._runtime.update(
                {
                    "status": "loaded",
                    "strategy": self.cfg.strategy,
                    "mode": "mirror",
                    "entry_mode": "theory_close_market",
                    "master": self.master_symbol,
                    "symbols": symbols,
                    "mt5_connected": self.mt5.connected,
                    "execute_mt5": self.cfg.execute_mt5 and not self.cfg.dry_run,
                    "rr_mode": self.cfg.rr_mode,
                    "min_rr": self.cfg.min_rr,
                    "rr_factor": self.cfg.rr_factor,
                }
            )
            logger.info(
                "Mirror history loaded | %s | master=%s | pairs=%d | execute_mt5=%s | capital=$%.2f",
                self.cfg.strategy,
                self.master_symbol,
                len(symbols),
                self._runtime["execute_mt5"],
                self.cfg.capital,
            )
            return

        with self._session() as session:
            repo = MirrorRepository(session)
            pf = repo.get_portfolio(self.cfg.capital)
            pf.initial_capital = float(self.cfg.capital)
            if pf.cash <= 0:
                repo.set_cash(self.cfg.capital)
            info = self.engine.start(repo, self.candles_by_symbol, self.master_symbol)
            session.commit()

        self._runtime.update(
            {
                "status": "running",
                "strategy": self.cfg.strategy,
                "mode": "mirror",
                "entry_mode": "theory_close_market",
                "master": self.master_symbol,
                "symbols": symbols,
                "mt5_connected": self.mt5.connected,
                "execute_mt5": self.cfg.execute_mt5 and not self.cfg.dry_run,
                "theory_start": info,
                "rr_mode": self.cfg.rr_mode,
                "min_rr": self.cfg.min_rr,
                "rr_factor": self.cfg.rr_factor,
            }
        )
        logger.info(
            "Mirror service started | %s | master=%s | execute_mt5=%s | capital=$%.2f",
            self.cfg.strategy,
            self.master_symbol,
            self._runtime["execute_mt5"],
            self.cfg.capital,
        )
        if self.cfg.database_backup_url and self.cfg.backup_interval_seconds > 0:
            self._maybe_backup_to_railway(force=True)

    def _maybe_backup_to_railway(self, *, force: bool = False) -> None:
        if not self.cfg.database_backup_url or not self.cfg.database_url:
            return
        now = time.time()
        if (
            not force
            and now - self._last_backup_at < float(self.cfg.backup_interval_seconds)
        ):
            return
        if not self._backup_lock.acquire(blocking=False):
            return
        try:
            from borex_mirror.backup_sync import sync_local_to_backup

            counts = sync_local_to_backup(
                self.cfg.database_url, self.cfg.database_backup_url
            )
            self._last_backup_at = time.time()
            logger.info("Mirror Railway backup ok | %s", counts)
        except Exception:
            logger.exception("Mirror Railway backup failed (non-fatal)")
        finally:
            self._backup_lock.release()

    def stop(self) -> None:
        self._stop.set()
        try:
            self.mt5.disconnect()
        except Exception:
            pass

    def _ingest_new_bars(self) -> int:
        """Append newly closed MT5 bars (live feed). Returns count added."""
        if not self.mt5.connected:
            return 0
        added = 0
        need = max(2, int(self.cfg.catchup_bars))
        for sym in list(self.candles_by_symbol.keys()):
            try:
                self.mt5.ensure_symbol(sym)
                bars = self.mt5.fetch_bars(sym, self.cfg.interval, count=need + 1)
            except Exception:
                continue
            if not bars:
                continue
            # Drop forming bar
            from borex_live.data.feed import _drop_forming_bar

            bars = _drop_forming_bar(bars, self.cfg.interval)
            series = self.candles_by_symbol.setdefault(sym, [])
            known = {str(c.timestamp) for c in series[-500:]}
            for c in bars:
                if str(c.timestamp) not in known:
                    series.append(c)
                    known.add(str(c.timestamp))
                    added += 1
            # Keep memory bounded
            if len(series) > self.cfg.warmup_bars + 500:
                self.candles_by_symbol[sym] = series[-(self.cfg.warmup_bars) :]
        return added

    def process_once(self) -> dict[str, Any]:
        """Live tick: ingest closed bars → shared processor with MT5 execute."""
        if self.engine is None:
            return {"error": "not started"}
        self._ingest_new_bars()
        execute = bool(self.cfg.execute_mt5 and not self.cfg.dry_run)
        with self._session() as session:
            repo = MirrorRepository(session)
            try:
                stats = self.engine.process_available(
                    repo,
                    self.candles_by_symbol,
                    self.master_symbol,
                    execute_mt5=execute,
                )
                # Reconcile broker closes that theory hasn't exited yet
                self._reconcile_broker_closes(repo)
                session.commit()
            except Exception as exc:
                session.rollback()
                logger.exception("Mirror process_once failed")
                return {"error": str(exc)}
        self._runtime["last_tick"] = str(pd.Timestamp.now(tz="UTC"))
        self._runtime["last_stats"] = stats
        return {"ok": True, **stats, "execute_mt5": execute}

    def process_bulk(
        self,
        candles_by_symbol: dict[str, list[Candle]] | None = None,
        *,
        start_ts: str | None = None,
        execute_mt5: bool = False,
    ) -> dict[str, Any]:
        """
        Bulk / hour-by-hour offline feed through the SAME processor.

        execute_mt5=False → paper mirrors (parity / backtest).
        Walks every closed bar from ``start_ts`` (or bar 0) to the tip.
        """
        from borex_mirror.models import MirrorTrade

        if self.engine is None:
            raise RuntimeError("call start() first")
        data = candles_by_symbol or self.candles_by_symbol
        master = data[self.master_symbol]
        start_i = 0
        if start_ts:
            for i, c in enumerate(master):
                if str(c.timestamp) >= start_ts:
                    start_i = i
                    break

        logger.info(
            "Bulk replay %s → %s (%d bars) execute_mt5=%s",
            master[start_i].timestamp if master else "?",
            master[-1].timestamp if master else "?",
            max(0, len(master) - start_i),
            execute_mt5,
        )

        with self._session() as session:
            repo = MirrorRepository(session)
            repo.reset_theory()
            session.query(MirrorTrade).delete()
            session.flush()
            # Fresh engine; walk bars cold→warm (same path as live hour-by-hour).
            self.engine = MirrorEngine(self.cfg, self.mt5)
            self.engine.shadow.portfolio = self.engine.shadow._new_portfolio()
            self.engine.shadow.last_master_ts = (
                str(master[start_i - 1].timestamp) if start_i > 0 else ""
            )
            self.engine._sync_open_keys()
            stats = self.engine.process_range(
                repo,
                data,
                self.master_symbol,
                start_i,
                len(master),
                execute_mt5=execute_mt5,
            )
            session.commit()
        self._runtime["status"] = "running"
        self._runtime["theory_start"] = {
            "mode": "bulk",
            "bars": stats.get("bars"),
            "opened": stats.get("opened"),
            "closed": stats.get("closed"),
        }
        return stats

    def _reconcile_broker_closes(self, repo: MirrorRepository) -> None:
        if not (self.mt5.connected and not self.mt5.dry_run and self.cfg.execute_mt5):
            return
        for trade in list(repo.open_mirror_trades()):
            if trade.paper or not trade.mt5_ticket:
                continue
            pos = self.mt5.position_by_ticket(int(trade.mt5_ticket))
            if pos is not None:
                self._enforce_dollar_sltp(repo, trade, pos)
                continue
            # Position gone — broker hit SL/TP
            deal = self.mt5.closed_deal_profit(int(trade.mt5_ticket))
            pnl = float(deal) if deal is not None else -float(trade.expected_loss_usd or 0)
            repo.close_mirror_trade(
                int(trade.id),
                exit_price=float(trade.stop_loss or trade.entry_price),
                exit_time=str(pd.Timestamp.now(tz="UTC")),
                exit_reason="mt5_closed",
                pnl=pnl,
            )
            pf = repo.get_portfolio(self.cfg.capital)
            repo.set_cash(float(pf.cash) + float(trade.margin) + pnl)
            logger.info(
                "Reconciled mirror close %s ticket=%s pnl=%.2f",
                trade.symbol,
                trade.mt5_ticket,
                pnl,
            )

    def _enforce_dollar_sltp(self, repo: MirrorRepository, trade: Any, pos: Any) -> None:
        """Keep broker SL/TP on the fill-anchored $ targets; close if PnL already there."""
        want_win = float(trade.expected_win_usd or 0.0)
        want_loss = float(trade.expected_loss_usd or trade.margin or 0.0)
        fill = float(pos.price_open or trade.entry_price or 0.0)
        volume = float(pos.volume or trade.volume or 0.0)
        side = str(trade.side)

        hit = dollar_exit_reason(float(pos.profit), want_win, want_loss)
        if hit:
            close_res = self.mt5.close_position(trade.symbol, ticket=int(trade.mt5_ticket))
            if not close_res.ok:
                logger.error(
                    "Mirror dollar-close failed %s ticket=%s: %s",
                    trade.symbol,
                    trade.mt5_ticket,
                    close_res.message,
                )
                return
            deal = self.mt5.closed_deal_profit(int(trade.mt5_ticket))
            pnl = float(deal) if deal is not None else float(pos.profit)
            repo.close_mirror_trade(
                int(trade.id),
                exit_price=float(pos.price_open),
                exit_time=str(pd.Timestamp.now(tz="UTC")),
                exit_reason=f"mt5:{hit}",
                pnl=pnl,
            )
            pf = repo.get_portfolio(self.cfg.capital)
            repo.set_cash(float(pf.cash) + float(trade.margin) + pnl)
            repo.log_event(
                "mirror_dollar_close",
                f"{trade.symbol} ticket={trade.mt5_ticket} {hit} pnl={pnl:.2f}",
            )
            logger.info(
                "Dollar-closed mirror %s ticket=%s %s pnl=%.2f",
                trade.symbol,
                trade.mt5_ticket,
                hit,
                pnl,
            )
            return

        sl, tp = self.mt5.stops_for_dollar_targets(
            trade.symbol, side, fill, volume, want_loss, want_win
        )
        if sl is None or tp is None:
            return
        broker_sl = float(pos.sl or 0.0)
        broker_tp = float(pos.tp or 0.0)
        missing = broker_sl <= 0 or broker_tp <= 0
        tp_dist = abs(fill - tp)
        drifted = abs(broker_tp - tp) > max(tp_dist * 0.15, fill * 1e-5)
        if not (missing or drifted):
            return
        mod = self.mt5.modify_position_sltp(int(trade.mt5_ticket), float(sl), float(tp))
        if not mod.ok:
            logger.warning(
                "Mirror SL/TP retry failed %s ticket=%s: %s",
                trade.symbol,
                trade.mt5_ticket,
                mod.message,
            )
            return
        trade.stop_loss = sl
        trade.take_profit = tp
        repo.log_event(
            "mirror_sltp_reanchor",
            f"{trade.symbol} ticket={trade.mt5_ticket} sl={sl} tp={tp}",
        )

    def run_loop(self, poll_seconds: int = 30) -> None:
        while not self._stop.is_set():
            try:
                self.process_once()
                self._maybe_backup_to_railway()
            except Exception:
                logger.exception("Mirror loop tick failed")
            self._stop.wait(poll_seconds)

    def dashboard_payload(self) -> dict[str, Any]:
        with self._session() as session:
            repo = MirrorRepository(session)
            theory = repo.theory_snapshot()
            mirror = repo.mirror_snapshot()
            session.commit()

        # Floating PnL from MT5 when possible
        for t in mirror.get("open_trades") or []:
            t["floating_pnl"] = None
            if (
                self.mt5.connected
                and not self.mt5.dry_run
                and t.get("mt5_ticket")
                and not t.get("paper")
            ):
                pos = self.mt5.position_for_ghost(t["symbol"])
                if pos is not None:
                    t["floating_pnl"] = float(pos.profit)
            if t["floating_pnl"] is None:
                # Project from last close vs entry
                series = self.candles_by_symbol.get(t["symbol"]) or []
                if series and t.get("entry_price"):
                    last = float(series[-1].close)
                    entry = float(t["entry_price"])
                    margin = float(t.get("margin") or 0)
                    if entry > 0 and margin > 0:
                        if t.get("side") == "buy":
                            pct = (last - entry) / entry
                        else:
                            pct = (entry - last) / entry
                        t["floating_pnl"] = margin * pct * self.cfg.leverage

        for t in theory.get("open_trades") or []:
            series = self.candles_by_symbol.get(t["symbol"]) or []
            t["floating_pnl"] = None
            if series and t.get("entry_price") and t.get("margin"):
                last = float(series[-1].close)
                entry = float(t["entry_price"])
                margin = float(t["margin"])
                if entry > 0:
                    if str(t.get("side")).lower() in ("buy", "long"):
                        pct = (last - entry) / entry
                    else:
                        pct = (entry - last) / entry
                    t["floating_pnl"] = margin * pct * self.cfg.leverage

        comparison = self._comparison(
            mirror.get("open_trades") or [],
            mirror.get("closed_trades") or [],
            theory.get("open_trades") or [],
            theory.get("closed_trades") or [],
        )
        theory["comparison"] = comparison

        account = {
            "source": "mt5" if self.mt5.connected and not self.mt5.dry_run else "paper",
            "mt5_balance": None,
            "mt5_equity": None,
            "display_equity": mirror.get("cash"),
        }
        if self.mt5.connected and not self.mt5.dry_run:
            try:
                account["mt5_balance"] = float(self.mt5.account_balance())
                account["mt5_equity"] = float(self.mt5.account_equity())
                account["display_equity"] = account["mt5_equity"]
            except Exception:
                pass

        return {
            "mode": "mirror",
            "cash": mirror.get("cash"),
            "open_trades": mirror.get("open_trades"),
            "closed_trades": mirror.get("closed_trades"),
            "pending_ghosts": [],  # no ghost wait in mirror mode
            "theory": theory,
            "runtime": self.runtime,
            "entry_mode": "theory_close_market",
            "account": account,
            "win_rate": self._win_rate(mirror.get("closed_trades") or []),
        }

    @staticmethod
    def _win_rate(closed: list[dict]) -> float | None:
        if not closed:
            return None
        wins = sum(1 for t in closed if float(t.get("pnl") or 0) > 0)
        return wins / len(closed)

    @staticmethod
    def _comparison(
        live_open: list[dict],
        live_closed: list[dict],
        theory_open: list[dict],
        theory_closed: list[dict],
    ) -> list[dict]:
        def side(s: object) -> str:
            s = str(s or "").lower()
            if s in ("buy", "long"):
                return "buy"
            if s in ("sell", "short"):
                return "sell"
            return s

        def hour(ts: object) -> str:
            try:
                return str(pd.Timestamp(ts).floor("h"))
            except Exception:
                return str(ts)

        live = [*live_closed, *live_open]
        live_used: set[int] = set()
        rows: list[dict] = []
        for theoretical in [*theory_closed, *theory_open]:
            key = (
                theoretical.get("symbol"),
                side(theoretical.get("side")),
                hour(theoretical.get("entry_time")),
            )
            actual = None
            for i, L in enumerate(live):
                if i in live_used:
                    continue
                if (
                    L.get("symbol"),
                    side(L.get("side")),
                    hour(L.get("entry_time")),
                ) == key:
                    actual = L
                    live_used.add(i)
                    break
            rows.append(
                {
                    "status": "matched" if actual else "theory_only",
                    "symbol": theoretical.get("symbol"),
                    "side": side(theoretical.get("side")),
                    "entry_hour": key[2],
                    "theory": theoretical,
                    "live": actual,
                    "entry_delta": (
                        float(actual["entry_price"]) - float(theoretical["entry_price"])
                        if actual
                        else None
                    ),
                    "pnl_delta": (
                        float(actual.get("pnl") or 0) - float(theoretical.get("pnl") or 0)
                        if actual
                        and str(actual.get("status")) == "closed"
                        and str(theoretical.get("status")) == "closed"
                        else None
                    ),
                }
            )
        for i, actual in enumerate(live):
            if i in live_used:
                continue
            rows.append(
                {
                    "status": "live_only",
                    "symbol": actual.get("symbol"),
                    "side": side(actual.get("side")),
                    "entry_hour": hour(actual.get("entry_time")),
                    "theory": None,
                    "live": actual,
                    "entry_delta": None,
                    "pnl_delta": None,
                }
            )
        return rows[-150:]
