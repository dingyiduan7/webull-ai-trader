"""Owner-approved 2026-10-16: flat recovery clears ledger reconciliation mismatches and monitor failures, and
journals broker-settled proceeds before its exact comparison (Thursday 2026-10-15 blocked Friday's start)."""
from datetime import datetime, timezone
from decimal import Decimal as D
from types import SimpleNamespace
import pytest

from autonomous_trader.real_daily import eligible_incidents
from autonomous_trader.real_trading import confirm_flat_settlements
from autonomous_trader.real_waiting import recoverable_flat_incident


def flat(*reasons):
    return dict(positions={}, reservations={}, orders={},
                latches={'incident': True, 'daily': False, 'drawdown': False, 'terminated': False},
                incidents={f'i{n}': {'reason': r, 'pending_correction': False} for n, r in enumerate(reasons)})


def test_thursday_incident_mix_is_recoverable_when_flat():
    s = flat(*(['RECONCILIATION_MISMATCH'] * 130), 'REAL_HELD_POSITION_MONITOR_FAILED',
             'REAL_ACCOUNT_RECONCILIATION_FAILED', 'REAL_POST_CLOSE_CASH_SETTLEMENT_MISMATCH')
    assert recoverable_flat_incident(s) and len(eligible_incidents(s)) == 133


def test_new_reasons_never_recover_with_exposure_or_corrections():
    s = flat('RECONCILIATION_MISMATCH'); s['positions']['x'] = {'quantity': '1'}
    assert not recoverable_flat_incident(s)
    s = flat('RECONCILIATION_MISMATCH'); s['incidents']['i0']['pending_correction'] = True
    assert not recoverable_flat_incident(s)
    with pytest.raises(ValueError):
        eligible_incidents(s)
    assert not recoverable_flat_incident(flat('RECONCILIATION_MISMATCH', 'PROTECTION_FAILURE'))


# Ledger after Thursday's sale: 201.35 unsettled, settling 2026-10-16.
NOW = datetime(2026, 10, 16, 14, 0, tzinfo=timezone.utc)


def controller():
    state = dict(cash={'settled_usd': '313.39', 'unsettled_usd': '201.35'},
                 settlements={'ex1': {'outstanding_usd': '201.35', 'amount_usd': '201.35', 'settlement_date': '2026-10-16'}},
                 session={'label': '2026-10-15', 'opens_at': '2026-10-15T13:30:00+00:00',
                          'closes_at': '2026-10-15T20:00:00+00:00'},
                 config={'settlement_days': ['2026-10-15', '2026-10-16', '2026-10-19']})
    sent = []
    state.update(instruments={}, positions={}, orders={}, reservations={})
    subject = SimpleNamespace(state=state, clock=lambda: NOW, lease=lambda: None, send=lambda c: sent.append(c) or state,
                              account_id='acct')
    return subject, sent


def account(settled, unsettled):
    return {'cash': {'settled_cash': settled, 'unsettled_cash': unsettled}, 'evidence_id': 'ev1',
            'account_binding': 'acct', 'positions': [], 'open_client_ids': (), 'observed_at': NOW, 'received_at': NOW}


def test_broker_settled_proceeds_are_journaled_exactly():
    subject, sent = controller()
    confirm_flat_settlements(subject, account('514.74', '0'))
    assert [(c.kind, c.execution_id, c.amount_usd, c.effective_on.isoformat()) for c in sent] == [
        ('SETTLE', 'ex1', D('201.35'), '2026-10-16')]


def test_nothing_journaled_when_broker_still_shows_unsettled():
    subject, sent = controller()
    confirm_flat_settlements(subject, account('313.39', '201.35'))
    assert sent == []


@pytest.mark.parametrize('settled,unsettled', [('514.75', '0'), ('514.74', '0.01'), ('400.00', '114.74')])
def test_broker_truth_adopts_any_remaining_difference(settled, unsettled):
    """Owner 2026-10-18: the broker's cash is the balance. Due proceeds the broker no longer shows unsettled are
    confirmed first; whatever difference remains is journaled as one CASH_ADOPT, never refused."""
    subject, sent = controller()
    confirm_flat_settlements(subject, account(settled, unsettled))
    confirmed = sum((c.amount_usd for c in sent if c.kind == 'SETTLE'), D(0))
    adopt = [c for c in sent if c.kind == 'CASH_ADOPT']
    assert len(adopt) == 1
    assert D('313.39') + confirmed + adopt[0].settled_delta_usd == D(settled)
    assert D('201.35') - confirmed + adopt[0].unsettled_delta_usd == D(unsettled)
