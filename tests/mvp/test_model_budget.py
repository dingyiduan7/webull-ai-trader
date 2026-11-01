"""The model-cost ceiling is shared, durable and cannot be spent on a failed claim."""
from copy import deepcopy
from datetime import datetime,timedelta
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
from threading import RLock
from uuid import uuid4
from zoneinfo import ZoneInfo
import json
import pytest
from psycopg import sql

from autonomous_trader.monitoring.model_budget import ModelBudget,month_bounds
from autonomous_trader.monitoring.store import Store as DatabaseStore,monthly_model_cost,costed_model_values,compact_performance_memory
from autonomous_trader.durable.ledger import LocalDatabase
from autonomous_trader.economic_policy import SandboxEconomics
from autonomous_trader.decision_api.astra_probe import AstraClient,PreflightError

DENVER=ZoneInfo('America/Denver')
NOW=datetime(2026,9,21,12,tzinfo=DENVER)


class Store:
    def __init__(self):self.rows=[];self.lock=RLock()
    def emit(self,kind,payload,*,mode='SANDBOX',at=None,event_id=None):
        with self.lock:self.rows.append((kind,deepcopy(payload),mode,at or NOW))
    def model_budget_evidence(self,start,end,*,modes=('SANDBOX','DRY_RUN','REAL')):
        with self.lock:
            relevant=[(k,p) for k,p,m,at in self.rows if m in modes and start<=at<end and k!='MODEL_WORK_RESULT']
            ids={p['id'] for k,p in relevant if k=='MODEL_WORK_CLAIM'}
            relevant.extend((k,p) for k,p,m,at in self.rows if m in modes and k=='MODEL_WORK_RESULT' and p['id'] in ids)
            return deepcopy(relevant)
    def model_budget_recent_claims(self,now,*,modes=('SANDBOX','DRY_RUN','REAL')):
        with self.lock:return [p for k,p,m,at in self.rows if k=='MODEL_WORK_CLAIM' and m in modes and now-timedelta(seconds=300)<at<=now]
    def reserve_model_claim(self,start,end,claim,allowance,*,recent_since,total_limit,
            non_position_limit,position_call_type,budget_modes=('SANDBOX','DRY_RUN','REAL'),portfolio_call_type=None,
            portfolio_session=None,portfolio_week=None,durable_position_call_type=None,
            position_session=None,position_week=None,all_calls_session=None,all_calls_week=None,
            daily_spend=None):
        with self.lock:
            now=datetime.fromisoformat(claim['at'])
            recent=[p for k,p,m,at in self.rows if k=='MODEL_WORK_CLAIM' and m in budget_modes
                and recent_since<at<=now]
            if (len(recent)>=total_limit or (claim['call_type']!=position_call_type
                    and sum(row.get('call_type')!=position_call_type for row in recent)>=non_position_limit)):
                raise ValueError('MODEL_CALL_BUDGET_WAIT')
            if daily_spend is not None:
                day_start,day_end,entry_limit,position_limit=daily_spend
                day=monthly_model_cost(self.model_budget_evidence(day_start,day_end,modes=budget_modes))
                limit=position_limit if claim['call_type']==position_call_type else entry_limit
                if day['known']+day['reserved']+Decimal(claim['reserved_usd'])>limit:
                    raise ValueError('MODEL_DAILY_SPEND_LIMIT')
            windows=()
            if claim['call_type']==portfolio_call_type:
                windows=((portfolio_session,'PORTFOLIO_SESSION_CALL_LIMIT','PORTFOLIO_CALL_HISTORY_UNAVAILABLE'),
                         (portfolio_week,'PORTFOLIO_WEEK_CALL_LIMIT','PORTFOLIO_CALL_HISTORY_UNAVAILABLE'))
            elif durable_position_call_type is not None and claim['call_type']==durable_position_call_type:
                windows=((position_session,'POSITION_SESSION_CALL_LIMIT','POSITION_CALL_HISTORY_UNAVAILABLE'),
                         (position_week,'POSITION_WEEK_CALL_LIMIT','POSITION_CALL_HISTORY_UNAVAILABLE'))
            for bounds,reason,unavailable in windows:
                    if bounds is None:raise ValueError(unavailable)
                    lower,upper,limit=bounds
                    count=sum(k=='MODEL_WORK_CLAIM' and m in budget_modes
                        and p.get('call_type')==claim['call_type'] and lower<=at<upper
                        for k,p,m,at in self.rows)
                    if count>=limit:raise ValueError(reason)
            for bounds,reason in ((all_calls_session,'REAL_SESSION_CALL_LIMIT'),
                                  (all_calls_week,'REAL_WEEK_CALL_LIMIT')):
                if bounds is not None:
                    lower,upper,limit=bounds
                    count=sum(k=='MODEL_WORK_CLAIM' and m in budget_modes and lower<=at<upper
                        for k,p,m,at in self.rows)
                    if count>=limit:raise ValueError(reason)
            state=monthly_model_cost(self.model_budget_evidence(start,end,modes=budget_modes))
            suspended=allowance is None
            if suspended and (claim.get('mode')!='REAL'
                    or claim.get('monthly_cost_control')!='OWNER_TEMPORARY_CAP_SUSPENSION_V1'
                    or claim.get('monthly_cost_suspension_id')!='owner-real-monthly-cap-suspension-2026-09-30'):
                raise ValueError('MODEL_MONTHLY_BUDGET_AUTHORITY_REQUIRED')
            if not suspended and (state['unbounded']
                    or state['known']+state['reserved']+Decimal(claim['reserved_usd'])>allowance):
                raise ValueError('MODEL_MONTHLY_BUDGET_EXHAUSTED')
            self.emit('MODEL_WORK_CLAIM',claim,mode=claim['mode'],at=datetime.fromisoformat(claim['at']))


