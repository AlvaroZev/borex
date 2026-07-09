from __future__ import annotations

import json
import sqlite3
import uuid
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from borex.config import RESULTS_DB, BacktestConfig, backtest_config_dict
from borex.data.symbols import FOREX_PAIRS
from borex.runner.parallel import default_workers
from borex.optimize.grid import reward_risk_ratio
from borex.data.timeframes import SUPPORTED_TIMEFRAMES
from borex.runner.mass import _entry_timeframes_for_strategy
from borex.runner.walk_forward import RollingWalkForwardConfig, run_rolling_walk_forward
from borex.strategy.mtf import validate_mtf_entry
from borex.strategy.registry import _REGISTRY, get_strategy


@dataclass
class ScreenGates:
    min_oos_sharpe: float = 0.5
    min_oos_trades: int = 10
    max_oos_drawdown_pct: float = 35.0
    min_oos_return_pct: float = 0.0
    min_positive_fold_ratio: float = 0.5
    allow_liquidation: bool = False
    min_reward_risk_ratio: float = 0.0  # tp_pct / sl_pct; 2.0 = at least 2:1 reward:risk


@dataclass
class ScreenConfig:
    strategies: list[str] | None = None
    symbols: list[str] | None = None
    timeframes: list[str] | None = field(default_factory=lambda: ["1h"])
    sweep_params: list[str] | None = None
    max_points: int = 6
    max_combos: int = 80
    workers: int = field(default_factory=default_workers)
    train_months: int = 6
    test_months: int = 1
    step_months: int = 1
    optimize_metric: str = "sharpe"
    backtest_config: BacktestConfig | None = None
    gates: ScreenGates = field(default_factory=ScreenGates)
    create_paper: bool = False
    top_n_paper: int = 3
    save: bool = True


@dataclass
class ScreenCandidate:
    strategy: str
    symbol: str
    timeframe: str
    passed: bool
    best_params: dict
    oos_metrics: dict
    gate_reasons: list[str] = field(default_factory=list)
    folds: int = 0
    rank_score: float = 0.0
    paper_session_id: str | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        out = {
            "strategy": self.strategy,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "passed": self.passed,
            "best_params": self.best_params,
            "oos_metrics": self.oos_metrics,
            "gate_reasons": self.gate_reasons,
            "folds": self.folds,
            "rank_score": round(self.rank_score, 4),
        }
        if self.paper_session_id:
            out["paper_session_id"] = self.paper_session_id
        if self.error:
            out["error"] = self.error
        return out


@dataclass
class ScreenSummary:
    run_id: str
    total: int
    promoted: list[ScreenCandidate] = field(default_factory=list)
    rejected: list[ScreenCandidate] = field(default_factory=list)
    errors: list[ScreenCandidate] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "total": self.total,
            "promoted_count": len(self.promoted),
            "rejected_count": len(self.rejected),
            "error_count": len(self.errors),
            "promoted": [c.to_dict() for c in self.promoted],
            "rejected": [c.to_dict() for c in self.rejected[:20]],
            "errors": [c.to_dict() for c in self.errors],
        }


