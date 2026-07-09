from __future__ import annotations

from dataclasses import dataclass

from borex.alexg.strategy4 import AlexG4Strategy


@dataclass
class AlexG5Strategy(AlexG4Strategy):
    """
    AlexG5 — AlexG4 entries with margin-stop based exit geometry.

    Execution model is AlexG4 (late entry at SL retest).
    The backtest engine then applies margin-stop SL and computes TP from
    winrate-derived RR when running in margin mode.
    """

    name: str = "alexg5"
