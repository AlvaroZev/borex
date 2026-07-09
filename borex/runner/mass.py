from __future__ import annotations

import itertools
import uuid
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

import pandas as pd

from borex.backtest.engine import BacktestEngine
from borex.config import BacktestConfig, backtest_config_dict
from borex.data.mtf import filter_df_range, load_bias_dfs
from borex.runner.parallel import default_workers
from borex.data.store import load_ohlcv
from borex.data.symbols import FOREX_PAIRS
from borex.data.timeframes import SUPPORTED_TIMEFRAMES
from borex.runner.results_db import save_result
from borex.strategy.mtf import is_mtf_strategy
from borex.strategy.registry import _REGISTRY, get_strategy

ProgressCallback = Callable[[dict, bool], None]

ENTRY_TIMEFRAMES = ("1m", "15m", "30m", "1h")


@dataclass
class MassRunConfig:
    strategies: list[str] | None = None
    symbols: list[str] | None = None
    timeframes: list[str] | None = None
    workers: int = field(default_factory=default_workers)
    backtest_config: BacktestConfig | None = None

    @property
    def leverage(self) -> float:
        cfg = self.backtest_config or BacktestConfig()
        return cfg.leverage

    @property
    def initial_capital(self) -> float:
        cfg = self.backtest_config or BacktestConfig()
        return cfg.initial_capital


def _entry_timeframes_for_strategy(name: str, requested: list[str] | None) -> list[str]:
    cls = _REGISTRY[name]
    pool = list(requested or SUPPORTED_TIMEFRAMES)
    if is_mtf_strategy(cls):
        allowed = set(cls.mtf_spec().entry_timeframes)
        return [tf for tf in pool if tf in allowed]
    return pool


def _run_job(payload: dict) -> dict:
    strategy_name = payload["strategy"]
    symbol = payload["symbol"]
    timeframe = payload["timeframe"]
    params = payload.get("params")
    config_dict = payload.get("config") or {}
    leverage = config_dict.get("leverage", 500.0)
    initial_capital = config_dict.get("initial_capital", 1_000.0)
    split = payload.get("split", "full")
    start = payload.get("start")
    end = payload.get("end")

    df = load_ohlcv(symbol, timeframe)
    df = filter_df_range(df, start, end)

    strategy = get_strategy(strategy_name, params)
    htf_dfs: dict[str, pd.DataFrame] = {}
    if is_mtf_strategy(type(strategy)):
        htf_dfs = load_bias_dfs(symbol, strategy.mtf_spec().bias_timeframes, end=end)

    engine = BacktestEngine(BacktestConfig(**config_dict) if config_dict else BacktestConfig(
        leverage=leverage, initial_capital=initial_capital
    ))
    result = engine.run(
        strategy,
        df,
        symbol=symbol,
        timeframe=timeframe,
        split=split,
        htf_dfs=htf_dfs,
    )
    out = result.to_dict()
    out["dataset_hash"] = get_dataset_hash(symbol, timeframe)
    return out


def build_mass_jobs(config: MassRunConfig | None = None) -> tuple[list[dict], str]:
    cfg = config or MassRunConfig()
    bt = backtest_config_dict(cfg.backtest_config or BacktestConfig())
    strategies = cfg.strategies or list(_REGISTRY.keys())
    symbols = cfg.symbols or FOREX_PAIRS
    run_group = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8]
    jobs: list[dict] = []
    for s, sym in itertools.product(strategies, symbols):
        for tf in _entry_timeframes_for_strategy(s, cfg.timeframes):
            jobs.append(
                {
                    "strategy": s,
                    "symbol": sym,
                    "timeframe": tf,
                    "config": bt,
                }
            )
    return jobs, run_group


def run_mass(
    config: MassRunConfig | None = None,
    *,
    save: bool = True,
    on_progress: ProgressCallback | None = None,
    run_group: str | None = None,
) -> list[dict]:
    cfg = config or MassRunConfig()
    jobs, group = build_mass_jobs(cfg)
    if run_group:
        group = run_group

    results: list[dict] = []
    with ProcessPoolExecutor(max_workers=cfg.workers) as pool:
        futures = {pool.submit(_run_job, job): job for job in jobs}
        for fut in as_completed(futures):
            job = futures[fut]
            try:
                res = fut.result()
                results.append(res)
                if save:
                    save_result(res, run_group=group)
                if on_progress:
                    on_progress(res, True)
            except Exception as exc:
                err = {**job, "error": str(exc), "metrics": {}}
                results.append(err)
                if on_progress:
                    on_progress(err, False)

    return results
