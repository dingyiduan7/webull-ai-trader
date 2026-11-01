"""Held-position incident recovery (owner approval 2026-10-19): an operational read failure while holding
clears only after two fresh distinct broker observations equal the ledger exactly on shares and cash."""
from copy import deepcopy
from datetime import datetime,timezone,timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from autonomous_trader.real_runner import RealRunner
from autonomous_trader.real_trading import RealController


NOW=datetime(2026,10,19,16,0,tzinfo=timezone.utc)
ACCOUNT='real-'+'a'*64


def state(reason='REAL_ACCOUNT_RECONCILIATION_FAILED'):
    return {'cash':{'settled_usd':'136.72','unsettled_usd':'0.00','reserved_usd':'0',
            'liquidation_equity_usd':'504.66'},
        'positions':{'intc':{'quantity':'2'},'crwv':{'quantity':'3'}},'reservations':{},
        'orders':{'o1':{'final':True,'phase':'FILLED'},'o2':{'final':True,'phase':'FILLED'}},
        'incidents':{'incident-a':{'reason':reason,'pending_correction':False}},
        'latches':{'daily':False,'drawdown':False,'incident':True,'terminated':False},
        'reconciliation':{'ready':True,'settlement_known':True}}


def packet(*,evidence='evidence-a',at=NOW,positions=None,orders=(),settled='136.72',unsettled='0.00',equity='504.66'):
    return SimpleNamespace(evidence_id=evidence,observed_at=at,account_binding=ACCOUNT,
        positions={'intc':Decimal(2),'crwv':Decimal(3)} if positions is None else positions,open_client_ids=orders,
        settled=Decimal(settled),unsettled=Decimal(unsettled),equity=Decimal(equity),
        context=SimpleNamespace(session_open=datetime(2026,10,19,13,30,tzinfo=timezone.utc)),
        validate=lambda now,flat=False:None)


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
    def __init__(self,value):self.state=value;self.account_id=ACCOUNT;self.held=[];self.flat=[]
    def resolve_held_reconciliation_incidents(self,value,ids):
        self.held.append((value.evidence_id,tuple(ids)))
        for key in ids:self.state['incidents'].pop(key)
        self.state['latches']['incident']=False
    def resolve_flat_reconciliation_incidents(self,value,ids):self.flat.append(tuple(ids))
    def incident(self,reason):raise AssertionError('existing incident must not be duplicated')


def runner(value=None):
    controller=Controller(value or state());store=Store()
    result=RealRunner(controller,SimpleNamespace(),SimpleNamespace(),SimpleNamespace(),store,
        lambda:pytest.fail('no model call'),lambda:NOW)
    result.authority_receipt={'account_binding':ACCOUNT,'build_sha256':'b'*64,'market_session_label':'2026-10-19'}
    return result


def close(value):value.worker.shutdown(wait=False,cancel_futures=True)


@pytest.mark.parametrize('reason',['REAL_ACCOUNT_RECONCILIATION_FAILED','REAL_RECOVERY_RECONCILIATION_FAILED',
    'REAL_HELD_POSITION_MONITOR_FAILED'])
def test_two_exact_held_observations_clear_read_failure(reason):
    value=runner(state(reason))
    try:
        assert value._record_reconciliation_success(packet(),None) is False
        assert value.controller.held==[] and value.controller.state['latches']['incident']
        assert value._record_reconciliation_success(packet(evidence='evidence-b',at=NOW+timedelta(seconds=5)),None) is True
        assert value.controller.held==[('evidence-b',('incident-a',))] and value.controller.flat==[]
        event=value.store.events[-1]
        assert event[0]=='REAL_RECONCILIATION_RESOLUTION_EVIDENCE'
        assert event[1]['broker_positions']==2 and event[1]['local_positions']==2 and event[1]['broker_mutations']==0
    finally:close(value)


def test_equity_moving_with_marks_does_not_block():
    value=runner()
    try:
        value._record_reconciliation_success(packet(equity='500.00'),None)
        assert value._record_reconciliation_success(packet(evidence='b',at=NOW+timedelta(seconds=5),equity='498.10'),None)
    finally:close(value)


@pytest.mark.parametrize('mutation',['share-count','missing-position','extra-position','broker-order','local-order',
    'unknown-order','reservation','settled','unsettled','mismatch-reason','settlement-mismatch-reason','other-incident',
    'daily-latch','drawdown-latch','terminated-latch','not-ready','settlement-unknown','pending-correction'])
