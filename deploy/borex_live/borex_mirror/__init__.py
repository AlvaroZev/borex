"""Theory-driven mirror: one bar processor for bulk backtest and live H1 feeds.

Authority = ShadowEngine / MultiMarketEngine.
On each closed master bar, newly opened theory trades are mirrored to MT5
(or paper) at market with the same SL/TP pip distances from the fill price.
"""

from __future__ import annotations

__version__ = "0.1.0"
