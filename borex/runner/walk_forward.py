from __future__ import annotations

import uuid
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Type

import pandas as pd

from borex.backtest.engine import BacktestEngine, BacktestResult
from borex.backtest.metrics import BacktestMetrics
from borex.backtest.regime import analyze_trades_by_regime
from borex.config import BacktestConfig, backtest_config_dict
from borex.data.manifest import get_dataset_hash
from borex.data.mtf import load_bias_dfs
from borex.data.store import load_ohlcv
from borex.optimize.grid import param_combinations
from borex.runner.parallel import resolve_wf_workers
from borex.runner.results_db import save_result
from borex.strategy.base import Strategy
from borex.strategy.mtf import is_mtf_strategy
from borex.strategy.registry import get_strategy


@dataclass
class WalkForwardConfig:
    train_ratio: float = 0.7
    min_train_bars: int = 200
    min_test_bars: int = 100


@dataclass
class RollingWalkForwardConfig:
    train_months: int = 6
    test_months: int = 1
    step_months: int = 1
    min_train_bars: int = 200
    min_test_bars: int = 50
    optimize_on_train: bool = False
    sweep_params: list[str] | None = None
    max_points: int = 6
    max_combos: int = 200
    optimize_metric: str = "sharpe"
    min_reward_risk_ratio: float = 0.0
    train_sweep_workers: int = 0  # 0=auto; parallel param combos on train window
    fold_workers: int = 0  # 0=auto; parallel OOS folds
    nested_job: bool = False  # True when already inside screen/mass pool worker


@dataclass
class WalkForwardFold:
    fold: int
    train_start: str
    train_end: str
    test_start: str
    test_end: str
    train: BacktestResult
    test: BacktestResult
    best_params: dict | None = None


@dataclass
class RollingWalkForwardSummary:
    strategy: str
    symbol: str
    timeframe: str
    folds: list[WalkForwardFold] = field(default_factory=list)
    oos_metrics: dict = field(default_factory=dict)
    regimes: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "strategy": self.strategy,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "folds": [
                {
                    "fold": f.fold,
                    "train_start": f.train_start,
                    "train_end": f.train_end,
                    "test_start": f.test_start,
                    "test_end": f.test_end,
                    "best_params": f.best_params,
                    "train": f.train.to_dict(),
                    "test": f.test.to_dict(),
                }
                for f in self.folds
            ],
            "oos_aggregate": self.oos_metrics,
            "regimes": self.regimes,
        }


def split_dataframe(df: pd.DataFrame, train_ratio: float = 0.7) -> tuple[pd.DataFrame, pd.DataFrame]:
    split_idx = int(len(df) * train_ratio)
    split_idx = max(1, min(split_idx, len(df) - 1))
    return df.iloc[:split_idx], df.iloc[split_idx:]


def iter_rolling_folds(
    df: pd.DataFrame,
    cfg: RollingWalkForwardConfig,
) -> list[tuple[int, pd.DataFrame, pd.DataFrame]]:
    if df.empty:
        return []
    start = df.index.min()
    end = df.index.max()
    cursor = start
    folds: list[tuple[int, pd.DataFrame, pd.DataFrame]] = []
    fold_id = 0

    while True:
        train_end = cursor + pd.DateOffset(months=cfg.train_months)
        test_end = train_end + pd.DateOffset(months=cfg.test_months)
        if test_end > end:
            break
        train_df = df[(df.index >= cursor) & (df.index < train_end)]
        test_df = df[(df.index >= train_end) & (df.index < test_end)]
        if len(train_df) >= cfg.min_train_bars and len(test_df) >= cfg.min_test_bars:
            folds.append((fold_id, train_df, test_df))
            fold_id += 1
        cursor = cursor + pd.DateOffset(months=cfg.step_months)

    return folds


def _aggregate_oos_metrics(folds: list[WalkForwardFold]) -> dict:
    if not folds:
        return {}
    tests = [f.test for f in folds]
    total_trades = sum(t.metrics.trades for t in tests)
    total_return = sum(t.metrics.total_return_pct for t in tests) / len(tests)
    sharpe = sum(t.metrics.sharpe for t in tests) / len(tests)
    max_dd = max(t.metrics.max_drawdown_pct for t in tests)
    win_rate = sum(t.metrics.win_rate for t in tests) / len(tests)
    liquidated = any(t.metrics.liquidated for t in tests)
    return {
        "folds": len(folds),
        "avg_oos_return_pct": round(total_return, 4),
        "avg_oos_sharpe": round(sharpe, 4),
        "max_oos_drawdown_pct": round(max_dd, 4),
        "avg_oos_win_rate": round(win_rate, 4),
        "total_oos_trades": total_trades,
        "any_liquidated": liquidated,
    }


