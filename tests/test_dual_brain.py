from datetime import datetime, timezone, timedelta
from decimal import Decimal

from autonomous_trader.contracts import AlphaDecision, AlphaOpen, AlphaHold, AlphaClose
from autonomous_trader.decision_api.dual_brain import combine_entry, combine_position

T = datetime(2026, 10, 15, 15, 0, tzinfo=timezone.utc)


def opn(iid='1', qty=None, notional='100', limit='10.00', stop='9.00', target=None, exp=2, review=30, label='a'):
    kw = dict(desired_quantity=Decimal(qty)) if qty else dict(desired_notional_usd=Decimal(notional))
    o = AlphaOpen(instrument_id=iid, symbol='ABC', strategy_label=label, thesis='t',
                  entry_limit_usd=Decimal(limit), entry_expires_at=T + timedelta(minutes=exp),
                  hard_stop_usd=Decimal(stop), latest_exit_at=T + timedelta(days=5),
                  target_usd=Decimal(target) if target else None,
                  next_review_at=T + timedelta(minutes=review), **kw)
    return AlphaDecision(action='OPEN', reason='r', open=o)


ABST = AlphaDecision(action='ABSTAIN', reason='no')


def test_both_failed_never_enters():
    r = combine_entry(None, None)
    assert r.decision.action == 'ABSTAIN' and r.mode == 'BOTH_FAILED'


def test_single_model_failure_falls_back_to_survivor():
    assert combine_entry(opn(), None).decision.action == 'OPEN'
    assert combine_entry(None, opn()).mode == 'DEGRADED_SECOND_ONLY'


def test_disagreement_abstains():
    assert combine_entry(opn(), ABST).decision.action == 'ABSTAIN'
    assert combine_entry(ABST, opn()).decision.action == 'ABSTAIN'
    assert combine_entry(opn('1'), opn('2')).decision.action == 'ABSTAIN'


def test_consensus_merge_is_never_larger_looser_or_chasing():
    a = opn(notional='200', limit='10.05', stop='9.00', target='13', exp=3, review=30)
    b = opn(qty='5', limit='10.00', stop='9.40', target='12', exp=2, review=10)
    m = combine_entry(a, b).decision.open
    assert m.entry_limit_usd == Decimal('10.00')
    assert m.hard_stop_usd == Decimal('9.40')
    # Owner-approved 2026-10-19: the higher target and the later deadline (stop stays the tighter one).
    assert m.target_usd == Decimal('13')
    assert m.desired_quantity == Decimal('5')           # 50 < 200
    assert m.entry_expires_at == T + timedelta(minutes=2)
    assert m.next_review_at == T + timedelta(minutes=10)


def test_stop_at_or_above_limit_after_merge_abstains():
    a = opn(limit='10.00', stop='9.00')
    b = opn(limit='9.50', stop='9.60')  # invalid alone? stop above limit is merged-rejected
    try:
        r = combine_entry(a, b)
    except Exception:
        return
    assert r.decision.action == 'ABSTAIN'


def hold(): return AlphaDecision(action='HOLD', reason='h', hold=AlphaHold(position_id='p', next_review_at=T + timedelta(hours=1)))
def close(): return AlphaDecision(action='CLOSE', reason='c', close=AlphaClose(position_id='p', full_close=True))


def test_second_model_close_never_sells_a_held_position_by_itself():
    r = combine_position(hold(), close())
    assert r.decision.action == 'HOLD' and r.challenge is True


def test_primary_close_stands_and_both_failed_leaves_position():
    assert combine_position(close(), hold()).decision.action == 'CLOSE'
    assert combine_position(None, None).decision is None


def test_primary_failed_falls_back_to_second_decision():
    assert combine_position(None, hold()).mode == 'DEGRADED_SECOND_ONLY'


def test_off_mode_builds_no_second_brain(monkeypatch):
    from autonomous_trader.decision_api import second_brain
    monkeypatch.setattr(second_brain, 'MODE', 'OFF')
    brain, reason = second_brain.make_second_brain(None)
    assert brain is None and reason == 'SECOND_BRAIN_OFF'


