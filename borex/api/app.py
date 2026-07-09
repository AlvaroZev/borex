from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from borex import __version__
from borex.api.tasks import start_download_run, start_dukascopy_run, start_mass_run
from borex.backtest.engine import BacktestEngine
from borex.backtest.regime import analyze_trades_by_regime
from borex.config import BacktestConfig, LiveConfig, make_backtest_config, make_iteration_config, make_live_config
from borex.optimize import SweepConfig, run_param_sweep
from borex.data import download_symbol, list_cached
from borex.data.audit import audit_all, audit_dataset
from borex.data.dukascopy_download import DEFAULT_END, DEFAULT_START
from borex.data.manifest import get_dataset_hash
from borex.data.mtf import load_bias_dfs
from borex.data.store import load_ohlcv
from borex.runner.live import runs
from borex.runner.mass import MassRunConfig, run_mass
from borex.runner.results_db import leaderboard, list_results
from borex.runner.walk_forward import RollingWalkForwardConfig, run_rolling_walk_forward, run_walk_forward
from borex.strategy.mtf import is_mtf_strategy, validate_mtf_entry
from borex.strategy.registry import get_strategy, list_strategies

try:
    from borex.runner.parallel import default_workers as _default_workers
except ImportError:
    def _default_workers() -> int:
        return 8

_DEFAULT_W = _default_workers()

app = FastAPI(title="Borex API", version=__version__)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class RealismFields(BaseModel):
    spread_pct: float = 0.0
    slippage_mode: str = "fixed"
    slippage_atr_mult: float = 0.1
    fill_mode: str = "close"
    entry_delay_bars: int = 0


class LiveFields(BaseModel):
    max_consecutive_errors: int = 3
    stale_data_minutes: int = 180
    divergence_warn_pct: float | None = 0.20
    kill_on_liquidation: bool = True
    kill_on_halt: bool = True


def _live_config_from_request(req) -> LiveConfig:
    return make_live_config(
        max_consecutive_errors=getattr(req, "max_consecutive_errors", 3),
        stale_data_minutes=getattr(req, "stale_data_minutes", 180),
        divergence_warn_pct=getattr(req, "divergence_warn_pct", 0.20),
        kill_on_liquidation=getattr(req, "kill_on_liquidation", True),
        kill_on_halt=getattr(req, "kill_on_halt", True),
    )


class RiskFields(BaseModel):
    size_mode: str = "fixed"
    risk_per_trade_pct: float = 0.01
    atr_period: int = 14
    atr_stop_mult: float = 1.5
    kelly_fraction: float = 0.5
    kelly_min_trades: int = 20
    max_daily_loss_pct: float | None = None
    max_drawdown_pct: float | None = None
    correlation_limit: bool = True
    max_currency_exposure: int = 1


def _backtest_config_from_request(req) -> BacktestConfig:
    return make_backtest_config(
        leverage=req.leverage,
        initial_capital=req.capital,
        commission_pct=req.commission_pct,
        slippage_pct=req.slippage_pct,
        size_mode=req.size_mode,
        risk_per_trade_pct=req.risk_per_trade_pct,
        atr_period=req.atr_period,
        atr_stop_mult=req.atr_stop_mult,
        kelly_fraction=req.kelly_fraction,
        kelly_min_trades=req.kelly_min_trades,
        max_daily_loss_pct=req.max_daily_loss_pct,
        max_drawdown_pct=req.max_drawdown_pct,
        correlation_limit=req.correlation_limit,
        max_currency_exposure=req.max_currency_exposure,
        spread_pct=getattr(req, "spread_pct", 0.0),
        slippage_mode=getattr(req, "slippage_mode", "fixed"),
        slippage_atr_mult=getattr(req, "slippage_atr_mult", 0.1),
        fill_mode=getattr(req, "fill_mode", "close"),
        entry_delay_bars=getattr(req, "entry_delay_bars", 0),
    )


class BacktestRequest(RiskFields, RealismFields):
    strategy: str
    symbol: str
    timeframe: str = "1h"
    params: dict[str, Any] | None = None
    leverage: float = 500.0
    capital: float = 1000.0
    commission_pct: float = 0.0
    slippage_pct: float = 0.0
    regimes: bool = False


