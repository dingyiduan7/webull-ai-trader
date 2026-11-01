from types import SimpleNamespace
from concurrent.futures import Future
from datetime import timedelta

import pytest

import autonomous_trader.real_runner as subject
from autonomous_trader.real_runner import RealRunner


class Calendar:
    def __init__(self,session):self.value=session
    def session(self,label):return self.value


class Controller:
    def __init__(self,state):
        self.state=state;self.incidents=[];self.account_id='real-'+'a'*64
    def lease(self):pass
    def incident(self,reason):self.incidents.append(reason)
    def downgrade_to_recovery(self,reason):self.incident(reason);self.recovery_only=True
    def refresh(self,packet):pass
    def refresh_flat_recovery(self,account):
        self.state['reconciliation']={'ready':True,'settlement_known':True}
    def send(self,command):
        row=self.state['positions'][command.instrument_id]
        row['protected_quantity']=str(command.quantity);row['stop_usd']=str(command.stop_usd)


class Store:
    def __init__(self):self.events=[]
    def emit_idempotent(self,kind,payload,**kwargs):self.events.append((kind,payload,kwargs))


class Source:
    def refresh(self,*args):pytest.fail('post-close account/market refresh is forbidden')


def real_order(intent):
    return intent(key='real-boundary',symbol='stock-a',quantity='1',price='99',stop='95').model_copy(
        update={'environment':'REAL','account_id':'real-'+'a'*64})


def runner(state,gateway,session,now,monkeypatch):
    state.setdefault('cash',{'settled_usd':'100','unsettled_usd':'0','liquidation_equity_usd':'100'})
    state.setdefault('settlements',{});state.setdefault('reservations',{});state.setdefault('latches',{})
    monkeypatch.setattr(subject,'reviewed_calendar',lambda:Calendar(session))
    source=Source()
    source.account_observation=lambda:{'open_client_ids':tuple(row['intent']['client_order_id'] for row in state['orders'].values() if not row['final']),
        'positions':tuple({'symbol':state['instruments'][iid]['ticker'],'quantity':row['quantity']} for iid,row in state['positions'].items()),
        'cash':{'settled_cash':'100','unsettled_cash':'0','cash_balance':'100','net_liquidation_value':'100'}}
    value=RealRunner(Controller(state),source,gateway,object(),Store(),
        lambda:pytest.fail('Astra must not run after close'),lambda:now)
    return value


def test_nonfinal_order_is_reconciled_after_close_then_latches_if_unresolved(intent,calendar,monkeypatch):
    order=real_order(intent);session=calendar.session(order.deadline.date());now=session.closes_at
    state={'orders':{order.intent_id:{'final':False,'phase':'ACKNOWLEDGED','intent':order.model_dump(mode='json')}},
        'instruments':{'stock-a':{'ticker':'AAA'}},'positions':{}}
    class Gateway:
        transport=SimpleNamespace(detail=lambda client:{'orders':[{'order_id':'broker-1'}]})
        def __init__(self):self.refresh=[]
        def reconcile_order(self,*args,refresh=True,**kwargs):self.refresh.append(refresh);return None
    gateway=Gateway();value=runner(state,gateway,session,now,monkeypatch)
    try:
        assert value.step()=='REAL_UNRESOLVED_ORDER_OUTSIDE_SESSION'
        assert gateway.refresh==[False]
        assert value.controller.incidents==['REAL_UNRESOLVED_ORDER_OUTSIDE_SESSION']
    finally:value.worker.shutdown(wait=False,cancel_futures=True)


def test_terminal_order_is_reconciled_before_closed_status(intent,calendar,monkeypatch):
    order=real_order(intent);session=calendar.session(order.deadline.date());now=session.closes_at
    state={'orders':{order.intent_id:{'final':False,'phase':'ACKNOWLEDGED','intent':order.model_dump(mode='json')}},
        'instruments':{'stock-a':{'ticker':'AAA'}},'positions':{}}
    class Gateway:
        transport=SimpleNamespace(detail=lambda client:{'orders':[{'order_id':'broker-1'}]})
        def reconcile_order(self,*args,refresh=True,**kwargs):
            state['orders'][order.intent_id]['final']=True
            return {'terminal':True}
    value=runner(state,Gateway(),session,now,monkeypatch)
    try:assert value.step()=='CLOSED_RECONCILED' and value.controller.incidents==[]
    finally:value.worker.shutdown(wait=False,cancel_futures=True)


