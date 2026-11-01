from datetime import datetime,timezone,timedelta,date
from decimal import Decimal as D
from uuid import uuid4
from pathlib import Path
from types import SimpleNamespace
import json
import pytest
from psycopg import sql
from autonomous_trader.contracts import payload_hash
from autonomous_trader.durable.ledger import Ledger,LocalDatabase,migrate,grant_writer
from autonomous_trader.durable.models import CreateAccount,OpeningStatement,Admit
from autonomous_trader.mvp.config import RuntimeConfig,ExecutionMode,reviewed_calendar
from autonomous_trader.mvp.context import Observation,UniverseObservation,make_context,instrument,capabilities,SOURCE
from autonomous_trader.mvp.controller import Controller
from autonomous_trader.mvp.cycle import run_trading_cycle
from autonomous_trader.mvp.gateway import ExecutionGateway,LocalPositionManager,validate_detail,executed_price
from autonomous_trader.execution import SimulatedBroker
from autonomous_trader.decision_api.astra_probe import AstraClient,request_body

NOW=datetime(2026,9,11,14,0,tzinfo=timezone.utc)


def synthetic_proposal(c,quantity='1',action='PROPOSE_ENTRY'):
    condition=lambda op,value:{'join':'ALL','predicates':[{'feature_id':'bid','source_id':SOURCE,'operator':op,'value':value,'persistence_ms':0}]}
    return dict(schema_version='1.0',decision_id=c.decision_id,context_id=c.context_id,context_hash=payload_hash(c),
        opportunity_id=c.opportunity_id,portfolio_state_version=c.account.reconciliation_version,policy_hash=c.policy_hash,
        capability_version=c.capabilities.version,strategy_id=c.strategy_id,strategy_version=c.strategy_version,
        strategy_artifact_hash=c.strategy_artifact_hash,created_at=c.as_of.isoformat(),valid_until=c.expires_at.isoformat(),
        action=action,amendment=None,exit_request=None,data_request=None,
        entry_plan=None if action!='PROPOSE_ENTRY' else dict(instrument_id=c.instrument.instrument_id,ticker='AAPL',direction='LONG',
            entry_condition=condition('GT','90'),entry_order_type='LIMIT',session='CORE',time_in_force='DAY',
            maximum_entry_usd='100.00',limit_price_usd='100.00',trigger_price_usd=None,
            entry_expires_at=(c.as_of+timedelta(seconds=60)).isoformat(),sizing={'unit':'SHARES','amount':quantity,'minimum_acceptable_quantity':'1'},
            protection={'initial_stop_usd':'99','trigger_reference':'BID','trigger_source_id':SOURCE,
                'execution_policy_id':'local-exit-v1','mechanism':'LOCAL_MONITORED_STOP','stop_limit_usd':None},targets=[],
            trailing={'rule_id':'none','mode':'NONE','activation_rule':None,'distance':None},max_holding_seconds=300,
            latest_exit_at=(c.as_of+timedelta(minutes=5)).isoformat(),invalidation=condition('LT','90'),
            partial_fill_policy_id='halt-reconcile',max_spread_bps='20',strategy_episode_id='mvp-aapl'),
        forecast={'event_definition':'Synthetic test only, not an Astra decision','horizon_seconds':300,
            'raw_probability':None,'expected_gross_return_bps':'20','estimated_execution_cost_bps':'5',
            'net_return_quantile_10_bps':None,'net_return_quantile_50_bps':None,'net_return_quantile_90_bps':None,
            'calibration_id':None,'estimate_provenance':'MODEL_JUDGMENT'},evidence_ids=[c.evidence[0].evidence_id],
        contradictions=[],missing_data=[],reasoning_summary='CONTROLLED SYNTHETIC EXECUTION PATH TEST ONLY',review_triggers=[])


