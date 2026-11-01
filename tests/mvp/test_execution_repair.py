"""Regressions from PMAX/APUS/STAK/GLND/SRZN/YDES on September 24."""
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
import json
import pytest
from test_cycle import NOW,setup_cycle
from test_entry_validity import prepare
from test_opportunity_pipeline import result,queue_fixture
from autonomous_trader.decisions import parse_alpha_decision,DecisionError
from autonomous_trader.mvp.alpha import compact_market_context
from autonomous_trader.monitoring.runner import deep_context_signature,hot_context_signature
from autonomous_trader.monitoring.store import compact_performance_memory

@pytest.mark.parametrize('symbol',['PMAX','APUS'])
def test_descriptive_overflow_keeps_action_and_authority(setup_cycle,tmp_path,symbol):
    e,store,broker,c,_,session,job,d=prepare(setup_cycle,tmp_path)
    d['reason']='description '*150;d['open']['thesis']='thesis '*200
    d['open']['strategy_label']='setup '*30
    parsed=parse_alpha_decision(json.dumps(d))
    assert parsed.open.desired_quantity==D(d['open']['desired_quantity'])
    assert parsed.open.entry_limit_usd==D(d['open']['entry_limit_usd'])
    e._candidate_result(job,c,result(d),session)
    assert len(broker.orders)==1
    raw={**d,'open':{**d['open'],'desired_quantity':True}}
    with pytest.raises(DecisionError):parse_alpha_decision(json.dumps(raw))

@pytest.mark.parametrize(('symbol','bid','limit','stop'),[
    ('SRZN','31.91','32.42','31.92'),('YDES','4.920','5.32','4.98')])
def test_actual_stop_geometry_returns_changed_evidence_to_astra(setup_cycle,tmp_path,symbol,bid,limit,stop):
    e,store,broker,c,_,session,job,d=prepare(setup_cycle,tmp_path)
    d['open'].update(entry_limit_usd=limit,hard_stop_usd=stop,target_usd=str(D(limit)+1))
    read=e.source.refresh_focus
    e.source.refresh_focus=lambda state,s:replace(read(state,s),candidates=tuple(
        replace(x,bid=D(bid),ask=D(limit)) for x in read(state,s).candidates))
    e._candidate_result(job,c,result(d),session)
    assert not broker.orders and job['status']=='PENDING' and job['reason']=='STOP_GEOMETRY_REASSESS'
    feedback=job['execution_feedback']
    assert feedback['bid']==bid and feedback['prior_open']['hard_stop_usd']==stop
    obs=e.source.refresh_focus(e.controller.state,job['symbol']).candidates[0]
    evidence=json.loads(compact_market_context(obs,e.opportunities.dossier(job,NOW)))
    assert evidence['execution_feedback']['reason']=='STOP_GEOMETRY_REASSESS'
    assert d['open']['hard_stop_usd']==stop


def test_glnd_genuinely_stale_native_observation_never_submits(setup_cycle,tmp_path):
    e,store,broker,c,_,session,job,d=prepare(setup_cycle,tmp_path)
    read=e.source.refresh_focus
    def stale(state,symbol):
        u=read(state,symbol)
        return replace(u,candidates=tuple(replace(x,quote_at=NOW-timedelta(seconds=7)) for x in u.candidates))
    e.source.refresh_focus=stale;e._candidate_result(job,c,result(d),session)
    assert not broker.orders and job['reason']=='STALE_MARKET_OR_ACCOUNT'