def test_owned_position_after_close_never_reports_closed(intent,calendar,monkeypatch):
    order=real_order(intent);session=calendar.session(order.deadline.date());now=session.closes_at
    state={'orders':{},'instruments':{'stock-a':{'ticker':'AAA'}},
        'positions':{'stock-a':{'quantity':'1'}}}
    gateway=SimpleNamespace()
    value=runner(state,gateway,session,now,monkeypatch)
    try:
        assert value.step()=='REAL_EXPOSURE_OUTSIDE_SESSION'
        assert value.controller.incidents==['REAL_EXPOSURE_OUTSIDE_SESSION']
    finally:value.worker.shutdown(wait=False,cancel_futures=True)


def test_position_budget_denial_keeps_safety_runner_alive_and_blocks_retry(calendar,monkeypatch):
    session=calendar.session(subject.datetime(2026,9,11).date());now=session.opens_at+timedelta(hours=1)
    value=runner({'orders':{},'instruments':{},'positions':{}},SimpleNamespace(),session,now,monkeypatch)
    future=Future();future.set_exception(ValueError('REAL_SESSION_CALL_LIMIT'))
    context=SimpleNamespace(session_open=session.opens_at,session_close=session.closes_at)
    value.pending=(future,context,'POSITION','plan-a')
    try:
        assert value._apply(object()) is None
        assert value.pending is None
        assert value.position_call_blocked_until['plan-a']==now
        assert value.store.events[0][0]=='REAL_MODEL_CALL_BLOCKED'
        assert value.store.events[0][1]['summary']=='REAL_SESSION_CALL_LIMIT'
    finally:value.worker.shutdown(wait=False,cancel_futures=True)


def test_partial_fill_after_crash_is_reconciled_and_protected_before_wait(intent,calendar,monkeypatch):
    order=real_order(intent);session=calendar.session(order.deadline.date());now=session.opens_at+timedelta(minutes=5)
    state={'orders':{order.intent_id:{'final':False,'phase':'UNKNOWN','intent':order.model_dump(mode='json')}},
        'instruments':{'stock-a':{'ticker':'AAA'}},'positions':{},'incidents':{}}
    packet=object()
    class Gateway:
        transport=SimpleNamespace(detail=lambda client:{'orders':[{'order_id':'broker-1'}]})
        def reconcile_order(self,*args,**kwargs):
            state['orders'][order.intent_id]['phase']='PARTIALLY_FILLED'
            state['positions']['stock-a']={'quantity':'1','protected_quantity':'0'}
            return {'terminal':False,'status':'PARTIAL'}
    value=runner(state,Gateway(),session,now,monkeypatch)
    value.source.symbols=();value.source.refresh=lambda *args:packet
    value.source.exit_packet=lambda *args:packet
    value.gate=SimpleNamespace(check_recovery=lambda *args:None,check_exit_observation=lambda *args:None)
    try:
        assert value.step()=='ORDER_PENDING'
        assert state['positions']['stock-a']['protected_quantity']=='1'
        assert state['positions']['stock-a']['stop_usd']==str(order.protection.initial_stop_usd)
    finally:value.worker.shutdown(wait=False,cancel_futures=True)


def test_unknown_post_state_is_never_replayed_and_is_latched(intent,calendar,monkeypatch):
    order=real_order(intent);session=calendar.session(order.deadline.date());now=session.opens_at+timedelta(minutes=5)
    state={'orders':{order.intent_id:{'final':False,'phase':'UNKNOWN','intent':order.model_dump(mode='json')}},
        'instruments':{'stock-a':{'ticker':'AAA'}},'positions':{},'incidents':{}}
    gateway=SimpleNamespace(transport=SimpleNamespace(detail=lambda client:(_ for _ in ()).throw(ValueError('network'))),
        submit=lambda *args:pytest.fail('uncertain POST must never be replayed'))
    value=runner(state,gateway,session,now,monkeypatch)
    try:
        assert value.step()=='REAL_ORDER_TRANSPORT_UNCERTAIN'
        assert value.controller.incidents==['REAL_ORDER_TRANSPORT_UNCERTAIN']
    finally:value.worker.shutdown(wait=False,cancel_futures=True)


