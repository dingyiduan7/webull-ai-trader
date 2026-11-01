"""Focused tests for bounded flat-account polling in the live runner."""
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from test_alpha_reset import NOW,observation,universe
from autonomous_trader.monitoring.runner import Engine
from autonomous_trader.mvp.config import RuntimeConfig


class Store:
    def __init__(self):self.events=[];self.heartbeats=[]
    def runtime(self):return {}
    def heartbeat(self,value):self.heartbeats.append(value)
    def recent_outcomes(self):return ()
    def emit(self,kind,payload,**kwargs):self.events.append((kind,payload,kwargs))


def flat_state(session):
    return {'positions':{},'orders':{},'reservations':{},'incidents':{},'latches':{
        'daily':False,'drawdown':False,'incident':False,'terminated':False},
        'cash':{'liquidation_equity_usd':'1000000','settled_usd':'1000000'},'day_start_equity':'1000000',
        'policy_hash':'test-policy','config':{'capabilities':{'version':'mvp-local-monitor-v1'}},
        'reconciliation':{'ready':True,'settlement_known':True,
            'valid_until':(NOW+timedelta(seconds=10)).isoformat()},'session':{
            'label':session.label.isoformat(),'opens_at':session.opens_at.isoformat(),'closes_at':session.closes_at.isoformat()}}


def flat_engine(monkeypatch,tmp_path):
    store=Store();current={'at':NOW};engine=Engine(tmp_path,RuntimeConfig(),store,clock=lambda:current['at'])
    session=engine.calendar.session(NOW.date());state=flat_state(session);source_calls=[];leases=[];refreshes=[]
    items=(observation('one-id','ONE'),observation('two-id','TWO'))
    class Controller:
        account_id='test'
        @property
        def state(self):return state
        def lease(self):leases.append(current['at']);return state
        def refresh(self,value):refreshes.append(value);return state
    class Source:
        def refresh_portfolio(self,_):
            source_calls.append(current['at'])
            fresh=tuple(replace(x,quote_at=current['at'],received_at=current['at'],quote_received_at=current['at']) for x in items)
            return replace(universe(fresh),received_at=current['at'])
        refresh_account=refresh_portfolio
    engine.controller=Controller();engine.source=Source();engine.alpha_manager=SimpleNamespace()
    engine.initialized=True
    engine.brain_factory=lambda:(_ for _ in ()).throw(AssertionError('cached flat wait must not call Astra'))
    engine.gateway=SimpleNamespace(dispatch=lambda *_:(_ for _ in ()).throw(AssertionError('cached flat wait must not order')))
    engine.deep_plan={'regime_label':'TEST','regime_view':'Test.','focus':[]};engine.deep_plan_at=NOW
    engine._run_candidates=lambda session:None  # Polling tests isolate the account/discovery cadence.
    monkeypatch.setattr('autonomous_trader.monitoring.runner.refresh_session',lambda controller,value,*_,**__:controller.refresh(value))
    return engine,store,session,state,current,source_calls,leases,refreshes


def test_flat_wait_uses_durable_state_without_broker_model_lease_or_order(monkeypatch,tmp_path):
    engine,store,session,_,current,source_calls,leases,refreshes=flat_engine(monkeypatch,tmp_path)
    engine.next_evaluation=NOW+timedelta(minutes=5);engine.next_discovery=NOW+timedelta(minutes=5)
    engine.next_poll=NOW+timedelta(seconds=30)
    for elapsed in (0,5,20):
        current['at']=NOW+timedelta(seconds=elapsed);engine._alpha_step(current['at'],session);engine.heartbeat()
    # The first two turns use the cached checkpoint. Once its ten-second
    # reconciliation window expires, account-only recovery runs immediately.
    assert source_calls==[NOW+timedelta(seconds=20)] and leases==[NOW+timedelta(seconds=20)] and len(refreshes)==1
    assert len(store.heartbeats)==3
    assert engine.state['activity']=='Flat account reconciliation refreshed; market discovery remains independent'
    assert engine.state['next_account_poll']==(NOW+timedelta(seconds=50)).isoformat()
    assert current['at']>NOW+timedelta(seconds=10)


