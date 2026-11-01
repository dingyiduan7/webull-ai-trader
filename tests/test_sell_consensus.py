"""Owner approval 2026-10-19: sells require both brains to agree, reviews may only sell on target or a broken
thesis, and the buy merge keeps the tighter stop but the later deadline and higher target."""
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

from test_dual_brain import T, opn, hold, close, _runner_with_second
from autonomous_trader.contracts import AlphaDecision, AlphaModify
from autonomous_trader.decision_api.dual_brain import combine_entry, consensus_close


def test_close_needs_both_models():
    assert consensus_close(close(), close(), T).decision.action == 'CLOSE'
    vetoed = consensus_close(close(), hold(), T + timedelta(minutes=15))
    assert vetoed.decision.action == 'HOLD' and vetoed.mode == 'DISAGREEMENT'
    assert vetoed.decision.hold.position_id == 'p' and vetoed.decision.hold.next_review_at == T + timedelta(hours=1)


def test_second_model_review_time_is_used_for_modify_and_fallback_otherwise():
    m = AlphaDecision(action='MODIFY', reason='m', modify=AlphaModify(position_id='p',
                      requested_next_review_at=T + timedelta(minutes=40)))
    assert consensus_close(close(), m, T).decision.hold.next_review_at == T + timedelta(minutes=40)
    abstain = AlphaDecision(action='ABSTAIN', reason='x')
    assert consensus_close(close(), abstain, T + timedelta(minutes=15)).decision.hold.next_review_at == T + timedelta(minutes=15)


def test_second_failure_leaves_primary_close_degraded_and_non_close_untouched():
    assert consensus_close(close(), None, T).mode == 'DEGRADED_PRIMARY_ONLY'
    assert consensus_close(hold(), close(), T).decision.action == 'HOLD'      # second alone never sells


def test_merge_takes_later_deadline_and_higher_target_but_tighter_stop():
    a = opn(limit='10.05', stop='9.00', target='13')
    b = opn(limit='10.00', stop='9.40', target='12')
    b = b.model_copy(update={'open': b.open.model_copy(update={'latest_exit_at': T + timedelta(days=2)})})
    m = combine_entry(a, b).decision.open
    assert m.latest_exit_at == T + timedelta(days=5) and m.target_usd == Decimal('13')
    assert m.hard_stop_usd == Decimal('9.40') and m.entry_limit_usd == Decimal('10.00')


def _context(latest=T + timedelta(days=2)):
    pos = SimpleNamespace(position_id='p', latest_exit_at=latest)
    return SimpleNamespace(as_of=T, positions=(pos,), exit_horizon=T + timedelta(days=10))


def test_runner_vetoed_close_becomes_valid_hold(monkeypatch):
    r, events = _runner_with_second(monkeypatch, {'decision': hold().model_dump(mode='json'), 'estimated_cost_usd': '0.02'})
    out = r._second_brain_close_check(_context(), {'decision': close().model_dump(mode='json'),
                                                   'estimated_cost_usd': '0.05'}, None)
    assert out['decision']['action'] == 'HOLD' and out['dual_brain']['mode'] == 'DISAGREEMENT'
    assert out['estimated_cost_usd'] == '0.07'
    assert events[-1][0] == 'REAL_DUAL_BRAIN_DECISION' and events[-1][1]['consensus_action'] == 'HOLD'


def test_runner_hold_review_is_clamped_inside_position_deadline(monkeypatch):
    r, _ = _runner_with_second(monkeypatch, {'decision': hold().model_dump(mode='json')})
    out = r._second_brain_close_check(_context(T + timedelta(minutes=20)), {'decision': close().model_dump(mode='json')}, None)
    review = out['decision']['hold']['next_review_at']
    assert review.startswith('2026-10-15T15:20:00')      # second model said +1h; clamped to the deadline
    # With no room for a valid hold (deadline now), the primary decides (stops/deadline exits handle it).
    r2, _ = _runner_with_second(monkeypatch, {'decision': hold().model_dump(mode='json')})
    assert r2._second_brain_close_check(_context(T), {'decision': close().model_dump(mode='json')}, None) is None


def test_runner_agreeing_close_sells_and_failed_second_lets_primary_decide(monkeypatch):
    r, _ = _runner_with_second(monkeypatch, {'decision': close().model_dump(mode='json')})
    assert r._second_brain_close_check(_context(), {'decision': close().model_dump(mode='json')}, None)['decision']['action'] == 'CLOSE'
    r2, _ = _runner_with_second(monkeypatch, second_error='OPENAI_TIMEOUT')
    out = r2._second_brain_close_check(_context(), {'decision': close().model_dump(mode='json')}, None)
    assert out['decision']['action'] == 'CLOSE' and out['dual_brain']['mode'] == 'DEGRADED_PRIMARY_ONLY'


def test_runner_hold_from_primary_never_calls_second(monkeypatch):
    r, _ = _runner_with_second(monkeypatch, second_error='MUST_NOT_BE_CALLED')
    assert r._second_brain_close_check(_context(), {'decision': hold().model_dump(mode='json')}, None) is None


def test_prompt_forbids_drift_sales():
    from autonomous_trader.decision_api.claude_prompts import POSITION_OUTPUT
    assert 'remaining expected payoff no longer justifies the capital' not in POSITION_OUTPUT
    assert 'Drift or a red mark above the stop is not invalidation' in POSITION_OUTPUT
