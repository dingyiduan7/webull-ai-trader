from datetime import timedelta
from decimal import Decimal as D
from types import SimpleNamespace
from uuid import uuid4
import json

import pytest

from test_cycle import NOW,SyntheticBrain,synthetic_proposal,setup_cycle
from autonomous_trader.contracts import AlphaManagedPlan,OrderIntent,Target,payload_hash
from autonomous_trader.decisions import parse_trade_decision,validate_semantics,DecisionError
from autonomous_trader.durable.models import Protection
from autonomous_trader.mvp.context import Observation,UniverseObservation,SandboxSource,make_position_review_context
from autonomous_trader.mvp.gateway import LocalPositionManager
from autonomous_trader.mvp.alpha import AlphaPortfolioManager
from autonomous_trader.monitoring.runner import recover_plan_state
from autonomous_trader.sandbox.http import Operation


def prepared(setup_cycle):
    args,broker,_=setup_cycle
    from autonomous_trader.mvp.cycle import run_trading_cycle
    result=run_trading_cycle(**args,brain=SyntheticBrain())
    intent=OrderIntent.model_validate(result['generated_intent'])
    original=parse_trade_decision(json.dumps(result['ai']['decision'])).entry_plan
    plan=original.model_copy(update={'latest_exit_at':NOW+timedelta(minutes=30)})
    controller=args['controller']
    controller.send(Protection(instrument_id=intent.instrument_id,quantity=intent.quantity,stop_usd='99',evidence_id=uuid4().hex))
    position=controller.state['positions'][intent.instrument_id]
    observation=Observation(intent.instrument_id,D('100'),D('100.01'),NOW+timedelta(minutes=5),NOW+timedelta(minutes=5),
        D(controller.state['cash']['settled_usd']),D(controller.state['cash']['unsettled_usd']),
        D(controller.state['cash']['liquidation_equity_usd']),{intent.instrument_id:position['quantity']},(),uuid4().hex)
    controller.clock=lambda:observation.received_at
    controller.refresh(observation)
    original_decision=synthetic_proposal(make_position_review_context(observation,controller.state,NOW+timedelta(minutes=5),
        intent=intent,plan=plan,plan_version=1,original_decision={})[0])
    return args,broker,intent,plan,observation,original_decision


def review_bundle(controller,intent,plan,observation,original_decision,action,**payload):
    now=observation.received_at
    context,registry=make_position_review_context(observation,controller.state,now,intent=intent,plan=plan,
        plan_version=1,original_decision=original_decision,maximum_favorable_excursion_usd='2',maximum_adverse_excursion_usd='1')
    raw=synthetic_proposal(context,action=action)
    raw.update(action=action,entry_plan=None,amendment=None,exit_request=None,data_request=None,context_hash=payload_hash(context))
    if action=='PROPOSE_AMENDMENT':
        raw['amendment']={'position_id':context.account.positions[0].position_id,'current_plan_id':intent.plan_id,
            'current_plan_version':'1','requested_stop_usd':payload.get('stop'),'requested_targets':payload.get('targets'),
            'requested_trailing':None,'requested_latest_exit_at':payload.get('latest_exit'),
            'requested_invalidation':payload.get('invalidation')}
    elif action=='PROPOSE_EXIT':
        raw['exit_request']={'position_id':context.account.positions[0].position_id,'current_plan_id':intent.plan_id,
            'mode':'CLOSE','requested_quantity':None,'execution_policy_id':'local-exit-v1','reason_code':'ASTRA_REVIEW'}
    decision=parse_trade_decision(json.dumps(raw));validate_semantics(decision,context,registry,now)
    return {'schema_valid':True,'semantics_valid':True,'decision':raw},context,registry


def test_hold_keeps_owned_position_and_protection(setup_cycle):
    args,_,intent,plan,o,original=prepared(setup_cycle);manager=LocalPositionManager();controller=args['controller']
    result=manager._apply_review(review_bundle(controller,intent,plan,o,original,'HOLD'),intent,plan,1,plan,o,
        args['gateway'],controller,args['calendar'].session(NOW.date()),lambda:o.received_at)
    assert result==(plan,1,None)
    assert controller.state['positions'][intent.instrument_id]['protected_quantity']=='1'


