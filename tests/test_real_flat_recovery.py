from copy import deepcopy
from datetime import datetime,timezone,timedelta
from types import SimpleNamespace
from decimal import Decimal
import pytest
from autonomous_trader.real_trading import RealController,RealGate,flat_recovery_values
from autonomous_trader.real_runner import RealRunner

NOW=datetime(2026,10,1,15,tzinfo=timezone.utc)
ACCOUNT='real-'+'a'*64

def state():
    return dict(positions={},reservations={},orders={},cash={'settled_usd':'505.44','unsettled_usd':'0',
        'liquidation_equity_usd':'505.44'},latches={'incident':True},
        incidents={'legacy':{'reason':'REAL_RECOVERY_RECONCILIATION_FAILED','pending_correction':False}})

def account():
    return dict(account_binding=ACCOUNT,observed_at=NOW,received_at=NOW,positions=(),open_client_ids=(),
        evidence_id='fresh-account',cash={'settled_cash':'505.44','unsettled_cash':'0',
            'cash_balance':'505.44','net_liquidation_value':'505.44'})

@pytest.mark.parametrize('bad',['identity','stale','future','broker-position','broker-order','local-position',
    'local-order','unknown-order','reservation','cash','unsettled','equity','nan'])
def test_flat_account_recovery_rejects_actual_safety_mismatch(bad):
    s=state();a=account()
    if bad=='identity':a['account_binding']='wrong'
    elif bad=='stale':a['observed_at']=NOW-timedelta(seconds=11)
    elif bad=='future':a['observed_at']=a['received_at']=NOW+timedelta(seconds=1)
    elif bad=='broker-position':a['positions']=({'quantity':'1'},)
    elif bad=='broker-order':a['open_client_ids']=('unknown',)
    elif bad=='local-position':s['positions']['held']={'quantity':'1'}
    elif bad=='local-order':s['orders']['pending']={'final':False,'phase':'ACKNOWLEDGED'}
    elif bad=='unknown-order':s['orders']['pending']={'final':True,'phase':'UNKNOWN'}
    elif bad=='reservation':s['reservations']['pending']={'cash_usd':'1'}
    elif bad=='cash':a['cash']['settled_cash']='504.44'
    elif bad=='unsettled':a['cash'].update(settled_cash='504.44',unsettled_cash='1')
    elif bad=='equity':a['cash']['net_liquidation_value']='504.44'
    elif bad=='nan':a['cash']['settled_cash']='NaN'
    with pytest.raises(ValueError):flat_recovery_values(a,s,ACCOUNT,NOW)

def test_flat_recovery_reconciliation_preserves_incident_and_all_latches():
    s=state();before=deepcopy(s);commands=[]
    controller=SimpleNamespace(state=s,account_id=ACCOUNT,clock=lambda:NOW,lease=lambda:None)
    controller.send=lambda command:commands.append(command) or s
    RealController.refresh_flat_recovery(controller,account())
    assert len(commands)==1 and commands[0].kind=='RECONCILE'
    assert commands[0].resolved_incident_ids==() and commands[0].positions==()
    assert commands[0].settled_usd==Decimal('505.44')
    assert s['incidents']==before['incidents'] and s['latches']==before['latches']

def test_flat_gate_checks_exact_offline_authority_before_database(monkeypatch):
    gate=object.__new__(RealGate);gate.root='root';gate.account=ACCOUNT
    def denied(*args,**kwargs):raise ValueError('JUDICATOR_REAL_BUILD_OR_SCOPE_MISMATCH')
    monkeypatch.setattr(RealGate,'offline_receipt',denied)
    gate.approved=lambda *args:pytest.fail('database before offline GO')
    with pytest.raises(ValueError,match='JUDICATOR_REAL_BUILD_OR_SCOPE_MISMATCH'):
        gate.check_flat_recovery(account(),NOW,state())

def _recovery_receipt(monkeypatch):
    # Current runtime contract: account-only recovery re-verifies a service
    # receipt (account, build, session, validity window) around every read.
    receipt=dict(account_binding=ACCOUNT,build_sha256='b'*64,market_session_label='2026-10-01',
        flat_checkpoint_sha256='c'*64,daily_authority_sha256='d'*64,
        issued_at=(NOW-timedelta(minutes=1)).isoformat(),expires_at=(NOW+timedelta(hours=2)).isoformat(),
        recovery_not_after=(NOW+timedelta(days=1)).isoformat())
    monkeypatch.setattr('autonomous_trader.real_runner.service_receipt',lambda *a,**kw:dict(receipt))
    return receipt

def _gate():
    checkpoint={'cash':{'settled_usd':'505.44','unsettled_usd':'0','equity_usd':'505.44'}}
    return SimpleNamespace(root='unused',checkpoint=lambda _:(deepcopy(checkpoint),None))

def test_flat_recovery_never_fetches_btcc_candidate_quote_or_calls_astra(monkeypatch):
    receipt=_recovery_receipt(monkeypatch)
    s=state();commands=[]
    controller=SimpleNamespace(state=s,account_id=ACCOUNT,recovery_only=True,clock=lambda:NOW,
        lease=lambda:None,send=lambda command:commands.append(command) or s)
    controller.refresh_flat_recovery=lambda a:RealController.refresh_flat_recovery(controller,a)
    source=SimpleNamespace(account_observation=account,
        refresh=lambda *args:pytest.fail('BTTC quote must not control flat recovery'))
    store=SimpleNamespace(emit_idempotent=lambda *args,**kwargs:None)
    runner=RealRunner(controller,source,SimpleNamespace(),_gate(),store,
        lambda:pytest.fail('no paid model in recovery'),lambda:NOW)
    runner.authority_receipt=dict(receipt)
    runner._single_writer=lambda:True
    try:
        # One clean observation is never enough: confirmation is pending and
        # the incident, its latch and recovery-only mode all remain.
        assert runner._recover_flat_account_incident()=='RECOVERY_ACCOUNT_CONFIRMATION_PENDING'
        assert commands[0].resolved_incident_ids==() and s['latches']['incident']
        assert runner.pending is None and controller.recovery_only
    finally:runner.worker.shutdown(wait=False,cancel_futures=True)

def test_flat_recovery_failure_retains_stage_and_safe_subcode(monkeypatch):
    receipt=_recovery_receipt(monkeypatch)
    s=state();events=[]
    def incident(reason):s['incidents']['new']={'reason':reason,'pending_correction':False}
    controller=SimpleNamespace(state=s,account_id=ACCOUNT,recovery_only=True,lease=lambda:None,incident=incident)
    def broken():raise ValueError('STALE_REAL_ACCOUNT')
    runner=RealRunner(controller,SimpleNamespace(account_observation=broken),SimpleNamespace(),_gate(),
        SimpleNamespace(emit_idempotent=lambda kind,payload,**kwargs:events.append(payload)),lambda:None,lambda:NOW)
    runner.authority_receipt=dict(receipt)
    runner._single_writer=lambda:True
    try:
        assert runner._recover_flat_account_incident()=='REAL_ACCOUNT_RECONCILIATION_FAILED'
        assert events[-1]['failure_stage']=='ACCOUNT_ONLY_RECOVERY'
        assert events[-1]['failure_subcode']=='STALE_REAL_ACCOUNT'
        assert s['latches']['incident']
    finally:runner.worker.shutdown(wait=False,cancel_futures=True)
