from datetime import datetime, timezone, timedelta
from types import SimpleNamespace as NS
from unittest.mock import Mock
import pytest
from scripts import run_real_trader as trader
from autonomous_trader.real_runner import RealRunner
from autonomous_trader.real_waiting import market_unavailable

def test_empty_eligible_set_is_recoverable_without_relaxing_account_errors():
    assert market_unavailable(ValueError('NO_ELIGIBLE_REAL_PORTFOLIO_CONTEXT'))
    assert not market_unavailable(ValueError('REAL_ACCOUNT_BINDING_MISMATCH'))

@pytest.mark.parametrize('poll',[5,30,180,300])
def test_scheduled_wait_not_hung_but_stalled_wake_and_work_are(poll):
    start=datetime(2026,10,6,14,tzinfo=timezone.utc)
    progress={'at':start,'idle':True}
    for seconds in (0,poll/2,poll):
        now=start+timedelta(seconds=seconds)
        assert (now-trader.progress_observation(progress,now,poll)).total_seconds()==0
    now=start+timedelta(seconds=poll+121)
    assert (now-trader.progress_observation(progress,now,poll)).total_seconds()==121
    progress={'at':start,'idle':False}
    now=start+timedelta(seconds=121)
    assert (now-trader.progress_observation(progress,now,poll)).total_seconds()==121

@pytest.mark.parametrize('reason',['STALE_REAL_ACCOUNT','STALE_REAL_QUOTE','STALE_REAL_CONTEXT','REAL_CASH_MISMATCH'])
@pytest.mark.parametrize('held',[False,True])
def test_stale_evidence_waits_only_with_clean_flat_account(reason,held):
    runner=RealRunner.__new__(RealRunner)
    state=dict(positions={'x':{}} if held else {},reservations={},orders={},incidents={},latches={})
    runner.controller=NS(state=state,refresh=Mock(side_effect=ValueError(reason)),waiting_for_market_data=False)
    runner.prepare_waiting_market=Mock();runner.emit=Mock()
    if held or reason=='REAL_CASH_MISMATCH':
        with pytest.raises(ValueError,match=reason):runner._refresh_or_wait(object())
        assert not runner.controller.waiting_for_market_data
    else:
        assert runner._refresh_or_wait(object()) is False
        assert runner.controller.waiting_for_market_data
        runner.emit.assert_called_once()

def test_startup_phase_heartbeat_has_no_trading_or_reconciliation_authority(monkeypatch):
    receipt={'account_binding':'fixture','build_sha256':'build','market_session_label':'2026-10-06'}
    beats=[]
    monkeypatch.setattr(trader,'supervisor_heartbeat',beats.append)
    expected=object()
    def compose(**kwargs):
        kwargs['startup_progress']('POLICY_AUTHORITY')
        return expected
    monkeypatch.setattr(trader,'compose',compose)
    assert trader.compose_with_heartbeat(receipt,True) is expected
    assert beats and beats[0]['status']=='STARTING'
    assert beats[0]['production_enabled'] is False
    assert beats[0]['reconciliation_ready'] is False
    assert beats[0]['unresolved'] is True

def test_slow_and_failing_monitoring_cannot_block_publisher():
    import threading
    entered=threading.Event();release=threading.Event();finished=threading.Event()
    def stalled(payload):
        entered.set()
        release.wait(3)
        finished.set()
        raise RuntimeError('optional monitoring unavailable')
    monitor=trader.MonitoringHeartbeat(NS(heartbeat=stalled))
    try:
        monitor.submit({'status':'RUNNING'})
        assert entered.wait(1)
        for i in range(50):monitor.submit({'status':'RUNNING','n':i})
        assert monitor.pending.qsize()<=1
        monitor.close()
        assert not finished.is_set()
    finally:
        release.set();monitor.close();monitor.thread.join(2)
    assert not monitor.thread.is_alive()

def test_concurrent_heartbeat_publication_is_serialized(tmp_path,monkeypatch):
    import threading,json
    monkeypatch.setattr(trader,'data_root',lambda root:tmp_path)
    first=threading.Event();release=threading.Event();errors=[]
    original=trader.os.replace
    def replace(source,target):
        if not first.is_set():
            first.set();assert release.wait(2)
        original(source,target)
    monkeypatch.setattr(trader.os,'replace',replace)
    def publish(value):
        try:trader.supervisor_heartbeat({'n':value})
        except Exception as exc:errors.append(exc)
    one=threading.Thread(target=publish,args=(1,));two=threading.Thread(target=publish,args=(2,))
    one.start();assert first.wait(1);two.start();release.set();one.join(2);two.join(2)
    assert not errors and not one.is_alive() and not two.is_alive()
    assert json.loads((tmp_path/'.local/real-supervisor/child-heartbeat.json').read_text())=={'n':2}

