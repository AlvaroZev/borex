"""Forex symbols and Dukascopy cache directory names."""

FOREX_PAIRS: list[str] = [
    "EURUSD=X",
    "GBPUSD=X",
    "USDJPY=X",
    "AUDUSD=X",
    "USDCAD=X",
    "USDCHF=X",
    "NZDUSD=X",
    "EURGBP=X",
    "EURJPY=X",
    "GBPJPY=X",
]

_CACHE_LOOKUP: dict[str, str] = {}


def cache_dir_name(symbol: str) -> str:
    return symbol.replace("=", "").replace("/", "_")


def to_canonical(symbol: str) -> str:
    if symbol in FOREX_PAIRS:
        return symbol
    for sym in FOREX_PAIRS:
        if cache_dir_name(sym).upper() == symbol.upper():
            return sym
    if symbol.endswith("X") and not symbol.endswith("=X"):
        candidate = f"{symbol[:-1]}=X"
        if candidate in FOREX_PAIRS:
            return candidate
    return symbol


def from_cache_dir(name: str) -> str:
    if not _CACHE_LOOKUP:
        _CACHE_LOOKUP.update({cache_dir_name(s): s for s in FOREX_PAIRS})
    if name in _CACHE_LOOKUP:
        return _CACHE_LOOKUP[name]
    return to_canonical(name)
