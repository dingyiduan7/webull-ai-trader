"""Comparison and routing carry no broker authority or duplicate-call privilege."""
import json
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
from types import SimpleNamespace
import pytest

from test_cycle import NOW,setup_cycle
from test_alpha_reset import observation,universe,open_raw,api_response
from test_opportunity_pipeline import engine_fixture,queue_fixture,result,ManualExecutor
from autonomous_trader.monitoring.runner import comparative_opportunities,fast_context_signature
from autonomous_trader.mvp.alpha import build_alpha_context
from autonomous_trader.decision_api.astra_probe import AstraClient,fast_alpha_request,FAST_ENTRY_PROMPT
from autonomous_trader.sandbox.secrets import PreflightError
from autonomous_trader.mvp.context import SandboxSource
from autonomous_trader.sandbox.http import Operation


def jobs(items):
    return [{'instrument_id':x.instrument_id,'symbol':x.ticker,'current':True,'rank':n,
        'status':'PENDING','reason':'DISCOVERED'} for n,x in enumerate(items)]


def test_bounded_fresh_comparison_excludes_stale_invalid_held_and_unknown():
    focal=observation('focus','FOCUS')
    good=[observation('alt'+str(n),'ALT'+str(n)) for n in range(12)]
    bad=[replace(observation('old','OLD'),quote_at=NOW-timedelta(seconds=3)),
        replace(observation('bad','BAD'),entry_eligible=False),
        replace(observation('invalid','INVALID'),bid=D('101'),ask=D('100')),
        observation('held','HELD'),observation('unknown','UNKNOWN')]
    items=[focal]+bad+good
    queued=jobs(items[:-1])  # Remove a good candidate; unknown is removed explicitly below.
    queued=[j for j in queued if j['instrument_id']!='unknown']
    rows=comparative_opportunities(universe(items),'focus',queued,
        {'focus':[{'instrument_id':'alt0','rank':1,'thesis':'Independent asymmetric opportunity.'}]},NOW,{'held'})
    decoded=list(map(json.loads,rows))
    assert len(rows)==8 and sum(map(len,rows))<=8000 and all(len(x)<=1900 for x in rows)
    assert not ({'focus','old','bad','invalid','held','unknown'} & {x['instrument_id'] for x in decoded})
    assert decoded[0]['deep_rank']==1 and decoded[0]['deep_thesis']
    assert all(x['quote_age_ms']==0 and x['pipeline_status']=='PENDING' for x in decoded)


@pytest.mark.parametrize('action',['OPEN','ABSTAIN','DEFER'])
def test_comparison_and_observed_costs_reach_real_request_boundary(monkeypatch,setup_cycle,action):
    args,_,_=setup_cycle;items=[observation('stock-b','BBB'),observation('alt','ALT')]
    u=universe(items);args['controller'].refresh(u)
    context=build_alpha_context(u,args['controller'].state,NOW,instrument_ids=('stock-b',))
    rows=comparative_opportunities(u,'stock-b',jobs(items),None,NOW)
    context=context.model_copy(update={'comparative_opportunities':rows,'focal_instrument_id':'stock-b'})
    decision=open_raw(NOW) if action=='OPEN' else result()['decision']
    decision={**decision,'defer_to':{'instrument_id':'alt','symbol':'ALT'} if action=='DEFER' else None}
    client=AstraClient('synthetic-key');requests=[]
    monkeypatch.setattr(client,'_request',lambda *args:(requests.append(args[2]) or api_response({'decision':decision})))
    value=client.fast_decide(context,mode='ENTRY',hard_policy={},clock=lambda:NOW)
    sent=json.loads(requests[0]['input'][1]['content'])['context']
    assert sent['comparative_opportunities']==list(rows) and sent['execution_cost_evidence']
    assert [x['instrument_id'] for x in sent['instruments']]==['stock-b']
    assert value['semantics_valid'] and value['decision']['action']==('OPEN' if action=='OPEN' else 'ABSTAIN')
    assert 'defer_to' not in value['decision']  # Canonical order contract unchanged.
    assert value['defer_to']==decision['defer_to']
    if action=='OPEN':assert 'forecast' not in decision and 'calibration_id' not in json.dumps(decision)
    assert all(term in FAST_ENTRY_PROMPT for term in ('sunk','symmetrically','Do not fabricate calibration','never\nforce OPEN'))


