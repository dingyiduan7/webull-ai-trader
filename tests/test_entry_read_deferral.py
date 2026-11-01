"""Owner approval 2026-10-19: "rate-limited reads are retried instead of logged as incidents". On 2026-10-22 a
held position turned 69 rate-limited entry searches (ENTRY_MARKET_REFRESH) into incident locks. An entry search
that Webull rate-limits now waits out the retry window instead; held positions were already gated and reconciled
earlier in the same cycle, so their monitoring is unaffected."""
from datetime import timedelta
from test_real_packet_handoff import setup
from test_real_daily import NOW
from autonomous_trader.mvp.config import reviewed_calendar
from autonomous_trader.reference import NEW_YORK
from autonomous_trader.real_read_budget import ReadRateLimited


def test_rate_limited_entry_search_defers_without_incident(tmp_path, monkeypatch):
    r, p, starts, events, checks = setup(tmp_path, monkeypatch)
    r.controller.waiting_for_market_data = False
    r.controller.state['positions'] = {}            # same path whether flat or holding
    def discover():raise ReadRateLimited('/market-data/stocks/snapshot', 30)
    r.source.discover = discover
    incidents = []
    r._incident = lambda reason, **kw: incidents.append(reason) or reason
    session = reviewed_calendar().session(NOW.astimezone(NEW_YORK).date())
    assert r._advance_packet(p, session) == 'ENTRY_READ_DEFERRED'
    assert not incidents and not starts
    assert r.entry_read_retry_at == NOW + timedelta(seconds=300)
    assert events[-1][0] == 'REAL_MARKET_DATA_WAIT' and events[-1][1]['stage'] == 'ENTRY_MARKET_REFRESH'
    # Inside the retry window the search is not attempted again.
    r.source.discover = lambda: (_ for _ in ()).throw(AssertionError('searched inside the retry window'))
    assert r._advance_packet(p, session) == 'ENTRY_READ_DEFERRED'


def test_other_entry_search_failures_still_reach_the_incident_path(tmp_path, monkeypatch):
    r, p, starts, events, checks = setup(tmp_path, monkeypatch)
    r.controller.waiting_for_market_data = False
    def discover():raise ValueError('REAL_ACCOUNT_BINDING_MISMATCH')
    r.source.discover = discover
    seen = []
    r._market_failure_or_incident = lambda exc, stage: seen.append((str(exc), stage)) or 'INCIDENT'
    session = reviewed_calendar().session(NOW.astimezone(NEW_YORK).date())
    assert r._advance_packet(p, session) == 'INCIDENT'
    assert seen == [('REAL_ACCOUNT_BINDING_MISMATCH', 'ENTRY_MARKET_REFRESH')]
