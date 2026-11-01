"""Universe recovery and portfolio-candidate containment, no live services."""
from datetime import timedelta
from types import SimpleNamespace
from dataclasses import replace

import pytest

from test_alpha_reset import NOW, observation, universe
from autonomous_trader.decision_api.astra_probe import MODEL
from autonomous_trader.monitoring.runner import Engine
from autonomous_trader.mvp.config import RuntimeConfig


class Store:
    def __init__(self):self.events=[]
    def runtime(self):return {}
    def heartbeat(self,_):pass
    def recent_outcomes(self):return ()
    def emit(self,kind,payload,**kwargs):self.events.append((kind,payload,kwargs))


def flat_state(session):
    return {'positions':{},'orders':{},'reservations':{},'latches':{
        'daily':False,'drawdown':False,'incident':False,'terminated':False},
        'cash':{'liquidation_equity_usd':'1000000','settled_usd':'1000000'},'day_start_equity':'1000000',
        'policy_hash':'test-policy','config':{'capabilities':{'version':'mvp-local-monitor-v1'}},
        'reconciliation':{'ready':True,'settlement_known':True,
            'valid_until':(NOW+timedelta(seconds=10)).isoformat()},'session':{
            'label':session.label.isoformat(),'opens_at':session.opens_at.isoformat(),
            'closes_at':session.closes_at.isoformat()}}


def test_startup_alpha_path_forces_full_portfolio_refresh(tmp_path):
    """Startup keeps the required immediate full broker refresh."""
    engine=Engine(tmp_path,RuntimeConfig(),Store(),clock=lambda:NOW)
    session=engine.calendar.session(NOW.date());state=flat_state(session)
    engine.controller=SimpleNamespace(state=state)
    engine.next_evaluation=NOW+timedelta(minutes=5)
    class Probe(Exception):pass
    engine.source=SimpleNamespace(refresh_portfolio=lambda _state:(_ for _ in ()).throw(Probe()))
    with pytest.raises(Probe):engine._alpha_step(NOW,session)


def universe_engine(monkeypatch,tmp_path,results):
    store=Store();current={'at':NOW};engine=Engine(tmp_path,RuntimeConfig(),store,clock=lambda:current['at'])
    session=engine.calendar.session(NOW.date());state=flat_state(session);source_calls=[];refreshes=[];leases=[]
    state['reconciliation']['valid_until']=session.closes_at.isoformat()
    ranked=(observation('focus-id','FOCUS'),observation('other-id','OTHER'))
    class Controller:
        account_id='test'
        @property
        def state(self):return state
        def lease(self):leases.append(current['at']);return state
        def refresh(self,value):refreshes.append(value);return state
    class Source:
        def refresh_portfolio(self,_):
            result=results.pop(0);source_calls.append((current['at'],result))
            if isinstance(result,Exception):raise result
            quote_at=current['at'] if result else current['at']-timedelta(seconds=3)
            items=tuple(replace(x,quote_at=quote_at,received_at=current['at'],quote_received_at=current['at'],
                evidence_id=x.evidence_id+'-'+str(len(source_calls))) for x in ranked)
            return replace(universe(items),received_at=current['at'],evidence_id='universe-'+str(len(source_calls)))
    engine.controller=Controller();engine.source=Source();engine.alpha_manager=SimpleNamespace(plans=lambda _:{})
    engine.brain_factory=lambda:(_ for _ in ()).throw(AssertionError('universe retry must not call Astra'))
    engine.gateway=SimpleNamespace(dispatch=lambda *_:(_ for _ in ()).throw(AssertionError('universe retry must not order')))
    engine.deep_plan={'regime_label':'TEST','regime_view':'Test.','focus':[]};engine.deep_plan_at=NOW;engine.next_evaluation=NOW
    monkeypatch.setattr('autonomous_trader.monitoring.runner.refresh_session',lambda controller,value,*_,**__:controller.refresh(value))
    engine._run_candidates=lambda session:None  # These cases isolate universe/reconciliation recovery.
    return engine,store,session,current,source_calls,refreshes,leases


def test_two_universe_insufficiencies_retry_without_busy_loop_model_or_order(monkeypatch,tmp_path):
    engine,store,session,current,source_calls,refreshes,leases=universe_engine(monkeypatch,tmp_path,[False,False,True])
    for expected in (1,2):
        engine._alpha_step(current['at'],session)
        retry_at=current['at']+timedelta(seconds=engine.poll)
        event=[x for x in store.events if x[0]=='UNIVERSE_RETRY'][-1]
        assert event[1]['consecutive_insufficiencies']==expected and event[1]['retry_at']==retry_at.isoformat()
        assert engine.state['status']=='RUNNING' and engine.state['last_error'] is None
        assert engine.state['next_evaluation']==retry_at.isoformat() and engine.universe_retry_at==retry_at
        calls=len(source_calls);engine._alpha_step(current['at'],session)
        assert len(source_calls)==calls and len([x for x in store.events if x[0]=='UNIVERSE_RETRY'])==expected
        assert not refreshes and not leases
        current['at']=retry_at
    engine._alpha_step(current['at'],session)
    assert len(source_calls)==3 and len({x[0] for x in source_calls})==3
    assert engine.consecutive_universe_insufficiencies==0 and engine.universe_retry_at is None
    assert len(refreshes)==1 and len(leases)==1
    assert not any(x[0] in ('ALPHA_DEEP_PLAN','ALPHA_DECISION','INTENT_APPROVED','BROKER_RESULT') for x in store.events)