def test_propose_exit_closes_exact_owned_quantity_once(setup_cycle):
    args,broker,intent,plan,o,original=prepared(setup_cycle);manager=LocalPositionManager();controller=args['controller']
    _,_,reason=manager._apply_review(review_bundle(controller,intent,plan,o,original,'PROPOSE_EXIT'),intent,plan,1,plan,o,
        args['gateway'],controller,args['calendar'].session(NOW.date()),lambda:o.received_at)
    manager._exit_owned(intent,o,args['gateway'],controller,args['calendar'].session(NOW.date()),lambda:o.received_at,reason,[reason])
    assert not controller.state['positions'] and len(broker.orders)==2
    with pytest.raises(Exception):
        manager._exit_owned(intent,o,args['gateway'],controller,args['calendar'].session(NOW.date()),lambda:o.received_at,reason,[reason])
    assert len(broker.orders)==2


def test_stop_tightening_and_target_amendments_apply_and_persist(setup_cycle):
    args,_,intent,plan,o,original=prepared(setup_cycle);events=[];manager=LocalPositionManager(emit=lambda k,p:events.append((k,p)))
    controller=args['controller'];session=args['calendar'].session(NOW.date());clock=lambda:o.received_at
    tightened,version,_=manager._apply_review(review_bundle(controller,intent,plan,o,original,'PROPOSE_AMENDMENT',stop='99.50'),
        intent,plan,1,plan,o,args['gateway'],controller,session,clock)
    assert tightened.protection.initial_stop_usd==D('99.50') and version==2
    assert controller.state['positions'][intent.instrument_id]['stop_usd']=='99.50'
    target=[{'price_usd':'101.00','fraction_of_initial_quantity':1}]
    bundle=review_bundle(controller,intent,plan,o,original,'PROPOSE_AMENDMENT',targets=target)
    # Bind this independent target test to version one/current pre-amendment context.
    targeted,_,_=LocalPositionManager()._apply_review(bundle,intent,plan,1,plan,o,args['gateway'],controller,session,clock)
    assert targeted.targets[0].price_usd==D('101.00')
    assert any(kind=='POSITION_PLAN_AMENDED' for kind,_ in events)


def test_unsafe_review_changes_and_stale_review_are_rejected(setup_cycle):
    args,_,intent,plan,o,original=prepared(setup_cycle);controller=args['controller']
    with pytest.raises(DecisionError,match='stop loosening'):
        review_bundle(controller,intent,plan,o,original,'PROPOSE_AMENDMENT',stop='98')
    with pytest.raises(DecisionError,match='exit deadline extension'):
        review_bundle(controller,intent,plan,o,original,'PROPOSE_AMENDMENT',latest_exit=(plan.latest_exit_at+timedelta(seconds=1)).isoformat())
    ai,context,registry=review_bundle(controller,intent,plan,o,original,'HOLD')
    with pytest.raises(DecisionError):validate_semantics(parse_trade_decision(json.dumps(ai['decision'])),context,registry,o.received_at+timedelta(seconds=91))
    malformed=ai['decision'].copy();malformed['amendment']={'requested_quantity':'2'};malformed['action']='PROPOSE_AMENDMENT'
    with pytest.raises(DecisionError):parse_trade_decision(json.dumps(malformed))


def test_invalid_or_timed_out_review_leaves_protection_intact(setup_cycle):
    args,_,intent,plan,o,_=prepared(setup_cycle);controller=args['controller'];manager=LocalPositionManager()
    with pytest.raises(ValueError,match='OPENAI_ERROR_OR_TIMEOUT'):
        manager._apply_review(({'schema_valid':False,'semantics_valid':False,'decision':None,'result':'OPENAI_ERROR_OR_TIMEOUT_NO_RETRY'},None,None),
            intent,plan,1,plan,o,args['gateway'],controller,args['calendar'].session(NOW.date()),lambda:o.received_at)
    assert controller.state['positions'][intent.instrument_id]['protected_quantity']=='1'