class SweepRequest(RiskFields, RealismFields):
    strategy: str
    symbol: str
    timeframe: str = "1h"
    sweep_params: list[str] | None = None
    max_points: int = 8
    max_combos: int = 500
    workers: int = Field(default=_DEFAULT_W, ge=1, le=32)
    metric: str = "sharpe"
    leverage: float = 500.0
    capital: float = 1000.0
    commission_pct: float = 0.0
    slippage_pct: float = 0.0


class RollingWalkForwardRequest(RiskFields, RealismFields):
    strategy: str
    symbol: str
    timeframe: str = "1h"
    params: dict[str, Any] | None = None
    train_months: int = 6
    test_months: int = 1
    step_months: int = 1
    optimize_on_train: bool = False
    sweep_params: list[str] | None = None
    max_points: int = 6
    max_combos: int = 200
    leverage: float = 500.0
    capital: float = 1000.0
    commission_pct: float = 0.0
    slippage_pct: float = 0.0
    save: bool = False
    include_regimes: bool = True


class MassRequest(RiskFields, RealismFields):
    strategies: list[str] | None = None
    symbols: list[str] | None = None
    timeframes: list[str] | None = None
    workers: int = Field(default=_DEFAULT_W, ge=1, le=32)
    leverage: float = 500.0
    capital: float = 1000.0
    commission_pct: float = 0.0
    slippage_pct: float = 0.0


class PaperCreateRequest(RiskFields, RealismFields, LiveFields):
    strategy: str
    symbol: str
    timeframe: str = "1h"
    params: dict[str, Any] | None = None
    leverage: float = 500.0
    capital: float = 1000.0
    commission_pct: float = 0.0
    slippage_pct: float = 0.0


class PaperTickRequest(BaseModel):
    refresh_data: bool = True


class IterationFields(BaseModel):
    recent_months: int = 3
    baseline_months: int | None = None
    decay_sharpe_delta: float = 0.5
    decay_return_pct: float = 15.0
    min_recent_trades: int = 10
    min_paper_days: int = 14
    min_paper_trades: int = 20
    scale_up_factor: float = 1.5
    max_capital_multiplier: float = 5.0


def _iteration_config_from_request(req) -> IterationConfig:
    from borex.config import IterationConfig

    return make_iteration_config(
        recent_months=getattr(req, "recent_months", 3),
        baseline_months=getattr(req, "baseline_months", None),
        decay_sharpe_delta=getattr(req, "decay_sharpe_delta", 0.5),
        decay_return_pct=getattr(req, "decay_return_pct", 15.0),
        min_recent_trades=getattr(req, "min_recent_trades", 10),
        min_paper_days=getattr(req, "min_paper_days", 14),
        min_paper_trades=getattr(req, "min_paper_trades", 20),
        scale_up_factor=getattr(req, "scale_up_factor", 1.5),
        max_capital_multiplier=getattr(req, "max_capital_multiplier", 5.0),
    )


class RevalidateRequest(RiskFields, RealismFields, IterationFields):
    strategy: str
    symbol: str
    timeframe: str = "1h"
    params: dict[str, Any] | None = None
    paper_session_id: str | None = None
    leverage: float = 500.0
    capital: float = 1000.0
    commission_pct: float = 0.0
    slippage_pct: float = 0.0
    save: bool = True


class ScreenGatesFields(BaseModel):
    min_oos_sharpe: float = 0.5
    min_oos_trades: int = 10
    max_oos_drawdown_pct: float = 35.0
    min_oos_return_pct: float = 0.0
    min_positive_fold_ratio: float = 0.5
    allow_liquidation: bool = False
    min_reward_risk_ratio: float = 0.0


class ScreenRequest(RiskFields, RealismFields, ScreenGatesFields):
    strategies: list[str] | None = None
    symbols: list[str] | None = None
    timeframes: list[str] | None = None
    sweep_params: list[str] | None = None
    max_points: int = 6
    max_combos: int = 80
    workers: int = Field(default=_DEFAULT_W, ge=1, le=32)
    train_sweep_workers: int = 0
    fold_workers: int = 0
    train_months: int = 6
    test_months: int = 1
    step_months: int = 1
    optimize_metric: str = "sharpe"
    create_paper: bool = False
    top_n_paper: int = 3
    leverage: float = 500.0
    capital: float = 1000.0
    commission_pct: float = 0.0
    slippage_pct: float = 0.0
    save: bool = True