def body(name='fast_alpha',model='gpt-6-astra',tokens=100):
    return {'model':model,'text':{'format':{'name':name}},'max_output_tokens':tokens,'input':'compact context'}


def legacy(store,cost,*,mode='SANDBOX',at=NOW,work_id=None):
    store.emit('ALPHA_DECISION',{'ai':{'estimated_cost_usd':cost,'model_work_id':work_id}},mode=mode,at=at)


def test_all_categories_unknown_cost_and_restart():
    s=Store();b=ModelBudget(s,'SANDBOX',lambda:NOW)
    for category in ('entry','deep','shadow','sandbox_economic_policy'):
        claim=b.claim(body(category));b.finish(claim,{'estimated_cost_usd':'0.1'})
    position=b.claim(body('position'),category='FAST_POSITION');b.finish(position,{'estimated_cost_usd':'0.1'})
    unknown=b.claim(body('failed'),category='FAST_POSITION');b.finish(unknown,{'estimated_cost_usd':None})
    health=b.health();assert health['unknown_cost_claims']==1
    assert Decimal(health['unresolved_reservations_usd'])>0
    restored=ModelBudget(s,'DRY_RUN',lambda:NOW)
    assert restored.health()['known_or_reserved_usd']==health['known_or_reserved_usd']
    assert restored.health()['calendar_month']=='2026-09'


def test_concurrent_modes_atomically_share_monthly_ceiling():
    s=Store();legacy(s,'124.90')
    brains=[ModelBudget(s,'SANDBOX' if i%2 else 'DRY_RUN',lambda:NOW) for i in range(12)]
    for b in brains:b.configure(SandboxEconomics(calls_per_300s=120))
    def attempt(b):
        try:return b.claim(body(),category='FAST_POSITION')
        except ValueError as exc:
            assert str(exc) in ('MODEL_MONTHLY_BUDGET_EXHAUSTED','MODEL_CALL_BUDGET_WAIT');return None
    with ThreadPoolExecutor(max_workers=12) as pool:results=list(pool.map(attempt,brains))
    assert any(results) and not all(results)
    assert Decimal(brains[0].health()['known_or_reserved_usd'])<=125
    assert len([k for k,p,m,at in s.rows if k=='MODEL_WORK_CLAIM'])==sum(bool(x) for x in results)


def test_policy_call_gets_no_extra_dollars_and_model_cannot_raise_cap():
    s=Store();legacy(s,'124.99');b=ModelBudget(s,'SANDBOX',lambda:NOW)
    with pytest.raises(ValueError,match='MODEL_MONTHLY_BUDGET_EXHAUSTED'):b.claim(body('sandbox_economic_policy'))
    assert not [k for k,p,m,at in s.rows if k=='MODEL_WORK_CLAIM']
    with pytest.raises(ValueError):SandboxEconomics(model_budget_usd='276')
    smaller=ModelBudget(Store(),'SANDBOX',lambda:NOW)
    smaller.configure(SandboxEconomics(model_budget_usd='0.001'))
    with pytest.raises(ValueError,match='MODEL_MONTHLY_BUDGET_EXHAUSTED'):smaller.claim(body('sandbox_economic_policy'))


def test_429_backpressure_applies_to_policy():
    time=[NOW];b=ModelBudget(Store(),'SANDBOX',lambda:time[0])
    c=b.claim(body());b.finish(c,{'error_code':'OPENAI_HTTP_429'})
    with pytest.raises(ValueError,match='MODEL_PROVIDER_BACKPRESSURE'):b.claim(body('sandbox_economic_policy'))
    time[0]+=timedelta(seconds=61)
    assert b.claim(body('sandbox_economic_policy'))


def test_legacy_cost_claim_result_copy_deduplicated_and_test_mode_excluded():
    s=Store();legacy(s,'1');legacy(s,'90',mode='WEEK2_synthetic')
    b=ModelBudget(s,'SANDBOX',lambda:NOW);c=b.claim(body(),category='FAST_POSITION')
    b.finish(c,{'model':'gpt-6-astra','estimated_cost_usd':'0.12','usage':{'input_tokens':100}})
    canonical=next(p for k,p,m,at in s.rows if k=='MODEL_WORK_RESULT')
    duplicate={'ai':{'model_work_id':c,'estimated_cost_usd':'0.12','decision':None}}
    s.emit('ALPHA_DECISION',duplicate)
    assert b.health()['known_cost_estimate_usd']=='1.12'
    assert len(costed_model_values([canonical,duplicate]))==1
    import json
    memory=json.loads(compact_performance_memory([],[canonical,duplicate])[0])
    assert memory['all_known_api_model_cost_usd']=='0.12'