def test_model_billing_alone_does_not_trigger_another_deep_or_fast_call(setup_cycle,tmp_path):
    _,_,_,c,_,_,_,_=prepare(setup_cycle,tmp_path)
    a=c.model_copy(update={'recent_outcomes':(json.dumps({'memory_type':'portfolio_economics',
        'completed_trades':0,'all_known_api_model_cost_usd':'1','costed_model_calls':1}),)})
    b=c.model_copy(update={'recent_outcomes':(json.dumps({'memory_type':'portfolio_economics',
        'completed_trades':0,'all_known_api_model_cost_usd':'2','costed_model_calls':2}),)})
    assert deep_context_signature(a)==deep_context_signature(b)
    assert hot_context_signature(a)==hot_context_signature(b)
    changed=b.model_copy(update={'instruments':tuple(x.model_copy(update={'ask_usd':x.ask_usd*D('1.05')}) for x in b.instruments)})
    assert hot_context_signature(b)!=hot_context_signature(changed)


def test_hot_fingerprint_ignores_unrelated_ranking_churn_and_small_quote_noise(setup_cycle,tmp_path):
    _,_,_,context,_,_,_,_=prepare(setup_cycle,tmp_path)
    focal=context.instruments[0]
    context=context.model_copy(update={'focal_instrument_id':focal.instrument_id})
    other=json.dumps({'instrument_id':'unrelated','percent_per_minute':'8'})
    ranked=context.model_copy(update={'observed_top_runners':context.observed_top_runners+(other,)})
    noisy=ranked.model_copy(update={'instruments':tuple(
        x.model_copy(update={'bid_usd':x.bid_usd*D('1.001'),'ask_usd':x.ask_usd*D('1.001')})
        if x.instrument_id==focal.instrument_id else x for x in ranked.instruments)})
    assert hot_context_signature(context)==hot_context_signature(ranked)==hot_context_signature(noisy)
    runner=noisy.model_copy(update={'instruments':tuple(
        x.model_copy(update={'bid_usd':x.bid_usd*D('1.03'),'ask_usd':x.ask_usd*D('1.03')})
        if x.instrument_id==focal.instrument_id else x for x in noisy.instruments)})
    assert hot_context_signature(noisy)!=hot_context_signature(runner)


def test_infrastructure_loss_is_not_strategy_loss_but_is_real_economic_cost():
    base={'strategy_label':'same','market_regime':'same','evidence_quality':'FULL_RUNTIME',
        'realized_net_after_broker_fees_usd':'10','realized_net_after_all_known_costs_usd':'9',
        'return_on_capital':'.009','capital_deployed_usd':'1000','exit_reason':'ASTRA_CLOSE'}
    rows=[dict(base,plan_id='a',closed_at='1'),dict(base,plan_id='b',closed_at='2',
        exit_reason='SAFETY_LATCH',realized_net_after_broker_fees_usd='-100',realized_net_after_all_known_costs_usd='-101')]
    memory=list(map(json.loads,compact_performance_memory(rows)))
    assert memory[0]['realized_net_after_broker_fees_usd']=='-90'
    g=memory[1]['groups'][0]
    assert g['wins']==1 and g['losses']==0 and g['net_after_all_known_costs_usd']=='9'
    assert g['nonvalidating_net_usd']=='-101' and g['strategy_evidence_samples']==1
    assert g['sample_confidence']=='EXPLORATORY'


def test_missed_open_path_is_recorded_as_counterfactual_not_strategy_failure():
    q,store,_,clock=queue_fixture();job=next(iter(q.jobs.values()))
    from test_alpha_reset import observation
    first=observation(job['instrument_id'],job['symbol'],bid='10')
    q.record_observation(job,first,NOW)
    q.record_decision(job,'OPEN','Thesis',NOW,details={'decision_id':'missed','strategy':'exploratory'})
    job['dossier']['decisions'][-1]['execution_block']='ALPHA_MARKET_CONTEXT_TOO_LARGE'
    later=replace(first,bid=D('11'),ask=D('11.02'),quote_at=NOW+timedelta(seconds=10))
    q.record_observation(job,later,NOW+timedelta(seconds=10))
    outcomes=[p for k,p,_ in store.events if k=='OPPORTUNITY_OUTCOME']
    assert len(outcomes)==1 and outcomes[0]['failure_origin']=='INFRASTRUCTURE'
    assert outcomes[0]['realized_net_pnl_usd'] is None and not outcomes[0]['filled']
    q.record_observation(job,later,NOW+timedelta(seconds=11))
    assert len([k for k,_,_ in store.events if k=='OPPORTUNITY_OUTCOME'])==1


