"""Owner-approved 2026-10-16: a blocked start retries itself in a fresh process every 15 minutes during the
session; it stops at the close, on revocation, and never doubles up on an already-active run."""
from datetime import datetime, timedelta, timezone

from scripts import run_real_daily as launcher

OPEN_DAY = datetime(2026, 10, 16, 16, 0, tzinfo=timezone.utc)   # Friday 10:00 MT, session open


class Clock:
    def __init__(self, t): self.t = t
    def __call__(self): return self.t
    def sleep(self, s): self.t += timedelta(seconds=s)


def run(code, start=OPEN_DAY, authority_ok=True, monkeypatch=None):
    clock, spawned = Clock(start), []
    monkeypatch.setattr(launcher, 'check_service_stop', lambda: None)
    def auth(root, now):
        if not authority_ok: raise ValueError('REVOKED')
    monkeypatch.setattr(launcher, 'load_authority', auth)
    out = launcher.retry_later('/root', code, clock=clock, sleep=clock.sleep, spawn=spawned.append)
    return out, spawned, clock.t - start


def test_blocked_start_spawns_a_fresh_daily_run_after_15_minutes(monkeypatch):
    out, spawned, waited = run('REAL_DAILY_UNRESOLVED_INCIDENT', monkeypatch=monkeypatch)
    assert out == 'RETRY_STARTED' and waited >= timedelta(minutes=15)
    assert len(spawned) == 1 and spawned[0][-1].replace('\\', '/').endswith('scripts/run_real_daily.py')
    assert '-I' in spawned[0] and '-B' in spawned[0]


def test_no_retry_after_the_session_ends(monkeypatch):
    late = datetime(2026, 10, 16, 19, 55, tzinfo=timezone.utc)
    out, spawned, _ = run('REAL_DAILY_UNRESOLVED_INCIDENT', start=late, monkeypatch=monkeypatch)
    assert out == 'SESSION_OVER' and not spawned


def test_revocation_ends_the_retry_chain(monkeypatch):
    out, spawned, _ = run('REAL_DAILY_CASH_MISMATCH', authority_ok=False, monkeypatch=monkeypatch)
    assert out == 'AUTHORITY_GONE' and not spawned


def test_never_retries_while_another_run_is_active(monkeypatch):
    for code in ('REAL_DAILY_ALREADY_ACTIVE', 'REAL_RUNNER_ALREADY_ACTIVE'):
        out, spawned, waited = run(code, monkeypatch=monkeypatch)
        assert out == 'NO_RETRY' and not spawned and waited == timedelta(0)


def test_weekend_never_retries(monkeypatch):
    saturday = datetime(2026, 10, 17, 16, 0, tzinfo=timezone.utc)
    out, spawned, _ = run('REAL_DAILY_UNRESOLVED_INCIDENT', start=saturday, monkeypatch=monkeypatch)
    assert out == 'SESSION_OVER' and not spawned
