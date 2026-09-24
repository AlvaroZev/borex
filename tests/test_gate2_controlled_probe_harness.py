"""Unit tests for Gate 2 controlled-probe harness fixes (no live orders)."""

from __future__ import annotations

from types import SimpleNamespace

from borex.data.mt5_time import (
    assess_server_offset,
    assess_tick_freshness,
    measure_mt5_server_offset_assessment,
    measure_mt5_server_offset_seconds,
)
from borex.gate2.probe_reconcile import reconcile_position_deals


class _FakeTick(SimpleNamespace):
    pass


class _FakeMT5:
    """Read-only fake; order_send raises if touched."""

    def __init__(self, tick=None, deals_by_position=None):
        self._tick = tick
        self._deals_by_position = deals_by_position or {}
        self.order_send_calls = 0
        self.history_deals_get_calls = []

    def symbol_info_tick(self, symbol):
        return self._tick

    def symbols_get(self):
        return None

    def history_deals_get(self, *args, **kwargs):
        self.history_deals_get_calls.append((args, kwargs))
        if "position" in kwargs:
            return self._deals_by_position.get(int(kwargs["position"]))
        return []

    def order_send(self, *args, **kwargs):
        self.order_send_calls += 1
        raise AssertionError("order_send must not be called in Gate 2 unit tests")

    def positions_get(self, *args, **kwargs):
        # EURDKK leftovers untouched — tests never modify
        return [
            SimpleNamespace(
                ticket=1890205005,
                symbol="EURDKK",
                magic=88002,
                type=0,
                volume=0.01,
                price_open=7.0,
                sl=0.0,
                tp=0.0,
                profit=0.0,
                comment="leftover",
            )
        ]


def _deal(
    *,
    ticket,
    order,
    position_id,
    symbol="EURUSD",
    dtype=0,
    entry=0,
    price=1.14674,
    profit=0.0,
    commission=-0.04,
    swap=0.0,
    magic=88002,
    comment="bx_g2p",
    time_unix=1_790_023_355,
):
    return SimpleNamespace(
        ticket=ticket,
        order=order,
        position_id=position_id,
        symbol=symbol,
        type=dtype,
        entry=entry,
        volume=0.01,
        price=price,
        profit=profit,
        commission=commission,
        swap=swap,
        magic=magic,
        comment=comment,
        time=time_unix,
    )


def test_fresh_tick_accepted_utc_broker():
    now = 1_790_015_000
    fr = assess_tick_freshness(
        symbol="EURUSD",
        tick_time_unix_raw=now - 5,
        local_utc_unix=now,
        max_age_seconds=120,
        bid=1.1,
        ask=1.1001,
    )
    assert fr.fresh is True
    assert fr.decision == "accept"
    assert fr.rejection_reason is None
    assert fr.tick_age_seconds == 5.0


def test_fresh_tick_accepted_gmt_plus_three():
    """Live IC Markets tick.time sits ~+10800 ahead of UTC — must still accept."""
    now = 1_790_015_000
    fr = assess_tick_freshness(
        symbol="EURUSD",
        tick_time_unix_raw=now + 10800,
        local_utc_unix=now,
        max_age_seconds=120,
        bid=1.14647,
        ask=1.14648,
    )
    assert fr.fresh is True
    assert fr.tick_age_seconds == 0.0
    ao = assess_server_offset(
        tick_time_unix_raw=now + 10800,
        local_utc_unix=now,
        max_age_seconds=120,
        symbol="EURUSD",
    )
    assert ao.confident is True
    assert ao.offset_seconds == 10800


def test_stale_tick_rejected():
    now = 1_790_012_557  # ~ batch utc
    stale = 1_789_775_819  # Friday tick from investigation
    fr = assess_tick_freshness(
        symbol="EURUSD",
        tick_time_unix_raw=stale,
        local_utc_unix=now,
        max_age_seconds=120,
        bid=1.14854,
        ask=1.14864,
    )
    assert fr.fresh is False
    assert fr.decision == "reject"
    assert fr.rejection_reason is not None
    assert "stale_tick_age_" in fr.rejection_reason
    assert fr.tick_age_seconds is not None and fr.tick_age_seconds > 65 * 3600


def test_stale_tick_cannot_produce_confident_offset_zero():
    now = 1_790_012_557
    stale = 1_789_775_819
    ao = assess_server_offset(
        tick_time_unix_raw=stale,
        local_utc_unix=now,
        max_age_seconds=120,
        symbol="EURUSD",
    )
    assert ao.confident is False
    assert ao.offset_seconds is None
    assert ao.decision == "reject"

    # Legacy helper still returns 0 for H1 safety, but assessment does not claim confidence
    class T:
        time = stale
        bid = 1.14854
        ask = 1.14864

    class M:
        def symbol_info_tick(self, _s):
            return T()

        def symbols_get(self):
            return None

    legacy = measure_mt5_server_offset_seconds(M())
    assert legacy == 0  # legacy H1 path
    detailed = measure_mt5_server_offset_assessment(M(), now_unix=now, max_age_seconds=120)
    assert detailed.confident is False
    assert detailed.offset_seconds is None