def test_entry_authority_expiry_downgrades_before_expiry(calendar,monkeypatch):
    session=calendar.session(subject.datetime(2026,9,11).date());now=session.opens_at+timedelta(minutes=5)
    value=runner({'orders':{},'instruments':{},'positions':{}},SimpleNamespace(),session,now,monkeypatch)
    value.authority_receipt={'expires_at':(now+timedelta(seconds=30)).isoformat()}
    try:
        value._authority_guard(now)
        assert value.controller.recovery_only is True
        assert value.controller.incidents==['REAL_ENTRY_AUTHORITY_EXPIRING']
    finally:value.worker.shutdown(wait=False,cancel_futures=True)


def test_pending_order_on_one_symbol_does_not_starve_other_position_safety_under_a_risk_latch(calendar,monkeypatch):
    session=calendar.session(subject.datetime(2026,9,11).date());now=session.opens_at+timedelta(hours=1)
    state={'orders':{},'instruments':{'a':{'ticker':'AAA'},'b':{'ticker':'BBB'}},
        'positions':{'a':{'quantity':'1','originating_orders':['oa']},'b':{'quantity':'2','originating_orders':['ob']}},
        'reservations':{'reserved-a':{'instrument_id':'a','shares':'1'}},'latches':{'drawdown':True},
        'cash':{'settled_usd':'100','unsettled_usd':'0','liquidation_equity_usd':'100'},'settlements':{}}
    exits=[]
    gateway=SimpleNamespace(protective_exit=lambda plan,reason,**kwargs:exits.append((plan.instrument_id,reason,kwargs['quantity'])),
        cancel_owned_buys_for_exit=lambda iid,packet,reason:packet)
    value=runner(state,gateway,session,now,monkeypatch)
    # Owner 2026-10-16: no latch (drawdown included) ever sells a holding; stops still protect.
    value.source.exit_packet=lambda *args:SimpleNamespace(bid_usd=subject.Decimal('5'))
    value.source.ownership_packet=lambda state,iid:pytest.fail('a latch must not use the forced-exit path')
    value.gate=SimpleNamespace(check_exit_observation=lambda *args:None,check_exit_ownership=lambda *args:None)
    value.plan=lambda plan_id:SimpleNamespace(plan_id=plan_id,instrument_id='a' if plan_id=='pa' else 'b',
        latest_exit_at=session.closes_at,active_stop_usd=subject.Decimal('1'),exit_request=None,exit_attempts=0)
    state['orders']={'oa':{'intent':{'plan_id':'pa'}},'ob':{'intent':{'plan_id':'pb'}}}
    try:
        assert value._monitor_owned_safety(session)==[]
        assert exits==[]
        value.source.exit_packet=lambda *args:SimpleNamespace(bid_usd=subject.Decimal('0.5'))
        assert value._monitor_owned_safety(session)==['PROTECTIVE_STOP']      # b; a's shares are reserved
        assert exits==[('b','PROTECTIVE_STOP',subject.Decimal('2'))]
    finally:value.worker.shutdown(wait=False,cancel_futures=True)


def test_post_close_cash_settlement_mismatch_never_reports_reconciled(calendar,monkeypatch):
    session=calendar.session(subject.datetime(2026,9,11).date());now=session.closes_at
    value=runner({'orders':{},'instruments':{},'positions':{},'cash':{'settled_usd':'100',
        'unsettled_usd':'0','liquidation_equity_usd':'100'},'settlements':{}},SimpleNamespace(),session,now,monkeypatch)
    account={'open_client_ids':(),'positions':(),
        'cash':{'settled_cash':'99','unsettled_cash':'0','cash_balance':'99','net_liquidation_value':'99'}}
    try:
        with pytest.raises(ValueError,match='REAL_POST_CLOSE_CASH_SETTLEMENT_MISMATCH'):
            value._post_close_exact(account)
    finally:value.worker.shutdown(wait=False,cancel_futures=True)