@pytest.mark.parametrize('action,target',[('OPEN','alt'),('ABSTAIN','missing')])
def test_invalid_routing_is_rejected(monkeypatch,setup_cycle,action,target):
    args,_,_=setup_cycle;items=[observation('stock-b','BBB'),observation('alt','ALT')]
    u=universe(items);args['controller'].refresh(u)
    context=build_alpha_context(u,args['controller'].state,NOW,instrument_ids=('stock-b',))
    context=context.model_copy(update={'comparative_opportunities':comparative_opportunities(u,'stock-b',jobs(items),None,NOW)})
    d=open_raw(NOW) if action=='OPEN' else result()['decision']
    d={**d,'defer_to':{'instrument_id':target,'symbol':'ALT'}}
    client=AstraClient('synthetic-key');monkeypatch.setattr(client,'_request',lambda *args:api_response({'decision':d}))
    with pytest.raises(PreflightError,match='INVALID_COMPARISON_ROUTING'):
        client.fast_decide(context,mode='ENTRY',hard_policy={},clock=lambda:NOW)


def test_route_only_reorders_existing_jobs_and_preserves_budget_cooldown_fingerprints():
    q,_,executor,current=queue_fixture();a,b,c=list(q.jobs.values())
    c['attempts']=99;c['not_before']=(NOW+timedelta(seconds=30)).isoformat()
    c['fingerprints']=['already-seen']
    assert q.prefer(c['instrument_id'],c['symbol'])
    assert c not in q.eligible_jobs()
    current['at']+=timedelta(seconds=31)
    assert q.eligible_jobs()[0] is c
    q.prepare(c)
    assert not q.submit(c,None,'already-seen',lambda:None) and not executor.tasks
    assert not q.prefer('nonexistent','UNKNOWN')
    q.calls=[current['at'].isoformat()]*3
    assert q.prefer(b['instrument_id'],b['symbol']) and q.capacity()==0
    assert not q.submit(b,None,'new',lambda:None) and not executor.tasks


def test_deferred_alternative_gets_own_exact_refresh_and_cannot_directly_order(setup_cycle,tmp_path):
    e,store,broker,context,versions,session=engine_fixture(setup_cycle,tmp_path)
    candidates=[observation(j['instrument_id'],j['symbol']) for j in e.opportunities.jobs.values()]
    focal,target=list(e.opportunities.jobs.values())[:2];target['attempts']=99
    rows=comparative_opportunities(universe(candidates),focal['instrument_id'],e.opportunities.jobs.values(),None,NOW)
    context=context.model_copy(update={'comparative_opportunities':rows})
    focal['status']='RESULT_READY';ai=result();ai['defer_to']={'instrument_id':target['instrument_id'],'symbol':target['symbol']}
    e._candidate_result(focal,context,ai,session)
    assert not broker.orders and not versions and e.opportunities.eligible_jobs()[0] is target
    e.config=replace(e.config,opportunity_workers=1)
    executor=ManualExecutor();e.opportunities.executor=executor
    e.brain_factory=lambda:SimpleNamespace(portfolio_decide=lambda c:result())
    e._run_candidates(session)
    assert len(versions)==1 and target['status']=='RUNNING' and len(executor.tasks)==1
    assert not broker.orders
    e._run_candidates(session)
    assert len([key for _,key,_ in e.opportunities.futures.values() if key==target['key']])==1


def test_comparison_alone_cannot_authorize_alternative_open(setup_cycle,tmp_path):
    e,_,broker,context,versions,session=engine_fixture(setup_cycle,tmp_path)
    focal,target=list(e.opportunities.jobs.values())[:2];focal['status']='RESULT_READY'
    context=context.model_copy(update={'instruments':tuple(x for x in context.instruments
        if x.instrument_id==focal['instrument_id']),
        'comparative_opportunities':('{"instrument_id":"'+target['instrument_id']+'","symbol":"'+target['symbol']+'"}',)})
    e._candidate_result(focal,context,result(open_raw(NOW,target['instrument_id'],target['symbol'])),session)
    assert focal['reason']=='ALPHA_PORTFOLIO_SELECTION_MISMATCH' and not broker.orders and not versions