def test_denver_rollover_and_late_result_belongs_to_claim_month():
    time=[datetime(2026,9,30,23,59,59,tzinfo=DENVER)];s=Store();b=ModelBudget(s,'DRY_RUN',lambda:time[0])
    legacy(s,'124.95',at=time[0]);claim=b.claim(body())
    time[0]=datetime(2026,10,1,0,0,1,tzinfo=DENVER)
    b.finish(claim,{'estimated_cost_usd':'0.04'})
    assert b.health()['known_cost_estimate_usd']=='0'
    assert b.health()['calendar_month']=='2026-10'
    time[0]=datetime(2026,9,30,23,59,59,tzinfo=DENVER)
    assert b.health()['known_cost_estimate_usd']=='124.99'
    assert month_bounds(datetime(2026,10,1,5,59,59,tzinfo=ZoneInfo('UTC')))[2]=='2026-09'


def test_unknown_legacy_cost_fails_closed_and_unbounded_request_denied():
    s=Store();legacy(s,None);b=ModelBudget(s,'SANDBOX',lambda:NOW)
    assert b.health()['exhausted'] and b.health()['remaining_available_usd']=='0'
    with pytest.raises(ValueError,match='MODEL_MONTHLY_BUDGET_EXHAUSTED'):b.claim(body())
    fresh=ModelBudget(Store(),'SANDBOX',lambda:NOW)
    with pytest.raises(ValueError,match='MODEL_COST_UNBOUNDED'):fresh.claim(body(model='unpriced-model'))
    with pytest.raises(ValueError,match='MODEL_COST_UNBOUNDED'):fresh.claim(body(tokens=0))


def test_real_http_dispatch_requires_budget_even_for_legacy_route():
    client=AstraClient('synthetic-test-key',session_factory=lambda:pytest.fail('network must not open'))
    with pytest.raises(PreflightError,match='MODEL_BUDGET_UNAVAILABLE'):
        client._request('POST','/responses',body())


def test_provider_dispatch_occurs_only_after_durable_claim():
    calls=[];store=Store();legacy(store,'124.99')
    class Session:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def request(self,*args,**kwargs):
            calls.append(args)
            assert any(k=='MODEL_WORK_CLAIM' for k,p,m,at in store.rows)
            class Response:
                status_code=200
                def __enter__(self):return self
                def __exit__(self,*args):pass
                def iter_content(self,n):
                    yield json.dumps({'model':'gpt-6-astra','status':'completed',
                        'usage':{'input_tokens':100,'output_tokens':50}}).encode()
            return Response()
    client=AstraClient('synthetic-test-key',session_factory=Session)
    client.budget=ModelBudget(store,'SANDBOX',lambda:NOW)
    client.last_call={'model_work_id':'earlier-call'}
    with pytest.raises(ValueError,match='MODEL_MONTHLY_BUDGET_EXHAUSTED'):
        client._request('POST','/responses',body())
    assert calls==[]
    assert client.last_call['response_status']=='NOT_DISPATCHED' and 'model_work_id' not in client.last_call
    store.rows.clear()
    result=client._request('POST','/responses',body())
    assert result['status']=='completed' and len(calls)==1
    assert Decimal(client.budget.health()['known_cost_estimate_usd'])==Decimal('0.0035')
    assert client.budget.health()['unresolved_reservations_usd']=='0'


def test_postgres_reservation_is_atomic_across_modes_and_process_objects():
    schema='budget_test_'+uuid4().hex[:12]
    admin=LocalDatabase('postgres','m2_admin',schema='public')
    with admin.connect() as c:
        c.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        c.execute(sql.SQL('CREATE TABLE {}.events (id text PRIMARY KEY, at timestamptz NOT NULL, kind text NOT NULL, mode text NOT NULL, payload jsonb NOT NULL)').format(sql.Identifier(schema)))
        c.execute(sql.SQL('GRANT USAGE ON SCHEMA {} TO mvp_observer').format(sql.Identifier(schema)))
        c.execute(sql.SQL('GRANT SELECT,INSERT ON {}.events TO mvp_observer').format(sql.Identifier(schema)))
    try:
        stores=[DatabaseStore() for _ in range(2)]
        for s in stores:s.db=LocalDatabase('postgres','mvp_observer',schema=schema)
        stores[0].emit('ALPHA_DECISION',{'ai':{'estimated_cost_usd':'124.90'}},mode='SANDBOX',at=NOW)
        budgets=[ModelBudget(stores[i%2],'DRY_RUN' if i%2 else 'SANDBOX',lambda:NOW) for i in range(12)]
        for b in budgets:b.configure(SandboxEconomics(calls_per_300s=120))
        def attempt(b):
            try:return b.claim(body(),category='FAST_POSITION')
            except ValueError as exc:
                assert str(exc) in ('MODEL_MONTHLY_BUDGET_EXHAUSTED','MODEL_CALL_BUDGET_WAIT');return None
        with ThreadPoolExecutor(max_workers=12) as pool:claims=list(pool.map(attempt,budgets))
        assert any(claims) and not all(claims)
        assert Decimal(budgets[0].health()['known_or_reserved_usd'])<=125
        assert budgets[1].health()['known_or_reserved_usd']==budgets[0].health()['known_or_reserved_usd']
        owner=next(i for i,c in enumerate(claims) if c)
        later=datetime(2026,10,1,12,tzinfo=DENVER)
        budgets[owner].clock=lambda:later
        budgets[owner].finish(claims[owner],{'estimated_cost_usd':'0.02'})
        stores[0].emit('ALPHA_DECISION',{'ai':{'model_work_id':claims[owner],
            'estimated_cost_usd':'0.02'}},mode='SANDBOX',at=later)
        assert ModelBudget(stores[0],'SANDBOX',lambda:later).health()['known_cost_estimate_usd']=='0'
        assert Decimal(ModelBudget(stores[0],'SANDBOX',lambda:NOW).health()['known_cost_estimate_usd'])==Decimal('124.92')
    finally:
        with admin.connect() as c:c.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