def test_hold_review_keeps_monitor_running_until_stop(setup_cycle):
    args,broker,intent,plan,o,original=prepared(setup_cycle);controller=args['controller'];events=[]
    current=[o.received_at];price=[D('100')]
    controller.clock=lambda:current[0]
    class Source:
        def refresh(self,state):
            return Observation(intent.instrument_id,price[0],price[0]+D('.01'),current[0],current[0],
                D(state['cash']['settled_usd']),D(state['cash']['unsettled_usd']),D(state['cash']['liquidation_equity_usd']),
                {intent.instrument_id:state['positions'][intent.instrument_id]['quantity']} if state['positions'] else {},(),uuid4().hex)
    def review(observation,*_):return review_bundle(controller,intent,plan,observation,original,'HOLD')
    def emit(kind,payload):
        events.append((kind,payload))
        if kind=='POSITION_REVIEW':price[0]=D('98')
    def sleep(_):current[0]+=timedelta(seconds=4)
    manager=LocalPositionManager(sleep=sleep,monitor_dry_run=True,review=review,emit=emit)
    manager.manage(intent,plan,Source(),args['gateway'],controller,args['calendar'].session(NOW.date()),lambda:current[0])
    assert any(k=='POSITION_REVIEW' and p['action']=='HOLD' and p['validator_result']=='APPROVED' for k,p in events)
    assert any(k=='POSITION_EXIT_TRIGGER' and 'STOP' in p['simultaneously_true_conditions'] for k,p in events)
    assert not controller.state['positions'] and len(broker.orders)==2


def test_safety_latch_exits_without_honoring_hold(setup_cycle):
    args,broker,intent,plan,o,_=prepared(setup_cycle);controller=args['controller'];events=[]
    controller.incident('SYNTHETIC_SAFETY_LATCH')
    manager=LocalPositionManager(sleep=lambda _:None,monitor_dry_run=True,
        review=lambda *_:pytest.fail('latch must override Astra review'),emit=lambda k,p:events.append((k,p)))
    class Source:
        def refresh(self,state):return Observation(intent.instrument_id,o.bid,o.ask,o.received_at,o.received_at,
            D(state['cash']['settled_usd']),D(state['cash']['unsettled_usd']),D(state['cash']['liquidation_equity_usd']),
            {intent.instrument_id:state['positions'][intent.instrument_id]['quantity']} if state['positions'] else {},(),uuid4().hex)
    manager.manage(intent,plan,Source(),args['gateway'],controller,args['calendar'].session(NOW.date()),lambda:o.received_at)
    assert any(k=='POSITION_EXIT_TRIGGER' and p['reason']=='SAFETY_LATCH' for k,p in events)
    assert not controller.state['positions'] and len(broker.orders)==2


def test_filled_position_refreshes_directly_when_absent_from_entry_universe(setup_cycle):
    args,_,intent,_,o,_=prepared(setup_cycle);state=args['controller'].state;calls=[]
    bars=[{'time':o.received_at.isoformat(),'open':'100','high':'100.01','low':'99.99','close':'100','volume':'10'}]
    bodies={
        Operation.ACCOUNTS:[{'account_id':'paper','account_type':'CASH','account_class':'INDIVIDUAL_CASH'}],
        Operation.BALANCE:{'account_currency_assets':[{'currency':'USD','settled_cash':state['cash']['settled_usd'],
            'unsettled_cash':state['cash']['unsettled_usd'],'net_liquidation_value':state['cash']['liquidation_equity_usd']}]},
        Operation.POSITIONS:[{'symbol':'AAPL','instrument_type':'EQUITY','quantity':'1'}],
        Operation.OPEN_ORDERS:{'data':[],'pagination_key':None},
        Operation.INSTRUMENTS:{'data':[{'symbol':'AAPL','instrument_id':intent.instrument_id,'category':'US_STOCK',
            'sub_category':'COMMON_STOCK','currency':'USD','status':'HALTED','exchange_code':'NSQ','lot_size':'1'}]},
        Operation.HISTORY_BATCH:{'result':[{'symbol':'AAPL','instrument_id':intent.instrument_id,'result':bars}]},
        Operation.SNAPSHOT:[{'symbol':'AAPL','instrument_id':intent.instrument_id,'bid':'100','ask':'100.01',
            'quote_time':str(int(o.received_at.timestamp()*1000)),'volume':'10','list_status':'HALTED','lot_size':'1'}]}
    class Reader:
        def __init__(self,op):self.op=op
        def read(self,op,*,symbols=('AAPL',)):
            calls.append((op,tuple(symbols)))
            if op==Operation.TOP_ACTIVE:pytest.fail('held-position refresh must not use entry discovery')
            return SimpleNamespace(status_code=200,body=bodies[op],received_at=o.received_at.isoformat())
    source=SandboxSource.__new__(SandboxSource);source.credentials=SimpleNamespace(account_id='paper')
    source.readers={op:Reader(op) for op in Operation};source._universe_symbols=('MSFT','NVDA');source._universe_at=0
    observed=source.refresh(state);observed.require_monitor_fresh(o.received_at)
    assert observed.instrument_id==intent.instrument_id and observed.ticker=='AAPL'
    assert (Operation.SNAPSHOT,('AAPL',)) in calls and not any(op==Operation.TOP_ACTIVE for op,_ in calls)


