from __future__ import annotations

from enum import Enum


class EntryMode(str, Enum):
    """
    How a strategy translates signals into broker orders.

    GHOST: setup queues a pending level; live service places MT5 pending
           limit/stop at the ghost SL (alexg4/5/6).
    IMMEDIATE: on_bar signal → market order with SL/TP (alexg3 and future).
    """

    GHOST = "ghost"
    IMMEDIATE = "immediate"