def test_owner_275_cap_retains_spend_and_rejects_overrun():
    s=Store();legacy(s,'124.99');b=ModelBudget(s,'SANDBOX',lambda:NOW)
    b.configure(SandboxEconomics(model_budget_usd='275'))
    assert b.health()['hard_cap_usd']=='275'
    assert b.health()['known_cost_estimate_usd']=='124.99'
    assert b.health()['remaining_available_usd']=='150.01'
    assert b.claim(body())
    s2=Store();legacy(s2,'274.99');b2=ModelBudget(s2,'SANDBOX',lambda:NOW)
    b2.configure(SandboxEconomics(model_budget_usd='275'))
    with pytest.raises(ValueError,match='MODEL_MONTHLY_BUDGET_EXHAUSTED'):b2.claim(body('sandbox_economic_policy'))


def test_temporary_25_sandbox_allowance_exists_only_during_september_28_session():
    times=[datetime(2026,9,28,7,29,59,tzinfo=DENVER)]
    s=Store();legacy(s,'275',at=datetime(2026,9,27,12,tzinfo=DENVER))
    b=ModelBudget(s,'SANDBOX',lambda:times[0]);b.configure(SandboxEconomics(model_budget_usd='275'))
    before=b.health()
    assert (before['hard_cap_usd'],before['budget_usd'],before['remaining_available_usd'])==('275','275','0')
    with pytest.raises(ValueError,match='MODEL_MONTHLY_BUDGET_EXHAUSTED'):b.claim(body())

    times[0]=datetime(2026,9,28,7,30,tzinfo=DENVER)
    during=b.health()
    assert (during['hard_cap_usd'],during['budget_usd'],during['remaining_available_usd'])==('300','300','25')
    assert during['temporary_session_bonus_usd']=='25'
    assert during['temporary_session_bonus_ends_at']=='2026-09-28T14:00:00-06:00'

    times[0]=datetime(2026,9,28,14,0,tzinfo=DENVER)
    after=b.health()
    assert (after['hard_cap_usd'],after['budget_usd'],after['remaining_available_usd'])==('275','275','0')
    assert after['temporary_session_bonus_usd']=='0' and after['temporary_session_bonus_ends_at'] is None
    with pytest.raises(ValueError,match='MODEL_MONTHLY_BUDGET_EXHAUSTED'):b.claim(body())
    times[0]=datetime(2026,9,29,12,tzinfo=DENVER)
    assert b.health()['budget_usd']=='275'


def test_temporary_allowance_counts_known_and_unresolved_cost_and_is_sandbox_only():
    session=datetime(2026,9,28,12,tzinfo=DENVER)
    times=[session]
    s=Store();legacy(s,'290',at=datetime(2026,9,28,8,tzinfo=DENVER))
    s.emit('MODEL_WORK_CLAIM',{'id':'unresolved','reserved_usd':'5'},mode='SANDBOX',at=session)
    sandbox=ModelBudget(s,'SANDBOX',lambda:times[0]);sandbox.configure(SandboxEconomics(model_budget_usd='275'))
    health=sandbox.health()
    assert health['known_cost_estimate_usd']=='290'
    assert health['unresolved_reservations_usd']=='5'
    assert health['known_or_reserved_usd']=='295' and health['remaining_available_usd']=='5'
    assert sandbox.claim(body())

    times[0]=datetime(2026,9,28,14,tzinfo=DENVER)
    assert sandbox.health()['exhausted'] and sandbox.health()['remaining_available_usd']=='0'
    with pytest.raises(ValueError,match='MODEL_MONTHLY_BUDGET_EXHAUSTED'):sandbox.claim(body())

    dry=ModelBudget(s,'DRY_RUN',lambda:session);dry.configure(SandboxEconomics(model_budget_usd='275'))
    assert dry.health()['hard_cap_usd']=='275' and dry.health()['exhausted']
    with pytest.raises(ValueError,match='MODEL_MONTHLY_BUDGET_EXHAUSTED'):dry.claim(body())


