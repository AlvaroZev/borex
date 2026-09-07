from datetime import datetime, timezone

from borex.data.mt5_time import mt5_unix_to_utc, utc_to_mt5_request


def test_mt5_unix_to_utc_zero_offset():
    ts = mt5_unix_to_utc(1_700_000_000, 0)
    assert ts.tzinfo is not None
    assert ts.utcoffset().total_seconds() == 0


def test_mt5_unix_to_utc_plus_three_hours():
    # Server wall 13:00 encoded as if UTC → real UTC is 10:00 when offset=3h.
    offset = 3 * 3600
    labeled = datetime(2026, 8, 13, 13, 0, tzinfo=timezone.utc)
    unix = int(labeled.timestamp())
    got = mt5_unix_to_utc(unix, offset)
    assert got == datetime(2026, 8, 13, 10, 0, tzinfo=timezone.utc)


def test_utc_to_mt5_request_roundtrip():
    utc = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)
    offset = 3 * 3600
    req = utc_to_mt5_request(utc, offset)
    assert req == datetime(2026, 8, 13, 15, 0, tzinfo=timezone.utc)


def test_snap_server_offset_to_whole_hours():
    from borex.data.mt5_time import snap_h1_open, snap_server_offset_seconds

    assert snap_server_offset_seconds(90) == 0
    assert snap_server_offset_seconds(3 * 3600) == 3 * 3600
    assert snap_server_offset_seconds(2 * 3600 + 10 * 60) == 2 * 3600
    assert snap_server_offset_seconds(2 * 3600 + 45 * 60) == 3 * 3600
    # PC timezone / weekend-stale ticks must not become a 14h shift
    assert snap_server_offset_seconds(-14 * 3600) == 0
    assert snap_server_offset_seconds(8 * 3600) == 0
    ts = snap_h1_open(datetime(2026, 8, 29, 12, 45, tzinfo=timezone.utc))
    assert ts == datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)


def test_icmarkets_spread_clamp_rejects_weekend_blowout():
    from borex.backtest.costs import (
        clamp_spread_pips,
        spread_pips_from_quote,
        typical_icmarkets_raw_spread_pips,
    )

    assert typical_icmarkets_raw_spread_pips("EURUSD=X") == 0.10
    assert typical_icmarkets_raw_spread_pips("USDZAR=X") == 10.0
    # Friday last quote is usable
    assert clamp_spread_pips(0.12, 0.10) == 0.12
    # Saturday 8-pip EURUSD is not
    assert clamp_spread_pips(8.0, 0.10) == 0.10
    eurusd = spread_pips_from_quote(bid=1.16000, ask=1.16001, symbol="EURUSD=X")
    assert abs(eurusd - 0.10) < 1e-9
    usdjpy = spread_pips_from_quote(bid=147.000, ask=147.010, symbol="USDJPY=X")
    assert abs(usdjpy - 1.0) < 1e-9


def test_intra_hour_tp_without_wick_sl():
    from datetime import datetime, timezone

    from borex.backtest.multi_market_engine import MultiMarketEngine
    from borex.backtest.portfolio import PositionSide, Trade
    from borex.models.candle import Candle

    candle = Candle(
        timestamp=datetime(2026, 8, 27, 12, tzinfo=timezone.utc),
        open=1.1000,
        high=1.1200,
        low=1.0800,
        close=1.1050,
        volume=1,
    )
    trade = Trade(
        side=PositionSide.LONG,
        entry_index=0,
        entry_price=1.1000,
        entry_time=candle.timestamp,
        pattern="t",
        stop_loss=1.0900,
        take_profit=1.1150,
    )
    sl_wick = MultiMarketEngine._hit_stop(trade, candle, 1.0900, wick=True)
    # Wick tags SL; close is still above it.
    assert sl_wick
    assert not MultiMarketEngine._hit_stop(trade, candle, 1.0900, wick=False)
    # Same bar also tags TP — engine must take SL, not TP.
    assert sl_wick and MultiMarketEngine._hit_take_profit(trade, candle)

    from borex.alexg import AlexG8Strategy
    from borex.backtest.engine import BacktestConfig
    from borex.backtest.multi_portfolio import MultiMarketPortfolio
    from borex.models.candle import SignalAction

    engine = MultiMarketEngine(
        AlexG8Strategy(),
        BacktestConfig(
            intra_hour_sl=False,
            size_mode="margin",
            leverage=5000.0,
            true_sl=True,
        ),
    )
    pf = MultiMarketPortfolio(
        initial_capital=1000.0,
        position_size_pct=0.01,
        leverage=5000.0,
        size_mode="margin",
    )
    pf.open_position(
        "EURUSD=X",
        SignalAction.BUY,
        0,
        1.1000,
        candle.timestamp,
        "t",
        stop_loss=1.0900,
        take_profit=1.1150,
    )
    assert engine._check_exit(pf, "EURUSD=X", 1, candle)
    assert pf.closed_trades[-1].exit_reason == "margin_stop"


def _m1(ts, o, h, l, c):
    from borex.models.candle import Candle

    return Candle(timestamp=ts, open=o, high=h, low=l, close=c, volume=1)


def test_ltf_hour_slice_and_tp_before_sl():
    from datetime import datetime, timedelta, timezone

    from borex.alexg import AlexG8Strategy
    from borex.backtest.engine import BacktestConfig
    from borex.backtest.multi_market_engine import (
        MultiMarketEngine,
        index_ltf_bars,
        ltf_bars_in_hour,
    )
    from borex.backtest.multi_portfolio import MultiMarketPortfolio
    from borex.models.candle import Candle, SignalAction

    hour = datetime(2026, 8, 27, 12, tzinfo=timezone.utc)
    h1 = Candle(
        timestamp=hour, open=1.1000, high=1.1200, low=1.0800, close=1.1050, volume=1
    )
    minutes = [
        _m1(hour + timedelta(minutes=i), 1.1000, 1.1005, 1.1000, 1.1002)
        for i in range(60)
    ]
    # TP prints at :10, SL only later at :40
    minutes[10] = _m1(hour + timedelta(minutes=10), 1.1002, 1.1160, 1.1000, 1.1140)
    minutes[40] = _m1(hour + timedelta(minutes=40), 1.1140, 1.1142, 1.0880, 1.0900)

    indexed = index_ltf_bars({"EURUSD=X": minutes})["EURUSD=X"]
    assert len(ltf_bars_in_hour(indexed, hour)) == 60

    engine = MultiMarketEngine(
        AlexG8Strategy(),
        BacktestConfig(intra_hour_sl=False, size_mode="margin", leverage=5000.0),
    )
    pf = MultiMarketPortfolio(
        initial_capital=1000.0,
        position_size_pct=0.01,
        leverage=5000.0,
        size_mode="margin",
    )
    pf.open_position(
        "EURUSD=X",
        SignalAction.BUY,
        0,
        1.1000,
        hour,
        "t",
        stop_loss=1.0900,
        take_profit=1.1150,
    )
    assert engine._check_exit_path(pf, "EURUSD=X", 1, h1, indexed)
    assert pf.closed_trades[-1].exit_reason == "take_profit"
    assert pf.closed_trades[-1].exit_time == minutes[10].timestamp
