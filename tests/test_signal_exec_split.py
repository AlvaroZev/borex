"""Signal scan must not be muted by an open sim trade."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from borex.backtest.engine import BacktestConfig
from borex.backtest.multi_market_engine import MultiMarketEngine
from borex.models.candle import Candle, Signal, SignalAction


@dataclass
class SpyStrategy:
    name: str = "spy"
    min_bars: int = 0
    strength_lookback: int = 1
    min_currency_edge: float = 0.0
    min_confirming_pairs: int = 0
    min_rr: float = 2.0
    calls: list[tuple[str, int]] = field(default_factory=list)
    _sym: str = ""

    def set_context(self, symbol: str, ctx) -> None:
        self._sym = symbol

    def on_bar(self, index: int, candles: list[Candle], mtf=None) -> Signal:
        self.calls.append((self._sym, index))
        c = candles[index]
        return Signal(
            action=SignalAction.BUY,
            pattern="spy|g:1|fill:close",
            index=index,
            price=c.close,
            timestamp=c.timestamp,
            stop_loss=c.close * 0.5,
            take_profit=c.close * 2.0,
            score=2.0,
        )


def _bars(symbol: str, n: int, px: float) -> list[Candle]:
    t0 = datetime(2026, 8, 3, 8, tzinfo=timezone.utc)
    out = []
    for i in range(n):
        p = px + i * 0.0001
        out.append(
            Candle(
                timestamp=t0 + timedelta(hours=i),
                open=p,
                high=p + 0.0002,
                low=p - 0.0002,
                close=p,
                volume=1,
            )
        )
    return out


def test_scan_keeps_running_while_pair_is_in_a_trade():
    spy = SpyStrategy()
    engine = MultiMarketEngine(
        spy,
        BacktestConfig(
            initial_capital=10_000.0,
            leverage=10.0,
            position_size_pct=0.01,
            size_mode="margin",
            true_sl=False,
            intra_hour_sl=False,
            force_flat_daily=False,
        ),
        max_positions=0,
    )
    data = {
        "EURUSD=X": _bars("EURUSD=X", 5, 1.10),
        "GBPUSD=X": _bars("GBPUSD=X", 5, 1.30),
    }
    tape: list[dict] = []
    result = engine.run(data, timeframe="1h", collect_decisions=tape)
    # 5 bars × 2 pairs — occupancy must not skip on_bar
    assert len(spy.calls) == 10
    assert len(tape) == 10
    # One fill per pair; later intents are rejected, not un-scanned
    by_sym = {}
    for t in result.trades:
        by_sym.setdefault(t.symbol, 0)
        by_sym[t.symbol] += 1
    assert by_sym["EURUSD=X"] == 1
    assert by_sym["GBPUSD=X"] == 1