def test_temporary_allowance_does_not_override_a_lower_configured_policy_limit():
    session=datetime(2026,9,28,12,tzinfo=DENVER)
    s=Store();legacy(s,'125',at=datetime(2026,9,28,8,tzinfo=DENVER))
    b=ModelBudget(s,'SANDBOX',lambda:session);b.configure(SandboxEconomics(model_budget_usd='125'))
    assert b.health()['budget_usd']=='125'
    assert b.health()['temporary_session_bonus_usd']=='0'
    with pytest.raises(ValueError,match='MODEL_MONTHLY_BUDGET_EXHAUSTED'):b.claim(body())


def test_hard_rolling_limit_reserves_two_slots_for_unresolved_position_reviews():
    time=[NOW];s=Store();b=ModelBudget(s,'SANDBOX',lambda:time[0])
    for call_type in ('FAST_ENTRY','DEEP','SHADOW','ECONOMIC_POLICY'):
        assert b.claim(body(),category=call_type)
    with pytest.raises(ValueError,match='MODEL_CALL_BUDGET_WAIT'):
        b.claim(body(),category='FAST_ENTRY')
    assert b.claim(body(),category='FAST_POSITION')
    assert b.claim(body(),category='FAST_POSITION')
    with pytest.raises(ValueError,match='MODEL_CALL_BUDGET_WAIT'):
        b.claim(body(),category='FAST_POSITION')
    assert len([row for row in s.rows if row[0]=='MODEL_WORK_CLAIM'])==6

    # The window is (now-300s, now], so claims exactly 300 seconds old expire.
    time[0]=NOW+timedelta(seconds=300)
    assert b.claim(body(),category='FAST_ENTRY')


def test_lower_policy_limit_remains_binding_and_no_policy_or_missing_category_bypass():
    s=Store();b=ModelBudget(s,'SANDBOX',lambda:NOW)
    b.configure(SandboxEconomics(calls_per_300s=3))
    assert b.claim(body(),category='FAST_ENTRY')
    assert b.claim(body(),category='DEEP')
    assert b.claim(body(),category='FAST_POSITION')
    with pytest.raises(ValueError,match='MODEL_CALL_BUDGET_WAIT'):
        b.claim(body('sandbox_economic_policy'),category='ECONOMIC_POLICY')

    s2=Store();b2=ModelBudget(s2,'SANDBOX',lambda:NOW)
    for _ in range(4):assert b2.claim(body())
    with pytest.raises(ValueError,match='MODEL_CALL_BUDGET_WAIT'):
        b2.claim(body('sandbox_economic_policy'))
    assert b2.health()['calls_per_300s']==6
    assert b2.health()['non_position_calls_per_300s']==4


def test_parallel_model_budget_objects_cannot_bypass_rolling_limits():
    s=Store();brains=[ModelBudget(s,'SANDBOX' if i%2 else 'DRY_RUN',lambda:NOW) for i in range(12)]
    categories=['FAST_ENTRY']*8+['FAST_POSITION']*4
    def attempt(pair):
        brain,category=pair
        try:return brain.claim(body(),category=category)
        except ValueError as exc:
            assert str(exc)=='MODEL_CALL_BUDGET_WAIT';return None
    with ThreadPoolExecutor(max_workers=12) as pool:
        results=list(pool.map(attempt,zip(brains,categories)))
    claims=[p for k,p,m,at in s.rows if k=='MODEL_WORK_CLAIM']
    assert len([value for value in results if value])==6==len(claims)
    assert sum(row['call_type']!='FAST_POSITION' for row in claims)==4
    assert sum(row['call_type']=='FAST_POSITION' for row in claims)==2


def test_portfolio_entry_calls_are_durable_across_restart_and_session_rollover():
    time=[datetime(2026,9,21,10,tzinfo=DENVER)];store=Store()
    first=ModelBudget(store,'SANDBOX',lambda:time[0])
    assert first.claim(body(),category='PORTFOLIO_ENTRY')
    time[0]+=timedelta(minutes=6)
    assert ModelBudget(store,'SANDBOX',lambda:time[0]).claim(body(),category='PORTFOLIO_ENTRY')
    time[0]+=timedelta(minutes=6)
    with pytest.raises(ValueError,match='PORTFOLIO_SESSION_CALL_LIMIT'):
        ModelBudget(store,'SANDBOX',lambda:time[0]).claim(body(),category='PORTFOLIO_ENTRY')
    # Position management is independent of the exhausted entry-research quota.
    assert ModelBudget(store,'SANDBOX',lambda:time[0]).claim(body(),category='FAST_POSITION')
    time[0]=datetime(2026,9,22,10,tzinfo=DENVER)
    assert ModelBudget(store,'SANDBOX',lambda:time[0]).claim(body(),category='PORTFOLIO_ENTRY')