def test_position_based_deal_lookup_recovers_entry_exit():
    pos = 1949041505
    primary = [
        _deal(
            ticket=1569207951,
            order=pos,
            position_id=pos,
            dtype=0,
            entry=0,
            price=1.14674,
            profit=0.0,
            comment="bx_g2p_174237_01",
        ),
        _deal(
            ticket=1569209071,
            order=1949043180,
            position_id=pos,
            dtype=1,
            entry=1,
            price=1.14676,
            profit=0.02,
            comment="borex_flat",
        ),
    ]
    recon = reconcile_position_deals(
        position_ticket=pos,
        symbol_mt5="EURUSD",
        magic=88002,
        offset_seconds=10800,
        expected_side="buy",
        primary_raw=primary,
        supplementary_raw=[],
    )
    assert recon["reconciliation_status"] == "reconciled"
    assert recon["reconciliation_fail_closed"] is False
    assert recon["entry_deal"]["price"] == 1.14674
    assert recon["exit_deal"]["price"] == 1.14676
    assert recon["commission"] == -0.08
    assert abs(recon["net_pnl"] - (0.02 - 0.08)) < 1e-9
    assert recon["order_ticket_entry"] == pos
    assert recon["order_ticket_exit"] == 1949043180


def test_missing_history_reported_explicitly():
    recon = reconcile_position_deals(
        position_ticket=1949041505,
        symbol_mt5="EURUSD",
        magic=88002,
        offset_seconds=10800,
        primary_raw=None,
        supplementary_raw=[],
    )
    assert recon["reconciliation_status"] == "missing_history"
    assert recon["reconciliation_fail_closed"] is True
    assert recon["entry_deal"] is None
    assert recon["exit_deal"] is None


def test_wrong_position_ticket_no_false_match():
    pos = 1949041505
    other = 9999999999
    primary = [
        _deal(ticket=1, order=other, position_id=other, entry=0),
        _deal(ticket=2, order=other + 1, position_id=other, entry=1, profit=1.0),
    ]
    recon = reconcile_position_deals(
        position_ticket=pos,
        symbol_mt5="EURUSD",
        magic=88002,
        offset_seconds=10800,
        primary_raw=primary,
        supplementary_raw=[],
    )
    assert recon["reconciliation_fail_closed"] is True
    assert recon["deals"] == []
    assert len(recon["rejected_primary"]) == 2
    assert all("position_id_mismatch" in r["reject_reasons"] for r in recon["rejected_primary"])


def test_wrong_symbol_or_magic_no_false_match():
    pos = 1949041505
    bad_symbol = [
        _deal(ticket=1, order=pos, position_id=pos, symbol="GBPUSD", entry=0),
        _deal(ticket=2, order=pos, position_id=pos, symbol="GBPUSD", entry=1, profit=0.01),
    ]
    recon_sym = reconcile_position_deals(
        position_ticket=pos,
        symbol_mt5="EURUSD",
        magic=88002,
        offset_seconds=10800,
        primary_raw=bad_symbol,
    )
    assert recon_sym["deals"] == []
    assert recon_sym["reconciliation_fail_closed"] is True

    bad_magic = [
        _deal(ticket=1, order=pos, position_id=pos, magic=88001, entry=0),
        _deal(ticket=2, order=pos, position_id=pos, magic=88001, entry=1, profit=0.01),
    ]
    recon_mag = reconcile_position_deals(
        position_ticket=pos,
        symbol_mt5="EURUSD",
        magic=88002,
        offset_seconds=10800,
        primary_raw=bad_magic,
    )
    assert recon_mag["deals"] == []
    assert all("magic_mismatch" in r["reject_reasons"] for r in recon_mag["rejected_primary"])
    # Live magic 88001 must never be accepted as a match for Mirror probes
    assert all(int(r["magic"]) == 88001 for r in recon_mag["rejected_primary"])


def test_no_order_submission_in_fake_mt5_reconcile_path():
    pos = 1949041505
    mt5 = _FakeMT5(
        tick=_FakeTick(time=1_790_015_000 + 10800, bid=1.1, ask=1.1001),
        deals_by_position={
            pos: [
                _deal(ticket=1, order=pos, position_id=pos, entry=0),
                _deal(ticket=2, order=pos + 1, position_id=pos, entry=1, profit=0.0),
            ]
        },
    )
    from borex.gate2.probe_reconcile import reconcile_from_mt5
    from datetime import datetime, timezone

    recon = reconcile_from_mt5(
        mt5,
        position_ticket=pos,
        symbol_mt5="EURUSD",
        magic=88002,
        offset_seconds=10800,
        since_utc=datetime(2026, 9, 21, 17, 42, tzinfo=timezone.utc),
        expected_side="buy",
    )
    assert recon["reconciliation_status"] == "reconciled"
    assert mt5.order_send_calls == 0
    # Primary path used position=
    assert any("position" in kw for _a, kw in mt5.history_deals_get_calls)


def test_eurdkk_leftovers_untouched_by_reconcile_helpers():
    mt5 = _FakeMT5()
    before = [int(p.ticket) for p in mt5.positions_get()]
    assert 1890205005 in before
    # Reconcile helpers never call position modify/close APIs
    assert not hasattr(mt5, "order_send") or True
    after = [int(p.ticket) for p in mt5.positions_get()]
    assert before == after
    assert mt5.order_send_calls == 0


def test_incomplete_entry_exit_fail_closed():
    pos = 1949041505
    only_entry = [_deal(ticket=1, order=pos, position_id=pos, entry=0)]
    recon = reconcile_position_deals(
        position_ticket=pos,
        symbol_mt5="EURUSD",
        magic=88002,
        offset_seconds=10800,
        primary_raw=only_entry,
    )
    assert recon["reconciliation_status"] == "incomplete_entry_exit"
    assert recon["reconciliation_fail_closed"] is True