def test_expired_flat_reconciliation_recovers_without_candidate_universe(monkeypatch,tmp_path):
    store=Store();current=NOW+timedelta(seconds=11);engine=Engine(tmp_path,RuntimeConfig(),store,clock=lambda:current)
    session=engine.calendar.session(NOW.date());state=flat_state(session);calls=[]
    class Controller:
        account_id='test'
        @property
        def state(self):return state
        def lease(self):calls.append('lease');return state
        def refresh(self,value):
            calls.append('reconciled');state['reconciliation']['valid_until']=(current+timedelta(seconds=10)).isoformat()
            return state
    class Source:
        def refresh_account(self,_):calls.append('account');return universe(())
        def refresh_portfolio(self,_):raise AssertionError('candidate universe must not gate account reconciliation')
    engine.controller=Controller();engine.source=Source();engine.alpha_manager=SimpleNamespace()
    engine.gateway=SimpleNamespace();engine.next_poll=current+timedelta(seconds=30)
    engine.next_evaluation=current+timedelta(minutes=5);engine.next_discovery=current+timedelta(minutes=5)
    monkeypatch.setattr('autonomous_trader.monitoring.runner.refresh_session',
        lambda controller,value,*_,**__:controller.refresh(value))
    engine._alpha_step(current,session)
    assert calls==['account','lease','reconciled']
    assert engine.state['status']=='RUNNING' and engine.state['last_error'] is None


def test_failed_initialize_is_rolled_back_and_retried_even_after_controller_assignment(monkeypatch,tmp_path):
    engine=Engine(tmp_path,RuntimeConfig(),Store(),clock=lambda:NOW);attempts=[];closed=[]
    class Stream:
        def close(self):closed.append(len(attempts))
    def initialize_once():
        attempts.append(len(attempts)+1);engine.live_stream=Stream()
        engine.controller=SimpleNamespace(state={'positions':{}});engine.alpha_manager=SimpleNamespace()
        if len(attempts)==1:raise ValueError('LEASE_HELD_BY_OTHER_WRITER')
        engine.model_budget=SimpleNamespace();engine.opportunities=SimpleNamespace()
    monkeypatch.setattr(engine,'_initialize_once',initialize_once)
    monkeypatch.setattr(engine,'_alpha_step',lambda now,session:'READY')
    with pytest.raises(ValueError,match='LEASE_HELD_BY_OTHER_WRITER'):engine.step()
    assert not engine.initialized and engine.controller is None and engine.model_budget is None
    assert engine.opportunities is None and closed==[1]
    assert engine.step()=='READY' and engine.initialized and len(attempts)==2


def test_due_account_poll_performs_one_full_refresh_and_rearms_from_completion(monkeypatch,tmp_path):
    engine,_,session,_,current,source_calls,leases,refreshes=flat_engine(monkeypatch,tmp_path)
    engine.next_evaluation=NOW+timedelta(minutes=5);engine.next_poll=NOW
    engine._alpha_step(NOW,session)
    assert source_calls==[NOW] and leases==[NOW] and len(refreshes)==1
    assert engine.next_poll==NOW+timedelta(seconds=30)
    assert engine.state['next_account_poll']==engine.next_poll.isoformat()
    current['at']=NOW+timedelta(seconds=5);engine._alpha_step(current['at'],session)
    assert source_calls==[NOW]


def test_actual_runner_loop_executes_overdue_poll_while_evaluation_is_future(monkeypatch,tmp_path):
    engine,store,_,_,_,source_calls,leases,refreshes=flat_engine(monkeypatch,tmp_path)
    engine.next_evaluation=NOW+timedelta(minutes=5);engine.next_poll=NOW-timedelta(seconds=1)
    class OneIterationStop:
        def __init__(self):self.checked=0
        def is_set(self):
            self.checked+=1
            return self.checked>1
        def wait(self,_):return False
    engine.run(OneIterationStop())
    assert source_calls==[NOW] and leases==[NOW] and len(refreshes)==1
    assert engine.next_poll==NOW+timedelta(seconds=30)
    assert engine.state['next_account_poll']==engine.next_poll.isoformat()
    assert store.heartbeats


def test_startup_and_due_evaluation_force_full_refresh(monkeypatch,tmp_path):
    engine,_,session,_,_,source_calls,leases,refreshes=flat_engine(monkeypatch,tmp_path)
    assert engine.next_poll==NOW and engine.next_evaluation==NOW
    engine._alpha_step(NOW,session)
    assert source_calls==[NOW] and leases==[NOW] and len(refreshes)==1

    engine,_,session,_,current,source_calls,leases,refreshes=flat_engine(monkeypatch,tmp_path)
    engine.next_poll=NOW+timedelta(seconds=30);engine.next_evaluation=NOW
    engine._alpha_step(current['at'],session)
    assert source_calls==[NOW] and leases==[NOW] and len(refreshes)==1


