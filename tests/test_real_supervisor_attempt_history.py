import json
from datetime import datetime,timezone,timedelta
import pytest
from scripts.run_real_supervisor import _record_attempt

NOW=datetime(2026,10,5,18,28,tzinfo=timezone.utc)


@pytest.mark.parametrize('reason,forced,code',[
    ('CHILD_EXIT',False,2),('PROGRESS_AGE_EXCEEDED',True,1),
    ('OWNED_HEARTBEAT_NOT_CONFIRMED',True,1),('SUPERVISOR_OBSERVATION_FAILED',True,None)])
def test_attempt_history_records_exact_reason_without_raw_configuration(tmp_path,reason,forced,code):
    heartbeat={'status':'WAITING_FOR_MARKET_DATA','at':(NOW-timedelta(seconds=3)).isoformat(),
        'last_step_at':(NOW-timedelta(seconds=46)).isoformat(),'secret':'MUST_NOT_APPEAR'}
    for attempt in range(2):
        assert _record_attempt(tmp_path,attempt=attempt,launcher_pid=12,child_pid=13,code=code,
            forced=forced,reason=reason,heartbeat=heartbeat,at=NOW)
    text=(tmp_path/'attempt-history.jsonl').read_text();rows=[json.loads(x) for x in text.splitlines()]
    assert [r['attempt'] for r in rows]==[1,2]
    assert rows[-1]['last_progress_age_seconds']==46
    assert rows[-1]['reason']==reason and rows[-1]['exit_code']==code
    assert rows[-1]['forced_stop']==forced and 'MUST_NOT_APPEAR' not in text


def test_malformed_observation_and_unwritable_diagnostics_do_not_control_worker(tmp_path):
    kwargs=dict(attempt=0,launcher_pid=12,child_pid=None,code=None,forced=False,
        reason='secret reason',heartbeat={'status':'secret status','at':'invalid'},at=NOW)
    assert _record_attempt(tmp_path,**kwargs)
    row=json.loads((tmp_path/'attempt-history.jsonl').read_text())
    assert row['heartbeat_status'] is None and row['heartbeat_age_seconds'] is None
    assert 'secret' not in (tmp_path/'attempt-history.jsonl').read_text()
    assert _record_attempt(tmp_path/'nonexistent',**kwargs) is False


@pytest.mark.parametrize('heartbeat',[None,[],{'status':[]},{'status':{}}])
def test_non_mapping_or_non_string_status_cannot_break_diagnostics(tmp_path,heartbeat):
    assert _record_attempt(tmp_path,attempt=0,launcher_pid=12,child_pid=None,code=2,
        forced=False,reason='CHILD_EXIT',heartbeat=heartbeat,at=NOW)
    row=json.loads((tmp_path/'attempt-history.jsonl').read_text())
    assert row['heartbeat_status'] is None and row['reason']=='CHILD_EXIT'
