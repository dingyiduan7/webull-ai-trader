from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
from test_real_runner_boundary import runner


def test_flat_liquidation_interval_reconciles_without_discovery(calendar,monkeypatch):
    session=calendar.session(datetime(2026,9,11).date())
    value=runner({'positions':{},'orders':{},'instruments':{}},SimpleNamespace(),session,
        session.liquidation_at+timedelta(seconds=1),monkeypatch)
    value.controller.waiting_for_market_data=True
    value.prepare_waiting_market=lambda:(_ for _ in ()).throw(AssertionError('no discovery after entry cutoff'))
    try:
        assert value.step()=='ENTRY_WINDOW_CLOSED_RECONCILED'
        assert value.controller.state['reconciliation']['ready']
        assert not value.controller.incidents
    finally:value.worker.shutdown(wait=False,cancel_futures=True)


def test_expiry_downgrades_flat_account_without_adding_incident(calendar,monkeypatch):
    session=calendar.session(datetime(2026,9,11).date());now=session.closes_at-timedelta(seconds=30)
    state={'positions':{},'orders':{},'instruments':{},'incidents':{'existing':{'reason':'OTHER'}},'latches':{'incident':True}}
    value=runner(state,SimpleNamespace(),session,now,monkeypatch)
    value.authority_receipt={'expires_at':session.closes_at.isoformat(),'daily_authority_sha256':'bound'}
    try:
        value._authority_guard(now)
        assert value.controller.recovery_only and not value.controller.incidents
        assert state['incidents']=={'existing':{'reason':'OTHER'}} and state['latches']['incident']
    finally:value.worker.shutdown(wait=False,cancel_futures=True)


def test_closed_account_refreshes_reconciliation_but_retains_unrelated_latch(calendar,monkeypatch):
    session=calendar.session(datetime(2026,9,11).date())
    state={'positions':{},'orders':{},'instruments':{},'incidents':{'x':{'reason':'REAL_ENTRY_AUTHORITY_EXPIRING'}},'latches':{'incident':True}}
    value=runner(state,SimpleNamespace(),session,session.closes_at+timedelta(minutes=1),monkeypatch)
    try:
        # 2026-10-16: an exactly reconciled closed day with a journaled incident ends the day (no all-night
        # child); the incident and latch are retained for the next preparation.
        assert value.step()=='CLOSED_RECONCILED_WITH_INCIDENTS'
        assert state['reconciliation']['ready'] and state['incidents'] and state['latches']['incident']
    finally:value.worker.shutdown(wait=False,cancel_futures=True)


def test_account_only_resolver_can_finish_closed_without_entry_authority(monkeypatch):
    from test_real_account_incident_recovery import setup
    from test_real_incident_recovery import close
    value,clock,account,receipt,commands=setup(monkeypatch)
    try:
        assert value._recover_flat_account_incident(closed=True)=='RECOVERY_ACCOUNT_CONFIRMATION_PENDING'
        clock[0]+=timedelta(seconds=1)
        assert value._recover_flat_account_incident(closed=True)=='CLOSED_RECONCILED'
        assert not value.controller.waiting_for_market_data
        assert not value.controller.state['latches']['incident']
        assert commands[-1].resolved_incident_ids==('incident-a',)
    finally:close(value)