def test_portfolio_entry_week_limit_and_exchange_week_rollover():
    time=[datetime(2026,9,21,10,tzinfo=DENVER)];store=Store()
    for day in range(5):
        for hour in (10,11):
            time[0]=datetime(2026,9,21+day,hour,tzinfo=DENVER)
            assert ModelBudget(store,'SANDBOX',lambda:time[0]).claim(body(),category='PORTFOLIO_ENTRY')
    time[0]=datetime(2026,9,26,10,tzinfo=DENVER)
    with pytest.raises(ValueError,match='PORTFOLIO_WEEK_CALL_LIMIT'):
        ModelBudget(store,'SANDBOX',lambda:time[0]).claim(body(),category='PORTFOLIO_ENTRY')
    time[0]=datetime(2026,9,28,10,tzinfo=DENVER)
    assert ModelBudget(store,'SANDBOX',lambda:time[0]).claim(body(),category='PORTFOLIO_ENTRY')


def test_portfolio_call_history_unavailable_fails_closed():
    class Broken(Store):
        def reserve_model_claim(self,*args,**kwargs):raise RuntimeError('history unavailable')
    with pytest.raises(RuntimeError,match='history unavailable'):
        ModelBudget(Broken(),'SANDBOX',lambda:NOW).claim(body(),category='PORTFOLIO_ENTRY')


def test_parallel_portfolio_claims_cannot_exceed_session_limit():
    store=Store();budgets=[ModelBudget(store,'SANDBOX',lambda:NOW) for _ in range(8)]
    def attempt(budget):
        try:return budget.claim(body(),category='PORTFOLIO_ENTRY')
        except ValueError as exc:
            assert str(exc) in ('PORTFOLIO_SESSION_CALL_LIMIT','MODEL_CALL_BUDGET_WAIT');return None
    with ThreadPoolExecutor(max_workers=8) as pool:claims=list(pool.map(attempt,budgets))
    assert len([claim for claim in claims if claim])==2


def test_real_monthly_dollar_cap_is_suspended_but_cost_and_combined_calls_remain_bounded():
    # The $99 REAL spend is earlier in the month: the monthly cap is suspended,
    # and the separate daily spend ceiling only counts the current session.
    time=[NOW];store=Store();legacy(store,'200',mode='SANDBOX');legacy(store,'99',mode='REAL',at=NOW-timedelta(days=3))
    budget=ModelBudget(store,'REAL',lambda:time[0])
    health=budget.health()
    assert health['hard_cap_usd'] is None and health['budget_usd'] is None
    assert health['remaining_available_usd'] is None and not health['exhausted']
    assert health['monthly_cost_control']=='OWNER_TEMPORARY_CAP_SUSPENSION_V1'
    assert health['monthly_cost_suspension_id']=='owner-real-monthly-cap-suspension-2026-09-30'
    first=budget.claim(body(),category='PORTFOLIO_ENTRY')
    claim=next(p for k,p,m,at in store.rows if k=='MODEL_WORK_CLAIM' and p['id']==first)
    assert Decimal(claim['reserved_usd'])>0 and claim['monthly_cost_control']==health['monthly_cost_control']
    time[0]+=timedelta(minutes=6)
    assert ModelBudget(store,'REAL',lambda:time[0]).claim(body(),category='FAST_POSITION')
    time[0]+=timedelta(minutes=6)
    assert ModelBudget(store,'REAL',lambda:time[0]).claim(body(),category='FAST_POSITION')
    assert ModelBudget(store,'SANDBOX',lambda:NOW).health()['known_cost_estimate_usd']=='200'


def test_unknown_real_cost_is_visible_but_does_not_invent_dollar_ceiling():
    store=Store();legacy(store,None,mode='REAL')
    budget=ModelBudget(store,'REAL',lambda:NOW);health=budget.health()
    assert health['unknown_cost_claims']==1 and not health['exhausted']
    assert health['known_cost_estimate_usd']=='0' and health['hard_cap_usd'] is None
    assert budget.claim(body(),category='PORTFOLIO_ENTRY')


def test_none_allowance_without_exact_real_suspension_authority_is_rejected():
    store=Store();claim={'id':'unauthorized','at':NOW.isoformat(),'month':'2026-09','mode':'SANDBOX',
        'category':'fast_alpha','call_type':'PORTFOLIO_ENTRY','reserved_usd':'0.01','cost_usd':None}
    bounds=__import__('autonomous_trader.monitoring.model_budget',fromlist=['exchange_call_bounds']).exchange_call_bounds(NOW)
    with pytest.raises(ValueError,match='MODEL_MONTHLY_BUDGET_AUTHORITY_REQUIRED'):
        store.reserve_model_claim(*month_bounds(NOW)[:2],claim,None,recent_since=NOW-timedelta(seconds=300),
            total_limit=6,non_position_limit=4,position_call_type='FAST_POSITION',
            portfolio_call_type='PORTFOLIO_ENTRY',portfolio_session=(*bounds[:2],2),
            portfolio_week=(*bounds[2:],10))


