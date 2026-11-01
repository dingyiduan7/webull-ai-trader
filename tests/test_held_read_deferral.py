"""2026-10-27: 15 incident locks while holding NOW were Webull rate limits on the full market refresh (GATE_CHECK).
Holding, a rate-limited refresh now waits instead of locking; stops are checked separately every cycle."""
from datetime import timedelta
from test_real_packet_handoff import setup
from test_real_daily import NOW
from autonomous_trader.mvp.config import reviewed_calendar
from autonomous_trader.reference import NEW_YORK
from autonomous_trader.real_read_budget import ReadRateLimited


def held(r):
    r.controller.state['positions'] = {'iid': {'quantity': '1'}}
    r.controller.state['reservations'] = {}
    r.controller.waiting_for_market_data = False


def test_held_rate_limit_defers_without_incident(tmp_path, monkeypatch):
    r, p, starts, events, checks = setup(tmp_path, monkeypatch); held(r)
    incidents = []
    r._incident = lambda reason, **kw: incidents.append(reason) or reason
    def read_checked(fetch, clock, **kw):raise ReadRateLimited('/market-data/stocks/snapshot', 20)
    r.gate.read_checked = read_checked
    session = reviewed_calendar().session(NOW.astimezone(NEW_YORK).date())
    assert r._advance_packet(None, session) == 'HELD_READ_DEFERRED'
    assert not incidents and r.held_read_retry_at == NOW + timedelta(seconds=30)
    r.gate.read_checked = lambda *a, **k: (_ for _ in ()).throw(AssertionError('read inside the wait'))
    assert r._advance_packet(None, session) == 'HELD_READ_DEFERRED'


def test_held_other_failures_still_lock(tmp_path, monkeypatch):
    r, p, starts, events, checks = setup(tmp_path, monkeypatch); held(r)
    seen = []
    r._reconciliation_failure = lambda exc, stage: seen.append((str(exc), stage)) or 'INCIDENT'
    def read_checked(fetch, clock, **kw):raise ValueError('REAL_ACCOUNT_BINDING_MISMATCH')
    r.gate.read_checked = read_checked
    session = reviewed_calendar().session(NOW.astimezone(NEW_YORK).date())
    assert r._advance_packet(None, session) == 'INCIDENT'


def test_latched_holding_still_takes_incident_path(tmp_path, monkeypatch):
    r, p, starts, events, checks = setup(tmp_path, monkeypatch); held(r)
    r.controller.state['latches']['incident'] = True
    seen = []
    r._reconciliation_failure = lambda exc, stage: seen.append(stage) or 'INCIDENT'
    assert r._market_failure_or_incident(ReadRateLimited('/x', 20), 'GATE_CHECK') == 'INCIDENT'