class SyntheticBrain:
    def __init__(self,q='1',action='PROPOSE_ENTRY'):self.q,self.action=q,action
    def decide(self,c,registry):
        from autonomous_trader.decisions import parse_trade_decision,validate_semantics
        proposal=synthetic_proposal(c,self.q,self.action)
        d=parse_trade_decision(json.dumps(proposal));validate_semantics(d,c,registry,NOW)
        return {'schema_valid':True,'semantics_valid':True,'decision':d.model_dump(mode='json'),
            'origin':'CONTROLLED_SYNTHETIC_FIXTURE_NOT_ASTRA','estimated_cost_usd':'0'}


@pytest.fixture
def setup_cycle():
    schema='mvp_test_'+uuid4().hex[:16]
    admin=LocalDatabase('postgres','m2_admin',schema=schema);migrate(admin);grant_writer(admin,'m2_writer')
    ledger=Ledger(LocalDatabase('postgres','m2_writer',schema=schema));cal=reviewed_calendar()
    aid='dryrun-'+uuid4().hex
    create=CreateAccount(account_id=aid,experiment_id='synthetic',owner_id='owner',initial_settled_usd='1000000',
        session=cal.session(NOW.date()),instruments=(instrument(),),capabilities=capabilities(),
        settlement_days=(date(2026,9,11),date(2026,9,14)),mark_max_age_seconds=2,reconciliation_max_age_seconds=10)
    ledger.create(create,event_id=uuid4().hex,at=NOW)
    controller=Controller(ledger,aid,lambda:NOW)
    o=Observation('913256135',D('99.99'),D('100'),NOW,NOW,D('1000000'),D(0),D('1000000'),{},(),uuid4().hex)
    class Source:
        def refresh(self,state):return o
    config=RuntimeConfig();broker=SimulatedBroker(cal)
    gateway=ExecutionGateway(config,simulation=broker)
    args=dict(config=config,calendar=cal,clock=lambda:NOW,source=Source(),controller=controller,
        gateway=gateway,context_factory=make_context,monitor=LocalPositionManager())
    try:
        yield args,broker,ledger
    finally:
        with admin.connect() as connection:
            connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


def test_complete_synthetic_path_uses_real_m2_and_one_simulated_order(setup_cycle):
    args,broker,ledger=setup_cycle
    r=run_trading_cycle(**args,brain=SyntheticBrain())
    assert r['blocker'] is None,r
    assert r['risk_approved'] and r['broker_status']=='FILLED' and not r['order_submitted']
    assert len(broker.orders)==1
    state=args['controller'].state
    assert len(state['orders'])==1 and len(state['permits'])==1
    assert next(iter(state['permits'].values()))['consumed'] is True
    assert D(state['positions']['913256135']['quantity'])==1
    assert ledger.rebuild(args['controller'].account_id)==state
    r['origin']='CONTROLLED_SYNTHETIC_FIXTURE_NOT_ASTRA';r['simulated_orders']=len(broker.orders)
    (Path(__file__).parents[2]/'docs/mvp-synthetic-cycle.json').write_text(json.dumps(r,indent=2)+'\n')


def test_failed_risk_creates_zero_orders(setup_cycle):
    args,broker,_=setup_cycle
    r=run_trading_cycle(**args,brain=SyntheticBrain('5000'))
    assert not r['risk_approved'] and not broker.orders
    assert not args['controller'].state['orders']
    assert 'STOCK_EXPOSURE_LIMIT' in r['blocker'],r
    assert args['controller'].ledger.journal(args['controller'].account_id)[-1]['command']['kind']=='REJECTED'


def test_abstain_has_zero_admission_and_orders(setup_cycle):
    args,broker,_=setup_cycle;r=run_trading_cycle(**args,brain=SyntheticBrain(action='ABSTAIN'))
    assert r['astra_action']=='ABSTAIN' and not r['risk_approved'] and not broker.orders


@pytest.mark.parametrize('instant',['2026-09-11T19:55:00+00:00','2026-09-11T20:01:00+00:00','2026-09-12T14:00:00+00:00'])
def test_closed_session_never_calls_brain_or_gateway(setup_cycle,instant):
    args,broker,_=setup_cycle;args['clock']=lambda:datetime.fromisoformat(instant)
    class Brain:
        def decide(self,*args):pytest.fail('closed session must stop before model call')
    r=run_trading_cycle(**args,brain=Brain())
    assert r['session_gate']=='CLOSED_OR_ENTRY_CUTOFF' and not broker.orders


