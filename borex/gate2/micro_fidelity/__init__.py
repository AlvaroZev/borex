"""Gate 2 Level-4 micro fidelity experiment (NOT alexg / production strategies).

Purpose: high-frequency deterministic 1m signal → Theory → Mirror broker →
reconciliation evidence. Economically meaningless by design.
"""

from borex.gate2.micro_fidelity.config import (
    FROZEN_CONFIG,
    STRATEGY_NAME,
    STRATEGY_VERSION,
    MicroFidelityConfig,
)
from borex.gate2.micro_fidelity.strategy import MicroFidelitySignal, evaluate_completed_bar

__all__ = [
    "FROZEN_CONFIG",
    "STRATEGY_NAME",
    "STRATEGY_VERSION",
    "MicroFidelityConfig",
    "MicroFidelitySignal",
    "evaluate_completed_bar",
]