def test_real_position_decisions_have_no_session_count_cap_across_restart():
    time=[datetime(2026,9,21,10,tzinfo=DENVER)];store=Store()
    assert ModelBudget(store,'REAL',lambda:time[0]).claim(body(),category='FAST_POSITION')
    time[0]+=timedelta(minutes=6)
    assert ModelBudget(store,'REAL',lambda:time[0]).claim(body(),category='FAST_POSITION')
    time[0]+=timedelta(minutes=6)
    assert ModelBudget(store,'REAL',lambda:time[0]).claim(body(),category='FAST_POSITION')
    time[0]=datetime(2026,9,22,10,tzinfo=DENVER)
    assert ModelBudget(store,'REAL',lambda:time[0]).claim(body(),category='FAST_POSITION')


def test_real_position_decisions_have_no_week_cap_but_atomic_rolling_limit():
    time=[datetime(2026,9,21,10,tzinfo=DENVER)];store=Store()
    for day in range(5):
        for hour in (10,11):
            time[0]=datetime(2026,9,21+day,hour,tzinfo=DENVER)
            assert ModelBudget(store,'REAL',lambda:time[0]).claim(body(),category='FAST_POSITION')
    time[0]=datetime(2026,9,26,10,tzinfo=DENVER)
    assert ModelBudget(store,'REAL',lambda:time[0]).claim(body(),category='FAST_POSITION')

    parallel=Store();budgets=[ModelBudget(parallel,'REAL',lambda:NOW) for _ in range(8)]
    def attempt(budget):
        try:return budget.claim(body(),category='FAST_POSITION')
        except ValueError as exc:
            assert str(exc) in ('POSITION_SESSION_CALL_LIMIT','MODEL_CALL_BUDGET_WAIT');return None
    with ThreadPoolExecutor(max_workers=8) as pool:claims=list(pool.map(attempt,budgets))
    assert len([claim for claim in claims if claim])==6


def test_postgres_real_mixed_claims_retain_atomic_rolling_limit():
    schema='budget_real_'+uuid4().hex[:12]
    admin=LocalDatabase('postgres','m2_admin',schema='public')
    with admin.connect() as c:
        c.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        c.execute(sql.SQL('CREATE TABLE {}.events (id text PRIMARY KEY, at timestamptz NOT NULL, kind text NOT NULL, mode text NOT NULL, payload jsonb NOT NULL)').format(sql.Identifier(schema)))
        c.execute(sql.SQL('GRANT USAGE ON SCHEMA {} TO mvp_observer').format(sql.Identifier(schema)))
        c.execute(sql.SQL('GRANT SELECT,INSERT ON {}.events TO mvp_observer').format(sql.Identifier(schema)))
    try:
        stores=[DatabaseStore() for _ in range(2)]
        for store in stores:store.db=LocalDatabase('postgres','mvp_observer',schema=schema)
        budgets=[ModelBudget(stores[index%2],'REAL',lambda:NOW) for index in range(8)]
        work=[(budget,'PORTFOLIO_ENTRY' if index%2 else 'FAST_POSITION') for index,budget in enumerate(budgets)]
        def attempt(item):
            budget,category=item
            try:return budget.claim(body(),category=category)
            except ValueError as exc:
                assert str(exc) in ('POSITION_SESSION_CALL_LIMIT','PORTFOLIO_SESSION_CALL_LIMIT',
                    'REAL_SESSION_CALL_LIMIT','MODEL_CALL_BUDGET_WAIT');return None
        with ThreadPoolExecutor(max_workers=8) as pool:claims=list(pool.map(attempt,work))
        assert len([claim for claim in claims if claim])==6
        with admin.connect() as c:
            assert c.execute(sql.SQL("SELECT count(*) FROM {}.events WHERE mode='REAL' AND kind='MODEL_WORK_CLAIM'").format(sql.Identifier(schema))).fetchone()[0]==6
    finally:
        with admin.connect() as c:c.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


def test_postgres_real_suspension_retains_atomic_combined_call_limits_and_cost_rows():
    schema='real_budget_test_'+uuid4().hex[:12]
    admin=LocalDatabase('postgres','m2_admin',schema='public')
    with admin.connect() as c:
        c.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        c.execute(sql.SQL('CREATE TABLE {}.events (id text PRIMARY KEY, at timestamptz NOT NULL, kind text NOT NULL, mode text NOT NULL, payload jsonb NOT NULL)').format(sql.Identifier(schema)))
        c.execute(sql.SQL('GRANT USAGE ON SCHEMA {} TO mvp_observer').format(sql.Identifier(schema)))
        c.execute(sql.SQL('GRANT SELECT,INSERT ON {}.events TO mvp_observer').format(sql.Identifier(schema)))
    try:
        stores=[DatabaseStore() for _ in range(2)]
        for store in stores:store.db=LocalDatabase('postgres','mvp_observer',schema=schema)
        stores[0].emit('ALPHA_DECISION',{'ai':{'estimated_cost_usd':'99'}},mode='REAL',at=NOW-timedelta(days=3))
        budgets=[ModelBudget(stores[i%2],'REAL',lambda:NOW) for i in range(8)]
        def attempt(budget):
            try:return budget.claim(body(),category='PORTFOLIO_ENTRY')
            except ValueError as exc:
                assert str(exc) in ('PORTFOLIO_SESSION_CALL_LIMIT','REAL_SESSION_CALL_LIMIT','MODEL_CALL_BUDGET_WAIT');return None
        with ThreadPoolExecutor(max_workers=8) as pool:claims=list(pool.map(attempt,budgets))
        accepted=[claim for claim in claims if claim]
        assert len(accepted)==4
        health=budgets[0].health()
        assert health['known_cost_estimate_usd']=='99' and health['hard_cap_usd'] is None
        with stores[0].db.connect() as c:
            rows=[row[0] for row in c.execute("SELECT payload FROM events WHERE kind='MODEL_WORK_CLAIM' ORDER BY id").fetchall()]
        assert len(rows)==4 and all(Decimal(row['reserved_usd'])>0 for row in rows)
        assert all(row['monthly_cost_control']=='OWNER_TEMPORARY_CAP_SUSPENSION_V1' for row in rows)
    finally:
        with admin.connect() as c:c.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