class PipelineRunRequest(ScreenRequest, IterationFields):
    run_audit: bool = True
    strict_audit: bool = True
    run_screen: bool = True
    tick_paper: bool = False
    run_revalidate: bool = True
    kill_on_decay: bool = True
    send_digest: bool = True
    create_paper: bool = True


class PipelineTickRequest(IterationFields):
    revalidate: bool = False
    kill_on_decay: bool = True
    send_digest: bool = False
    save: bool = True


class PaperKillRequest(BaseModel):
    reason: str = "manual"


class DownloadRequest(BaseModel):
    all: bool = False
    symbol: str | None = None
    timeframe: str | None = None
    force: bool = False


class DukascopyDownloadRequest(BaseModel):
    start: str = DEFAULT_START
    end: str | None = DEFAULT_END
    symbols: list[str] | None = None
    timeframes: list[str] | None = None
    force: bool = False


class AlertConfigRequest(BaseModel):
    enabled: bool = False
    webhook_url: str = ""
    slack_webhook_url: str = ""
    min_severity: str = "warn"


class AlertTestRequest(BaseModel):
    session_id: str = "test"


@app.get("/api/alerts/config")
def get_alert_config() -> dict:
    import os

    from borex.runner.alert_delivery import load_alert_config

    cfg = load_alert_config()
    return {
        **cfg.to_dict(),
        "env_webhook": bool(os.environ.get("BOREX_WEBHOOK_URL")),
        "env_slack": bool(os.environ.get("BOREX_SLACK_WEBHOOK_URL")),
    }


@app.put("/api/alerts/config")
def put_alert_config(req: AlertConfigRequest) -> dict:
    from borex.runner.alert_delivery import AlertDeliveryConfig, load_alert_config, save_alert_config

    cfg = AlertDeliveryConfig(
        enabled=req.enabled,
        webhook_url=req.webhook_url,
        slack_webhook_url=req.slack_webhook_url,
        min_severity=req.min_severity,
    )
    save_alert_config(cfg)
    return cfg.to_dict()


@app.post("/api/alerts/test")
def post_alert_test(req: AlertTestRequest | None = None) -> dict:
    from borex.runner.alert_delivery import send_test_alert

    sid = req.session_id if req else "test"
    return send_test_alert(sid)


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "version": __version__}


@app.get("/api/data/cache")
def get_cache() -> list[dict]:
    return list_cached()


@app.get("/api/data/audit")
def get_audit(
    symbol: str | None = None,
    timeframe: str | None = None,
) -> list[dict] | dict:
    if symbol and timeframe:
        return audit_dataset(symbol, timeframe).to_dict()
    return [r.to_dict() for r in audit_all()]


@app.post("/api/data/download")
def post_download(req: DownloadRequest) -> dict:
    if req.all:
        run_id = start_download_run(force=req.force)
        return {"run_id": run_id, "status": "started"}
    if not req.symbol or not req.timeframe:
        raise HTTPException(400, "symbol and timeframe required unless all=true")
    download_symbol(req.symbol, req.timeframe, force=req.force)
    return {"status": "ok", "cache": list_cached()}


@app.post("/api/data/download/dukascopy")
def post_download_dukascopy(req: DukascopyDownloadRequest) -> dict:
    run_id = start_dukascopy_run(
        start=req.start,
        end=req.end,
        symbols=req.symbols,
        timeframes=req.timeframes,
        force=req.force,
    )
    state = runs.get(run_id)
    return {
        "run_id": run_id,
        "status": "started",
        "total": state.total if state else 0,
        "start": req.start,
        "end": req.end or DEFAULT_END,
    }


@app.get("/api/strategies")
def get_strategies() -> list[dict]:
    return list_strategies()