def _backtest_result_from_dict(d: dict) -> BacktestResult:
    m = d.get("metrics") or {}
    return BacktestResult(
        strategy=d.get("strategy", ""),
        symbol=d.get("symbol", ""),
        timeframe=d.get("timeframe", ""),
        params=d.get("params") or {},
        metrics=BacktestMetrics(
            total_return_pct=float(m.get("total_return_pct", 0)),
            cagr_pct=float(m.get("cagr_pct", 0)),
            sharpe=float(m.get("sharpe", 0)),
            max_drawdown_pct=float(m.get("max_drawdown_pct", 0)),
            win_rate=float(m.get("win_rate", 0)),
            profit_factor=float(m.get("profit_factor", 0)),
            trades=int(m.get("trades", 0)),
            liquidated=bool(m.get("liquidated", False)),
            final_equity=float(m.get("final_equity", 0)),
        ),
        split=d.get("split", "full"),
        mtf_bias=list(d.get("mtf_bias") or []),
        risk_stats=dict(d.get("risk_stats") or {}),
        execution_stats=dict(d.get("execution_stats") or {}),
    )


def _slice_df(df: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    return df[(df.index >= pd.Timestamp(start)) & (df.index < pd.Timestamp(end))]


def _train_combo_worker(payload: dict) -> dict:
    from borex.strategy.registry import _REGISTRY

    strategy_cls = _REGISTRY[payload["strategy_name"]]
    params = payload["params"]
    symbol = payload["symbol"]
    timeframe = payload["timeframe"]
    config = BacktestConfig(**payload["config"])
    df = load_ohlcv(symbol, timeframe)
    train_df = _slice_df(df, payload["train_start"], payload["train_end"])
    htf_dfs = None
    if is_mtf_strategy(strategy_cls):
        htf_dfs = load_bias_dfs(symbol, strategy_cls.mtf_spec().bias_timeframes)
    strategy = strategy_cls(params)
    result = BacktestEngine(config).run(
        strategy, train_df, symbol=symbol, timeframe=timeframe, split="train", htf_dfs=htf_dfs
    )
    score = getattr(result.metrics, payload["optimize_metric"], 0)
    return {"params": params, "score": float(score), "train": result.to_dict()}


def _best_params_on_train(
    strategy_cls: Type[Strategy],
    train_df: pd.DataFrame,
    *,
    symbol: str,
    timeframe: str,
    config: BacktestConfig,
    htf_dfs: dict | None,
    cfg: RollingWalkForwardConfig,
    sweep_workers: int = 1,
) -> tuple[dict, BacktestResult]:
    combos = param_combinations(
        strategy_cls,
        sweep_params=cfg.sweep_params,
        max_points=cfg.max_points,
        max_combos=cfg.max_combos,
        min_reward_risk_ratio=cfg.min_reward_risk_ratio,
    )
    best_params = strategy_cls().params
    best_result: BacktestResult | None = None
    best_score = float("-inf")
    config_dict = backtest_config_dict(config)
    train_start = str(train_df.index.min())
    train_end = str(train_df.index.max())

    if sweep_workers <= 1 or len(combos) <= 1:
        engine = BacktestEngine(config)
        for params in combos:
            strategy = strategy_cls(params)
            result = engine.run(
                strategy, train_df, symbol=symbol, timeframe=timeframe, split="train", htf_dfs=htf_dfs
            )
            score = getattr(result.metrics, cfg.optimize_metric, 0)
            if score > best_score:
                best_score = score
                best_params = params
                best_result = result
    else:
        jobs = [
            {
                "strategy_name": strategy_cls.name,
                "params": params,
                "symbol": symbol,
                "timeframe": timeframe,
                "config": config_dict,
                "train_start": train_start,
                "train_end": train_end,
                "optimize_metric": cfg.optimize_metric,
            }
            for params in combos
        ]
        with ProcessPoolExecutor(max_workers=sweep_workers) as pool:
            for raw in pool.map(_train_combo_worker, jobs, chunksize=1):
                if raw["score"] > best_score:
                    best_score = raw["score"]
                    best_params = raw["params"]
                    best_result = _backtest_result_from_dict(raw["train"])

    assert best_result is not None
    return best_params, best_result


def _wf_fold_worker(payload: dict) -> dict:
    from borex.strategy.registry import _REGISTRY

    wf = RollingWalkForwardConfig(**payload["wf"])
    train_w, _ = resolve_wf_workers(
        nested_job=wf.nested_job,
        train_sweep_workers=wf.train_sweep_workers,
        fold_workers=1,
        outer_workers=payload.get("outer_workers", 1),
    )
    strategy_cls = _REGISTRY[payload["strategy_name"]]
    base_params = payload.get("base_params") or {}
    symbol = payload["symbol"]
    timeframe = payload["timeframe"]
    bt_cfg = BacktestConfig(**payload["config"])
    df = load_ohlcv(symbol, timeframe)
    train_df = _slice_df(df, payload["train_start"], payload["train_end"])
    test_df = _slice_df(df, payload["test_start"], payload["test_end"])
    fold_id = int(payload["fold_id"])
    htf_dfs = None
    if is_mtf_strategy(strategy_cls):
        htf_dfs = load_bias_dfs(symbol, strategy_cls.mtf_spec().bias_timeframes)

    best_params = None
    if wf.optimize_on_train:
        best_params, train_result = _best_params_on_train(
            strategy_cls,
            train_df,
            symbol=symbol,
            timeframe=timeframe,
            config=bt_cfg,
            htf_dfs=htf_dfs,
            cfg=wf,
            sweep_workers=train_w,
        )
        test_strategy = strategy_cls(best_params)
    else:
        test_strategy = strategy_cls(base_params)
        train_result = BacktestEngine(bt_cfg).run(
            test_strategy,
            train_df,
            symbol=symbol,
            timeframe=timeframe,
            split="train",
            htf_dfs=htf_dfs,
        )

    test_result = BacktestEngine(bt_cfg).run(
        test_strategy,
        test_df,
        symbol=symbol,
        timeframe=timeframe,
        split=f"oos_{fold_id}",
        htf_dfs=htf_dfs,
    )
    return {
        "fold": fold_id,
        "train_start": payload["train_start"],
        "train_end": payload["train_end"],
        "test_start": payload["test_start"],
        "test_end": payload["test_end"],
        "best_params": best_params,
        "train": train_result.to_dict(),
        "test": test_result.to_dict(),
    }


def _run_single_wf_fold(
    fold_id: int,
    train_df: pd.DataFrame,
    test_df: pd.DataFrame,
    *,
    strategy_cls: Type[Strategy],
    strategy: Strategy,
    symbol: str,
    timeframe: str,
    bt_cfg: BacktestConfig,
    htf_dfs: dict | None,
    wf_cfg: RollingWalkForwardConfig,
    engine: BacktestEngine,
    train_sweep_workers: int,
) -> WalkForwardFold:
    best_params = None
    if wf_cfg.optimize_on_train:
        best_params, train_result = _best_params_on_train(
            strategy_cls,
            train_df,
            symbol=symbol,
            timeframe=timeframe,
            config=bt_cfg,
            htf_dfs=htf_dfs,
            cfg=wf_cfg,
            sweep_workers=train_sweep_workers,
        )
        test_strategy = strategy_cls(best_params)
    else:
        test_strategy = strategy_cls(strategy.params)
        train_result = engine.run(
            test_strategy,
            train_df,
            symbol=symbol,
            timeframe=timeframe,
            split="train",
            htf_dfs=htf_dfs,
        )

    test_result = engine.run(
        test_strategy,
        test_df,
        symbol=symbol,
        timeframe=timeframe,
        split=f"oos_{fold_id}",
        htf_dfs=htf_dfs,
    )
    return WalkForwardFold(
        fold=fold_id,
        train_start=str(train_df.index.min()),
        train_end=str(train_df.index.max()),
        test_start=str(test_df.index.min()),
        test_end=str(test_df.index.max()),
        train=train_result,
        test=test_result,
        best_params=best_params,
    )


def run_walk_forward(
    strategy: Strategy,
    symbol: str,
    timeframe: str,
    *,
    config: BacktestConfig | None = None,
    wf: WalkForwardConfig | None = None,
) -> tuple[BacktestResult, BacktestResult]:
    wf_cfg = wf or WalkForwardConfig()
    df = load_ohlcv(symbol, timeframe)
    train_df, test_df = split_dataframe(df, wf_cfg.train_ratio)

    if len(train_df) < wf_cfg.min_train_bars or len(test_df) < wf_cfg.min_test_bars:
        raise ValueError(
            f"Insufficient bars for walk-forward: train={len(train_df)} test={len(test_df)}"
        )

    engine = BacktestEngine(config)
    htf_dfs = None
    if is_mtf_strategy(type(strategy)):
        htf_dfs = load_bias_dfs(symbol, strategy.mtf_spec().bias_timeframes)
    train_result = engine.run(
        strategy, train_df, symbol=symbol, timeframe=timeframe, split="train", htf_dfs=htf_dfs
    )
    test_result = engine.run(
        strategy, test_df, symbol=symbol, timeframe=timeframe, split="test", htf_dfs=htf_dfs
    )
    return train_result, test_result


def run_rolling_walk_forward(
    strategy: Strategy,
    symbol: str,
    timeframe: str,
    *,
    config: BacktestConfig | None = None,
    wf: RollingWalkForwardConfig | None = None,
    save: bool = False,
    run_group: str | None = None,
    include_regimes: bool = True,
    outer_workers: int = 1,
) -> RollingWalkForwardSummary:
    wf_cfg = wf or RollingWalkForwardConfig()
    bt_cfg = config or BacktestConfig()
    df = load_ohlcv(symbol, timeframe)
    strategy_cls = type(strategy)
    htf_dfs = None
    if is_mtf_strategy(strategy_cls):
        htf_dfs = load_bias_dfs(symbol, strategy.mtf_spec().bias_timeframes)

    group = run_group or (
        datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_wf_") + uuid.uuid4().hex[:8]
    )
    dataset_hash = get_dataset_hash(symbol, timeframe)
    engine = BacktestEngine(bt_cfg)
    fold_specs = iter_rolling_folds(df, wf_cfg)
    train_sweep_w, fold_w = resolve_wf_workers(
        nested_job=wf_cfg.nested_job,
        train_sweep_workers=wf_cfg.train_sweep_workers,
        fold_workers=wf_cfg.fold_workers,
        outer_workers=outer_workers,
    )
    fold_results: list[WalkForwardFold] = []

    if fold_w > 1 and len(fold_specs) > 1:
        wf_payload = {
            "train_months": wf_cfg.train_months,
            "test_months": wf_cfg.test_months,
            "step_months": wf_cfg.step_months,
            "min_train_bars": wf_cfg.min_train_bars,
            "min_test_bars": wf_cfg.min_test_bars,
            "optimize_on_train": wf_cfg.optimize_on_train,
            "sweep_params": wf_cfg.sweep_params,
            "max_points": wf_cfg.max_points,
            "max_combos": wf_cfg.max_combos,
            "optimize_metric": wf_cfg.optimize_metric,
            "min_reward_risk_ratio": wf_cfg.min_reward_risk_ratio,
            "train_sweep_workers": wf_cfg.train_sweep_workers,
            "fold_workers": 1,
            "nested_job": wf_cfg.nested_job,
        }
        config_dict = backtest_config_dict(bt_cfg)
        jobs = [
            {
                "strategy_name": strategy.name,
                "base_params": dict(strategy.params),
                "symbol": symbol,
                "timeframe": timeframe,
                "config": config_dict,
                "wf": wf_payload,
                "fold_id": fold_id,
                "train_start": str(train_df.index.min()),
                "train_end": str(train_df.index.max()),
                "test_start": str(test_df.index.min()),
                "test_end": str(test_df.index.max()),
                "outer_workers": outer_workers,
            }
            for fold_id, train_df, test_df in fold_specs
        ]
        raw_folds: list[dict] = []
        with ProcessPoolExecutor(max_workers=fold_w) as pool:
            for raw in pool.map(_wf_fold_worker, jobs, chunksize=1):
                raw_folds.append(raw)
        raw_folds.sort(key=lambda r: r["fold"])
        for raw in raw_folds:
            train_result = _backtest_result_from_dict(raw["train"])
            test_result = _backtest_result_from_dict(raw["test"])
            best_params = raw.get("best_params")
            fold_results.append(
                WalkForwardFold(
                    fold=raw["fold"],
                    train_start=raw["train_start"],
                    train_end=raw["train_end"],
                    test_start=raw["test_start"],
                    test_end=raw["test_end"],
                    train=train_result,
                    test=test_result,
                    best_params=best_params,
                )
            )
            if save:
                for res, is_test in ((train_result, False), (test_result, True)):
                    row = res.to_dict()
                    row["dataset_hash"] = dataset_hash
                    if best_params and is_test:
                        row["params"] = best_params
                    save_result(row, run_group=group)
    else:
        for fold_id, train_df, test_df in fold_specs:
            fold = _run_single_wf_fold(
                fold_id,
                train_df,
                test_df,
                strategy_cls=strategy_cls,
                strategy=strategy,
                symbol=symbol,
                timeframe=timeframe,
                bt_cfg=bt_cfg,
                htf_dfs=htf_dfs,
                wf_cfg=wf_cfg,
                engine=engine,
                train_sweep_workers=train_sweep_w,
            )
            fold_results.append(fold)
            if save:
                for res in (fold.train, fold.test):
                    row = res.to_dict()
                    row["dataset_hash"] = dataset_hash
                    if fold.best_params and res.split.startswith("oos"):
                        row["params"] = fold.best_params
                    save_result(row, run_group=group)

    regimes: dict = {}
    if include_regimes and fold_results:
        full = get_strategy(strategy.name, strategy.params)
        full_result = engine.run(full, df, symbol=symbol, timeframe=timeframe, htf_dfs=htf_dfs)
        regimes = analyze_trades_by_regime(full_result, df)

    summary = RollingWalkForwardSummary(
        strategy=strategy.name,
        symbol=symbol,
        timeframe=timeframe,
        folds=fold_results,
        oos_metrics=_aggregate_oos_metrics(fold_results),
        regimes=regimes,
    )
    return summary