def test_protective_stop_requires_fresh_snapshot(calendar,monkeypatch):
    session=calendar.session(subject.datetime(2026,9,11).date());now=session.opens_at+timedelta(hours=1)
    state={'orders':{'oa':{'intent':{'plan_id':'pa'}}},'instruments':{'a':{'ticker':'AAA'}},
        'positions':{'a':{'quantity':'1','originating_orders':['oa']}},'reservations':{},'latches':{},
        'cash':{'settled_usd':'100','unsettled_usd':'0','liquidation_equity_usd':'100'},'settlements':{}}
    exits=[];value=runner(state,SimpleNamespace(protective_exit=lambda *args,**kwargs:exits.append(args)),session,now,monkeypatch)
    value.source.exit_packet=lambda *args:(_ for _ in ()).throw(ValueError('snapshot outage'))
    value.source.ownership_packet=lambda *args:pytest.fail('stop must not use quote-free authority')
    value.gate=SimpleNamespace(check_exit_observation=lambda *args:None,check_exit_ownership=lambda *args:None)
    value.plan=lambda plan_id:SimpleNamespace(plan_id=plan_id,instrument_id='a',latest_exit_at=session.closes_at,
        active_stop_usd=subject.Decimal('99'))
    try:
        assert value._monitor_owned_safety(session)==[]
        assert exits==[] and value.controller.incidents==['REAL_HELD_POSITION_MONITOR_FAILED']
    finally:value.worker.shutdown(wait=False,cancel_futures=True)


@pytest.mark.parametrize('outcome',['terminal-fill','uncertain-cancel'])
def test_mandatory_exit_cancels_owned_live_buy_before_fresh_exact_sell(calendar,monkeypatch,outcome):
    session=calendar.session(subject.datetime(2026,9,11).date());now=session.liquidation_at
    state={'orders':{'oa':{'intent':{'plan_id':'pa'}}},'instruments':{'a':{'ticker':'AAA'}},
        'positions':{'a':{'quantity':'1','originating_orders':['oa']}},'reservations':{},'latches':{},
        'cash':{'settled_usd':'100','unsettled_usd':'0','liquidation_equity_usd':'100'},'settlements':{}}
    exits=[]
    def cancel(iid,packet,reason):
        if outcome=='uncertain-cancel':raise ValueError('REAL_EXIT_BUY_CANCEL_UNCERTAIN')
        state['positions'][iid]['quantity']='2';return packet
    gateway=SimpleNamespace(cancel_owned_buys_for_exit=cancel,
        protective_exit=lambda plan,reason,**kwargs:exits.append(kwargs['quantity']))
    value=runner(state,gateway,session,now,monkeypatch)
    value.source.ownership_packet=lambda *args:SimpleNamespace()
    value.source.exit_packet=lambda *args:pytest.fail('mandatory exit cannot depend on snapshot')
    value.gate=SimpleNamespace(check_exit_ownership=lambda *args:None)
    value.plan=lambda plan_id:SimpleNamespace(plan_id=plan_id,instrument_id='a',latest_exit_at=session.closes_at,
        active_stop_usd=subject.Decimal('1'))
    try:
        result=value._monitor_owned_safety(session)
        if outcome=='terminal-fill':assert result==['MANDATORY_SAME_DAY_EXIT'] and exits==[subject.Decimal('2')]
        else:assert result==[] and exits==[] and value.controller.incidents==['REAL_EXIT_BUY_CANCEL_UNCERTAIN']
    finally:value.worker.shutdown(wait=False,cancel_futures=True)


