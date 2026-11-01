"""Owner approval 2026-10-19: the close check reads only today's orders, rate-limited reads are retried instead
of journaled as incidents, and read-failure incidents may clear on a flat account after two exact matches."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from autonomous_trader.real_daily import eligible_incidents
from autonomous_trader.real_read_budget import ReadRateLimited
from autonomous_trader.real_runner import RealRunner
from autonomous_trader.real_waiting import recoverable_flat_incident


def flat(reason):
    return dict(positions={}, reservations={}, orders={},
                latches={'incident': True, 'daily': False, 'drawdown': False, 'terminated': False},
                incidents={'i1': {'reason': reason, 'pending_correction': False}})


@pytest.mark.parametrize('reason', ['REAL_READ_RATE_LIMITED', 'REAL_POST_CLOSE_ACCOUNT_READ_FAILED'])
def test_read_failure_incidents_recover_on_flat_account(reason):
    s = flat(reason)
    assert recoverable_flat_incident(s) and eligible_incidents(s) == ('i1',)


@pytest.mark.parametrize('reason', ['REAL_READ_RATE_LIMITED', 'REAL_POST_CLOSE_ACCOUNT_READ_FAILED'])
def test_read_failure_incidents_do_not_recover_while_holding(reason):
    s = flat(reason);s['positions']['x'] = {'quantity': '1'}
    assert not recoverable_flat_incident(s)


class Runner(RealRunner):
    def __init__(self, state, source, transport):
        self.controller = SimpleNamespace(state=state, account_id='a')
        self.source = source
        self.gateway = SimpleNamespace(transport=transport, reconcile_order=lambda *a, **k: None)
        self.incidents = []

    def _incident(self, reason, **evidence):
        self.incidents.append(reason);return reason


def limited(*a, **k):
    raise ReadRateLimited('/trading/orders/get', 30)


def test_rate_limited_account_read_is_deferred_not_an_incident():
    r = Runner({}, SimpleNamespace(account_observation=limited), None)
    assert r._post_close(False, None, {}) == 'REAL_POST_CLOSE_READ_DEFERRED' and r.incidents == []


def test_rate_limited_exact_check_is_deferred_not_an_incident():
    r = Runner({}, SimpleNamespace(account_observation=lambda: {}), None)
    r._post_close_exact = limited
    assert r._post_close(False, None, {}) == 'REAL_POST_CLOSE_READ_DEFERRED' and r.incidents == []


def test_other_account_read_failure_is_still_an_incident():
    def broken():raise RuntimeError('boom')
    r = Runner({}, SimpleNamespace(account_observation=broken), None)
    assert r._post_close(False, None, {}) == 'REAL_POST_CLOSE_ACCOUNT_READ_FAILED'


def test_mismatch_is_still_an_incident():
    r = Runner({}, SimpleNamespace(account_observation=lambda: {}), None)
    def mismatch(account):raise ValueError('REAL_POST_CLOSE_POSITION_MISMATCH')
    r._post_close_exact = mismatch
    assert r._post_close(False, None, {}) == 'REAL_POST_CLOSE_POSITION_MISMATCH'


def test_close_check_reads_only_todays_orders(monkeypatch):
    import autonomous_trader.real_runner as rr
    monkeypatch.setattr(rr, 'OrderIntent', SimpleNamespace(model_validate=lambda v: SimpleNamespace(**v)))
    monkeypatch.setattr(rr, 'confirm_flat_settlements', lambda *a, **k: None)
    old = dict(client_order_id='old', instrument_id='i', deadline=datetime(2026, 10, 15, 19, 55, tzinfo=timezone.utc))
    new = dict(client_order_id='new', instrument_id='i', deadline=datetime(2026, 10, 19, 19, 55, tzinfo=timezone.utc))
    state = {'orders': {'o1': {'final': True, 'intent': old}, 'o2': {'final': True, 'intent': new}},
             'instruments': {'i': {'ticker': 'X'}}, 'positions': {}, 'settlements': {}, 'session': {'label': '2026-10-19'},
             'cash': {'settled_usd': '100', 'unsettled_usd': '0', 'liquidation_equity_usd': '100'}}
    read = []
    transport = SimpleNamespace(detail=lambda cid: read.append(cid) or {'orders': [{'order_id': 'b-' + cid}]})
    r = Runner(state, None, transport)
    account = {'open_client_ids': (), 'positions': (),
               'cash': {'settled_cash': '100', 'unsettled_cash': '0', 'cash_balance': '100', 'net_liquidation_value': '100'}}
    r._post_close_exact(account)
    assert read == ['new']