def test_modes_default_dry_run_and_production_disabled(monkeypatch):
    monkeypatch.delenv('AI_TRADER_MODE',raising=False)
    assert RuntimeConfig.from_env().mode==ExecutionMode.DRY_RUN
    with pytest.raises(ValueError,match='PRODUCTION_DISABLED'):ExecutionGateway(RuntimeConfig(ExecutionMode.PRODUCTION))
    with pytest.raises(ValueError):ExecutionGateway(RuntimeConfig(),transport=object())


def test_explicit_executed_notional_preserves_subcent_average_and_conflicts_fail_closed():
    row={'filled_quantity':'10000','filled_price':'.41','executed_notional_usd':'4082.00'}
    assert executed_price(row)==D('.4082')
    row['executed_notional_usd']='4000.00'
    with pytest.raises(ValueError,match='EXECUTED_NOTIONAL_CONFLICT'):executed_price(row)
    del row['executed_notional_usd']
    assert executed_price(row)==D('.41')


def test_model_and_reasoning_runtime_switch_without_trading_changes(context,monkeypatch):
    monkeypatch.setenv('AI_TRADER_MODEL','gpt-6-astra');monkeypatch.setenv('AI_TRADER_REASONING','high')
    config=RuntimeConfig.from_env();client=AstraClient('synthetic',model=config.model,reasoning=config.reasoning)
    body=request_body(context,model=client.model,reasoning=client.reasoning)
    assert body['reasoning']=={'effort':'high'} and body['model']=='gpt-6-astra'


def test_opening_checkpoint_keeps_unsettled_and_original_highwater(setup_cycle):
    args,_,ledger=setup_cycle;cal=args['calendar'];aid='opening-'+uuid4().hex
    opening=OpeningStatement(evidence_id='accepted-m3b',unsettled_usd='332.73',settlement_date=date(2026,9,14),
        experiment_reference_usd='1000000',historic_high_water_usd='1000000.03',day_start_equity_usd='1000000',
        realized_gross_usd='-0.01',fees_usd='0.02',episode_id='mvp-aapl',instrument_id='913256135',consumed_loss_usd='0.03')
    state=ledger.create(CreateAccount(account_id=aid,experiment_id='original',owner_id='owner',initial_settled_usd='999667.24',
        session=cal.session(NOW.date()),instruments=(instrument(),),capabilities=capabilities(),
        settlement_days=(date(2026,9,11),date(2026,9,14)),mark_max_age_seconds=2,reconciliation_max_age_seconds=10,
        opening_statement=opening),event_id=uuid4().hex,at=NOW)
    assert D(state['cash']['settled_usd'])==D('999667.24')
    assert D(state['cash']['unsettled_usd'])==D('332.73')
    assert D(state['cash']['realized_net_usd'])==D('-.03')
    assert D(state['historic_high_water'])==D('1000000.03') and state['segments'][0]['reference_usd']=='1000000'
    assert ledger.rebuild(aid)==state


