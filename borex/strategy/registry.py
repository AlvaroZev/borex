from __future__ import annotations

from typing import Type

from borex.strategy.base import Strategy
from borex.strategy.daytrading import ALL_STRATEGIES as DAYTRADING
from borex.strategy.examples.rsi_revert import RsiMeanRevert
from borex.strategy.examples.sma_cross import SmaCross
from borex.strategy.mtf import is_mtf_strategy

_REGISTRY: dict[str, Type[Strategy]] = {}


def register(strategy_cls: Type[Strategy]) -> Type[Strategy]:
    _REGISTRY[strategy_cls.name] = strategy_cls
    return strategy_cls


def get_strategy(name: str, params: dict | None = None) -> Strategy:
    if name not in _REGISTRY:
        raise KeyError(f"Unknown strategy: {name}. Available: {sorted(_REGISTRY)}")
    return _REGISTRY[name](params)


def list_strategies() -> list[dict]:
    out: list[dict] = []
    for cls in sorted(_REGISTRY.values(), key=lambda c: c.name):
        item: dict = {
            "name": cls.name,
            "schema": [p.to_dict() for p in cls.param_schema()],
        }
        if is_mtf_strategy(cls):
            spec = cls.mtf_spec()
            item["mtf"] = {
                "bias_timeframes": list(spec.bias_timeframes),
                "entry_timeframes": list(spec.entry_timeframes),
            }
        out.append(item)
    return out


def _bootstrap() -> None:
    for cls in (SmaCross, RsiMeanRevert, *DAYTRADING):
        register(cls)


_bootstrap()
