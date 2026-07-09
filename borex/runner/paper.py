from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from borex.backtest.engine import BacktestEngine, EngineState, _BARS_PER_YEAR
from borex.backtest.fills import PendingEntry
from borex.backtest.risk import RiskTracker
from borex.backtest.portfolio import Portfolio
from borex.config import (
    RESULTS_DB,
    BacktestConfig,
    LiveConfig,
    backtest_config_dict,
    live_config_dict,
    make_backtest_config,
    make_live_config,
)
from borex.data.downloader import download_symbol
from borex.data.mtf import load_bias_dfs
from borex.data.store import load_ohlcv
from borex.models.signal import SignalAction
from borex.runner.decision_log import create_alert, list_alerts, list_decisions, log_decision
from borex.runner.killswitch import KillSwitch, KillSwitchState
from borex.runner.monitor import compute_divergence, evaluate_live_health
from borex.strategy.mtf import is_mtf_strategy, validate_mtf_entry
from borex.strategy.registry import get_strategy


def _connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or RESULTS_DB
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    _init_schema(conn)
    return conn


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS paper_sessions (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            strategy TEXT NOT NULL,
            symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            params TEXT NOT NULL,
            config TEXT NOT NULL,
            last_bar_ts TEXT,
            status TEXT NOT NULL,
            state TEXT NOT NULL
        )
        """
    )
    cols = {row[1] for row in conn.execute("PRAGMA table_info(paper_sessions)")}
    for col, ddl in (
        ("baseline_metrics", "ALTER TABLE paper_sessions ADD COLUMN baseline_metrics TEXT"),
        ("live_config", "ALTER TABLE paper_sessions ADD COLUMN live_config TEXT"),
        ("kill_state", "ALTER TABLE paper_sessions ADD COLUMN kill_state TEXT"),
    ):
        if col not in cols:
            conn.execute(ddl)
    conn.commit()


def serialize_engine_state(state: EngineState) -> dict:
    pending = []
    for p in state.pending:
        pending.append(
            {
                "execute_index": p.execute_index,
                "action": p.action.value,
                "signal_price": p.signal_price,
                "stop_loss": p.stop_loss,
                "take_profit": p.take_profit,
                "size_pct": p.size_pct,
                "tag": p.tag,
                "symbol": p.symbol,
            }
        )
    return {
        "portfolio": state.portfolio.to_state(),
        "risk_peak": state.risk.peak_equity,
        "risk_day_start": state.risk.day_start_equity,
        "risk_day": str(state.risk.current_day) if state.risk.current_day else None,
        "risk_stats": state.risk.stats.to_dict(),
        "pending": pending,
        "equity_curve": state.equity_curve[-2000:],
        "last_index": state.last_index,
    }


def deserialize_engine_state(raw: dict, config: BacktestConfig) -> EngineState:
    portfolio = Portfolio.from_state(raw["portfolio"])
    risk = RiskTracker(config)
    risk.peak_equity = raw.get("risk_peak", config.initial_capital)
    risk.day_start_equity = raw.get("risk_day_start", config.initial_capital)
    risk.current_day = raw.get("risk_day")
    stats = raw.get("risk_stats", {})
    risk.stats.halted = stats.get("halted", False)
    risk.stats.halt_reason = stats.get("halt_reason", "")
    risk.stats.circuit_breaker_triggers = stats.get("circuit_breaker_triggers", 0)
    risk.stats.correlation_blocks = stats.get("correlation_blocks", 0)

    pending: list[PendingEntry] = []
    for p in raw.get("pending", []):
        pending.append(
            PendingEntry(
                execute_index=p["execute_index"],
                action=SignalAction(p["action"]),
                signal_price=p["signal_price"],
                stop_loss=p.get("stop_loss"),
                take_profit=p.get("take_profit"),
                size_pct=p.get("size_pct", 1.0),
                tag=p.get("tag", ""),
                symbol=p.get("symbol", ""),
            )
        )

    return EngineState(
        portfolio=portfolio,
        risk=risk,
        pending=pending,
        equity_curve=list(raw.get("equity_curve", [])),
        last_index=raw.get("last_index", -1),
    )


@dataclass
class PaperSession:
    id: str
    strategy: str
    symbol: str
    timeframe: str
    params: dict
    config: BacktestConfig
    live_config: LiveConfig
    baseline_metrics: dict
    kill_switch: KillSwitch
    last_bar_ts: str | None
    status: str
    engine_state: EngineState


def _row_to_session(row: sqlite3.Row) -> PaperSession:
    config = make_backtest_config(**json.loads(row["config"]))
    live_raw = row["live_config"] if "live_config" in row.keys() else None
    live_config = make_live_config(**json.loads(live_raw)) if live_raw else LiveConfig()
    kill_raw = row["kill_state"] if "kill_state" in row.keys() else None
    baseline_raw = row["baseline_metrics"] if "baseline_metrics" in row.keys() else None
    return PaperSession(
        id=row["id"],
        strategy=row["strategy"],
        symbol=row["symbol"],
        timeframe=row["timeframe"],
        params=json.loads(row["params"]),
        config=config,
        live_config=live_config,
        baseline_metrics=json.loads(baseline_raw) if baseline_raw else {},
        kill_switch=KillSwitch(state=KillSwitchState.from_dict(json.loads(kill_raw) if kill_raw else None)),
        last_bar_ts=row["last_bar_ts"],
        status=row["status"],
        engine_state=deserialize_engine_state(json.loads(row["state"]), config),
    )


def save_session(session: PaperSession, db_path: Path | None = None) -> None:
    conn = _connect(db_path)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO paper_sessions (
            id, created_at, updated_at, strategy, symbol, timeframe,
            params, config, last_bar_ts, status, state,
            baseline_metrics, live_config, kill_state
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            updated_at=excluded.updated_at,
            last_bar_ts=excluded.last_bar_ts,
            status=excluded.status,
            state=excluded.state,
            baseline_metrics=excluded.baseline_metrics,
            live_config=excluded.live_config,
            kill_state=excluded.kill_state
        """,
        (
            session.id,
            now,
            now,
            session.strategy,
            session.symbol,
            session.timeframe,
            json.dumps(session.params),
            json.dumps(backtest_config_dict(session.config)),
            session.last_bar_ts,
            session.status,
            json.dumps(serialize_engine_state(session.engine_state)),
            json.dumps(session.baseline_metrics),
            json.dumps(live_config_dict(session.live_config)),
            json.dumps(session.kill_switch.state.to_dict()),
        ),
    )
    conn.commit()
    conn.close()