def test_after_close_exact_settlement_transition_reconciles_through_confirm_event(setup_cycle):
    from autonomous_trader.contracts import OrderIntent
    from autonomous_trader.decisions import parse_trade_decision
    from autonomous_trader.mvp.bootstrap import refresh_session
    args,_,ledger=setup_cycle;result=run_trading_cycle(**args,brain=SyntheticBrain())
    intent=OrderIntent.model_validate(result['generated_intent'])
    plan=parse_trade_decision(json.dumps(result['ai']['decision'])).entry_plan
    controller=args['controller']
    class ExitSource:
        def refresh(self,state):
            return Observation(intent.instrument_id,D('98'),D('98.01'),NOW,NOW,D(state['cash']['settled_usd']),
                D(state['cash']['unsettled_usd']),D(state['cash']['liquidation_equity_usd']),
                {intent.instrument_id:state['positions'][intent.instrument_id]['quantity']} if state['positions'] else {},(),uuid4().hex)
    LocalPositionManager(sleep=lambda _:None,monitor_dry_run=True).manage(intent,plan,ExitSource(),args['gateway'],controller,
        args['calendar'].session(NOW.date()),lambda:NOW)
    before=controller.state;receivable=D(before['cash']['unsettled_usd']);assert receivable>0 and not before['positions']
    after_close=args['calendar'].session(NOW.date()).closes_at+timedelta(seconds=1)
    controller.clock=lambda:after_close
    observed=Observation(intent.instrument_id,D('98'),D('98.01'),after_close,after_close,
        D(before['cash']['settled_usd'])+receivable,D(0),D(before['cash']['liquidation_equity_usd']),{},(),uuid4().hex)
    controller.lease();refresh_session(controller,observed,lambda:after_close)
    state=controller.state
    assert D(state['cash']['unsettled_usd'])==0 and state['reconciliation']['ready']
    assert any(e['command']['kind']=='SETTLE' for e in ledger.journal(controller.account_id)[-5:])

def test_local_stop_exits_exact_owned_simulated_quantity(setup_cycle):
    from types import SimpleNamespace
    from autonomous_trader.contracts import OrderIntent
    from autonomous_trader.decisions import parse_trade_decision
    args,broker,_=setup_cycle
    result=run_trading_cycle(**args,brain=SyntheticBrain())
    intent=OrderIntent.model_validate(result['generated_intent'])
    plan=parse_trade_decision(json.dumps(result['ai']['decision'])).entry_plan
    class Source:
        def refresh(self,state):
            return Observation('913256135',D('98'),D('98.01'),NOW,NOW,
                D(state['cash']['settled_usd']),D(state['cash']['unsettled_usd']),D(state['cash']['liquidation_equity_usd']),
                {k:v['quantity'] for k,v in state['positions'].items()},(),uuid4().hex)
    class Gateway:
        config=RuntimeConfig(ExecutionMode.SANDBOX)
        def dispatch(self,*a):return args['gateway'].dispatch(*a)
    LocalPositionManager(sleep=lambda _:None).manage(intent,plan,Source(),Gateway(),args['controller'],args['calendar'].session(NOW.date()),lambda:NOW)
    assert not args['controller'].state['positions']
    assert len(broker.orders)==2
    orders=list(args['controller'].state['orders'].values())
    sells=[o for o in orders if o['intent']['side']=='SELL_OWNED']
    assert len(sells)==1 and D(sells[0]['intent']['quantity'])==1


def test_stale_quote_blocks_before_model(setup_cycle):
    args,broker,_=setup_cycle
    args['clock']=lambda:NOW+timedelta(seconds=3)
    r=run_trading_cycle(**args,brain=SyntheticBrain())
    assert r['blocker']=='STALE_MARKET_OR_ACCOUNT' and not broker.orders


def test_consumed_permit_cannot_dispatch_again(setup_cycle):
    from autonomous_trader.contracts import OrderIntent
    args,broker,_=setup_cycle
    r=run_trading_cycle(**args,brain=SyntheticBrain())
    intent=OrderIntent.model_validate(r['generated_intent']);controller=args['controller']
    nonce=controller.state['outbox'][intent.intent_id]['nonce']
    with pytest.raises(Exception):
        args['gateway'].dispatch(intent,nonce,args['source'].refresh(controller.state),controller,args['calendar'].session(NOW.date()),lambda:NOW)
    assert len(broker.orders)==1

def test_sandbox_timeout_is_reported_as_attempted_and_never_retried(setup_cycle,monkeypatch):
    args,broker,_=setup_cycle
    class Transport:
        _account_id='synthetic-account'
        calls=0
        def place(self,intent,symbol):
            assert symbol=='AAPL'
            self.calls+=1
            raise TimeoutError('synthetic timeout')
    transport=Transport()
    monkeypatch.setattr('autonomous_trader.mvp.gateway.sandbox_ledger_id',lambda _:args['controller'].account_id)
    args['config']=RuntimeConfig(ExecutionMode.SANDBOX)
    args['gateway']=ExecutionGateway(args['config'],transport=transport)
    result=run_trading_cycle(**args,brain=SyntheticBrain())
    assert result['risk_approved'] and result['order_submitted'] and result['blocker']
    assert transport.calls==1 and not broker.orders
    state=args['controller'].state
    assert D(state['cash']['reserved_usd'])>0
    assert next(iter(state['permits'].values()))['consumed']