def test_shadow_record_compares_models_and_consensus(monkeypatch):
    from types import SimpleNamespace
    from autonomous_trader.decision_api import second_brain
    ctx = SimpleNamespace(as_of=T)
    p = {'decision': opn().model_dump(mode='json')}
    s = {'decision': ABST.model_dump(mode='json'), 'estimated_cost_usd': '0.02'}
    row = second_brain.shadow_record('ENTRY', ctx, p, s, None)
    assert row['primary_action'] == 'OPEN' and row['second_action'] == 'ABSTAIN'
    assert row['consensus_mode'] == 'DISAGREEMENT' and row['consensus_action'] == 'ABSTAIN'
    assert row['authority'] == 'NONE_SHADOW_ONLY'
    failed = second_brain.shadow_record('ENTRY', ctx, p, None, 'SECOND_BRAIN_UNAVAILABLE:X')
    assert failed['consensus_mode'] == 'DEGRADED_PRIMARY_ONLY'


def test_shadow_unavailable_key_is_recorded_not_raised(monkeypatch):
    from autonomous_trader.decision_api import second_brain
    monkeypatch.setattr(second_brain, 'MODE', 'SHADOW')
    def no_key():
        raise ValueError('OPENAI_KEY_LOCATION_REQUIRED')
    brain, reason = second_brain.make_second_brain(None, key_loader=no_key)
    assert brain is None and reason == 'SECOND_BRAIN_UNAVAILABLE:OPENAI_KEY_LOCATION_REQUIRED'


def _runner_with_second(monkeypatch, second_result=None, second_error=None):
    from types import SimpleNamespace
    from autonomous_trader import real_runner
    from autonomous_trader.decision_api import second_brain
    monkeypatch.setattr(second_brain, 'MODE', 'CONSENSUS')
    class Second:
        last_call = {}
        def fast_decide(self, *a, **k):
            if second_error: raise ValueError(second_error)
            return second_result
    monkeypatch.setattr(second_brain, 'make_second_brain', lambda budget: (Second(), None))
    r = real_runner.RealRunner.__new__(real_runner.RealRunner)
    events = []
    r.emit = lambda kind, payload, **kw: events.append((kind, payload))
    r.clock = lambda: T
    return r, events


def test_consensus_merges_agreeing_entries(monkeypatch):
    from types import SimpleNamespace
    a = opn(notional='200', limit='10.05', stop='9.00')
    b = opn(qty='5', limit='10.00', stop='9.40')
    r, events = _runner_with_second(monkeypatch, {'decision': b.model_dump(mode='json'), 'estimated_cost_usd': '0.03'})
    out = r._second_brain_check(SimpleNamespace(as_of=T), {'decision': a.model_dump(mode='json'),
                                                           'estimated_cost_usd': '0.10'}, None)
    assert out['decision']['action'] == 'OPEN' and out['decision']['open']['entry_limit_usd'] == '10.00'
    assert out['estimated_cost_usd'] == '0.13' and events[-1][0] == 'REAL_DUAL_BRAIN_DECISION'


def test_consensus_disagreement_abstains_and_second_alone_never_trades(monkeypatch):
    from types import SimpleNamespace
    r, events = _runner_with_second(monkeypatch, {'decision': ABST.model_dump(mode='json')})
    out = r._second_brain_check(SimpleNamespace(as_of=T), {'decision': opn().model_dump(mode='json')}, None)
    assert out['decision']['action'] == 'ABSTAIN'
    # Primary abstains: the second model is not even asked (it can never open by itself).
    assert r._second_brain_check(SimpleNamespace(as_of=T), {'decision': ABST.model_dump(mode='json')}, None) is None


def test_consensus_second_failure_leaves_primary_in_charge(monkeypatch):
    from types import SimpleNamespace
    r, events = _runner_with_second(monkeypatch, second_error='OPENAI_KEY_LOCATION_REQUIRED')
    out = r._second_brain_check(SimpleNamespace(as_of=T), {'decision': opn().model_dump(mode='json')}, None)
    assert out['decision']['action'] == 'OPEN' and out['dual_brain']['mode'] == 'DEGRADED_PRIMARY_ONLY'
