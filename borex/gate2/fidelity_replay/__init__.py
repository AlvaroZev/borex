"""Gate 2 Level 5 — exact replay fidelity (Theory event → Borex simulator).

Read-only research layer. Does not connect to MT5, place orders, or tune strategy.
"""

from __future__ import annotations

from borex.gate2.fidelity_replay.runner import run_fidelity_replay

__all__ = ["run_fidelity_replay"]