def test_exact_insufficiency_raised_by_universe_source_uses_same_bounded_retry(monkeypatch,tmp_path):
    engine,store,session,current,source_calls,refreshes,leases=universe_engine(
        monkeypatch,tmp_path,[ValueError('INSUFFICIENT_ELIGIBLE_UNIVERSE'),True])
    engine._alpha_step(current['at'],session)
    assert engine.state['status']=='RUNNING' and engine.state['last_error'] is None
    assert engine.consecutive_universe_insufficiencies==1 and not refreshes and not leases
    assert [x[0] for x in store.events].count('UNIVERSE_RETRY')==1
    current['at']=engine.universe_retry_at;engine._alpha_step(current['at'],session)
    assert engine.consecutive_universe_insufficiencies==0 and engine.universe_retry_at is None
    assert len(source_calls)==2 and len(refreshes)==1 and len(leases)==1


def test_valid_universe_resets_streak_then_queue_survives_later_discovery_insufficiency(monkeypatch,tmp_path):
    engine,store,session,current,_,_,_=universe_engine(monkeypatch,tmp_path,[False,True,False])
    engine._alpha_step(current['at'],session);current['at']=engine.universe_retry_at
    engine._alpha_step(current['at'],session)
    assert engine.consecutive_universe_insufficiencies==0
    current['at']=engine.next_discovery;engine._alpha_step(current['at'],session)
    events=[x for x in store.events if x[0]=='UNIVERSE_RETRY']
    assert [x[1]['consecutive_insufficiencies'] for x in events]==[1]
    assert len([x for x in store.events if x[0]=='DISCOVERY_RETRY'])==1
    assert len(engine.opportunities.jobs)==2
    assert engine.state['status']=='RUNNING' and engine.state['last_error'] is None


def test_third_consecutive_universe_insufficiency_escalates_fail_closed(monkeypatch,tmp_path):
    engine,store,session,current,source_calls,refreshes,leases=universe_engine(monkeypatch,tmp_path,[False,False,False])
    for _ in range(2):
        engine._alpha_step(current['at'],session);current['at']=engine.universe_retry_at
    with pytest.raises(ValueError,match='^INSUFFICIENT_ELIGIBLE_UNIVERSE$') as caught:
        engine._alpha_step(current['at'],session)
    engine.record_error(caught.value)
    assert engine.state['status']=='ERROR' and engine.state['last_error']=='INSUFFICIENT_ELIGIBLE_UNIVERSE'
    assert engine.consecutive_universe_insufficiencies==3 and len(source_calls)==3
    assert not refreshes and not leases
    assert len([x for x in store.events if x[0]=='UNIVERSE_RETRY'])==3
    assert not any(x[0] in ('ALPHA_DEEP_PLAN','ALPHA_DECISION','INTENT_APPROVED','BROKER_RESULT') for x in store.events)


@pytest.mark.parametrize('message',[
    'BROKER_READ_FAILED SNAPSHOT HTTP 503','UNIVERSE_SHAPE','FOCUSED_INSTRUMENT_IDENTITY_MISMATCH',
    'STALE_MARKET_OR_ACCOUNT'])
def test_other_universe_failures_remain_immediately_fatal(monkeypatch,tmp_path,message):
    engine,store,session,_,source_calls,refreshes,leases=universe_engine(monkeypatch,tmp_path,[ValueError(message)])
    with pytest.raises(ValueError,match=message):engine._alpha_step(NOW,session)
    assert len(source_calls)==1 and not refreshes and not leases
    assert engine.consecutive_universe_insufficiencies==0 and engine.universe_retry_at is None
    assert not any(x[0]=='UNIVERSE_RETRY' for x in store.events)


