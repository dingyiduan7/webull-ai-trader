"""Daily start re-measures deployment evidence itself after a release (no manual bootstrap)."""
from datetime import datetime,timedelta,timezone
import pytest
from scripts import run_real_daily as launcher

OPEN=datetime(2026,10,14,13,40,tzinfo=timezone.utc)   # 09:40 ET Wednesday
CLOSED_NIGHT=datetime(2026,10,14,0,4,tzinfo=timezone.utc)


def stale(monkeypatch,reason='REAL_PERSISTENT_AUTHORITY_INVALID'):
    def fail(root,now):raise ValueError(reason)
    monkeypatch.setattr(launcher,'load_authority',fail)


def test_current_evidence_does_not_bootstrap(monkeypatch):
    monkeypatch.setattr(launcher,'load_authority',lambda root,now:{})
    assert launcher.refresh_evidence_if_stale('r',lambda:OPEN,bootstrap=lambda:pytest.fail('no bootstrap needed'))=='CURRENT'


def test_stale_evidence_in_session_runs_bootstrap_once(monkeypatch):
    stale(monkeypatch);calls=[]
    result=launcher.refresh_evidence_if_stale('r',lambda:OPEN,bootstrap=lambda:calls.append(1) or {'result':'OK_DEPLOYMENT_EVIDENCE_WRITTEN'})
    assert result=='OK_DEPLOYMENT_EVIDENCE_WRITTEN' and calls==[1]


@pytest.mark.parametrize('reason',['REAL_OWNER_SIGNATURE_INVALID','REAL_OWNER_AUTHORIZATION_REVOKED','SOMETHING_ELSE'])
def test_other_authority_failures_never_trigger_bootstrap(monkeypatch,reason):
    stale(monkeypatch,reason)
    assert launcher.refresh_evidence_if_stale('r',lambda:OPEN,bootstrap=lambda:pytest.fail('must not run'))=='NOT_REFRESHABLE'


def test_closed_market_does_not_bootstrap(monkeypatch):
    stale(monkeypatch)
    assert launcher.refresh_evidence_if_stale('r',lambda:CLOSED_NIGHT,sleep=lambda s:None,max_wait_seconds=0,
        bootstrap=lambda:pytest.fail('market closed'))in('CLOSED','MARKET_NOT_OPEN')


def test_waits_for_the_open_then_bootstraps(monkeypatch):
    stale(monkeypatch);now=[datetime(2026,10,14,13,29,40,tzinfo=timezone.utc)]   # 09:29:40 ET
    def tick(seconds):now[0]+=timedelta(seconds=seconds)
    result=launcher.refresh_evidence_if_stale('r',lambda:now[0],sleep=tick,bootstrap=lambda:{'result':'OK_DEPLOYMENT_EVIDENCE_WRITTEN'})
    assert result=='OK_DEPLOYMENT_EVIDENCE_WRITTEN'


def test_bootstrap_failure_is_reported_not_raised(monkeypatch):
    stale(monkeypatch)
    def boom():raise RuntimeError('x')
    assert launcher.refresh_evidence_if_stale('r',lambda:OPEN,bootstrap=boom)=='BOOTSTRAP_ERROR'


def test_checkpoint_binding_mismatch_counts_as_stale_and_triggers_bootstrap(monkeypatch):
    """Live regression 2026-10-14: authority loaded fine, but the evidence checkpoint no longer matched the observation
    ledger (an after-hours bootstrap had advanced it), so the open was blocked with no refresh."""
    monkeypatch.setattr(launcher,'load_authority',lambda root,now:{'account_binding':'a','flat_checkpoint_sha256':'x'})
    def mismatch(root,authority):raise ValueError('REAL_FLAT_CHECKPOINT_BINDING')
    calls=[]
    result=launcher.refresh_evidence_if_stale('r',lambda:OPEN,binding_check=mismatch,
        bootstrap=lambda:calls.append(1) or {'result':'OK_DEPLOYMENT_EVIDENCE_WRITTEN'})
    assert result=='OK_DEPLOYMENT_EVIDENCE_WRITTEN' and calls==[1]


def test_unreachable_database_in_binding_check_is_not_treated_as_stale(monkeypatch):
    monkeypatch.setattr(launcher,'load_authority',lambda root,now:{'account_binding':'a','flat_checkpoint_sha256':'x'})
    def down(root,authority):raise OSError('db down')
    assert launcher.refresh_evidence_if_stale('r',lambda:OPEN,binding_check=down,bootstrap=lambda:pytest.fail('no'))=='CURRENT'