@pytest.mark.parametrize('uncertain',[False,True])
def test_latch_cancels_live_buy_while_flat_before_order_pending(intent,calendar,monkeypatch,uncertain):
    order=real_order(intent);session=calendar.session(order.deadline.date());now=session.opens_at+timedelta(minutes=5)
    state={'orders':{order.intent_id:{'final':False,'phase':'ACKNOWLEDGED','intent':order.model_dump(mode='json')}},
        'instruments':{'stock-a':{'ticker':'AAA'}},'positions':{},'reservations':{},'latches':{'incident':True},
        'cash':{'settled_usd':'100','unsettled_usd':'0','liquidation_equity_usd':'100'},'settlements':{},'incidents':{}}
    gateway=SimpleNamespace()
    def cancel(*args):
        if uncertain:raise ValueError('uncertain')
        state['orders'][order.intent_id]['final']=True;state['orders'][order.intent_id]['phase']='CANCELED'
    gateway.cancel_owned_buys_for_exit=cancel
    gateway.transport=SimpleNamespace(detail=lambda *args:pytest.fail('terminal buy must not reach generic pending loop'))
    value=runner(state,gateway,session,now,monkeypatch);value.controller.recovery_only=True
    value.source.symbols=('AAA',);value.source.account_order_packet=lambda *args:object();value.source.refresh=lambda *args:object()
    # Current runtime acquires flat-recovery evidence through gate.read_checked
    # (authority audited before the read); this double accepts the read unchanged.
    value.gate=SimpleNamespace(check_recovery=lambda *args:None,check_flat_recovery=lambda *args:None,
        read_checked=lambda fetch,clock,**kwargs:(fetch(),None))
    value.source.account_observation=lambda:{'evidence':'flat'}
    value.controller.refresh_flat_recovery=lambda *args:None
    try:
        result=value.step()
        if uncertain:
            assert result=='REAL_LATCH_BUY_CANCEL_UNCERTAIN' and not state['orders'][order.intent_id]['final']
        else:
            assert result=='RECOVERY_FLAT_RECONCILED' and state['orders'][order.intent_id]['final']
    finally:value.worker.shutdown(wait=False,cancel_futures=True)


def test_incident_latch_never_force_sells_a_holding_but_the_stop_still_works(calendar,monkeypatch):
    session=calendar.session(subject.datetime(2026,9,11).date());now=session.opens_at+timedelta(hours=1)
    def build(bid):
        state={'orders':{'oa':{'intent':{'plan_id':'pa'}}},'instruments':{'a':{'ticker':'AAA'}},
            'positions':{'a':{'quantity':'1','originating_orders':['oa']}},'reservations':{},'latches':{'incident':True},
            'cash':{'settled_usd':'100','unsettled_usd':'0','liquidation_equity_usd':'100'},'settlements':{}}
        exits=[];value=runner(state,SimpleNamespace(protective_exit=lambda plan,reason,**kw:exits.append(reason),
            cancel_owned_buys_for_exit=lambda iid,packet,reason:packet),session,now,monkeypatch)
        value.source.exit_packet=lambda *args:SimpleNamespace(bid_usd=subject.Decimal(bid))
        value.source.ownership_packet=lambda *args:pytest.fail('an incident latch must not use the forced-exit path')
        value.gate=SimpleNamespace(check_exit_observation=lambda *args:None,check_exit_ownership=lambda *args:None)
        value.plan=lambda plan_id:SimpleNamespace(plan_id=plan_id,instrument_id='a',latest_exit_at=session.closes_at,
            active_stop_usd=subject.Decimal('66'))
        return value,exits
    value,exits=build('67')
    try:
        assert value._monitor_owned_safety(session)==[] and exits==[]
    finally:value.worker.shutdown(wait=False,cancel_futures=True)
    value,exits=build('65')
    try:
        assert value._monitor_owned_safety(session)==['PROTECTIVE_STOP'] and exits==['PROTECTIVE_STOP']
    finally:value.worker.shutdown(wait=False,cancel_futures=True)


def test_post_close_mismatch_records_which_check_failed_and_still_refuses(calendar,monkeypatch):
    session=calendar.session(subject.datetime(2026,9,11).date());now=session.closes_at
    value=runner({'orders':{},'instruments':{},'positions':{},'cash':{'settled_usd':'100',
        'unsettled_usd':'0','liquidation_equity_usd':'100'},'settlements':{},'session':{'label':'x'}},SimpleNamespace(),session,now,monkeypatch)
    events=[];value.emit=lambda kind,payload,key=None:events.append((kind,payload))
    value.controller.account_id='acct'
    account={'open_client_ids':(),'positions':(),
        'cash':{'settled_cash':'99','unsettled_cash':'0','cash_balance':'99','net_liquidation_value':'99'}}
    try:
        with pytest.raises(ValueError,match='REAL_POST_CLOSE_CASH_SETTLEMENT_MISMATCH'):
            value._post_close_exact(account)
        kind,payload=events[0]
        assert kind=='REAL_POST_CLOSE_MISMATCH_DETAIL'
        assert 'broker_settled_vs_ledger' in payload['failed'] and payload['broker']['settled']=='99'
    finally:value.worker.shutdown(wait=False,cancel_futures=True)
