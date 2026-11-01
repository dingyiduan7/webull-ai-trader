"""Entry-call trigger policy: hysteresis, heartbeat, and no tick-driven calls."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from autonomous_trader.real_triggers import (HEARTBEAT, MATERIAL_MOVE, MIN_INTERVAL, entry_trigger, reference)

NOW = datetime(2026, 10, 13, 15, 0, tzinfo=timezone.utc)


def ctx(*quotes, cost='cost-v1'):
    return SimpleNamespace(execution_cost_evidence=cost, instruments=tuple(
        SimpleNamespace(instrument_id=i, bid_usd=b, ask_usd=a) for i, b, a in quotes))


def last(context, at=NOW):
    return {'at': at.isoformat(), 'reference': reference(context)}


def test_first_call_of_session():
    assert entry_trigger(None, reference(ctx(('a', '10.00', '10.01'))), NOW) == 'FIRST_OF_SESSION'


def test_tick_flicker_never_retriggers_even_across_old_bucket_edges():
    base = last(ctx(('a', '10.00', '10.01'), ('b', '99.99', '100.00')))
    for cents in range(-9, 10):  # +/- 9 cents on a $100 stock < 1%
        moved = ctx(('a', '10.00', '10.01'), ('b', str(99.99 + cents / 100), str(100.00 + cents / 100)))
        assert entry_trigger(base, reference(moved), NOW + timedelta(minutes=5)) is None


def test_material_move_new_candidate_cost_change_and_heartbeat():
    base = last(ctx(('a', '10.00', '10.00')))
    up = str(10 * (1 + MATERIAL_MOVE))
    later = NOW + MIN_INTERVAL
    assert entry_trigger(base, reference(ctx(('a', up, up))), later) == 'MATERIAL_MOVE'
    assert entry_trigger(base, reference(ctx(('a', '10', '10'), ('b', '5', '5'))), later) == 'NEW_CANDIDATE'
    assert entry_trigger(base, reference(ctx(('a', '10', '10'), cost='cost-v2')), later) == 'COST_EVIDENCE_CHANGED'
    assert entry_trigger(base, reference(ctx(('a', '10', '10'))), NOW + HEARTBEAT) == 'HEARTBEAT'
    assert entry_trigger(base, reference(ctx(('a', '10', '10'))), NOW + HEARTBEAT - timedelta(seconds=1)) is None


def test_dropping_a_candidate_alone_is_not_new_evidence():
    base = last(ctx(('a', '10', '10'), ('b', '5', '5')))
    assert entry_trigger(base, reference(ctx(('a', '10', '10'))), NOW) is None


def test_no_entry_call_within_15_minutes_for_any_reason():
    """2026-10-20: ~70 calls in two hours on volatile gap-ups exhausted the daily model budget by 9:40 MT."""
    base = last(ctx(('a', '10', '10')))
    soon = NOW + MIN_INTERVAL - timedelta(seconds=1)
    assert MIN_INTERVAL == timedelta(minutes=15) and MATERIAL_MOVE == __import__('decimal').Decimal('0.03')
    assert entry_trigger(base, reference(ctx(('a', '13', '13'))), soon) is None                 # big move
    assert entry_trigger(base, reference(ctx(('a', '10', '10'), ('b', '5', '5'))), soon) is None  # new name
    assert entry_trigger(base, reference(ctx(('a', '10', '10'), cost='v9')), soon) is None       # cost evidence
    up2 = ctx(('a', '10.2', '10.2'))                                                              # 2%: noise now
    assert entry_trigger(base, reference(up2), NOW + MIN_INTERVAL) is None
