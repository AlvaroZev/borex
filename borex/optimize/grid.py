from __future__ import annotations

import itertools
from typing import Type

from borex.models.params import ParamDef
from borex.strategy.base import Strategy


def reward_risk_ratio(params: dict) -> float | None:
    """Return take-profit distance / stop-loss distance from param dict."""
    sl = params.get("sl_pct")
    tp = params.get("tp_pct")
    if sl is None or tp is None:
        return None
    sl_f, tp_f = float(sl), float(tp)
    if sl_f <= 0:
        return None
    return tp_f / sl_f


def params_passes_reward_risk(params: dict, min_rr: float) -> bool:
    if min_rr <= 0:
        return True
    rr = reward_risk_ratio(params)
    if rr is None:
        return True
    return rr >= min_rr


def bounded_grid(param: ParamDef, max_points: int = 8) -> list:
    """Sample param grid to at most max_points evenly spaced values."""
    vals = param.grid_values()
    if len(vals) <= max_points:
        return vals
    if max_points <= 1:
        return [param.default]
    step = (len(vals) - 1) / (max_points - 1)
    picked: list = []
    seen_set: set = set()
    for i in range(max_points):
        v = vals[int(round(i * step))]
        key = repr(v)
        if key not in seen_set:
            seen_set.add(key)
            picked.append(v)
    return picked


def param_combinations(
    strategy_cls: Type[Strategy],
    *,
    sweep_params: list[str] | None = None,
    max_points: int = 8,
    max_combos: int = 500,
    min_reward_risk_ratio: float = 0.0,
) -> list[dict]:
    """Cartesian product of strategy param grids (bounded for sweeps)."""
    schema = strategy_cls.param_schema()
    if sweep_params:
        names = set(sweep_params)
        schema = [p for p in schema if p.name in names]
    base = strategy_cls().params
    if not schema:
        return [base]

    grids = [bounded_grid(p, max_points=max_points) for p in schema]
    names = [p.name for p in schema]
    combos: list[dict] = []
    for values in itertools.product(*grids):
        params = dict(base)
        for name, val in zip(names, values):
            params[name] = val
        if not params_passes_reward_risk(params, min_reward_risk_ratio):
            continue
        combos.append(params)

    if not combos:
        if params_passes_reward_risk(base, min_reward_risk_ratio):
            return [base]
        return [base]

    if len(combos) > max_combos:
        raise ValueError(
            f"Param grid too large ({len(combos)} combos after R:R filter). "
            f"Narrow --sweep-params or reduce --max-combos (max {max_combos})."
        )
    return combos