def evaluate_gates(
    oos_metrics: dict,
    gates: ScreenGates,
    *,
    fold_returns: list[float],
    best_params: dict | None = None,
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    passed = True

    if gates.min_reward_risk_ratio > 0:
        params = best_params or {}
        rr = reward_risk_ratio(params)
        if rr is None:
            passed = False
            reasons.append("missing sl_pct/tp_pct for reward:risk gate")
        elif rr < gates.min_reward_risk_ratio:
            passed = False
            reasons.append(
                f"reward:risk {rr:.2f} < {gates.min_reward_risk_ratio} "
                f"(tp={params.get('tp_pct')}, sl={params.get('sl_pct')})"
            )

    sharpe = float(oos_metrics.get("avg_oos_sharpe", 0))
    if sharpe < gates.min_oos_sharpe:
        passed = False
        reasons.append(f"avg_oos_sharpe {sharpe:.2f} < {gates.min_oos_sharpe}")

    trades = int(oos_metrics.get("total_oos_trades", 0))
    if trades < gates.min_oos_trades:
        passed = False
        reasons.append(f"total_oos_trades {trades} < {gates.min_oos_trades}")

    max_dd = float(oos_metrics.get("max_oos_drawdown_pct", 100))
    if max_dd > gates.max_oos_drawdown_pct:
        passed = False
        reasons.append(f"max_oos_drawdown {max_dd:.1f}% > {gates.max_oos_drawdown_pct}%")

    avg_ret = float(oos_metrics.get("avg_oos_return_pct", 0))
    if avg_ret < gates.min_oos_return_pct:
        passed = False
        reasons.append(f"avg_oos_return {avg_ret:.1f}% < {gates.min_oos_return_pct}%")

    if oos_metrics.get("any_liquidated") and not gates.allow_liquidation:
        passed = False
        reasons.append("liquidated in OOS window")

    if fold_returns:
        pos_ratio = sum(1 for r in fold_returns if r > 0) / len(fold_returns)
        if pos_ratio < gates.min_positive_fold_ratio:
            passed = False
            reasons.append(
                f"positive folds {pos_ratio:.0%} < {gates.min_positive_fold_ratio:.0%}"
            )

    if passed:
        reasons.append("passed all gates")
    return passed, reasons


def _best_params_from_folds(folds) -> dict:
    if not folds:
        return {}
    best = max(folds, key=lambda f: f.test.metrics.sharpe)
    if best.best_params:
        return dict(best.best_params)
    return dict(best.test.params)


def _screen_job(payload: dict) -> dict:
    strategy_name = payload["strategy"]
    symbol = payload["symbol"]
    timeframe = payload["timeframe"]
    bt_config = BacktestConfig(**payload["backtest_config"])
    wf_cfg = RollingWalkForwardConfig(**payload["wf"])
    gates = ScreenGates(**payload["gates"])

    try:
        validate_mtf_entry(get_strategy(strategy_name), timeframe)
        strategy = get_strategy(strategy_name)
        summary = run_rolling_walk_forward(
            strategy,
            symbol,
            timeframe,
            config=bt_config,
            wf=wf_cfg,
            save=False,
            include_regimes=False,
        )
        if not summary.folds:
            return {
                "strategy": strategy_name,
                "symbol": symbol,
                "timeframe": timeframe,
                "passed": False,
                "error": "no_walkforward_folds",
                "best_params": {},
                "oos_metrics": {},
                "gate_reasons": ["insufficient history for rolling windows"],
                "folds": 0,
            }

        fold_returns = [f.test.metrics.total_return_pct for f in summary.folds]
        best_params = _best_params_from_folds(summary.folds)
        passed, gate_reasons = evaluate_gates(
            summary.oos_metrics,
            gates,
            fold_returns=fold_returns,
            best_params=best_params,
        )
        score = float(summary.oos_metrics.get("avg_oos_sharpe", 0))

        return {
            "strategy": strategy_name,
            "symbol": symbol,
            "timeframe": timeframe,
            "passed": passed,
            "best_params": best_params,
            "oos_metrics": summary.oos_metrics,
            "gate_reasons": gate_reasons,
            "folds": len(summary.folds),
            "rank_score": score,
        }
    except Exception as exc:
        return {
            "strategy": strategy_name,
            "symbol": symbol,
            "timeframe": timeframe,
            "passed": False,
            "error": str(exc),
            "best_params": {},
            "oos_metrics": {},
            "gate_reasons": [],
            "folds": 0,
        }


def build_screen_jobs(cfg: ScreenConfig) -> list[dict]:
    import itertools

    strategies = cfg.strategies or list(_REGISTRY.keys())
    symbols = cfg.symbols or FOREX_PAIRS
    timeframes = cfg.timeframes if cfg.timeframes is not None else ["1h"]
    bt = backtest_config_dict(cfg.backtest_config or BacktestConfig())
    wf = {
        "train_months": cfg.train_months,
        "test_months": cfg.test_months,
        "step_months": cfg.step_months,
        "optimize_on_train": True,
        "sweep_params": cfg.sweep_params,
        "max_points": cfg.max_points,
        "max_combos": cfg.max_combos,
        "optimize_metric": cfg.optimize_metric,
        "min_reward_risk_ratio": cfg.gates.min_reward_risk_ratio,
        "nested_job": True,
    }
    gates = {
        "min_oos_sharpe": cfg.gates.min_oos_sharpe,
        "min_oos_trades": cfg.gates.min_oos_trades,
        "max_oos_drawdown_pct": cfg.gates.max_oos_drawdown_pct,
        "min_oos_return_pct": cfg.gates.min_oos_return_pct,
        "min_positive_fold_ratio": cfg.gates.min_positive_fold_ratio,
        "allow_liquidation": cfg.gates.allow_liquidation,
        "min_reward_risk_ratio": cfg.gates.min_reward_risk_ratio,
    }
    jobs: list[dict] = []
    for strategy, symbol in itertools.product(strategies, symbols):
        for tf in _entry_timeframes_for_strategy(strategy, timeframes):
            jobs.append(
                {
                    "strategy": strategy,
                    "symbol": symbol,
                    "timeframe": tf,
                    "backtest_config": bt,
                    "wf": wf,
                    "gates": gates,
                }
            )
    return jobs


def _dict_to_candidate(raw: dict) -> ScreenCandidate:
    return ScreenCandidate(
        strategy=raw["strategy"],
        symbol=raw["symbol"],
        timeframe=raw["timeframe"],
        passed=bool(raw.get("passed")),
        best_params=raw.get("best_params") or {},
        oos_metrics=raw.get("oos_metrics") or {},
        gate_reasons=list(raw.get("gate_reasons") or []),
        folds=int(raw.get("folds") or 0),
        rank_score=float(raw.get("rank_score") or 0),
        error=raw.get("error"),
    )


def _save_screen_run(summary: ScreenSummary, cfg: ScreenConfig) -> None:
    path = RESULTS_DB
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS screen_runs (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            promoted_count INTEGER,
            total INTEGER,
            report TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        INSERT INTO screen_runs (id, created_at, promoted_count, total, report)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            summary.run_id,
            datetime.now(timezone.utc).isoformat(),
            len(summary.promoted),
            summary.total,
            json.dumps(summary.to_dict()),
        ),
    )
    conn.commit()
    conn.close()