def _cancel_race_exit(setup_cycle,monkeypatch,terminal_status):
    from types import SimpleNamespace
    from autonomous_trader.contracts import OrderIntent
    args,_,_=setup_cycle
    result=run_trading_cycle(**args,brain=SyntheticBrain())
    controller=args['controller'];current=controller.state
    controller.refresh(Observation('913256135',D('99.99'),D('100'),NOW,NOW,D(current['cash']['settled_usd']),
        D(current['cash']['unsettled_usd']),D(current['cash']['liquidation_equity_usd']),
        {'913256135':current['positions']['913256135']['quantity']},(),uuid4().hex))
    position=controller.state['positions']['913256135']
    intent=OrderIntent(intent_id='exit-cancel-race',client_order_id='client-exit-cancel-race',
        account_id=controller.account_id,environment='OFFLINE',instrument_id='913256135',side='SELL_OWNED',
        quantity=position['quantity'],order_type='LIMIT',session='CORE',time_in_force='DAY',limit_price_usd='98.00',
        trigger_price_usd=None,maximum_entry_usd=None,protection=None,plan_id=result['generated_intent']['plan_id'],
        episode_id=result['generated_intent']['episode_id'],deadline=NOW+timedelta(seconds=60),
        capability_version='mvp-local-monitor-v1')
    admitted=controller.send(Admit(intent=intent,fee_allowance_usd='3',exit_cost_usd='0',stress_allowance_usd='0',
        latest_exit_at=NOW+timedelta(minutes=5),max_holding_seconds=60))
    nonce=admitted['outbox'][intent.intent_id]['nonce'];calls={'detail':0,'cancel':0}
    def response(status,body):
        return SimpleNamespace(status_code=status,body=body,path='/synthetic',received_at=NOW.isoformat(),headers={},header_names=())
    def detail_row(status):
        filled='1' if status=='FILLED' else '0'
        return {'client_order_id':intent.client_order_id,'orders':[{'order_id':'broker-exit','client_order_id':intent.client_order_id,
            'symbol':'AAPL','side':'SELL','order_type':'LIMIT','time_in_force':'DAY','support_trading_session':'CORE',
            'total_quantity':'1','limit_price':'98.00','filled_quantity':filled,'filled_price':'98.50' if filled=='1' else '0',
            'status':status,'filled_time_at':NOW.isoformat(),'fees':[],'commission':{}}]}
    class Transport:
        _account_id='synthetic-account'
        def place(self,submitted,symbol):return response(200,{'client_order_id':submitted.client_order_id,'order_id':'broker-exit'})
        def detail(self,client_id):
            calls['detail']+=1
            return response(200,detail_row('SUBMITTED' if calls['detail']<=6 else terminal_status))
        def cancel(self,client_id):
            calls['cancel']+=1
            return response(409,{'error':'already terminal or uncertain'})
    monkeypatch.setattr('autonomous_trader.mvp.gateway.sandbox_ledger_id',lambda _:controller.account_id)
    gateway=ExecutionGateway(RuntimeConfig(ExecutionMode.SANDBOX),transport=Transport());gateway._capture=lambda _:None
    return gateway,intent,nonce,args['source'].refresh(controller.state),controller,calls


def test_cancel_error_followed_by_proven_fill_reconciles_exact_owned_exit(setup_cycle,monkeypatch):
    gateway,intent,nonce,observation,controller,calls=_cancel_race_exit(setup_cycle,monkeypatch,'FILLED')
    result=gateway.dispatch(intent,nonce,observation,controller,setup_cycle[0]['calendar'].session(NOW.date()),lambda:NOW)
    assert result['status']=='FILLED' and calls=={'detail':7,'cancel':1}
    state=controller.state
    assert not state['positions'] and state['orders'][intent.intent_id]['final']
    assert state['orders'][intent.intent_id]['filled_quantity']=='1'
    assert intent.intent_id not in state['reservations'] and state['outbox'][intent.intent_id]['status']=='FINAL'


