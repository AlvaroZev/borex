"""Smoke tests for Internet1–20 factory."""

from __future__ import annotations

from borex.alexg.internet.strategies import INTERNET_FNS, internet1_donchian20
from borex.models.candle import Candle


def _bars(n=80):
    out = []
    px = 1.1
    for i in range(n):
        px += 0.0001 * ((i % 7) - 3)
        out.append(
            Candle(
                f"2026-06-02T{i%24:02d}:00:00+00:00" if i < 24 else f"2026-06-0{3+i//24}T{i%24:02d}:00:00+00:00",
                px,
                px + 0.001,
                px - 0.001,
                px,
            )
        )
    return out


def test_internet_registry_has_20():
    assert len(INTERNET_FNS) == 20


def test_internet1_deterministic():
    bars = _bars(90)
    a = internet1_donchian20(bars, 70)
    b = internet1_donchian20(bars, 70)
    assert a == b


def test_internet_fns_no_crash_emptyish():
    bars = _bars(100)
    for name, fn in INTERNET_FNS.items():
        fn(bars, 80)  # may return None