def test_one_missing_heartbeat_after_long_uptime_does_not_restart(monkeypatch,tmp_path):
    from scripts import run_real_supervisor as supervisor
    now=datetime.now(timezone.utc)
    receipt=dict(build_sha256='build',account_binding='fixture',market_session_label='2026-10-06',recovery_not_after=now.isoformat())
    monkeypatch.setattr(supervisor,'ROOT',tmp_path)
    monkeypatch.setattr(supervisor,'_authority',lambda now:(receipt,False))
    monkeypatch.setattr(supervisor,'service_receipt',lambda *a,**k:receipt)
    child=NS(pid=123,running=True,wait=lambda:0)
    child.poll=lambda:None if child.running else 0
    monkeypatch.setattr(supervisor.subprocess,'Popen',lambda *a,**k:child)
    monkeypatch.setattr(supervisor,'spawn',lambda *a,**k:child)
    monkeypatch.setattr(supervisor,'confirm_stop',lambda value:None)
    beat=dict(pid=124,at=now.isoformat(),last_step_at=now.isoformat(),status='RUNNING',recovery_only=False,unresolved=False)
    readings=iter([beat,None,beat])
    def read(path):
        try:value=next(readings)
        except StopIteration:
            child.running=False
            return {**beat,'status':'STOPPED'}
        return value
    monkeypatch.setattr(supervisor,'_read',read)
    monkeypatch.setattr(supervisor,'_owned_heartbeat',lambda value,*a:bool(value))
    monkeypatch.setattr(supervisor,'_heartbeat_matches',lambda *a:True)
    ticks=iter([0,100,102,104,106])
    monkeypatch.setattr(supervisor.time,'monotonic',lambda:next(ticks))
    monkeypatch.setattr(supervisor.time,'sleep',lambda *a:None)
    monkeypatch.setattr(supervisor,'_stop_child',lambda *a:pytest.fail('healthy child stopped'))
    supervisor.main()

def test_validation_progress_scope_is_restored_after_exception():
    from autonomous_trader import real_owner_trust as trust
    events=[]
    with trust.observe_validation_progress(lambda:events.append('outer')):
        trust._validated_progress()
        with pytest.raises(RuntimeError):
            with trust.observe_validation_progress(lambda:events.append('inner')):
                trust._validated_progress();raise RuntimeError('fixture')
        trust._validated_progress()
    trust._validated_progress()
    assert events==['outer','inner','outer']

def test_monitor_close_preserves_pending_terminal_payload():
    import threading
    entered=threading.Event();release=threading.Event();seen=[]
    def write(payload):
        if not seen:entered.set();release.wait(3)
        seen.append(payload['status'])
    monitor=trader.MonitoringHeartbeat(NS(heartbeat=write))
    try:
        monitor.submit({'status':'RUNNING'});assert entered.wait(1)
        monitor.submit({'status':'STOPPED'});monitor.close();release.set();monitor.thread.join(2)
        assert seen==['RUNNING','STOPPED'] and not monitor.thread.is_alive()
    finally:release.set();monitor.close()


def test_heartbeat_publish_survives_a_momentary_windows_sharing_violation(tmp_path,monkeypatch):
    # 2026-10-14 18:06Z: a failed os.replace on the heartbeat file ended the trader with no trace.
    import os
    monkeypatch.setattr(trader,'data_root',lambda root:tmp_path)
    real=os.replace;calls=[]
    def flaky(src,dst):
        calls.append(1)
        if len(calls)<=5:raise PermissionError(13,'sharing violation')
        return real(src,dst)
    monkeypatch.setattr(trader.os,'replace',flaky)
    trader.supervisor_heartbeat({'n':1})
    assert len(calls)==6 and (tmp_path/'.local/real-supervisor/child-heartbeat.json').read_text()=='{"n":1}'


def test_a_persistent_heartbeat_failure_still_raises_and_leaves_a_trace(tmp_path,monkeypatch):
    import pytest
    monkeypatch.setattr(trader,'data_root',lambda root:tmp_path)
    monkeypatch.setattr(trader.time,'sleep',lambda s:None)
    def never(src,dst):raise PermissionError(13,'locked')
    monkeypatch.setattr(trader.os,'replace',never)
    with pytest.raises(PermissionError) as caught:trader.supervisor_heartbeat({'n':2})
    trader.record_pulse_failure(caught.value)
    log=(tmp_path/'.local/real-supervisor/child-failure.log').read_text()
    assert 'PermissionError' in log and 'locked' not in log


def test_step_history_records_only_changes_and_startup_failure_is_traced(tmp_path,monkeypatch):
    import json
    monkeypatch.setattr(trader,'data_root',lambda root:tmp_path)
    monkeypatch.setattr(trader,'_LAST_STEP',[None])
    for result in ('A','A','B','B','A'):trader.record_step(result)
    rows=[json.loads(x) for x in (tmp_path/'.local/real-supervisor/step-history.jsonl').read_text().splitlines()]
    assert [r['result'] for r in rows]==['A','B','A']
    trader.record_pulse_failure(ValueError('REAL_SOMETHING'),'startup')
    last=json.loads((tmp_path/'.local/real-supervisor/child-failure.log').read_text().splitlines()[-1])
    assert last['where']=='startup' and last['code']=='REAL_SOMETHING' and last['error_class']=='ValueError'


def test_entry_slot_database_timeout_fails_closed_instead_of_crashing_the_trader():
    from psycopg import errors
    runner = RealRunner.__new__(RealRunner)
    runner.clock = lambda: datetime(2026, 10, 15, 14, 0, tzinfo=timezone.utc)
    class Boom:
        def connect(self): raise errors.QueryCanceled('statement timeout')
    runner.store = NS(db=Boom())
    packet = NS(context={'quotes': []})
    from unittest.mock import patch
    with patch('autonomous_trader.real_triggers.reference', return_value={}):
        assert runner._entry_slot(NS(label=datetime(2026, 10, 15).date()), packet) == (None, None)
    assert runner.entry_slot_db_skips == 1


def test_events_hot_path_has_a_supporting_index_and_startup_rate_limit_is_tolerated():
    import inspect
    from autonomous_trader.monitoring import store
    assert 'events_kind_at ON events (kind, at DESC)' in inspect.getsource(store.provision)
    source = inspect.getsource(trader)
    assert 'isinstance(exc,ReadRateLimited)' in source and 'Pydantic serializer warnings' in source