def test_held_refresh_retries_stale_http_snapshot_until_source_time_is_fresh(setup_cycle):
    args,_,intent,_,o,_=prepared(setup_cycle);state=args['controller'].state;received=o.received_at+timedelta(seconds=10);snapshots=[]
    bars=[{'time':received.isoformat(),'open':'100','high':'100.01','low':'99.99','close':'100','volume':'10'}]
    bodies={
        Operation.ACCOUNTS:[{'account_id':'paper','account_type':'CASH','account_class':'INDIVIDUAL_CASH'}],
        Operation.BALANCE:{'account_currency_assets':[{'currency':'USD','settled_cash':state['cash']['settled_usd'],
            'unsettled_cash':state['cash']['unsettled_usd'],'net_liquidation_value':state['cash']['liquidation_equity_usd']}]},
        Operation.POSITIONS:[{'symbol':'AAPL','instrument_type':'EQUITY','quantity':'1'}],
        Operation.OPEN_ORDERS:{'data':[],'pagination_key':None},
        Operation.INSTRUMENTS:{'data':[{'symbol':'AAPL','instrument_id':intent.instrument_id,'category':'US_STOCK',
            'currency':'USD','status':'OC','exchange_code':'NSQ','lot_size':'1'}]},
        Operation.HISTORY_BATCH:{'result':[{'symbol':'AAPL','instrument_id':intent.instrument_id,'result':bars}]}}
    class Reader:
        def read(self,op,*,symbols=('AAPL',)):
            if op==Operation.SNAPSHOT:
                snapshots.append(op);at=received-(timedelta(seconds=7) if len(snapshots)==1 else timedelta(milliseconds=500))
                body=[{'symbol':'AAPL','instrument_id':intent.instrument_id,'bid':'100','ask':'100.01',
                    'quote_time':str(int(at.timestamp()*1000)),'volume':'10','list_status':'LISTED','lot_size':'1'}]
            else:body=bodies[op]
            return SimpleNamespace(status_code=200,body=body,received_at=received.isoformat())
    source=SandboxSource.__new__(SandboxSource);source.credentials=SimpleNamespace(account_id='paper')
    source.readers={op:Reader() for op in Operation};source._universe_symbols=();source._universe_at=0
    result=source.refresh_held(state);held=result.held(intent.instrument_id,received)
    assert len(snapshots)==2 and received-held.quote_at==timedelta(milliseconds=500)


def test_broker_read_failure_reports_only_operation_and_status():
    class Reader:
        def read(self,op,*,symbols=('AAPL',),rank_type='VOLUME',direction='DESC'):
            return SimpleNamespace(status_code=503,body={'secret':'must-not-escape'},received_at=NOW.isoformat())
    source=SandboxSource.__new__(SandboxSource);source.credentials=SimpleNamespace(account_id='paper')
    source.readers={op:Reader() for op in Operation};source._universe_symbols=();source._universe_at=0
    with pytest.raises(ValueError) as caught:source.refresh({},portfolio=True)
    assert str(caught.value)=='BROKER_READ_FAILED GAINERS_LOSERS MIN_3 HTTP 503'
    assert 'secret' not in str(caught.value) and 'must-not-escape' not in str(caught.value)


class EntryIneligibleHeldObservation:
    """A valid held quote that must never pass new-entry selection."""
    def __init__(self,observation):self.observation=observation
    def __getattr__(self,name):return getattr(self.observation,name)
    def require_fresh(self,now):raise ValueError('SELECTED_SYMBOL_NOT_ELIGIBLE')
    def require_monitor_fresh(self,now):return self.observation.require_monitor_fresh(now)


