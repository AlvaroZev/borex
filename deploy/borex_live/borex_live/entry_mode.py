from __future__ import annotations

from enum import Enum


class EntryMode(str, Enum):
    """
    How a strategy translates signals into broker orders.

    GHOST: setup queues a ghost SL in DB/strategy only (no resting MT5 limit).
           On a later *closed* H1 bar, if strategy confirms the SL tag,
           live places a market order + protective SL/TP (H1 backtest parity).
    IMMEDIATE: on_bar signal → market order with SL/TP (alexg3 and future).
    """

    GHOST = "ghost"
    IMMEDIATE = "immediate"
