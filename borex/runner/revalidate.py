from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from borex.backtest.engine import BacktestEngine, _BARS_PER_YEAR
from borex.config import RESULTS_DB, BacktestConfig, IterationConfig
from borex.data.manifest import get_dataset_hash
from borex.data.mtf import load_bias_dfs
from borex.data.store import load_ohlcv
from borex.runner.decay import DecayReport, capital_scale_recommendation, compare_metrics
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
        CREATE TABLE IF NOT EXISTS revalidation_runs (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            strategy TEXT NOT NULL,
            symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            paper_session_id TEXT,
            verdict TEXT NOT NULL,
            report TEXT NOT NULL,
            dataset_hash TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_reval_strategy ON revalidation_runs(strategy, symbol, timeframe)"
    )
    conn.commit()


def split_baseline_recent(
    df: pd.DataFrame,
    *,
    recent_months: int,
    baseline_months: int | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, str], dict[str, str]]:
    if df.empty:
        raise ValueError("Empty dataframe")
    end = df.index.max()
    recent_start = end - pd.DateOffset(months=recent_months)
    recent_df = df[df.index >= recent_start]
    baseline_df = df[df.index < recent_start]
    if baseline_months is not None and not baseline_df.empty:
        baseline_start = recent_start - pd.DateOffset(months=baseline_months)
        baseline_df = baseline_df[baseline_df.index >= baseline_start]

    recent_period = {
        "start": str(recent_df.index.min()) if len(recent_df) else "",
        "end": str(recent_df.index.max()) if len(recent_df) else "",
    }
    baseline_period = {
        "start": str(baseline_df.index.min()) if len(baseline_df) else "",
        "end": str(baseline_df.index.max()) if len(baseline_df) else "",
    }
    return baseline_df, recent_df, baseline_period, recent_period


def _run_window(
    strategy,
    df: pd.DataFrame,
    *,
    symbol: str,
    timeframe: str,
    config: BacktestConfig,
    htf_dfs: dict[str, pd.DataFrame] | None,
) -> dict:
    if len(df) < strategy.warmup_bars() + 10:
        return {"trades": 0, "sharpe": 0.0, "total_return_pct": 0.0, "insufficient_bars": True}
    engine = BacktestEngine(config)
    result = engine.run(strategy, df, symbol=symbol, timeframe=timeframe, htf_dfs=htf_dfs)
    return result.metrics.to_dict()