@pytest.mark.parametrize('mutation',['session','not_ready','unknown_order','pending_correction','latch'])
def test_untrusted_local_state_forces_full_refresh(monkeypatch,tmp_path,mutation):
    engine,_,session,state,_,source_calls,_,_=flat_engine(monkeypatch,tmp_path)
    engine.next_evaluation=NOW+timedelta(minutes=5);engine.next_poll=NOW+timedelta(seconds=30)
    if mutation=='session':state['session']['label']=(session.label-timedelta(days=1)).isoformat()
    elif mutation=='not_ready':state['reconciliation']['ready']=False
    elif mutation=='unknown_order':state['orders']['order-id']={'final':False,'phase':'UNKNOWN'}
    elif mutation=='pending_correction':state['incidents']['incident-id']={'pending_correction':True}
    else:state['latches']['incident']=True
    engine._alpha_step(NOW,session)
    assert source_calls==([] if mutation=='unknown_order' else [NOW])


def test_flat_generic_error_waits_then_clean_refresh_clears_error(monkeypatch,tmp_path):
    engine,_,session,_,current,source_calls,_,_=flat_engine(monkeypatch,tmp_path)
    engine.state.update(status='ERROR',last_error='BROKER_READ_FAILED SNAPSHOT HTTP 503')
    engine.next_evaluation=NOW+timedelta(minutes=5);engine.next_poll=NOW+timedelta(seconds=30)
    engine._alpha_step(NOW,session)
    assert not source_calls and engine.state['status']=='ERROR'
    assert engine.state['last_error']=='BROKER_READ_FAILED SNAPSHOT HTTP 503'
    assert engine.state['activity']=='Error; bounded flat broker retry pending; no order authority'
    current['at']=engine.next_poll;engine._alpha_step(current['at'],session)
    assert source_calls==[NOW+timedelta(seconds=30)]
    assert engine.state['status']=='RUNNING' and engine.state['last_error'] is None


def test_reconciliation_error_is_not_delayed_by_flat_retry_gate(monkeypatch,tmp_path):
    engine,_,session,_,_,source_calls,_,_=flat_engine(monkeypatch,tmp_path)
    engine.state.update(status='ERROR',last_error='SETTLEMENT_CHANGE_UNRECONCILED')
    engine.next_evaluation=NOW+timedelta(minutes=5);engine.next_poll=NOW+timedelta(seconds=30)
    engine._alpha_step(NOW,session)
    assert source_calls==[NOW]


def test_held_supervision_precedes_any_flat_wait(monkeypatch,tmp_path):
    engine=Engine(tmp_path,RuntimeConfig(),Store(),clock=lambda:NOW);session=engine.calendar.session(NOW.date())
    state=flat_state(session);state['positions']={'held-id':{'quantity':'1'}}
    class HeldProbe(Exception):pass
    engine.controller=SimpleNamespace(state=state)
    engine.source=SimpleNamespace(refresh_held=lambda _:(_ for _ in ()).throw(HeldProbe()),
        refresh_portfolio=lambda _:(_ for _ in ()).throw(AssertionError('broad refresh preceded held supervision')))
    engine.alpha_manager=SimpleNamespace();engine.next_evaluation=NOW+timedelta(minutes=5)
    with pytest.raises(HeldProbe):engine._alpha_step(NOW,session)


def test_cancel_pending_recovery_precedes_any_flat_wait(tmp_path):
    engine=Engine(tmp_path,RuntimeConfig(),Store(),clock=lambda:NOW);session=engine.calendar.session(NOW.date())
    state=flat_state(session);state['orders']={'order-id':{'final':False,'phase':'CANCEL_PENDING'}}
    class RecoveryProbe(Exception):pass
    engine.controller=SimpleNamespace(state=state)
    engine.gateway=SimpleNamespace(recover_terminal_cancellations=lambda _:(_ for _ in ()).throw(RecoveryProbe()))
    engine.source=SimpleNamespace(refresh_portfolio=lambda _:(_ for _ in ()).throw(AssertionError('refresh preceded recovery')))
    engine.alpha_manager=SimpleNamespace();engine.next_evaluation=NOW+timedelta(minutes=5)
    with pytest.raises(RecoveryProbe):engine._alpha_step(NOW,session)


def test_existing_intervals_supply_nonaggressive_cadence(tmp_path):
    engine=Engine(tmp_path,RuntimeConfig(),Store(),clock=lambda:NOW)
    assert engine.poll==30 and engine.interval==300 and engine.next_poll==NOW and engine.next_evaluation==NOW
