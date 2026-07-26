"""Forex symbols and Dukascopy cache directory names.

Universe matches the 60 IC Markets SC Demo Forex symbols from MetaTrader 5.
"""

FOREX_PAIRS: list[str] = [
    # Majors
    "EURUSD=X",
    "GBPUSD=X",
    "USDJPY=X",
    "AUDUSD=X",
    "USDCAD=X",
    "USDCHF=X",
    "NZDUSD=X",
    # Liquid crosses (original + second tier)
    "EURGBP=X",
    "EURJPY=X",
    "GBPJPY=X",
    "EURCHF=X",
    "EURCAD=X",
    "EURAUD=X",
    "EURNZD=X",
    "GBPCAD=X",
    "AUDJPY=X",
    "NZDJPY=X",
    "CADJPY=X",
    "CHFJPY=X",
    "AUDCAD=X",
    # Remaining MT5 minors / crosses
    "AUDCHF=X",
    "AUDNZD=X",
    "CADCHF=X",
    "GBPAUD=X",
    "GBPCHF=X",
    "GBPNZD=X",
    "NZDCAD=X",
    "NZDCHF=X",
    "USDSGD=X",
    # MT5 exotics
    "AUDSGD=X",
    "CHFSGD=X",
    "EURDKK=X",
    "EURHKD=X",
    "EURNOK=X",
    "EURPLN=X",
    "EURSEK=X",
    "EURSGD=X",
    "EURTRY=X",
    "EURZAR=X",
    "GBPDKK=X",
    "GBPNOK=X",
    "GBPSEK=X",
    "GBPSGD=X",
    "GBPTRY=X",
    "NOKJPY=X",
    "NOKSEK=X",
    "SEKJPY=X",
    "SGDJPY=X",
    "USDCNH=X",
    "USDCZK=X",
    "USDDKK=X",
    "USDHKD=X",
    "USDHUF=X",
    "USDMXN=X",
    "USDNOK=X",
    "USDPLN=X",
    "USDSEK=X",
    "USDTHB=X",
    "USDTRY=X",
    "USDZAR=X",
]

# First 20 already downloaded earlier; remaining 40 are the expansion set.
MT5_EXTRA_PAIRS: list[str] = FOREX_PAIRS[20:]

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
