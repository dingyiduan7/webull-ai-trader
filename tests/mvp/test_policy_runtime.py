from concurrent.futures import Future
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
import json
import inspect
import pytest
from test_cycle import NOW,setup_cycle
from test_alpha_reset import observation,universe,api_response
from test_opportunity_pipeline import Store
from autonomous_trader.economic_policy import (AuthorizeSandboxEconomics, ReleaseSandboxPreferences, AuthorizeDailyDownsideBudget, SandboxEconomics, assert_policy_current,
    captured_policy)
from autonomous_trader.mvp.alpha import build_alpha_context
from autonomous_trader.mvp.config import RuntimeConfig,ExecutionMode
from autonomous_trader.monitoring.runner import Engine,ObservedBrain
from autonomous_trader.monitoring.views import risk_view
from autonomous_trader.decision_api.astra_probe import AstraClient


def test_active_alpha_loop_does_not_spend_model_budget_on_policy_mutation():
    assert 'self._economic_step(' not in inspect.getsource(Engine._alpha_step)


def authorize(controller):
    controller.ledger.execution_scope='SANDBOX';controller.lease();controller.send(AuthorizeSandboxEconomics())


class Executor:
    def __init__(self):self.tasks=[]
    def submit(self,fn,*args):
        f=Future();self.tasks.append((f,fn,args));return f


def test_policy_context_admission_display_and_structured_boundary(setup_cycle,monkeypatch):
    args,_,_=setup_cycle;c=args['controller'];authorize(c)
    u=universe((observation('stock-b','BBB'),));c.refresh(u)
    context=build_alpha_context(u,c.state,NOW)
    assert context.economic_policy_hash==c.state['policy_hash']
    assert captured_policy(context)==SandboxEconomics()
    assert risk_view(c.state)['effective_policy']['revision']==1
    assert_policy_current(context,c.state)
    with pytest.raises(ValueError,match='STALE_POLICY_VERSION'):
        assert_policy_current(context,c.state|{'policy_hash':'changed'})
    proposal={'based_revision':1,'based_policy_hash':c.state['policy_hash'],'policy':SandboxEconomics().model_dump(mode='json'),
        'release_halts':[],'reason':'Current defaults remain adequate','evidence':'Fresh account','expected_incremental_value_usd':100}
    client=AstraClient('fixture-key');seen=[]
    def request(method,path,body):
        seen.append(body);return api_response(proposal)
    monkeypatch.setattr(client,'_request',request)
    result=client.policy_decide({'based_revision':1,'based_policy_hash':c.state['policy_hash']})
    assert result['proposal']['policy']['maximum_positions']==5
    schema=seen[0]['text']['format']['schema']
    assert set(schema['$defs']['SandboxEconomics']['required'])==set(schema['$defs']['SandboxEconomics']['properties'])
    assert 'fixture-key' not in json.dumps(seen)


def test_economic_review_reachable_while_halted_without_order_authority(setup_cycle,tmp_path):
    args,broker,_=setup_cycle;c=args['controller'];authorize(c)
    u=universe((observation('stock-b','BBB'),));c.refresh(u)
    state=deepcopy(c.state);state['latches']['daily']=True
    store=Store();e=Engine(tmp_path,RuntimeConfig(mode=ExecutionMode.SANDBOX),store,clock=lambda:NOW)
    e.controller=SimpleNamespace(state=state);e.policy_executor=Executor()
    e.brain_factory=lambda:SimpleNamespace(policy_decide=lambda ctx:None)
    e._economic_step(e.calendar.session(NOW.date()))
    assert len(e.policy_executor.tasks)==1 and not broker.orders
    context=e.policy_executor.tasks[0][2][0]
    assert context['latches']['daily'] and context['based_revision']==1
    e._economic_step(e.calendar.session(NOW.date()))
    assert len(e.policy_executor.tasks)==1
    state['latches']['incident']=True;e.policy_pending=None
    e._economic_step(e.calendar.session(NOW.date()))
    assert len(e.policy_executor.tasks)==1


def test_policy_completion_runs_on_writer_and_does_not_call_again_for_own_cost(setup_cycle,tmp_path):
    args,broker,_=setup_cycle;c=args['controller'];authorize(c)
    u=universe((observation('stock-b','BBB'),));c.refresh(u)
    store=Store();e=Engine(tmp_path,RuntimeConfig(mode=ExecutionMode.SANDBOX),store,clock=lambda:NOW)
    e.controller=c;e.source=SimpleNamespace(refresh_account=lambda s:u);e.policy_executor=Executor()
    e.brain_factory=lambda:SimpleNamespace(policy_decide=lambda ctx:None)
    session=e.calendar.session(NOW.date());e._economic_step(session)
    future,_,(context,)=e.policy_executor.tasks[0]
    value={'proposal':{'based_revision':1,'based_policy_hash':c.state['policy_hash'],
        'policy':SandboxEconomics(stock_exposure='0.4',calls_per_300s=30).model_dump(mode='json'),
        'release_halts':[],'reason':'Current payoff merits budget','evidence':'Synthetic test only',
        'expected_incremental_value_usd':'200'},'model':'gpt-6-astra','reasoning':'medium','usage':{},
        'estimated_cost_usd':'0.05','response_at':NOW.isoformat()}
    future.set_result(value);e._economic_step(session)
    assert c.state['economic_policy']['revision']==2
    assert e._queue().calls_per_window==30 and not broker.orders
    store.recent_outcomes=lambda:(json.dumps({'memory_type':'portfolio_economics','all_known_api_model_cost_usd':'0.05'}),)
    e._economic_step(session)
    assert len(e.policy_executor.tasks)==1


