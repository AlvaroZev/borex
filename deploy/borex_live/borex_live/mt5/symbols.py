from __future__ import annotations


def yahoo_to_mt5(yahoo: str) -> str:
    """EURUSD=X → EURUSD (ICMarkets-style names)."""
    raw = yahoo.strip().upper().replace("=X", "").replace("=x", "").replace("/", "")
    return raw


def mt5_to_yahoo(mt5: str) -> str:
    """EURUSD → EURUSD=X (canonical borex / Yahoo-style key)."""
    raw = yahoo_to_mt5(mt5)
    return f"{raw}=X"