def test_real_trade_counts_do_not_cap_combined_decisions():
    time=[datetime(2026,9,21,10,tzinfo=DENVER)];store=Store()
    assert ModelBudget(store,'REAL',lambda:time[0]).claim(body(),category='PORTFOLIO_ENTRY')
    time[0]+=timedelta(minutes=6)
    assert ModelBudget(store,'REAL',lambda:time[0]).claim(body(),category='FAST_POSITION')
    time[0]+=timedelta(minutes=6)
    assert ModelBudget(store,'REAL',lambda:time[0]).claim(body(),category='PORTFOLIO_ENTRY')

    week=Store()
    for day in range(5):
        for hour,category in ((10,'PORTFOLIO_ENTRY'),(11,'FAST_POSITION')):
            time[0]=datetime(2026,9,21+day,hour,tzinfo=DENVER)
            assert ModelBudget(week,'REAL',lambda:time[0]).claim(body(),category=category)
    time[0]=datetime(2026,9,26,10,tzinfo=DENVER)
    assert ModelBudget(week,'REAL',lambda:time[0]).claim(body(),category='PORTFOLIO_ENTRY')


def test_real_daily_spend_ceiling_stops_entry_calls_but_keeps_position_reserve():
    from autonomous_trader.monitoring.model_budget import REAL_DAILY_MODEL_SPEND_USD,POSITION_RESERVE_MULTIPLIER
    store=Store();legacy(store,str(REAL_DAILY_MODEL_SPEND_USD-Decimal('0.0001')),mode='REAL')
    budget=ModelBudget(store,'REAL',lambda:NOW)
    with pytest.raises(ValueError,match='MODEL_DAILY_SPEND_LIMIT'):
        budget.claim(body(),category='PORTFOLIO_ENTRY')
    # Position management keeps a reserve so open positions can still be managed.
    assert budget.claim(body(),category='FAST_POSITION')
    assert budget.health()['daily_spend_ceiling_usd']==str(REAL_DAILY_MODEL_SPEND_USD)
    legacy(store,str(REAL_DAILY_MODEL_SPEND_USD*POSITION_RESERVE_MULTIPLIER),mode='REAL')
    with pytest.raises(ValueError,match='MODEL_DAILY_SPEND_LIMIT'):
        ModelBudget(store,'REAL',lambda:NOW+timedelta(minutes=6)).claim(body(),category='FAST_POSITION')


def test_real_daily_spend_ceiling_resets_next_session():
    store=Store();legacy(store,'50',mode='REAL',at=NOW-timedelta(days=1))
    assert ModelBudget(store,'REAL',lambda:NOW).claim(body(),category='PORTFOLIO_ENTRY')


def test_claude_models_are_priced_and_unknown_models_are_refused():
    store=Store();budget=ModelBudget(store,'REAL',lambda:NOW)
    first=budget.claim(body(model='claude-sonnet-5-5',tokens=8000),category='PORTFOLIO_ENTRY')
    claim=next(p for k,p,m,at in store.rows if k=='MODEL_WORK_CLAIM' and p['id']==first)
    assert Decimal('0')<Decimal(claim['reserved_usd'])<Decimal('0.2')
    with pytest.raises(ValueError,match='MODEL_COST_UNBOUNDED'):
        budget.claim(body(model='claude-unknown-9'),category='PORTFOLIO_ENTRY')


@pytest.mark.parametrize('code',['ANTHROPIC_HTTP_429','ANTHROPIC_HTTP_529','OPENAI_HTTP_429'])
def test_provider_backpressure_cools_down(code):
    store=Store();budget=ModelBudget(store,'REAL',lambda:NOW)
    claim=budget.claim(body(),category='PORTFOLIO_ENTRY')
    budget.finish(claim,{'error_code':code,'estimated_cost_usd':None})
    with pytest.raises(ValueError,match='MODEL_PROVIDER_BACKPRESSURE'):
        budget.claim(body(),category='FAST_POSITION')
