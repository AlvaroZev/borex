from __future__ import annotations

import uuid
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Type

from borex.backtest.engine import BacktestEngine
from borex.config import BacktestConfig, backtest_config_dict
from borex.data.manifest import get_dataset_hash
from borex.data.mtf import load_bias_dfs
from borex.data.store import load_ohlcv
from borex.optimize.grid import param_combinations
from borex.runner.parallel import default_workers
from borex.runner.results_db import save_result
from borex.strategy.base import Strategy
from borex.strategy.mtf import is_mtf_strategy, validate_mtf_entry
from borex.strategy.registry import _REGISTRY, get_strategy


def _run_sweep_job(payload: dict) -> dict:
    strategy_name = payload["strategy"]
    symbol = payload["symbol"]
    timeframe = payload["timeframe"]
    params = payload["params"]
    config_dict = payload["config"]

    df = load_ohlcv(symbol, timeframe)
    strategy = get_strategy(strategy_name, params)
    htf_dfs = None
    if is_mtf_strategy(type(strategy)):
        htf_dfs = load_bias_dfs(symbol, strategy.mtf_spec().bias_timeframes)

    engine = BacktestEngine(BacktestConfig(**config_dict))
    result = engine.run(strategy, df, symbol=symbol, timeframe=timeframe, htf_dfs=htf_dfs)
    out = result.to_dict()
    out["dataset_hash"] = get_dataset_hash(symbol, timeframe)
    return out


@dataclass
class SweepConfig:
    strategy: str
    symbol: str
    timeframe: str = "1h"
    sweep_params: list[str] | None = None
    max_points: int = 8
    max_combos: int = 500
    workers: int = field(default_factory=default_workers)
    backtest_config: BacktestConfig | None = None
    metric: str = "sharpe"
    min_reward_risk_ratio: float = 0.0

    @property
    def leverage(self) -> float:
        return (self.backtest_config or BacktestConfig()).leverage

    @property
    def initial_capital(self) -> float:
        return (self.backtest_config or BacktestConfig()).initial_capital


def run_param_sweep(
    cfg: SweepConfig,
    *,
    save: bool = True,
    run_group: str | None = None,
) -> tuple[list[dict], dict | None]:
    """Grid search over strategy params; returns all results + best row."""
    cls = _REGISTRY[cfg.strategy]
    validate_mtf_entry(cls(), cfg.timeframe)

    combos = param_combinations(
        cls,
        sweep_params=cfg.sweep_params,
        max_points=cfg.max_points,
        max_combos=cfg.max_combos,
        min_reward_risk_ratio=cfg.min_reward_risk_ratio,
    )
    group = run_group or (
        datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_sweep_") + uuid.uuid4().hex[:8]
    )
    config_dict = backtest_config_dict(cfg.backtest_config or BacktestConfig())

    jobs = [
        {
            "strategy": cfg.strategy,
            "symbol": cfg.symbol,
            "timeframe": cfg.timeframe,
            "params": params,
            "config": config_dict,
        }
        for params in combos
    ]

    results: list[dict] = []
    with ProcessPoolExecutor(max_workers=cfg.workers) as pool:
        futures = {pool.submit(_run_sweep_job, job): job for job in jobs}
        for fut in as_completed(futures):
            try:
                res = fut.result()
                results.append(res)
                if save:
                    save_result(res, run_group=group)
            except Exception as exc:
                job = futures[fut]
                results.append({**job, "error": str(exc), "metrics": {}})

    ok = [r for r in results if "error" not in r]
    best = None
    if ok:
        best = max(ok, key=lambda r: r["metrics"].get(cfg.metric, 0))
    return results, best
