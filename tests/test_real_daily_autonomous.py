"""Offline owner-armed startup recovery; never starts a real process."""
from contextlib import nullcontext
from datetime import timedelta
import json
import pytest
from test_real_daily import fixture,NOW
from scripts import run_real_daily as launcher
from autonomous_trader.real_read_budget import ReadRateLimited

def setup(tmp_path,monkeypatch):
    authority,save,_=fixture(tmp_path,monkeypatch)
    now=[NOW];calls=[]
    monkeypatch.setattr(launcher,'launcher_lock',lambda *a:nullcontext())
    monkeypatch.setattr(launcher,'ensure_database',lambda *a:calls.append('db'))
    # Current launcher binds startup to the exact certificate the collection produced.
    # These fakes' collect() produces nothing, so the stored certificate mirrors that.
    monkeypatch.setattr(launcher,'current_certificate',lambda *a:(None,None))
    monkeypatch.setattr(launcher,'supervise',lambda *a:calls.append('supervise') or 'CLOSED_RECONCILED')
    def sleep(seconds):
        assert seconds>0
        calls.append(('sleep',seconds));now[0]+=timedelta(seconds=seconds)
    return authority,save,now,calls,sleep

@pytest.mark.parametrize('error',[ValueError('REAL_TRANSPORT_UNCERTAIN_NO_RETRY'),
    ValueError('REAL_HTTP_503_UNAVAILABLE'),ReadRateLimited('/trading/accounts/list',32)])
def test_preflight_read_failure_recovers_without_owner_relay(tmp_path,monkeypatch,error):
    _,_,now,calls,sleep=setup(tmp_path,monkeypatch)
    def collect(*args):
        calls.append('collect')
        if calls.count('collect')==1:raise error
    monkeypatch.setattr(launcher,'collect',collect)
    assert launcher.run(tmp_path,lambda:now[0],retry_readiness=True,sleep=sleep)=='CLOSED_RECONCILED'
    assert calls.count('collect')==2 and calls.count('supervise')==1
    if isinstance(error,ReadRateLimited):assert next(x[1] for x in calls if isinstance(x,tuple))>=32

@pytest.mark.parametrize('code',['REAL_ACCOUNT_BINDING_MISMATCH','REAL_DAILY_SAFETY_LATCH',
    'REAL_DAILY_UNRESOLVED_EXPOSURE_OR_ORDER','REAL_HTTP_401_TOKEN_EXPIRED','SETTLEMENT_CHANGE_UNRECONCILED'])
def test_account_or_authority_failure_is_never_blindly_retried(tmp_path,monkeypatch,code):
    _,_,now,calls,sleep=setup(tmp_path,monkeypatch)
    def collect(*args):raise ValueError(code)
    monkeypatch.setattr(launcher,'collect',collect)
    with pytest.raises(ValueError,match=code):launcher.run(tmp_path,lambda:now[0],retry_readiness=True,sleep=sleep)
    assert 'supervise' not in calls and not any(isinstance(x,tuple) for x in calls)

def test_revocation_during_retry_precedes_next_runtime_touch(tmp_path,monkeypatch):
    authority,save,now,calls,sleep=setup(tmp_path,monkeypatch)
    def collect(*args):calls.append('collect');raise ValueError('REAL_HTTP_503_UNAVAILABLE')
    monkeypatch.setattr(launcher,'collect',collect)
    def revoke(seconds):sleep(seconds);authority['revoked']=True;save()
    with pytest.raises(ValueError,match='PERSISTENT_AUTHORITY_INVALID'):
        launcher.run(tmp_path,lambda:now[0],retry_readiness=True,sleep=revoke)
    assert calls.count('collect')==1 and 'supervise' not in calls

def test_no_retry_crosses_session_or_replays_supervisor(tmp_path,monkeypatch):
    _,_,now,calls,sleep=setup(tmp_path,monkeypatch)
    def collect(*args):calls.append('collect');raise ValueError('REAL_HTTP_503_UNAVAILABLE')
    monkeypatch.setattr(launcher,'collect',collect)
    def close(seconds):sleep(seconds);now[0]=NOW.replace(hour=22)
    assert launcher.run(tmp_path,lambda:now[0],retry_readiness=True,sleep=close)=='CLOSED'
    assert calls.count('collect')==1 and 'supervise' not in calls
    now[0]=NOW;monkeypatch.setattr(launcher,'collect',lambda *a:None)
    def uncertain(*args):calls.append('supervise');raise ValueError('REAL_TRANSPORT_UNCERTAIN_NO_RETRY')
    monkeypatch.setattr(launcher,'supervise',uncertain)
    with pytest.raises(ValueError,match='UNCERTAIN'):
        launcher.run(tmp_path,lambda:now[0],retry_readiness=True,sleep=sleep)
    assert calls.count('supervise')==1

def test_prepare_only_still_never_supervises_or_retries(tmp_path,monkeypatch):
    _,_,now,calls,sleep=setup(tmp_path,monkeypatch)
    def collect(*args):raise ValueError('REAL_HTTP_503_UNAVAILABLE')
    monkeypatch.setattr(launcher,'collect',collect)
    with pytest.raises(ValueError):launcher.run(tmp_path,lambda:now[0],prepare_only=True,retry_readiness=True,sleep=sleep)
    assert 'supervise' not in calls and not any(isinstance(x,tuple) for x in calls)

def test_readonly_outage_outlasting_old_restart_count_can_recover(tmp_path,monkeypatch):
    _,_,now,calls,sleep=setup(tmp_path,monkeypatch)
    def collect(*args):
        calls.append('collect')
        if calls.count('collect')<=8:raise ValueError('REAL_HTTP_503_UNAVAILABLE')
    monkeypatch.setattr(launcher,'collect',collect)
    assert launcher.run(tmp_path,lambda:now[0],retry_readiness=True,sleep=sleep)=='CLOSED_RECONCILED'
    assert calls.count('collect')==9 and calls.count('supervise')==1

def test_readiness_wait_records_a_safe_blocker(tmp_path,monkeypatch):
    _,_,now,calls,sleep=setup(tmp_path,monkeypatch)
    def collect(*args):
        calls.append('collect')
        if calls.count('collect')==1:raise ValueError('REAL_HTTP_503_UNAVAILABLE')
    monkeypatch.setattr(launcher,'collect',collect)
    launcher.run(tmp_path,lambda:now[0],retry_readiness=True,sleep=sleep)
    reports=list((tmp_path/'.local/real-readiness').glob('daily-blocked-*.json'))
    assert reports
    value=json.loads(reports[0].read_text())
    assert value['reason'].startswith('REAL_') and value['production_enabled'] is False

def test_startup_rejects_certificate_that_differs_from_collection(tmp_path,monkeypatch):
    _,_,now,calls,sleep=setup(tmp_path,monkeypatch)
    monkeypatch.setattr(launcher,'collect',lambda *a:{'evidence':'collected'})
    monkeypatch.setattr(launcher,'current_certificate',lambda *a:({'evidence':'swapped'},None))
    with pytest.raises(ValueError,match='REAL_DAILY_START_EVIDENCE_EXPIRED'):
        launcher.run(tmp_path,lambda:now[0],retry_readiness=True,sleep=sleep)
    assert 'supervise' not in calls