@pytest.mark.parametrize('action',['HOLD','PROPOSE_EXIT','PROPOSE_AMENDMENT'])
def test_entry_ineligible_holding_reaches_all_astra_review_actions(setup_cycle,action):
    args,broker,intent,plan,o,original=prepared(setup_cycle);controller=args['controller'];events=[]
    current=[o.received_at];price=[D('100')];controller.clock=lambda:current[0]
    class Source:
        def refresh(self,state):
            raw=Observation(intent.instrument_id,price[0],price[0]+D('.01'),current[0],current[0],
                D(state['cash']['settled_usd']),D(state['cash']['unsettled_usd']),D(state['cash']['liquidation_equity_usd']),
                {intent.instrument_id:state['positions'][intent.instrument_id]['quantity']} if state['positions'] else {},(),uuid4().hex)
            return EntryIneligibleHeldObservation(raw)
    def review(observation,*_):
        assert controller.state['positions'][intent.instrument_id]['protected_quantity']=='1'
        assert not any(controller.state['latches'].values())
        payload={'stop':'99.50'} if action=='PROPOSE_AMENDMENT' else {}
        return review_bundle(controller,intent,plan,observation,original,action,**payload)
    def emit(kind,payload):
        events.append((kind,payload))
        if kind=='POSITION_REVIEW' and action=='HOLD':price[0]=D('98')
        if kind=='POSITION_PLAN_AMENDED':price[0]=D('99.40')
    def sleep(_):current[0]+=timedelta(seconds=4)
    LocalPositionManager(sleep=sleep,monitor_dry_run=True,review=review,emit=emit).manage(
        intent,plan,Source(),args['gateway'],controller,args['calendar'].session(NOW.date()),lambda:current[0])
    reviews=[p for k,p in events if k=='POSITION_REVIEW']
    assert reviews and reviews[0]['rejection_reason'] is None,reviews
    assert reviews[0]['action']==action and reviews[0]['validator_result']=='APPROVED'
    assert not controller.state['positions'] and len(broker.orders)==2
    assert not controller.state['incidents']


def test_entry_ineligible_holding_still_exits_on_stop(setup_cycle):
    args,broker,intent,plan,o,_=prepared(setup_cycle);controller=args['controller'];events=[]
    class Source:
        def refresh(self,state):
            raw=Observation(intent.instrument_id,D('98'),D('98.01'),o.received_at,o.received_at,
                D(state['cash']['settled_usd']),D(state['cash']['unsettled_usd']),D(state['cash']['liquidation_equity_usd']),
                {intent.instrument_id:state['positions'][intent.instrument_id]['quantity']} if state['positions'] else {},(),uuid4().hex)
            return EntryIneligibleHeldObservation(raw)
    LocalPositionManager(sleep=lambda _:None,monitor_dry_run=True,emit=lambda k,p:events.append((k,p))).manage(
        intent,plan,Source(),args['gateway'],controller,args['calendar'].session(NOW.date()),lambda:o.received_at)
    assert any(k=='POSITION_EXIT_TRIGGER' and p['reason']=='STOP' for k,p in events)
    assert not controller.state['positions'] and len(broker.orders)==2 and not controller.state['incidents']


@pytest.mark.parametrize(('bid','reason'),[('98','STOP'),('101','TARGET')])
def test_freshly_received_batched_quote_can_trigger_owned_exit(setup_cycle,bid,reason):
    args,broker,intent,plan,o,_=prepared(setup_cycle);controller=args['controller'];now=o.received_at
    if reason=='TARGET':plan=plan.model_copy(update={'targets':(Target(price_usd='101',fraction_of_initial_quantity=1),)})
    class Source:
        def refresh(self,state):
            return Observation(intent.instrument_id,D(bid),D(bid)+D('.01'),now-timedelta(seconds=7),now,
                D(state['cash']['settled_usd']),D(state['cash']['unsettled_usd']),D(state['cash']['liquidation_equity_usd']),
                {intent.instrument_id:state['positions'][intent.instrument_id]['quantity']} if state['positions'] else {},(),uuid4().hex,
                quote_received_at=now)
    LocalPositionManager(sleep=lambda _:None,monitor_dry_run=True).manage(
        intent,plan,Source(),args['gateway'],controller,args['calendar'].session(NOW.date()),lambda:now)
    assert not controller.state['positions'] and len(broker.orders)==2 and not controller.state['incidents']
    assert list(broker.orders.values())[-1].quantity==D('1')


def test_stale_broker_receipt_still_latches_monitor_incident(setup_cycle):
    args,_,intent,plan,o,_=prepared(setup_cycle);controller=args['controller'];now=o.received_at
    class Source:
        def refresh(self,state):
            return Observation(intent.instrument_id,D('98'),D('98.01'),now-timedelta(seconds=20),now-timedelta(seconds=20),
                D(state['cash']['settled_usd']),D(state['cash']['unsettled_usd']),D(state['cash']['liquidation_equity_usd']),
                {intent.instrument_id:state['positions'][intent.instrument_id]['quantity']},(),uuid4().hex,
                quote_received_at=now-timedelta(seconds=20))
    with pytest.raises(ValueError,match='STALE_EXIT_OBSERVATION'):
        LocalPositionManager(monitor_dry_run=True).manage(
            intent,plan,Source(),args['gateway'],controller,args['calendar'].session(NOW.date()),lambda:now)
    assert controller.state['latches']['incident'] and controller.state['positions']