def test_cancel_error_with_nonterminal_detail_remains_uncertain_and_reserved(setup_cycle,monkeypatch):
    gateway,intent,nonce,observation,controller,calls=_cancel_race_exit(setup_cycle,monkeypatch,'SUBMITTED')
    with pytest.raises(ValueError,match='CANCEL_UNCERTAIN'):
        gateway.dispatch(intent,nonce,observation,controller,setup_cycle[0]['calendar'].session(NOW.date()),lambda:NOW)
    state=controller.state
    assert calls=={'detail':7,'cancel':1} and state['orders'][intent.intent_id]['phase']=='CANCEL_PENDING'
    assert not state['orders'][intent.intent_id]['final'] and intent.intent_id in state['reservations']
    assert state['outbox'][intent.intent_id]['status']=='CLAIMED_UNCERTAIN'


def test_later_terminal_cancel_detail_recovers_pending_order_without_new_write(setup_cycle,monkeypatch):
    gateway,intent,nonce,observation,controller,calls=_cancel_race_exit(setup_cycle,monkeypatch,'SUBMITTED')
    with pytest.raises(ValueError,match='CANCEL_UNCERTAIN'):
        gateway.dispatch(intent,nonce,observation,controller,setup_cycle[0]['calendar'].session(NOW.date()),lambda:NOW)
    state=controller.state
    account=Observation(intent.instrument_id,D('99.99'),D('100'),NOW,NOW,D(state['cash']['settled_usd']),
        D(state['cash']['unsettled_usd']),D(state['cash']['liquidation_equity_usd']),
        {intent.instrument_id:state['positions'][intent.instrument_id]['quantity']},(),uuid4().hex)
    controller.refresh(account)
    scoped=next(key for key,value in controller.state['incidents'].items()
        if value.get('scope')=='CANCEL_PENDING_OUTSTANDING_ONLY')
    controller.incident('PROTECTION_FAILURE');unrelated=set(controller.state['incidents'])-{scoped}
    writes={'place':0,'cancel':0,'detail':0}
    def response(body):
        return SimpleNamespace(status_code=200,body=body,path='/synthetic',received_at=NOW.isoformat(),headers={},header_names=())
    class RecoveryTransport:
        _account_id='synthetic-account'
        def place(self,*_):writes['place']+=1;raise AssertionError('no recovery submission')
        def cancel(self,*_):writes['cancel']+=1;raise AssertionError('no repeated cancel')
        def detail(self,client_id):
            writes['detail']+=1
            return response({'client_order_id':client_id,'orders':[{'order_id':'broker-exit',
                'client_order_id':client_id,'symbol':'AAPL','side':'SELL','order_type':'LIMIT','time_in_force':'DAY',
                'support_trading_session':'CORE','total_quantity':'1','limit_price':'98.00','filled_quantity':'0',
                'filled_price':'0','status':'CANCELLED','filled_time_at':None,'fees':[],'commission':{}}]})
    gateway.transport=RecoveryTransport();gateway._capture=lambda _:None
    recovered=gateway.recover_terminal_cancellations(controller)
    assert recovered==(intent.intent_id,)
    controller.refresh(account)
    controller.resolve_recovered_cancellation_incidents(account,recovered)
    state=controller.state
    assert writes=={'place':0,'cancel':0,'detail':1}
    assert state['orders'][intent.intent_id]['phase']=='CANCELED' and state['orders'][intent.intent_id]['final']
    assert intent.intent_id not in state['reservations'] and state['outbox'][intent.intent_id]['status']=='FINAL'
    assert scoped not in state['incidents'] and set(state['incidents'])==unrelated and state['latches']['incident']


