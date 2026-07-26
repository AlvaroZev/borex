from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from borex_live.entry_mode import EntryMode
from borex_live.paths import ensure_borex_main_on_path


@dataclass(frozen=True)
class StrategySpec:
    name: str
    entry_mode: EntryMode
    factory: Callable[..., Any]


def _ghost_kw(params: dict[str, Any]) -> dict[str, Any]:
    return {
        "min_rr": params.get("min_rr", 2.0),
        "tp_fraction": params.get("tp_fraction", 1.0),
        "strength_lookback": params.get("strength_lookback", 24),
        "min_currency_edge": params.get("min_currency_edge", 0.00005),
        "min_confirming_pairs": params.get("min_confirming_pairs", 2),
        "filter_false_positives": params.get("filter_false_positives", True),
    }


def build_strategy_registry() -> dict[str, StrategySpec]:
    ensure_borex_main_on_path()
    from borex.alexg import (
        AlexG3Strategy,
        AlexG4Strategy,
        AlexG5Strategy,
        AlexG6Strategy,
    )

    def g3(**kw):
        return AlexG3Strategy(**_ghost_kw(kw))

    def g4(**kw):
        return AlexG4Strategy(**_ghost_kw(kw))

    def g5(**kw):
        return AlexG5Strategy(**_ghost_kw(kw))

    def g6(**kw):
        return AlexG6Strategy(
            **_ghost_kw(kw),
            second_signal=kw.get("second_signal", "off"),
        )

    return {
        "alexg3": StrategySpec("alexg3", EntryMode.IMMEDIATE, g3),
        "alexg4": StrategySpec("alexg4", EntryMode.GHOST, g4),
        "alexg5": StrategySpec("alexg5", EntryMode.GHOST, g5),
        "alexg6": StrategySpec("alexg6", EntryMode.GHOST, g6),
    }


def create_strategy(name: str, **params: Any):
    registry = build_strategy_registry()
    spec = registry.get(name)
    if spec is None:
        names = ", ".join(sorted(registry))
        raise ValueError(f"Unknown strategy {name!r}. Choose: {names}")
    return spec.factory(**params), spec


def get_entry_mode(name: str) -> EntryMode:
    registry = build_strategy_registry()
    spec = registry.get(name)
    if spec is None:
        raise ValueError(f"Unknown strategy {name!r}")
    return spec.entry_mode