def test_quote_aging_during_admission_refreshes_and_uses_new_bound_permit(setup_cycle,tmp_path):
    e,store,broker,c,_,session,job,d=prepare(setup_cycle,tmp_path)
    current={'at':NOW};e.clock=lambda:current['at'];e.controller.clock=e.clock
    original_admit=e.controller.admit
    def admit(*args,**kwargs):
        nonce=original_admit(*args,**kwargs)
        current['at']+=timedelta(seconds=3)
        return nonce
    e.controller.admit=admit;read=e.source.refresh_focus
    def fresh(state,symbol):
        u=read(state,symbol)
        return replace(u,received_at=current['at'],candidates=tuple(replace(x,
            quote_at=current['at'],received_at=current['at'],quote_received_at=current['at']) for x in u.candidates))
    e.source.refresh_focus=fresh
    e._candidate_result(job,c,result(d),session)
    assert job['reason']=='BROKER_FILLED' and len(broker.orders)==1
    assert len(e.controller.state['permits'])==2
    assert sum(p['consumed'] for p in e.controller.state['permits'].values())==1
    assert job['dispatch_refresh_source_at']==current['at'].isoformat()


def test_quote_still_stale_after_bounded_dispatch_refresh_does_not_consume_or_submit(setup_cycle,tmp_path):
    e,store,broker,c,_,session,job,d=prepare(setup_cycle,tmp_path)
    current={'at':NOW};e.clock=lambda:current['at'];e.controller.clock=e.clock
    original=e.controller.admit
    def admit(*args,**kwargs):
        nonce=original(*args,**kwargs);current['at']+=timedelta(seconds=3);return nonce
    e.controller.admit=admit
    e._candidate_result(job,c,result(d),session)
    assert not broker.orders and job['reason']=='STALE_MARKET_OR_ACCOUNT'
    assert not e.controller.state['reservations']
    assert not any(p['consumed'] for p in e.controller.state['permits'].values())


def test_fast_duplicate_fingerprint_ignores_receipt_clocks_and_previous_call_text(setup_cycle,tmp_path):
    from autonomous_trader.monitoring.runner import fast_context_signature
    _,_,_,c,_,_,_,_=prepare(setup_cycle,tmp_path)
    def make(stamp):
        return c.model_copy(update={'instruments':tuple(x.model_copy(update={'market_context':json.dumps({
            'quote_at':stamp,'session_opportunity_dossier':{'prior_astra_decisions':[stamp]},
            'live_windows':{'1':{'change_bps':'5','source_end_at':stamp}}})}) for x in c.instruments)})
    assert fast_context_signature(make('before'))==fast_context_signature(make('after'))


def test_non_hot_abstain_requery_requires_material_opportunity_change(setup_cycle,tmp_path):
    from autonomous_trader.monitoring.runner import fast_context_signature
    _,_,_,context,_,_,_,_=prepare(setup_cycle,tmp_path)
    focal=context.instruments[0]
    context=context.model_copy(update={'focal_instrument_id':focal.instrument_id})
    signature=fast_context_signature(context)
    noise=context.model_copy(update={'instruments':tuple(x.model_copy(update={
        'bid_usd':x.bid_usd*D('1.001'),'ask_usd':x.ask_usd*D('1.001'),
        'bid_size':None if x.bid_size is None else x.bid_size*D('1.01'),
        'ask_size':None if x.ask_size is None else x.ask_size*D('1.01')}) if x.instrument_id==focal.instrument_id else x
        for x in context.instruments)})
    assert signature==fast_context_signature(noise)
    accelerated=noise.model_copy(update={'instruments':tuple(x.model_copy(update={
        'bid_usd':x.bid_usd*D('1.02'),'ask_usd':x.ask_usd*D('1.02')})
        if x.instrument_id==focal.instrument_id else x for x in noise.instruments)})
    assert signature!=fast_context_signature(accelerated)