def run_screen(cfg: ScreenConfig | None = None) -> ScreenSummary:
    """Sweep + rolling walk-forward; promote configs passing OOS gates."""
    scfg = cfg or ScreenConfig()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_screen_") + uuid.uuid4().hex[:8]
    jobs = build_screen_jobs(scfg)

    raw_results: list[dict] = []
    with ProcessPoolExecutor(max_workers=scfg.workers) as pool:
        futures = {pool.submit(_screen_job, job): job for job in jobs}
        for fut in as_completed(futures):
            raw_results.append(fut.result())

    candidates = [_dict_to_candidate(r) for r in raw_results]
    promoted = sorted(
        [c for c in candidates if c.passed and not c.error],
        key=lambda c: c.rank_score,
        reverse=True,
    )
    rejected = [c for c in candidates if not c.passed and not c.error]
    errors = [c for c in candidates if c.error]

    if scfg.create_paper and promoted:
        from borex.runner.paper import create_session

        for cand in promoted[: scfg.top_n_paper]:
            try:
                session = create_session(
                    cand.strategy,
                    cand.symbol,
                    cand.timeframe,
                    params=cand.best_params,
                    config=scfg.backtest_config or BacktestConfig(),
                    refresh_data=False,
                )
                cand.paper_session_id = session.id
            except Exception as exc:
                cand.gate_reasons.append(f"paper_create_failed: {exc}")

    summary = ScreenSummary(
        run_id=run_id,
        total=len(jobs),
        promoted=promoted,
        rejected=rejected,
        errors=errors,
    )
    if scfg.save:
        _save_screen_run(summary, scfg)
    return summary


def list_screen_runs(limit: int = 20) -> list[dict]:
    if not RESULTS_DB.is_file():
        return []
    conn = sqlite3.connect(RESULTS_DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, created_at, promoted_count, total FROM screen_runs ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_screen_run(run_id: str) -> dict | None:
    if not RESULTS_DB.is_file():
        return None
    conn = sqlite3.connect(RESULTS_DB)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT id, created_at, promoted_count, total, report FROM screen_runs WHERE id = ?",
        (run_id,),
    ).fetchone()
    conn.close()
    if not row:
        return None
    out = dict(row)
    out["report"] = json.loads(out["report"])
    return out
