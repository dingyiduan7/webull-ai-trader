from copy import deepcopy
from datetime import datetime,timezone,timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from autonomous_trader.real_runner import RealRunner,_reconciliation_failure_subcode
from autonomous_trader.real_trading import RealController


NOW=datetime(2026,9,30,18,0,tzinfo=timezone.utc)
ACCOUNT='real-'+'a'*64


def state():
    return {'cash':{'settled_usd':'505.44','unsettled_usd':'0.00','reserved_usd':'0',
            'liquidation_equity_usd':'505.44'},'positions':{},'reservations':{},'orders':{},
        'incidents':{'incident-a':{'reason':'REAL_ACCOUNT_RECONCILIATION_FAILED','pending_correction':False}},
        'latches':{'daily':False,'drawdown':False,'incident':True,'terminated':False},
        'reconciliation':{'ready':True,'settlement_known':True}}


def packet(*,evidence='evidence-a',at=NOW,positions=None,orders=(),settled='505.44',
        unsettled='0.00',equity='505.44'):
    return SimpleNamespace(evidence_id=evidence,observed_at=at,account_binding=ACCOUNT,
        positions={} if positions is None else positions,open_client_ids=orders,
        settled=Decimal(settled),unsettled=Decimal(unsettled),equity=Decimal(equity),
        context=SimpleNamespace(session_open=datetime(2026,9,30,13,30,tzinfo=timezone.utc)))


CHECKPOINT={'cash':{'settled_usd':'505.44','unsettled_usd':'0.00','equity_usd':'505.44'}}


class Result:
    def fetchall(self):return [(True,1)]


class Connection:
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def execute(self,*args):return Result()


class Store:
    def __init__(self):self.events=[];self.db=SimpleNamespace(connect=lambda:Connection())
    def emit_idempotent(self,kind,payload,**kwargs):self.events.append((kind,payload,kwargs))


class Controller:
    def __init__(self,value):self.state=value;self.account_id=ACCOUNT;self.resolutions=[]
    def resolve_flat_reconciliation_incidents(self,value,ids):
        self.resolutions.append((value.evidence_id,tuple(ids)))
        for key in ids:self.state['incidents'].pop(key)
        self.state['latches']['incident']=False
    def incident(self,reason):raise AssertionError('existing incident must not be duplicated')


def runner(value=None):
    controller=Controller(value or state());store=Store()
    result=RealRunner(controller,SimpleNamespace(),SimpleNamespace(),SimpleNamespace(),store,
        lambda:pytest.fail('Astra must remain unreachable'),lambda:NOW)
    result.authority_receipt={'account_binding':ACCOUNT,'build_sha256':'b'*64,'market_session_label':'2026-09-30'}
    return result


def close(value):value.worker.shutdown(wait=False,cancel_futures=True)


def test_two_consecutive_exact_flat_successes_resolve_only_exact_incident():
    value=runner()
    try:
        assert value._record_reconciliation_success(packet(),CHECKPOINT) is False
        assert value.controller.resolutions==[] and value.controller.state['latches']['incident']
        assert value._record_reconciliation_success(packet(evidence='evidence-b',at=NOW+timedelta(seconds=1)),CHECKPOINT) is True
        assert value.controller.resolutions==[('evidence-b',('incident-a',))]
        assert value.controller.state['incidents']=={} and not value.controller.state['latches']['incident']
        event=value.store.events[-1]
        assert event[0]=='REAL_RECONCILIATION_RESOLUTION_EVIDENCE'
        assert event[1]['incident_ids']==['incident-a']
        assert [row['evidence_id'] for row in event[1]['observations']]==['evidence-a','evidence-b']
        assert event[1]['broker_positions']==event[1]['broker_open_orders']==0
        assert event[1]['broker_mutations']==0 and event[1]['writer_count']==1
    finally:close(value)


def test_failure_between_successes_resets_streak():
    value=runner()
    try:
        value._record_reconciliation_success(packet(),CHECKPOINT)
        assert value._reconciliation_failure(ValueError('STALE_REAL_QUOTE'),'SOURCE_REFRESH')=='REAL_ACCOUNT_RECONCILIATION_FAILED'
        assert value.reconciliation_successes==[]
        value._record_reconciliation_success(packet(evidence='evidence-b',at=NOW+timedelta(seconds=1)),CHECKPOINT)
        assert value.controller.resolutions==[]
    finally:close(value)


def test_process_restart_does_not_inherit_success():
    first=runner();first._record_reconciliation_success(packet(),CHECKPOINT);close(first)
    second=runner()
    try:
        second._record_reconciliation_success(packet(evidence='evidence-b',at=NOW+timedelta(seconds=1)),CHECKPOINT)
        assert second.controller.resolutions==[] and len(second.reconciliation_successes)==1
    finally:close(second)


@pytest.mark.parametrize('mutation',["broker-position","broker-order","local-position","local-order",
    "unknown-order","cash","other-incident","daily-latch","drawdown-latch","terminated-latch",
    "settlement-unknown","checkpoint-cash","pending-correction"])