def focus_engine(monkeypatch,tmp_path,focus_results):
    store=Store();current={'at':NOW};engine=Engine(tmp_path,RuntimeConfig(),store,clock=lambda:current['at'])
    session=engine.calendar.session(NOW.date());state=flat_state(session);refreshes=[]
    ranked=(observation('focus-id','FOCUS'),observation('other-id','OTHER'),observation('third-id','THIRD'))
    class Controller:
        account_id='test'
        @property
        def state(self):return state
        def lease(self):return state
        def refresh(self,value):refreshes.append(value);return state
    class Source:
        def refresh_portfolio(self,_):
            items=tuple(replace(x,quote_at=current['at'],received_at=current['at'],quote_received_at=current['at']) for x in ranked)
            return replace(universe(items),received_at=current['at'])
        def refresh_focus(self,_,symbol):
            result=focus_results.pop(0)
            if isinstance(result,Exception):raise result
            item=next(x for x in ranked if x.ticker==symbol)
            item=replace(item,entry_eligible=result,quote_at=current['at'],received_at=current['at'],quote_received_at=current['at'])
            return replace(universe((item,)),received_at=current['at'])
        def refresh_comparative_focus(self,_,symbol,alternatives):
            result=focus_results.pop(0)
            if isinstance(result,Exception):raise result
            names={symbol,*alternatives}
            items=tuple(replace(x,entry_eligible=result,quote_at=current['at'],received_at=current['at'],
                quote_received_at=current['at']) for x in ranked if x.ticker in names)
            return replace(universe(items),received_at=current['at'])
    class Brain:
        def fast_decide(self,context,mode):
            return {'decision':{'schema_version':'1.0','action':'ABSTAIN','reason':'No positive net edge now.',
                'open':None,'hold':None,'modify':None,'close':None},'schema_valid':True,'semantics_valid':True,
                'model':MODEL,'reasoning':'medium','latency_ms':1,
                'usage':{'input_tokens':100,'output_tokens':20},'estimated_cost_usd':'0.001',
                'model_work_id':'test-portfolio-claim'}
        def portfolio_decide(self,context):return self.fast_decide(context,'ENTRY')
    engine.controller=Controller();engine.source=Source();engine.alpha_manager=SimpleNamespace(plans=lambda _:{})
    engine.brain_factory=lambda:Brain();engine.gateway=SimpleNamespace()
    engine.deep_plan={'regime_label':'TEST','regime_view':'Test.','focus':[
        {'instrument_id':x.instrument_id,'symbol':x.ticker,'rank':n,'strategy_label':'test',
         'thesis':'Current evidence.','horizon':'intraday'} for n,x in enumerate(ranked,1)]}
    engine.deep_plan_at=NOW;engine.next_evaluation=NOW
    monkeypatch.setattr('autonomous_trader.monitoring.runner.refresh_session',lambda *_args,**_kwargs:state)
    return engine,store,session,refreshes,current


def test_two_invalid_candidates_drop_without_aborting_batch(monkeypatch,tmp_path):
    engine,store,session,refreshes,current=focus_engine(monkeypatch,tmp_path,[False,False,True])
    engine._alpha_step(current['at'],session)
    jobs=list(engine.opportunities.jobs.values())
    assert [j['status'] for j in jobs]==['DROPPED','PENDING','PENDING']
    assert not refreshes and engine.state['last_error'] is None
    engine._run_candidates(session)
    assert [j['status'] for j in jobs]==['DROPPED','DROPPED','PENDING'] and not refreshes
    engine._run_candidates(session)
    assert len(refreshes)==1 and jobs[2]['status']=='RUNNING'
    for future,_,_ in engine.opportunities.futures.values():future.result(timeout=2)
    engine._run_candidates(session)
    assert jobs[2]['status']=='COMPLETED' and jobs[2]['reason']=='ABSTAIN'
    assert not any(x[0] in ('INTENT_APPROVED','BROKER_RESULT') for x in store.events)


@pytest.mark.parametrize('message',[
    'SELECTED_SYMBOL_NOT_ELIGIBLE','BROKER_READ_FAILED SNAPSHOT HTTP 503',
    'FOCUSED_INSTRUMENT_IDENTITY_MISMATCH','STALE_MARKET_OR_ACCOUNT'])
def test_symbol_local_refresh_error_does_not_stop_other_candidates(monkeypatch,tmp_path,message):
    engine,store,session,refreshes,current=focus_engine(monkeypatch,tmp_path,[ValueError(message),True])
    engine._alpha_step(current['at'],session)
    jobs=list(engine.opportunities.jobs.values())
    assert jobs[0]['status']=='DROPPED' and jobs[0]['reason']==message
    assert jobs[1]['status']=='PENDING' and not refreshes
    engine._run_candidates(session)
    assert jobs[1]['status']=='RUNNING' and len(refreshes)==1
    assert engine.state['status']=='RUNNING'


@pytest.mark.parametrize('message',['BROKER_READ_FAILED BALANCE HTTP 503','UNTRACKED_BROKER_ORDER','UNIVERSE_SHAPE'])
def test_global_or_unknown_refresh_error_remains_fail_closed(monkeypatch,tmp_path,message):
    engine,store,session,refreshes,_=focus_engine(monkeypatch,tmp_path,[ValueError(message)])
    with pytest.raises(ValueError,match=message):engine._alpha_step(NOW,session)
    assert not refreshes and not engine.opportunities.futures


def test_repeated_fail_closed_error_events_are_not_deduplicated(tmp_path):
    store=Store();engine=Engine(tmp_path,RuntimeConfig(),store,clock=lambda:NOW)
    engine.next_evaluation=NOW+timedelta(minutes=5)
    for _ in range(3):engine.record_error(ValueError('BROKER_READ_FAILED SNAPSHOT HTTP 503'))
    assert [kind for kind,_,_ in store.events]==['ERROR','ERROR','ERROR']
    assert engine.next_evaluation==NOW+timedelta(minutes=5)
    assert engine.state['last_error']=='BROKER_READ_FAILED SNAPSHOT HTTP 503'