def test_non_hot_fingerprint_uses_focal_rank_and_material_velocity_not_unrelated_churn(setup_cycle,tmp_path):
    from autonomous_trader.monitoring.runner import fast_context_signature
    _,_,_,context,_,_,_,_=prepare(setup_cycle,tmp_path)
    focal=context.instruments[0]
    runner={'instrument_id':focal.instrument_id,'percent_per_minute':'1.2','screen_at':NOW.isoformat()}
    other={'instrument_id':'other','percent_per_minute':'9.9','screen_at':NOW.isoformat()}
    base=context.model_copy(update={'focal_instrument_id':focal.instrument_id,
        'observed_top_runners':(json.dumps(runner),json.dumps(other))})
    unrelated=base.model_copy(update={'observed_top_runners':(
        json.dumps(runner),json.dumps({**other,'percent_per_minute':'12.5'}))})
    tiny=base.model_copy(update={'observed_top_runners':(
        json.dumps({**runner,'percent_per_minute':'1.4'}),json.dumps(other))})
    faster=base.model_copy(update={'observed_top_runners':(
        json.dumps({**runner,'percent_per_minute':'2.2'}),json.dumps(other))})
    reranked=base.model_copy(update={'observed_top_runners':(json.dumps(other),json.dumps(runner))})
    assert fast_context_signature(base)==fast_context_signature(unrelated)==fast_context_signature(tiny)
    assert fast_context_signature(base)!=fast_context_signature(faster)
    assert fast_context_signature(base)!=fast_context_signature(reranked)


@pytest.mark.parametrize('confirmed_fill',[True,False])
def test_known_entry_fill_race_is_re_read_before_held_reconciliation(tmp_path,monkeypatch,confirmed_fill):
    from types import SimpleNamespace
    from autonomous_trader.monitoring.runner import Engine
    from autonomous_trader.mvp.config import RuntimeConfig,reviewed_calendar
    from test_opportunity_pipeline import Store
    state={'positions':{'owned':{}},'orders':{'buy':{'final':False,'phase':'ACKNOWLEDGED',
        'intent':{'side':'BUY','instrument_id':'new'}}},'reconciliation':{'ready':True}}
    seen=[];calls=[]
    def service(*args):
        calls.append('detail')
        if len(calls)==1 or not confirmed_fill:return ()
        state['positions']['new']={};state['orders']['buy']['final']=True
        return ({'broker':{'status':'FILLED'}},)
    def held(current):
        seen.append(set(current['positions']))
        if 'new' not in current['positions']:raise ValueError('HELD_INSTRUMENT_BROKER_BINDING_MISMATCH')
        return 'fresh-broker-positions'
    e=Engine(tmp_path,RuntimeConfig(),Store(),clock=lambda:NOW)
    e._sync_stream_watch=lambda:None;e.update=lambda **kw:None
    e.controller=SimpleNamespace(state=state,lease=lambda:None)
    e.gateway=SimpleNamespace(service_working_entries=service)
    e.source=SimpleNamespace(refresh_held=held)
    e.alpha_manager=SimpleNamespace(tick=lambda *a:{'exited':'owned'})
    reconciled=[]
    monkeypatch.setattr('autonomous_trader.monitoring.runner.refresh_session',lambda *a,**kw:reconciled.append(a[1]))
    if confirmed_fill:
        e._alpha_step(NOW,reviewed_calendar().session(NOW.date()))
        assert seen==[{'owned'},{'owned','new'}] and reconciled==['fresh-broker-positions']
    else:
        with pytest.raises(ValueError,match='HELD_INSTRUMENT_BROKER_BINDING_MISMATCH'):
            e._alpha_step(NOW,reviewed_calendar().session(NOW.date()))
        assert not reconciled