def test_mismatch_or_other_safety_state_never_resolves(mutation):
    current=state();observed=packet();checkpoint=deepcopy(CHECKPOINT)
    if mutation=='broker-position':observed=packet(positions={'stock-a':Decimal(1)})
    elif mutation=='broker-order':observed=packet(orders=('client-a',))
    elif mutation=='local-position':current['positions']['stock-a']={'quantity':'1'}
    elif mutation=='local-order':current['orders']['order-a']={'final':False,'phase':'ACKNOWLEDGED'}
    elif mutation=='unknown-order':current['orders']['order-a']={'final':True,'phase':'UNKNOWN'}
    elif mutation=='cash':observed=packet(settled='505.43',equity='505.43')
    elif mutation=='other-incident':current['incidents']['incident-b']={'reason':'OTHER','pending_correction':False}
    elif mutation.endswith('-latch'):current['latches'][mutation.removesuffix('-latch')]=True
    elif mutation=='settlement-unknown':current['reconciliation']['settlement_known']=False
    elif mutation=='checkpoint-cash':checkpoint['cash']['settled_usd']='505.43'
    elif mutation=='pending-correction':current['incidents']['incident-a']['pending_correction']=True
    value=runner(current)
    try:
        value._record_reconciliation_success(observed,checkpoint)
        value._record_reconciliation_success(packet(evidence='evidence-b',at=NOW+timedelta(seconds=1)),checkpoint)
        assert value.controller.resolutions==[]
    finally:close(value)


def test_duplicate_or_nonmonotonic_observation_is_not_two_fresh_successes():
    value=runner()
    try:
        first=packet();value._record_reconciliation_success(first,CHECKPOINT)
        value._record_reconciliation_success(first,CHECKPOINT)
        assert value.reconciliation_successes==[] and value.controller.resolutions==[]
        value._record_reconciliation_success(packet(evidence='older',at=NOW-timedelta(seconds=1)),CHECKPOINT)
        assert len(value.reconciliation_successes)==1 and value.controller.resolutions==[]
    finally:close(value)


def test_stale_gate_failure_resets_first_success():
    value=runner()
    try:
        value._record_reconciliation_success(packet(),CHECKPOINT)
        value._reconciliation_failure(ValueError('STALE_REAL_ACCOUNT'),'GATE_CHECK')
        value._record_reconciliation_success(packet(evidence='evidence-b',at=NOW+timedelta(seconds=1)),CHECKPOINT)
        assert len(value.reconciliation_successes)==1 and value.controller.resolutions==[]
    finally:close(value)


def test_missing_single_writer_never_resolves(monkeypatch):
    value=runner();monkeypatch.setattr(value,'_single_writer',lambda:False)
    try:
        value._record_reconciliation_success(packet(),CHECKPOINT)
        value._record_reconciliation_success(packet(evidence='evidence-b',at=NOW+timedelta(seconds=1)),CHECKPOINT)
        assert value.reconciliation_successes==[] and value.controller.resolutions==[]
    finally:close(value)


@pytest.mark.parametrize('field,value',[('account_binding','real-'+'b'*64),('market_session_label','2026-09-29')])
def test_receipt_binding_mismatch_never_resolves(field,value):
    subject=runner();subject.authority_receipt[field]=value
    try:
        subject._record_reconciliation_success(packet(),CHECKPOINT)
        subject._record_reconciliation_success(packet(evidence='evidence-b',at=NOW+timedelta(seconds=1)),CHECKPOINT)
        assert subject.controller.resolutions==[]
    finally:close(subject)


def test_failure_subcode_never_retains_arbitrary_text():
    assert _reconciliation_failure_subcode(ValueError('STALE_REAL_QUOTE'),'SOURCE_REFRESH')=='STALE_REAL_QUOTE'
    assert _reconciliation_failure_subcode(ValueError('token=secret broker body'),'SOURCE_REFRESH')=='SOURCE_REFRESH_VALUE_ERROR'
    assert _reconciliation_failure_subcode(RuntimeError('private response'),'GATE_CHECK')=='GATE_CHECK_UNEXPECTED_ERROR'


def test_controller_resolution_command_is_exact_and_history_is_not_rewritten():
    current=state();history=[{'kind':'INCIDENT','event_id':'incident-a','reason':'REAL_ACCOUNT_RECONCILIATION_FAILED'}]
    value=packet();value.validate=lambda now,flat=False:None
    subject=SimpleNamespace(clock=lambda:NOW,account_id=ACCOUNT,state=current,lease=lambda:None)
    def send(command):
        history.append({'kind':command.kind,'resolved_incident_ids':command.resolved_incident_ids})
        for key in command.resolved_incident_ids:current['incidents'].pop(key)
        current['latches']['incident']=bool(current['incidents']);return current
    subject.send=send
    result=RealController.resolve_flat_reconciliation_incidents(subject,value,('incident-a',))
    assert result['incidents']=={} and history[0]['kind']=='INCIDENT'
    assert history[-1]=={'kind':'RECONCILE','resolved_incident_ids':('incident-a',)}