def test_flat_symbol_must_again_pass_entry_eligibility(setup_cycle):
    args,_,intent,_,o,_=prepared(setup_cycle);ineligible=Observation(intent.instrument_id,D('100'),D('100.30'),o.received_at,o.received_at,
        D('1000000'),D('0'),D('1000000'),{},(),uuid4().hex,ticker='AAPL',instrument=o.instrument,entry_eligible=False)
    ineligible.require_monitor_fresh(o.received_at)
    universe=UniverseObservation((ineligible,),o.received_at,D('1000000'),D('0'),D('1000000'),{},(),uuid4().hex)
    with pytest.raises(ValueError,match='INELIGIBLE_OR_ILLIQUID_STOCK'):universe.select('AAPL',o.received_at)


def test_alpha_excursion_and_position_model_cost_survive_manager_restart():
    saved={}
    class Store:
        def alpha_metrics(self,plan_id,instrument_id):return saved.get((plan_id,instrument_id))
        def shadow_audit(self,_):return None
    store=Store()
    def emit(kind,payload):
        if kind=='ALPHA_POSITION_METRICS':saved[(payload['plan_id'],payload['instrument_id'])]=dict(payload)
    plan=AlphaManagedPlan(plan_id='plan-1',instrument_id='iid-1',symbol='AAA',episode_id='episode-1',
        strategy_label='Astra-owned',thesis='Current evidence only.',quantity='10',entry_limit_usd='100',
        original_stop_usd='95',active_stop_usd='95',target_usd=None,
        entry_expires_at=NOW+timedelta(minutes=1),next_review_at=NOW+timedelta(minutes=2),
        original_latest_exit_at=NOW+timedelta(hours=1),latest_exit_at=NOW+timedelta(hours=1))
    first=AlphaPortfolioManager(store,review=lambda *_:None,emit=emit,exit_manager=None)
    metrics=first._metrics(plan,D('100'));metrics.update(max_bid=D('105'),min_bid=D('98'),mfe=D('50'),mae=D('20'),model_cost=D('.25'))
    first._persist_metrics(plan,metrics)
    second=AlphaPortfolioManager(store,review=lambda *_:None,emit=emit,exit_manager=None)
    restored=second._metrics(plan,D('101'))
    assert restored['max_bid']==D('105') and restored['min_bid']==D('98')
    assert restored['mfe']==D('50') and restored['mae']==D('20') and restored['model_cost']==D('.25')


def test_recovery_restores_persisted_amended_plan(setup_cycle):
    args,_,intent,_,_,_=prepared(setup_cycle)
    # Recover the canonical original decision from a fresh synthetic cycle binding.
    context=args['context_factory'](args['source'].refresh({}),None,NOW,analysis_only=True)[0]
    decision=synthetic_proposal(context)
    decision['decision_id']=intent.plan_id
    decision['entry_plan']['instrument_id']=intent.instrument_id
    decision['entry_plan']['protection']['initial_stop_usd']='99'
    decision['entry_plan']['sizing']['amount']='1'
    decision['entry_plan']['maximum_entry_usd']='100'
    decision['entry_plan']['limit_price_usd']='100'
    decision['entry_plan']['strategy_episode_id']=intent.episode_id
    decision['entry_plan']['latest_exit_at']=(NOW+timedelta(minutes=5)).isoformat()
    # Only recovery binding matters here; use the actual entry decision persisted by the order's plan id.
    class Store:
        def plan(self,_):return decision
        def amended_plan(self,_):
            amended=dict(decision['entry_plan']);amended['protection']=dict(amended['protection'],initial_stop_usd='99.50')
            return {'plan_id':intent.plan_id,'instrument_id':intent.instrument_id,'plan_version':'2','plan':amended}
    recovered_intent,recovered,version,original=recover_plan_state(args['controller'].state,Store())
    assert recovered_intent.intent_id==intent.intent_id and version==2
    assert original.protection.initial_stop_usd==D('99') and recovered.protection.initial_stop_usd==D('99.50')