def test_typed_portfolio_shortlist_enters_ledger_refresh_once(setup_cycle,tmp_path):
    e,_,broker,_,_,session=engine_fixture(setup_cycle,tmp_path)
    items=[observation(j['instrument_id'],j['symbol']) for j in e.opportunities.jobs.values()]
    e.source.refresh_comparative_focus=lambda state,symbol,alternatives:universe(items)
    observed=[];refresh=e.controller.refresh
    def capture(u):
        observed.append([x.instrument_id for x in u.candidates]);return refresh(u)
    e.controller.refresh=capture;e.config=replace(e.config,opportunity_workers=1)
    e.brain_factory=lambda:SimpleNamespace(portfolio_decide=lambda c:result())
    executor=ManualExecutor();e.opportunities.executor=executor
    e._run_candidates(session)
    assert observed==[[x.instrument_id for x in items]] and not broker.orders
    context=next(iter(e.opportunities.futures.values()))[2]
    assert context.comparative_opportunities==()
    assert [x.instrument_id for x in context.instruments]==[x.instrument_id for x in items]


def test_comparative_focus_uses_one_bounded_native_batch_without_discovery():
    calls=[];snapshot_calls=[0]
    class Reader:
        def read(self,op,*,symbols=('AAPL',),**kwargs):
            calls.append((op,tuple(symbols)))
            if op==Operation.ACCOUNTS:body=[{'account_id':'paper','account_type':'CASH','account_class':'INDIVIDUAL_CASH'}]
            elif op==Operation.BALANCE:body={'account_currency_assets':[{'currency':'USD','settled_cash':'1000000','unsettled_cash':'0','net_liquidation_value':'1000000'}]}
            elif op==Operation.POSITIONS:body=[]
            elif op==Operation.OPEN_ORDERS:body={'data':[],'pagination_key':None}
            elif op==Operation.INSTRUMENTS:body={'data':[{'symbol':s,'instrument_id':'id-'+s,'category':'US_STOCK','sub_category':'COMMON_STOCK','currency':'USD','status':'OC','exchange_code':'NSQ','lot_size':'1'} for s in symbols]}
            elif op==Operation.HISTORY_BATCH:body={'result':[{'symbol':s,'result':[{'time':NOW.isoformat(),'open':'10','high':'11','low':'9','close':'10','volume':'100000'}]} for s in symbols]}
            elif op==Operation.SNAPSHOT:
                snapshot_calls[0]+=1
                body=[{'symbol':s,'instrument_id':'id-'+s,'bid':'10','ask':'10.01','lot_size':'1',
                    'list_status':'LISTED','volume':'100000','quote_time':int((NOW-timedelta(seconds=3)
                        if snapshot_calls[0]==2 and s=='FOCUS' else NOW).timestamp()*1000)} for s in symbols]
            else:raise AssertionError('No broad discovery required')
            return SimpleNamespace(status_code=200,body=body,received_at=NOW.isoformat())
    source=SandboxSource.__new__(SandboxSource);source.credentials=SimpleNamespace(account_id='paper')
    source.readers={op:Reader() for op in Operation};source.discovery_stats={}
    alternatives=tuple('ALT'+chr(65+n) for n in range(8))
    u=source.refresh_comparative_focus({},'FOCUS',alternatives)
    assert len(u.eligible(NOW))==9 and u.select('FOCUS',NOW).ticker=='FOCUS'
    assert sum(op==Operation.INSTRUMENTS for op,_ in calls)==1
    assert sum(op==Operation.HISTORY_BATCH for op,_ in calls)==1
    assert sum(op==Operation.SNAPSHOT for op,_ in calls)==3  # Initial, stale focal, fresh focal.
    assert all(len(symbols)<=10 for _,symbols in calls)


def test_comparison_change_does_not_defeat_focal_call_dedup(setup_cycle,tmp_path):
    _,_,_,context,_,_=engine_fixture(setup_cycle,tmp_path)
    changed=context.model_copy(update={'comparative_opportunities':('{"instrument_id":"alt","symbol":"ALT"}',)})
    assert fast_context_signature(context)==fast_context_signature(changed)
