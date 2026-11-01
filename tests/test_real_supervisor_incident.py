from datetime import datetime,timezone
import json
import pytest
import scripts.run_real_supervisor as subject


def test_status_sharing_violation_retries_atomic_replace(monkeypatch,tmp_path):
    path=tmp_path/'status.json';path.write_text('{"prior":true}')
    replace=subject.os.replace;attempts=[];sleeps=[]
    def transient(source,dest):
        attempts.append(1)
        if len(attempts)<3:raise PermissionError('simulated Windows sharing violation')
        replace(source,dest)
    monkeypatch.setattr(subject.os,'replace',transient)
    monkeypatch.setattr(subject.time,'sleep',sleeps.append)
    subject._write(path,{'current':True})
    assert json.loads(path.read_text())=={'current':True}
    assert len(attempts)==3 and sleeps==[.05,.1]


def test_permanent_status_failure_is_bounded_and_preserves_last_snapshot(monkeypatch,tmp_path):
    path=tmp_path/'status.json';path.write_text('{"prior":true}')
    attempts=[];sleeps=[]
    def denied(*args):attempts.append(1);raise PermissionError('simulated persistent failure')
    monkeypatch.setattr(subject.os,'replace',denied)
    monkeypatch.setattr(subject.time,'sleep',sleeps.append)
    with pytest.raises(PermissionError):subject._write(path,{'current':True})
    assert len(attempts)==6 and sum(sleeps)<=1.6
    assert json.loads(path.read_text())=={'prior':True}


def test_status_failure_cannot_leave_owned_child_running(monkeypatch,tmp_path):
    receipt={'build_sha256':'build','account_binding':'real-test','market_session_label':'2026-10-01',
        'recovery_not_after':'2026-10-02T20:00:00+00:00'}
    monkeypatch.setattr(subject,'ROOT',tmp_path)
    monkeypatch.setattr(subject,'_authority',lambda now:(receipt,False))
    monkeypatch.setattr(subject.RealGate,'offline_receipt',lambda *a,**k:receipt)
    class Child:
        pid=123
        running=True
        def poll(self):return None if self.running else 1
    child=Child();stopped=[]
    monkeypatch.setattr(subject.subprocess,'Popen',lambda *a,**k:child)
    monkeypatch.setattr(subject,'spawn',lambda *a,**k:child)
    # The final ownership stop must go through the (patched) stop routine.
    monkeypatch.setattr(subject,'confirm_stop',lambda value:subject._stop_child(value))
    monkeypatch.setattr(subject,'_owned_heartbeat',lambda *a,**k:True)
    now=datetime.now(timezone.utc).isoformat()
    monkeypatch.setattr(subject,'_read',lambda path:{'pid':124,'at':now,'last_step_at':now,
        'status':'RUNNING','recovery_only':False,'unresolved':False})
    write=subject._write
    def fail_status(path,value):
        if path.name=='supervisor-status.json':raise PermissionError('status publication failed')
        return write(path,value)
    monkeypatch.setattr(subject,'_write',fail_status)
    def stop(value):stopped.append(value.pid);value.running=False
    monkeypatch.setattr(subject,'_stop_child',stop)
    with pytest.raises(PermissionError):subject.main()
    assert stopped==[123] and not child.running