def run_revalidation(
    strategy_name: str,
    symbol: str,
    timeframe: str,
    *,
    params: dict | None = None,
    config: BacktestConfig | None = None,
    iteration: IterationConfig | None = None,
    paper_session_id: str | None = None,
    save: bool = True,
) -> DecayReport:
    """Re-run backtest on baseline vs recent windows; optional paper session context."""
    cfg = config or BacktestConfig()
    icfg = iteration or IterationConfig()
    strategy = get_strategy(strategy_name, params)
    validate_mtf_entry(strategy, timeframe)

    df = load_ohlcv(symbol, timeframe)
    baseline_df, recent_df, baseline_period, recent_period = split_baseline_recent(
        df,
        recent_months=icfg.recent_months,
        baseline_months=icfg.baseline_months,
    )

    htf_dfs = None
    if is_mtf_strategy(type(strategy)):
        htf_dfs = load_bias_dfs(symbol, strategy.mtf_spec().bias_timeframes)

    baseline_metrics = _run_window(
        strategy, baseline_df, symbol=symbol, timeframe=timeframe, config=cfg, htf_dfs=htf_dfs
    )
    recent_metrics = _run_window(
        strategy, recent_df, symbol=symbol, timeframe=timeframe, config=cfg, htf_dfs=htf_dfs
    )

    if baseline_metrics.get("insufficient_bars") or recent_metrics.get("insufficient_bars"):
        report = DecayReport(
            verdict="insufficient_data",
            baseline={"metrics": baseline_metrics, "period": baseline_period},
            recent={"metrics": recent_metrics, "period": recent_period},
            reasons=["Not enough bars in baseline or recent window for reliable comparison"],
        )
    else:
        report = compare_metrics(
            baseline_metrics,
            recent_metrics,
            cfg=icfg,
            baseline_period=baseline_period,
            recent_period=recent_period,
        )

    paper_days = 0.0
    paper_trades = 0
    health = "ok"
    killed = False
    current_capital = cfg.initial_capital

    if paper_session_id:
        from borex.runner.monitor import compute_divergence, evaluate_live_health
        from borex.runner.paper import load_session

        session = load_session(paper_session_id)
        if session:
            paper_trades = len(session.engine_state.portfolio.closed_trades)
            conn = _connect()
            row = conn.execute(
                "SELECT created_at FROM paper_sessions WHERE id = ?", (paper_session_id,)
            ).fetchone()
            conn.close()
            if row:
                created = datetime.fromisoformat(str(row["created_at"]))
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                paper_days = (datetime.now(timezone.utc) - created).total_seconds() / 86400

            div = compute_divergence(
                session.baseline_metrics,
                live_equity=session.engine_state.portfolio.equity,
                initial_capital=session.config.initial_capital,
                live_bars=max(0, len(session.engine_state.equity_curve)),
                bars_per_year=_BARS_PER_YEAR.get(session.timeframe, 365 * 24),
            )
            health_info = evaluate_live_health(
                paper_session_id,
                kill=session.kill_switch,
                live_config=session.live_config,
                last_bar_ts=session.last_bar_ts,
                status=session.status,
                portfolio_liquidated=session.engine_state.portfolio.liquidated,
                risk_halted=session.engine_state.risk.stats.halted,
                divergence=div,
                trip_kill=False,
            )
            health = health_info.get("health", "ok")
            killed = health_info.get("killed", False)
            current_capital = session.engine_state.portfolio.equity

    report.capital = capital_scale_recommendation(
        cfg=icfg,
        decay_verdict=report.verdict,
        initial_capital=cfg.initial_capital,
        current_capital=current_capital,
        paper_days=paper_days,
        paper_trades=paper_trades,
        health=health,
        killed=killed,
    )

    if save:
        save_revalidation(
            report,
            strategy=strategy_name,
            symbol=symbol,
            timeframe=timeframe,
            paper_session_id=paper_session_id,
        )

    return report


def save_revalidation(
    report: DecayReport,
    *,
    strategy: str,
    symbol: str,
    timeframe: str,
    paper_session_id: str | None = None,
    db_path: Path | None = None,
) -> str:
    run_id = uuid.uuid4().hex[:12]
    conn = _connect(db_path)
    conn.execute(
        """
        INSERT INTO revalidation_runs (
            id, created_at, strategy, symbol, timeframe, paper_session_id, verdict, report, dataset_hash
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            datetime.now(timezone.utc).isoformat(),
            strategy,
            symbol,
            timeframe,
            paper_session_id,
            report.verdict,
            json.dumps(report.to_dict()),
            get_dataset_hash(symbol, timeframe),
        ),
    )
    conn.commit()
    conn.close()
    return run_id


def list_revalidations(
    *,
    strategy: str | None = None,
    symbol: str | None = None,
    limit: int = 50,
    db_path: Path | None = None,
) -> list[dict]:
    conn = _connect(db_path)
    q = "SELECT id, created_at, strategy, symbol, timeframe, paper_session_id, verdict FROM revalidation_runs"
    params: list[Any] = []
    clauses: list[str] = []
    if strategy:
        clauses.append("strategy = ?")
        params.append(strategy)
    if symbol:
        clauses.append("symbol = ?")
        params.append(symbol)
    if clauses:
        q += " WHERE " + " AND ".join(clauses)
    q += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_revalidation(run_id: str, db_path: Path | None = None) -> dict | None:
    conn = _connect(db_path)
    row = conn.execute("SELECT * FROM revalidation_runs WHERE id = ?", (run_id,)).fetchone()
    conn.close()
    if not row:
        return None
    out = dict(row)
    out["report"] = json.loads(out["report"])
    return out