def load_session(session_id: str, db_path: Path | None = None) -> PaperSession | None:
    conn = _connect(db_path)
    row = conn.execute(
        "SELECT * FROM paper_sessions WHERE id = ?", (session_id,)
    ).fetchone()
    conn.close()
    if not row:
        return None
    return _row_to_session(row)


def list_sessions(limit: int = 20, db_path: Path | None = None) -> list[dict]:
    conn = _connect(db_path)
    rows = conn.execute(
        """
        SELECT id, strategy, symbol, timeframe, last_bar_ts, status, updated_at, kill_state
        FROM paper_sessions ORDER BY updated_at DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()
    conn.close()
    out: list[dict] = []
    for r in rows:
        item = dict(r)
        kill = KillSwitchState.from_dict(json.loads(item.pop("kill_state") or "{}"))
        item["killed"] = kill.killed
        item["kill_reason"] = kill.reason
        out.append(item)
    return out


def list_active_sessions(limit: int = 100, db_path: Path | None = None) -> list[dict]:
    """Active paper sessions that are not kill-switched."""
    return [
        s
        for s in list_sessions(limit=limit, db_path=db_path)
        if s.get("status") == "active" and not s.get("killed")
    ]


def _make_decision_logger(session_id: str, min_bar_index: int):
    def handler(event: dict) -> None:
        detail = event.get("detail")
        if not isinstance(detail, dict):
            detail = {"value": detail} if detail is not None else {}
        log_decision(
            session_id,
            event_type=str(event.get("event_type", "unknown")),
            bar_index=event.get("bar_index"),
            bar_ts=event.get("bar_ts"),
            action=str(event.get("action", "")),
            reason=str(event.get("reason", "")),
            detail=detail,
        )

    return handler


def create_session(
    strategy_name: str,
    symbol: str,
    timeframe: str,
    *,
    params: dict | None = None,
    config: BacktestConfig | None = None,
    live_config: LiveConfig | None = None,
    refresh_data: bool = True,
) -> PaperSession:
    cfg = config or BacktestConfig()
    live = live_config or LiveConfig()
    strategy = get_strategy(strategy_name, params)
    validate_mtf_entry(strategy, timeframe)

    if refresh_data:
        download_symbol(symbol, timeframe, force=False)

    df = load_ohlcv(symbol, timeframe)
    htf_dfs = None
    if is_mtf_strategy(type(strategy)):
        htf_dfs = load_bias_dfs(symbol, strategy.mtf_spec().bias_timeframes)

    engine = BacktestEngine(cfg)
    warmup_result, state = engine.warmup_state(
        strategy, df, symbol=symbol, timeframe=timeframe, htf_dfs=htf_dfs
    )

    last_ts = str(df.index[-1]) if len(df) else None
    session = PaperSession(
        id=uuid.uuid4().hex[:12],
        strategy=strategy_name,
        symbol=symbol,
        timeframe=timeframe,
        params=strategy.params,
        config=cfg,
        live_config=live,
        baseline_metrics=warmup_result.metrics.to_dict(),
        kill_switch=KillSwitch(),
        last_bar_ts=last_ts,
        status="active",
        engine_state=state,
    )
    save_session(session)
    log_decision(
        session.id,
        event_type="session",
        reason="created",
        detail={
            "baseline_return_pct": session.baseline_metrics.get("total_return_pct"),
            "baseline_sharpe": session.baseline_metrics.get("sharpe"),
        },
    )
    return session


def kill_session(session_id: str, reason: str = "manual") -> PaperSession:
    session = load_session(session_id)
    if not session:
        raise KeyError(f"Paper session not found: {session_id}")
    session.kill_switch.trip(reason)
    session.status = "killed"
    save_session(session)
    log_decision(session_id, event_type="kill", reason=reason)
    create_alert(
        session_id,
        severity="critical",
        code="manual_kill",
        message=f"Session killed: {reason}",
    )
    return session


def resume_session(session_id: str) -> PaperSession:
    session = load_session(session_id)
    if not session:
        raise KeyError(f"Paper session not found: {session_id}")
    session.kill_switch.reset()
    session.status = "active"
    save_session(session)
    log_decision(session_id, event_type="resume", reason="manual")
    return session


def get_monitor_status(session_id: str) -> dict:
    session = load_session(session_id)
    if not session:
        raise KeyError(f"Paper session not found: {session_id}")

    live_bars = max(0, len(session.engine_state.equity_curve))
    bpy = _BARS_PER_YEAR.get(session.timeframe, 365 * 24)
    divergence = compute_divergence(
        session.baseline_metrics,
        live_equity=session.engine_state.portfolio.equity,
        initial_capital=session.config.initial_capital,
        live_bars=live_bars,
        bars_per_year=bpy,
    )
    health = evaluate_live_health(
        session_id,
        kill=session.kill_switch,
        live_config=session.live_config,
        last_bar_ts=session.last_bar_ts,
        status=session.status,
        portfolio_liquidated=session.engine_state.portfolio.liquidated,
        risk_halted=session.engine_state.risk.stats.halted,
        divergence=divergence,
        trip_kill=False,
    )
    if health["killed"] and session.status != "killed":
        session.status = "killed"
        save_session(session)

    return {
        "session_id": session_id,
        "strategy": session.strategy,
        "symbol": session.symbol,
        "timeframe": session.timeframe,
        "status": session.status,
        "baseline_metrics": session.baseline_metrics,
        "equity": round(session.engine_state.portfolio.equity, 2),
        "open_positions": len(session.engine_state.portfolio.open_trades),
        "last_bar_ts": session.last_bar_ts,
        "kill_switch": session.kill_switch.state.to_dict(),
        **health,
    }


def paper_tick(
    session_id: str,
    *,
    refresh_data: bool = True,
) -> dict:
    session = load_session(session_id)
    if not session:
        raise KeyError(f"Paper session not found: {session_id}")

    if session.kill_switch.killed or session.status == "killed":
        monitor = get_monitor_status(session_id)
        return {
            "session_id": session_id,
            "status": "killed",
            "kill_reason": session.kill_switch.state.reason,
            "monitor": monitor,
            "new_bars": 0,
        }

    prev_index = session.engine_state.last_index

    try:
        if refresh_data:
            download_symbol(session.symbol, session.timeframe, force=False)

        df = load_ohlcv(session.symbol, session.timeframe)
        strategy = get_strategy(session.strategy, session.params)
        htf_dfs = None
        if is_mtf_strategy(type(strategy)):
            htf_dfs = load_bias_dfs(session.symbol, strategy.mtf_spec().bias_timeframes)

        engine = BacktestEngine(session.config)
        engine.set_decision_context(
            _make_decision_logger(session_id, min_bar_index=prev_index + 1),
            min_bar_index=prev_index + 1,
        )
        result, state = engine.run_incremental(
            strategy,
            df,
            session.engine_state,
            symbol=session.symbol,
            timeframe=session.timeframe,
            htf_dfs=htf_dfs,
        )
        session.kill_switch.record_success()

        new_bars = max(0, state.last_index - prev_index)
        session.engine_state = state
        session.last_bar_ts = str(df.index[-1]) if len(df) else session.last_bar_ts

        if new_bars > 0:
            log_decision(
                session_id,
                event_type="tick",
                bar_ts=session.last_bar_ts,
                reason="processed",
                detail={"new_bars": new_bars, "equity": round(state.portfolio.equity, 2)},
            )

        monitor = evaluate_live_health(
            session_id,
            kill=session.kill_switch,
            live_config=session.live_config,
            last_bar_ts=session.last_bar_ts,
            status=session.status,
            portfolio_liquidated=state.portfolio.liquidated,
            risk_halted=state.risk.stats.halted,
            divergence=compute_divergence(
                session.baseline_metrics,
                live_equity=state.portfolio.equity,
                initial_capital=session.config.initial_capital,
                live_bars=max(0, len(state.equity_curve)),
                bars_per_year=_BARS_PER_YEAR.get(session.timeframe, 365 * 24),
            ),
            trip_kill=True,
        )
        if monitor["killed"]:
            session.status = "killed"
        save_session(session)

        out = result.to_dict()
        out["session_id"] = session_id
        out["new_bars"] = new_bars
        out["last_bar_ts"] = session.last_bar_ts
        out["open_positions"] = len(state.portfolio.open_trades)
        out["equity"] = round(state.portfolio.equity, 2)
        out["status"] = session.status
        out["monitor"] = monitor
        return out

    except Exception as exc:
        session.kill_switch.record_error(session.live_config)
        log_decision(
            session_id,
            event_type="error",
            reason=str(exc),
        )
        if session.kill_switch.killed:
            session.status = "killed"
            create_alert(
                session_id,
                severity="critical",
                code="consecutive_errors",
                message=f"Kill-switch after errors: {session.kill_switch.state.reason}",
            )
        save_session(session)
        raise


def run_paper_loop(
    session_id: str,
    *,
    poll_seconds: float = 300.0,
    max_ticks: int | None = None,
) -> None:
    ticks = 0
    while max_ticks is None or ticks < max_ticks:
        session = load_session(session_id)
        if session and (session.kill_switch.killed or session.status == "killed"):
            break
        try:
            paper_tick(session_id)
        except Exception:
            session = load_session(session_id)
            if session and session.kill_switch.killed:
                break
            raise
        ticks += 1
        if max_ticks is not None and ticks >= max_ticks:
            break
        time.sleep(poll_seconds)
