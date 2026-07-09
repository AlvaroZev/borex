from borex.strategy.daytrading.aoi_bounce import AoiBounce
from borex.strategy.daytrading.mtf_strategies import MTF_STRATEGIES as _MTF_STRATEGIES
from borex.strategy.daytrading.strategies import ALL_STRATEGIES as SINGLE_TF_STRATEGIES

MTF_STRATEGIES = [AoiBounce, *_MTF_STRATEGIES]
ALL_STRATEGIES = [*MTF_STRATEGIES, *SINGLE_TF_STRATEGIES]

__all__ = ["ALL_STRATEGIES", "AoiBounce", "MTF_STRATEGIES", "SINGLE_TF_STRATEGIES"]