@pytest.mark.parametrize(('terminal','fees','error'),[
    ('FILLED',[],'CANCEL_RECOVERY_FILL_REQUIRES_RECONCILIATION'),
    ('CANCELLED',[{'actual_value':'1','receivable_value':'1'}],'TERMINAL_TOTALS_UNCERTAIN')])
def test_cancel_recovery_refuses_fill_or_fee(setup_cycle,monkeypatch,terminal,fees,error):
    gateway,intent,nonce,observation,controller,_=_cancel_race_exit(setup_cycle,monkeypatch,'SUBMITTED')
    with pytest.raises(ValueError,match='CANCEL_UNCERTAIN'):
        gateway.dispatch(intent,nonce,observation,controller,setup_cycle[0]['calendar'].session(NOW.date()),lambda:NOW)
    writes={'place':0,'cancel':0,'detail':0}
    class Transport:
        _account_id='synthetic-account'
        def place(self,*_):writes['place']+=1;raise AssertionError('no recovery submission')
        def cancel(self,*_):writes['cancel']+=1;raise AssertionError('no repeated cancel')
        def detail(self,client_id):
            writes['detail']+=1;filled='1' if terminal=='FILLED' else '0'
            body={'client_order_id':client_id,'orders':[{'order_id':'broker-exit','client_order_id':client_id,
                'symbol':'AAPL','side':'SELL','order_type':'LIMIT','time_in_force':'DAY','support_trading_session':'CORE',
                'total_quantity':'1','limit_price':'98.00','filled_quantity':filled,
                'filled_price':'98.50' if filled=='1' else '0','status':terminal,'filled_time_at':NOW.isoformat(),
                'fees':fees,'commission':{}}]}
            return SimpleNamespace(status_code=200,body=body,path='/synthetic',received_at=NOW.isoformat(),headers={},header_names=())
    gateway.transport=Transport();gateway._capture=lambda _:None
    with pytest.raises(ValueError,match=error):gateway.recover_terminal_cancellations(controller)
    state=controller.state
    assert writes=={'place':0,'cancel':0,'detail':1}
    assert state['orders'][intent.intent_id]['phase']=='CANCEL_PENDING' and not state['orders'][intent.intent_id]['final']


def test_astra_selects_non_aapl_candidate_and_existing_risk_path_executes(setup_cycle):
    args,broker,ledger=setup_cycle;controller=args['controller']
    msft=instrument({'instrument_id':'913323997','symbol':'MSFT','exchange_code':'NSQ','lot_size':'1'})
    aapl=args['source'].refresh(controller.state)
    second=Observation(msft.instrument_id,D('99.99'),D('100'),NOW,NOW,D('1000000'),D(0),D('1000000'),{},(),
        'multi-evidence',ticker='MSFT',instrument=msft,volume=D('2000000'))
    universe=UniverseObservation((aapl,second),NOW,D('1000000'),D(0),D('1000000'),{},(),'multi-evidence')
    class Source:
        def refresh(self,state):return universe
    class Brain:
        def select_candidate(self,u,state,*,clock):
            assert [x['symbol'] for x in u.selection_payload(clock())]==['AAPL','MSFT']
            return {'selected_symbol':'MSFT','reasoning_summary':'synthetic selector','candidate_symbols':['AAPL','MSFT']}
        def decide(self,c,registry):
            from autonomous_trader.decisions import parse_trade_decision,validate_semantics
            raw=synthetic_proposal(c);raw['entry_plan']['ticker']='MSFT';raw['entry_plan']['strategy_episode_id']='mvp-msft'
            d=parse_trade_decision(json.dumps(raw));validate_semantics(d,c,registry,NOW)
            return {'schema_valid':True,'semantics_valid':True,'decision':d.model_dump(mode='json'),'estimated_cost_usd':'0'}
    result=run_trading_cycle(**{**args,'source':Source()},brain=Brain())
    assert result['selection']['selected_symbol']=='MSFT' and result['risk_approved']
    assert msft.instrument_id in controller.state['instruments'] and msft.instrument_id in controller.state['positions']
    assert next(iter(broker.orders.values())).instrument_id==msft.instrument_id
    assert ledger.rebuild(controller.account_id)==controller.state