@app.post("/api/backtest")
def post_backtest(req: BacktestRequest) -> dict:
    try:
        strategy = get_strategy(req.strategy, req.params)
        validate_mtf_entry(strategy, req.timeframe)
        df = load_ohlcv(req.symbol, req.timeframe)
        htf_dfs = None
        if is_mtf_strategy(type(strategy)):
            htf_dfs = load_bias_dfs(req.symbol, strategy.mtf_spec().bias_timeframes)
        engine = BacktestEngine(_backtest_config_from_request(req))
        result = engine.run(
            strategy,
            df,
            symbol=req.symbol,
            timeframe=req.timeframe,
            htf_dfs=htf_dfs,
        )
        out = result.to_dict()
        out["dataset_hash"] = get_dataset_hash(req.symbol, req.timeframe)
        if req.regimes:
            out["regimes"] = analyze_trades_by_regime(result, df)
        return out
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/backtest/walkforward")
def post_walkforward(req: BacktestRequest) -> dict:
    try:
        strategy = get_strategy(req.strategy, req.params)
        validate_mtf_entry(strategy, req.timeframe)
        train, test = run_walk_forward(
            strategy,
            req.symbol,
            req.timeframe,
            config=_backtest_config_from_request(req),
        )
        return {"train": train.to_dict(), "test": test.to_dict()}
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/backtest/walkforward/rolling")
def post_rolling_walkforward(req: RollingWalkForwardRequest) -> dict:
    try:
        strategy = get_strategy(req.strategy, req.params)
        validate_mtf_entry(strategy, req.timeframe)
        summary = run_rolling_walk_forward(
            strategy,
            req.symbol,
            req.timeframe,
            config=_backtest_config_from_request(req),
            wf=RollingWalkForwardConfig(
                train_months=req.train_months,
                test_months=req.test_months,
                step_months=req.step_months,
                optimize_on_train=req.optimize_on_train,
                sweep_params=req.sweep_params,
                max_points=req.max_points,
                max_combos=req.max_combos,
            ),
            save=req.save,
            include_regimes=req.include_regimes,
        )
        return summary.to_dict()
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/sweep")
def post_sweep(req: SweepRequest) -> dict:
    try:
        validate_mtf_entry(get_strategy(req.strategy), req.timeframe)
        cfg = SweepConfig(
            strategy=req.strategy,
            symbol=req.symbol,
            timeframe=req.timeframe,
            sweep_params=req.sweep_params,
            max_points=req.max_points,
            max_combos=req.max_combos,
            workers=req.workers,
            metric=req.metric,
            backtest_config=_backtest_config_from_request(req),
        )
        results, best = run_param_sweep(cfg)
        ok = [r for r in results if "error" not in r]
        return {"completed": len(ok), "total": len(results), "best": best, "results": results[:50]}
    except (FileNotFoundError, KeyError) as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/mass")
def post_mass(req: MassRequest) -> dict:
    """Blocking mass run (CLI-style). Prefer /api/mass/start for live UI."""
    cfg = MassRunConfig(
        strategies=req.strategies,
        symbols=req.symbols,
        timeframes=req.timeframes,
        workers=req.workers,
        backtest_config=_backtest_config_from_request(req),
    )
    results = run_mass(cfg)
    ok = sum(1 for r in results if "error" not in r)
    return {"completed": ok, "total": len(results), "results": results[:50]}


@app.post("/api/mass/start")
def post_mass_start(req: MassRequest) -> dict:
    cfg = MassRunConfig(
        strategies=req.strategies,
        symbols=req.symbols,
        timeframes=req.timeframes,
        workers=req.workers,
        backtest_config=_backtest_config_from_request(req),
    )
    run_id = start_mass_run(cfg)
    state = runs.get(run_id)
    return {"run_id": run_id, "total": state.total if state else 0}


@app.get("/api/paper/sessions")
def get_paper_sessions(limit: int = 20) -> list[dict]:
    from borex.runner.paper import list_sessions

    return list_sessions(limit=limit)