def test_any_disagreement_or_other_safety_state_keeps_lock(mutation):
    current=state();observed=packet()
    if mutation=='share-count':observed=packet(positions={'intc':Decimal(2),'crwv':Decimal(2)})
    elif mutation=='missing-position':observed=packet(positions={'intc':Decimal(2)})
    elif mutation=='extra-position':observed=packet(positions={'intc':Decimal(2),'crwv':Decimal(3),'x':Decimal(1)})
    elif mutation=='broker-order':observed=packet(orders=('client-a',))
    elif mutation=='local-order':current['orders']['o3']={'final':False,'phase':'ACKNOWLEDGED'}
    elif mutation=='unknown-order':current['orders']['o3']={'final':True,'phase':'UNKNOWN'}
    elif mutation=='reservation':current['reservations']['r']={}
    elif mutation=='settled':observed=packet(settled='136.71')
    elif mutation=='unsettled':observed=packet(unsettled='0.01')
    elif mutation=='mismatch-reason':current=state('RECONCILIATION_MISMATCH')
    elif mutation=='settlement-mismatch-reason':current=state('REAL_POST_CLOSE_CASH_SETTLEMENT_MISMATCH')
    elif mutation=='other-incident':current['incidents']['incident-b']={'reason':'REAL_ORDER_TRANSPORT_UNCERTAIN','pending_correction':False}
    elif mutation.endswith('-latch'):current['latches'][mutation.removesuffix('-latch')]=True
    elif mutation=='not-ready':current['reconciliation']['ready']=False
    elif mutation=='settlement-unknown':current['reconciliation']['settlement_known']=False
    elif mutation=='pending-correction':current['incidents']['incident-a']['pending_correction']=True
    value=runner(current)
    try:
        value._record_reconciliation_success(observed,None)
        value._record_reconciliation_success(deepcopy(observed).__class__(**{**vars(observed),'evidence_id':'b',
            'observed_at':NOW+timedelta(seconds=5)}),None)
        assert value.controller.held==[] and value.controller.flat==[]
    finally:close(value)


def test_single_or_repeated_observation_is_not_enough():
    value=runner()
    try:
        first=packet();value._record_reconciliation_success(first,None)
        value._record_reconciliation_success(first,None)
        assert value.controller.held==[]
    finally:close(value)


# ---- controller: journals one exact RECONCILE with the broker positions and the resolved ids ----

def controller(value):
    sent=[]
    class Fake(RealController):
        state=None
    c=Fake.__new__(Fake)
    c.state=value;c.account_id=ACCOUNT;c.clock=lambda:NOW;c.lease=lambda:None
    def send(command):
        sent.append(command)
        for key in command.resolved_incident_ids:value['incidents'].pop(key)
        value['latches']['incident']=bool(value['incidents'])
        return value
    c.send=send
    return c,sent


def test_controller_journals_exact_reconcile():
    c,sent=controller(state())
    c.resolve_held_reconciliation_incidents(packet(),('incident-a',))
    (command,)=sent
    assert command.kind=='RECONCILE' and command.resolved_incident_ids==('incident-a',)
    assert dict(command.positions)=={'intc':Decimal(2),'crwv':Decimal(3)} and command.outstanding_intent_ids==()
    assert command.settled_usd==Decimal('136.72') and command.unsettled_usd==Decimal('0.00')


@pytest.mark.parametrize('mutation,code',[('shares','REAL_RECONCILIATION_RECOVERY_HELD_MISMATCH'),
    ('cash','REAL_RECONCILIATION_RECOVERY_CASH_MISMATCH'),('reason','REAL_RECONCILIATION_INCIDENT_SCOPE_INVALID'),
    ('flat','REAL_RECONCILIATION_RECOVERY_HELD_MISMATCH'),('latch','REAL_RECONCILIATION_RECOVERY_SAFETY_STATE')])
def test_controller_refuses_disagreement(mutation,code):
    current=state();observed=packet()
    if mutation=='shares':observed=packet(positions={'intc':Decimal(1),'crwv':Decimal(3)})
    elif mutation=='cash':observed=packet(settled='100.00')
    elif mutation=='reason':current=state('RECONCILIATION_MISMATCH')
    elif mutation=='flat':current['positions']={};observed=packet(positions={})
    elif mutation=='latch':current['latches']['drawdown']=True
    c,sent=controller(current)
    with pytest.raises(ValueError,match=code):c.resolve_held_reconciliation_incidents(observed,('incident-a',))
    assert sent==[]
