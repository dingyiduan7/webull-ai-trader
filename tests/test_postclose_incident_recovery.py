"""Owner-approved 2026-10-16: the flat post-close cash/settlement incident is recoverable, nothing else widens."""
from copy import deepcopy
import pytest
from autonomous_trader.real_daily import RECOVERABLE_REASONS, eligible_incidents
from autonomous_trader.real_waiting import recoverable_flat_incident, clean_flat

REASON = 'REAL_POST_CLOSE_CASH_SETTLEMENT_MISMATCH'


def state(reason=REASON, **over):
    s = dict(positions={}, reservations={}, orders={},
             latches={'incident': True, 'daily': False, 'drawdown': False, 'terminated': False},
             incidents={'i1': {'reason': reason, 'pending_correction': False}})
    s.update(over)
    return s


def test_post_close_cash_incident_is_recoverable_when_flat():
    s = state()
    assert recoverable_flat_incident(s) and REASON in RECOVERABLE_REASONS
    assert eligible_incidents(s) == ('i1',)
    assert not clean_flat(s)


@pytest.mark.parametrize('mutate', [
    lambda s: s['positions'].update(x={'quantity': '1'}),
    lambda s: s['reservations'].update(x={}),
    lambda s: s['orders'].update(o={'final': False}),
    lambda s: s['orders'].update(o={'final': True, 'phase': 'UNKNOWN'}),
    lambda s: s['latches'].update(daily=True),
    lambda s: s['latches'].update(drawdown=True),
    lambda s: s['latches'].update(terminated=True),
    lambda s: s['incidents']['i1'].update(pending_correction=True),
])
def test_unsafe_state_is_never_recoverable(mutate):
    s = deepcopy(state());mutate(s)
    assert not recoverable_flat_incident(s)


@pytest.mark.parametrize('other', ['REAL_EXPOSURE_OUTSIDE_SESSION', 'REAL_POST_CLOSE_POSITION_MISMATCH',
                                   'REAL_POST_CLOSE_ORDER_MISMATCH'])
def test_other_post_close_incidents_stay_unrecoverable(other):
    s = state(other)
    assert not recoverable_flat_incident(s)
    with pytest.raises(ValueError):eligible_incidents(s)


def test_mixed_incidents_are_not_recoverable():
    s = state();s['incidents']['i2'] = {'reason': 'REAL_EXPOSURE_OUTSIDE_SESSION', 'pending_correction': False}
    assert not recoverable_flat_incident(s)


def _resolve(reason):
    from types import SimpleNamespace
    from test_real_incident_recovery import state as base_state, packet, NOW, ACCOUNT
    from autonomous_trader.real_trading import RealController
    current = base_state();current['incidents']['incident-a']['reason'] = reason
    value = packet();value.validate = lambda now, flat=False: None
    subject = SimpleNamespace(clock=lambda: NOW, account_id=ACCOUNT, state=current, lease=lambda: None)
    sent = []
    def send(c):
        sent.append(c)
        for k in c.resolved_incident_ids:current['incidents'].pop(k)
        current['latches']['incident'] = bool(current['incidents'])
        return current
    subject.send = send
    RealController.resolve_flat_reconciliation_incidents(subject, value, ('incident-a',))
    return sent


def test_controller_resolves_only_the_approved_reason():
    assert len(_resolve(REASON)) == 1
    with pytest.raises(ValueError, match='REAL_RECONCILIATION_INCIDENT_SCOPE_INVALID'):
        _resolve('REAL_EXPOSURE_OUTSIDE_SESSION')