@app.post("/api/paper/sessions")
def post_paper_session(req: PaperCreateRequest) -> dict:
    from borex.runner.paper import create_session

    try:
        validate_mtf_entry(get_strategy(req.strategy, req.params), req.timeframe)
        session = create_session(
            req.strategy,
            req.symbol,
            req.timeframe,
            params=req.params,
            config=_backtest_config_from_request(req),
            live_config=_live_config_from_request(req),
        )
        return {
            "session_id": session.id,
            "strategy": session.strategy,
            "symbol": session.symbol,
            "timeframe": session.timeframe,
            "last_bar_ts": session.last_bar_ts,
            "status": session.status,
            "baseline_metrics": session.baseline_metrics,
        }
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/paper/sessions/{session_id}/tick")
def post_paper_tick(session_id: str, req: PaperTickRequest | None = None) -> dict:
    from borex.runner.paper import paper_tick

    try:
        return paper_tick(
            session_id,
            refresh_data=True if req is None else req.refresh_data,
        )
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/paper/sessions/{session_id}/monitor")
def get_paper_monitor(session_id: str) -> dict:
    from borex.runner.paper import get_monitor_status

    try:
        return get_monitor_status(session_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/paper/sessions/{session_id}/decisions")
def get_paper_decisions(session_id: str, limit: int = 100) -> list[dict]:
    from borex.runner.decision_log import list_decisions
    from borex.runner.paper import load_session

    if not load_session(session_id):
        raise HTTPException(404, "Session not found")
    return list_decisions(session_id, limit=limit)


@app.get("/api/paper/sessions/{session_id}/alerts")
def get_paper_alerts(session_id: str, limit: int = 50) -> list[dict]:
    from borex.runner.decision_log import list_alerts
    from borex.runner.paper import load_session

    if not load_session(session_id):
        raise HTTPException(404, "Session not found")
    return list_alerts(session_id, limit=limit)


@app.post("/api/paper/sessions/{session_id}/kill")
def post_paper_kill(session_id: str, req: PaperKillRequest | None = None) -> dict:
    from borex.runner.paper import kill_session

    try:
        session = kill_session(session_id, reason=req.reason if req else "manual")
        return {
            "session_id": session.id,
            "status": session.status,
            "kill_reason": session.kill_switch.state.reason,
        }
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/paper/sessions/{session_id}/resume")
def post_paper_resume(session_id: str) -> dict:
    from borex.runner.paper import resume_session

    try:
        session = resume_session(session_id)
        return {"session_id": session.id, "status": session.status}
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/revalidate")
def post_revalidate(req: RevalidateRequest) -> dict:
    from borex.runner.revalidate import run_revalidation

    try:
        validate_mtf_entry(get_strategy(req.strategy, req.params), req.timeframe)
        report = run_revalidation(
            req.strategy,
            req.symbol,
            req.timeframe,
            params=req.params,
            config=_backtest_config_from_request(req),
            iteration=_iteration_config_from_request(req),
            paper_session_id=req.paper_session_id,
            save=req.save,
        )
        return report.to_dict()
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/revalidate")
def get_revalidate_history(
    strategy: str | None = None,
    symbol: str | None = None,
    limit: int = 50,
) -> list[dict]:
    from borex.runner.revalidate import list_revalidations

    return list_revalidations(strategy=strategy, symbol=symbol, limit=limit)


@app.get("/api/revalidate/{run_id}")
def get_revalidate_run(run_id: str) -> dict:
    from borex.runner.revalidate import get_revalidation

    row = get_revalidation(run_id)
    if not row:
        raise HTTPException(404, "Revalidation run not found")
    return row


@app.post("/api/screen")
def post_screen(req: ScreenRequest) -> dict:
    from borex.runner.screen import ScreenConfig, ScreenGates, run_screen

    cfg = ScreenConfig(
        strategies=req.strategies,
        symbols=req.symbols,
        timeframes=req.timeframes,
        sweep_params=req.sweep_params,
        max_points=req.max_points,
        max_combos=req.max_combos,
        workers=req.workers,
        train_months=req.train_months,
        test_months=req.test_months,
        step_months=req.step_months,
        optimize_metric=req.optimize_metric,
        backtest_config=_backtest_config_from_request(req),
        gates=ScreenGates(
            min_oos_sharpe=req.min_oos_sharpe,
            min_oos_trades=req.min_oos_trades,
            max_oos_drawdown_pct=req.max_oos_drawdown_pct,
            min_oos_return_pct=req.min_oos_return_pct,
            min_positive_fold_ratio=req.min_positive_fold_ratio,
            allow_liquidation=req.allow_liquidation,
            min_reward_risk_ratio=req.min_reward_risk_ratio,
        ),
        create_paper=req.create_paper,
        top_n_paper=req.top_n_paper,
        save=req.save,
    )
    try:
        summary = run_screen(cfg)
        return summary.to_dict()
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/screen")
def get_screen_history(limit: int = 20) -> list[dict]:
    from borex.runner.screen import list_screen_runs

    return list_screen_runs(limit=limit)


@app.get("/api/screen/{run_id}")
def get_screen_run(run_id: str) -> dict:
    from borex.runner.screen import get_screen_run as fetch_screen_run

    row = fetch_screen_run(run_id)
    if not row:
        raise HTTPException(404, "Screen run not found")
    return row


@app.post("/api/pipeline/run")
def post_pipeline_run(req: PipelineRunRequest) -> dict:
    from borex.runner.pipeline import PipelineConfig, run_pipeline
    from borex.runner.screen import ScreenConfig, ScreenGates

    screen_cfg = ScreenConfig(
        strategies=req.strategies,
        symbols=req.symbols,
        timeframes=req.timeframes,
        sweep_params=req.sweep_params,
        max_points=req.max_points,
        max_combos=req.max_combos,
        workers=req.workers,
        train_months=req.train_months,
        test_months=req.test_months,
        step_months=req.step_months,
        optimize_metric=req.optimize_metric,
        backtest_config=_backtest_config_from_request(req),
        gates=ScreenGates(
            min_oos_sharpe=req.min_oos_sharpe,
            min_oos_trades=req.min_oos_trades,
            max_oos_drawdown_pct=req.max_oos_drawdown_pct,
            min_oos_return_pct=req.min_oos_return_pct,
            min_positive_fold_ratio=req.min_positive_fold_ratio,
            allow_liquidation=req.allow_liquidation,
            min_reward_risk_ratio=req.min_reward_risk_ratio,
        ),
        create_paper=req.create_paper,
        top_n_paper=req.top_n_paper,
        save=req.save,
    )
    cfg = PipelineConfig(
        run_audit=req.run_audit,
        strict_audit=req.strict_audit,
        run_screen=req.run_screen,
        screen=screen_cfg,
        tick_paper=req.tick_paper,
        run_revalidate=req.run_revalidate,
        kill_on_decay=req.kill_on_decay,
        iteration=_iteration_config_from_request(req),
        send_digest=req.send_digest,
        save=req.save,
    )
    summary = run_pipeline(cfg)
    return summary.to_dict()


@app.post("/api/pipeline/tick")
def post_pipeline_tick(req: PipelineTickRequest) -> dict:
    from borex.runner.pipeline import run_pipeline_tick

    summary = run_pipeline_tick(
        revalidate=req.revalidate,
        kill_on_decay=req.kill_on_decay,
        iteration=_iteration_config_from_request(req),
        send_digest=req.send_digest,
        save=req.save,
    )
    return summary.to_dict()


@app.get("/api/pipeline")
def get_pipeline_history(limit: int = 20) -> list[dict]:
    from borex.runner.pipeline import list_pipeline_runs

    return list_pipeline_runs(limit=limit)


@app.get("/api/pipeline/{run_id}")
def get_pipeline_run(run_id: str) -> dict:
    from borex.runner.pipeline import get_pipeline_run as fetch_pipeline_run

    row = fetch_pipeline_run(run_id)
    if not row:
        raise HTTPException(404, "Pipeline run not found")
    return row


@app.get("/api/runs")
def get_runs(limit: int = 20) -> list[dict]:
    return runs.list_runs(limit=limit)


@app.get("/api/runs/{run_id}")
def get_run(run_id: str) -> dict:
    data = runs.get_enriched(run_id)
    if not data:
        raise HTTPException(404, "Run not found")
    return data


@app.get("/api/runs/{run_id}/stream")
async def stream_run(run_id: str) -> StreamingResponse:
    state = runs.get(run_id)
    if not state:
        raise HTTPException(404, "Run not found")

    async def event_generator():
        cursor = 0
        while True:
            new_events, cursor = runs.drain_events(run_id, cursor)
            for ev in new_events:
                payload = {
                    "type": ev.type,
                    "run_id": ev.run_id,
                    "kind": ev.kind,
                    **ev.payload,
                }
                yield f"data: {json.dumps(payload)}\n\n"

            snapshot = runs.get_enriched(run_id)
            if snapshot and snapshot.get("status") in ("done", "error"):
                yield f"data: {json.dumps({'type': 'snapshot', 'progress': snapshot})}\n\n"
                break

            if snapshot:
                yield f"data: {json.dumps({'type': 'heartbeat', 'progress': snapshot})}\n\n"

            await asyncio.sleep(0.4)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


@app.get("/api/results")
def get_results(limit: int = 200, run_group: str | None = None) -> list[dict]:
    return list_results(limit=limit, run_group=run_group)


@app.get("/api/leaderboard")
def get_leaderboard(metric: str = "sharpe", limit: int = 50, run_group: str | None = None) -> list[dict]:
    return leaderboard(metric=metric, limit=limit, run_group=run_group)