def test_released_preferences_reach_context_queue_and_changed_evidence_planning(setup_cycle,tmp_path):
    args,broker,_=setup_cycle;c=args['controller'];authorize(c)
    u=universe((observation('stock-b','BBB'),));c.refresh(u)
    c.send(ReleaseSandboxPreferences())
    context=build_alpha_context(u,c.state,NOW)
    policy=captured_policy(context)
    assert (policy.stock_exposure,policy.trade_risk,policy.open_risk,policy.maximum_positions)==(1,1,1,9)
    assert risk_view(c.state)['effective_policy']['revision']==2
    engine=Engine(tmp_path,RuntimeConfig(mode=ExecutionMode.SANDBOX),Store(),clock=lambda:NOW)
    engine.controller=c;engine.planning_executor=Executor()
    engine.brain_factory=lambda:SimpleNamespace(deep_plan=lambda ctx:None)
    engine.deep_plan={'focus':[]};engine.deep_plan_at=NOW-timedelta(seconds=1)
    engine.deep_context_signature='previous-material-context'
    assert engine._queue().calls_per_window==120
    engine._start_planning(context)
    assert len(engine.planning_executor.tasks)==1
    # No duplicate work while pending or when the same evidence is seen again.
    engine._start_planning(context)
    _,_,signature,_=engine.background.pop('deep')
    engine.deep_context_signature=signature
    engine._start_planning(context)
    assert len(engine.planning_executor.tasks)==1 and not broker.orders


def test_queue_respects_hard_non_position_call_gate(setup_cycle,tmp_path):
    args,_,_=setup_cycle;c=args['controller'];authorize(c)
    c.refresh(universe((observation('stock-b','BBB'),)))
    c.send(ReleaseSandboxPreferences())
    engine=Engine(tmp_path,RuntimeConfig(mode=ExecutionMode.SANDBOX),Store(),clock=lambda:NOW)
    engine.controller=c
    engine.model_budget=SimpleNamespace(health=lambda:{'non_position_calls_per_300s':4})
    queue=engine._queue()
    assert queue.calls_per_window==4
    queue.calls=[NOW.isoformat()]*4
    assert queue.capacity()==0


def test_budget_wait_keeps_provider_available_and_records_no_failed_dispatch(monkeypatch):
    monkeypatch.setattr('autonomous_trader.monitoring.runner.captured_policy',lambda _:SandboxEconomics())
    class Client:
        last_call=None
        def fast_decide(self,*args,**kwargs):raise ValueError('MODEL_CALL_BUDGET_WAIT')
    class Runtime:
        model_budget=None
        config=SimpleNamespace(model='gpt-6-astra')
        updates=[];events=[]
        def clock(self):return NOW
        def update(self,**values):self.updates.append(values)
        def emit(self,*values):self.events.append(values)
    runtime=Runtime();brain=ObservedBrain(Client(),runtime)
    with pytest.raises(ValueError,match='^MODEL_CALL_BUDGET_WAIT$'):
        brain.fast_decide(SimpleNamespace(),mode='ENTRY')
    assert runtime.updates[-1]['openai']=='AVAILABLE'
    assert runtime.events==[]


def test_daily_downside_headroom_supplied_to_astra(setup_cycle):
    args,_,_=setup_cycle;c=args['controller'];authorize(c)
    u=universe((observation('stock-b','BBB'),));c.refresh(u)
    c.send(ReleaseSandboxPreferences());c.send(AuthorizeDailyDownsideBudget())
    context=build_alpha_context(u,c.state,NOW)
    assert context.remaining_risk_usd==50000
    assert context.economic_policy_revision==3
    assert captured_policy(context).stock_exposure==1
    view=risk_view(c.state)
    downside=next(row for row in view['rows'] if row['name']=='Aggregate planned downside (effective)')
    assert downside['limit_percent']==5
    assert view['daily_downside_active'] is True
    assert view['daily_downside_limit_usd']==50000
    assert view['remaining_daily_downside_usd']==50000
    assert view['aggregate_preference_ceiling_percent']==100
