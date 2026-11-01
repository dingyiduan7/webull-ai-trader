import sys
from datetime import datetime,timezone
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'/'watcher'))
import trade_watcher as w

OPEN=datetime(2026,10,15,16,0,tzinfo=timezone.utc)   # Thursday, market hours

def cur(**kw):
    base=dict(hb={'status':'RUNNING','unresolved':False,'recovery_only':False,'last_step_result':'ASTRA_ABSTAIN_RECORDED'},
        sup={},age=2.0,attempts=1,failures=0,blocked=[])
    base.update(kw);return base

def run(prev_cur,new_cur,now=OPEN):
    _,prev=w.decide(None,prev_cur,now)
    return {k for k,_ in w.decide(prev,new_cur,now)[0]}

def test_quiet_trader_raises_nothing():
    assert run(cur(),cur())==set()

def test_restart_and_failure_log_and_blocked_file_are_events():
    assert 'RESTART' in run(cur(),cur(attempts=2))
    assert 'CHILD_FAILURE' in run(cur(),cur(failures=1))
    assert 'LAUNCH_BLOCKED' in run(cur(),cur(blocked=['daily-blocked-x.json']))

def test_stale_heartbeat_in_market_hours_alerts_but_not_after_clean_stop():
    assert 'HEARTBEAT_STALE' in run(cur(),cur(age=300.0))
    stopped=cur(age=300.0,hb={'status':'STOPPED','last_step_result':'CLOSED_RECONCILED'})
    assert 'HEARTBEAT_STALE' not in run(cur(),stopped)

def test_stale_heartbeat_overnight_is_quiet():
    night=datetime(2026,10,15,3,0,tzinfo=timezone.utc)
    assert run(cur(),cur(age=9999.0),night)==set()

def test_fill_and_exit_and_recovery_and_close():
    hb=lambda **k:{**cur()['hb'],**k}
    assert 'POSITION_OR_ORDER_OPEN' in run(cur(),cur(hb=hb(unresolved=True)))
    assert 'POSITION_OR_ORDER_CLOSED' in run(cur(hb=hb(unresolved=True)),cur())
    assert 'RECOVERY_ONLY' in run(cur(),cur(hb=hb(recovery_only=True)))
    assert 'SESSION_CLOSED' in run(cur(),cur(hb=hb(last_step_result='CLOSED_RECONCILED')))

def test_not_running_at_open_alerts_once_started_never_seen():
    late=datetime(2026,10,15,14,0,tzinfo=timezone.utc)
    assert 'NOT_RUNNING_AT_OPEN' in run(cur(age=None,hb={}),cur(age=None,hb={}),late)
    assert 'NOT_RUNNING_AT_OPEN' not in run(cur(),cur(age=None,hb={}),late)  # seen running earlier today

def test_first_observation_only_baselines():
    assert w.decide(None,cur(attempts=5,failures=3),OPEN)[0]==[]
