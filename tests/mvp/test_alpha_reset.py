from datetime import datetime,timedelta,timezone
from decimal import Decimal as D
from types import SimpleNamespace
from dataclasses import replace
import json
import pytest

from test_cycle import NOW,setup_cycle
from autonomous_trader.contracts import AlphaDecision,AlphaManagedPlan,Instrument
from autonomous_trader.decision_api.astra_probe import (AstraClient,MODEL,DEEP_ALPHA_PROMPT,FAST_ENTRY_PROMPT,
    FAST_POSITION_PROMPT,fast_alpha_request)
from autonomous_trader.decisions import parse_alpha_decision,validate_alpha_semantics
from autonomous_trader.monitoring.runner import (Engine,deep_plan_due,deep_context_signature,
    fast_context_signature,shadow_snapshot,evaluate_shadow)
from autonomous_trader.mvp.alpha import (AlphaPortfolioManager,baseline_snapshot,build_alpha_context,
    evaluate_baseline,translate_open)
from autonomous_trader.mvp.config import RuntimeConfig,ExecutionMode
from autonomous_trader.mvp.context import Observation,UniverseObservation,SandboxSource,MAX_CANDIDATES
from autonomous_trader.sandbox.http import Operation,TOP_ACTIVE_RANK_TYPES
from autonomous_trader.mvp.gateway import LocalPositionManager
from autonomous_trader.durable.models import Protection


def observation(iid,symbol,bid='99.99',*,positions=None):
    instrument=Instrument(instrument_id=iid,ticker=symbol,country='US',asset_type='STOCK',listing_exchange='TEST',
        liquid=True,tradable=True,quantity_increment='1',price_increment_usd='0.01',minimum_quantity='1')
    bars=tuple({'time':(NOW-timedelta(minutes=n)).isoformat(),'open':str(D(bid)-D('.02')*n),
        'high':str(D(bid)+D('.05')),'low':str(D(bid)-D('.05')),'close':str(D(bid)-D('.01')*n),'volume':'100000'} for n in range(5))
    return Observation(iid,D(bid),D(bid)+D('.01'),NOW,NOW,D('1000000'),D(0),D('1000000'),positions or {},(),iid+'-evidence',
        bars,True,NOW,symbol,instrument,D('2000000'),True)


def universe(items,positions=None):
    return UniverseObservation(tuple(items),NOW,D('1000000'),D(0),D('1000000'),positions or {},(), 'universe-evidence')


def open_raw(now,iid='stock-b',symbol='BBB',quantity='1'):
    return {'schema_version':'1.0','action':'OPEN','reason':'Current evidence supports a bounded intraday entry.',
        'open':{'instrument_id':iid,'symbol':symbol,'strategy_label':'relative-strength continuation',
            'thesis':'Strength persists across the supplied horizons.','desired_notional_usd':None,'desired_quantity':quantity,
            'entry_limit_usd':'100','entry_expires_at':(now+timedelta(minutes=2)).isoformat(),'hard_stop_usd':'95',
            'latest_exit_at':now.replace(hour=19,minute=45).isoformat(),'target_usd':None,
            'next_review_at':(now+timedelta(minutes=17)).isoformat()},'hold':None,'modify':None,'close':None}


def api_response(value):
    return {'model':MODEL,'status':'completed','usage':{'input_tokens':100,'output_tokens':20},
        'output':[{'type':'message','content':[{'type':'output_text','text':json.dumps(value)}]}]}


def test_deep_and_fast_calls_use_compact_boundary_without_execution(monkeypatch,setup_cycle):
    args,_,_=setup_cycle;u=universe((observation('stock-b','BBB'),observation('stock-c','CCC')))
    memory=json.dumps({'memory_type':'recent_completed_trade_evidence','trades':[{'symbol':'INTC',
        'realized_net_after_all_known_costs_usd':'-25.32','maximum_favorable_excursion_usd':'27',
        'maximum_adverse_excursion_usd':'60','exit_reason':'ASTRA_CLOSE'}]})
    runner=json.dumps({'symbol':'BBB','instrument_id':'stock-b','percent_per_minute':'1.2',
        'window_minutes':3,'screen_age_seconds':5,'quote_age_seconds':1,'authority':'DISCOVERY_ONLY'})
    args['controller'].refresh(u);context=build_alpha_context(u,args['controller'].state,NOW,
        recent_outcomes=(memory,),observed_top_runners=(runner,))
    responses=iter([api_response({'regime_label':'SELECTIVE_RISK_ON','regime_view':'Selective risk-on tape.','focus':[{'instrument_id':'stock-b','symbol':'BBB','rank':1,
        'strategy_label':'relative strength','thesis':'Leading current candidates.','horizon':'intraday'}]}),
        api_response({'decision':{**open_raw(NOW),'defer_to':None}})])
    client=AstraClient('synthetic-key');seen=[]
    def request(*call):seen.append(call);return next(responses)
    monkeypatch.setattr(client,'_request',request)
    plan=client.deep_plan(context,{'maximum_positions':5});result=client.fast_decide(context,mode='ENTRY',hard_policy={},clock=lambda:NOW)
    assert plan['plan']['focus'][0]['symbol']=='BBB' and result['decision']['action']=='OPEN'
    assert all(call[0]=='POST' and call[1]=='/responses' for call in seen)
    assert all('tools' not in call[2] for call in seen)
    assert all('recent_completed_trade_evidence' in json.dumps(call[2]) and 'percent_per_minute' in json.dumps(call[2])
        for call in seen)


@pytest.mark.parametrize('mode',['DEEP','ENTRY','SHADOW'])
def test_incomplete_call_retains_sanitized_cost_without_decision_authority(monkeypatch,setup_cycle,mode):
    from autonomous_trader.monitoring.runner import ObservedBrain
    args,_,_=setup_cycle;u=universe((observation('stock-b','BBB'),))
    args['controller'].refresh(u);context=build_alpha_context(u,args['controller'].state,NOW)
    client=AstraClient('synthetic-secret');events=[]
    response={'model':MODEL,'status':'incomplete','incomplete_details':{'reason':'max_output_tokens'},
        'usage':{'input_tokens':100,'output_tokens':2048,'input_tokens_details':{'cached_tokens':0,'untrusted':'synthetic-secret'},
            'output_tokens_details':{'reasoning_tokens':1900},'untrusted':'synthetic-secret'},
        'output':[{'type':'message','content':[{'type':'output_text','text':'synthetic-secret'}]}]}
    monkeypatch.setattr(client,'_request',lambda *args:response)
    brain=ObservedBrain(client,SimpleNamespace(clock=lambda:NOW,update=lambda **kw:None,
        emit=lambda k,p:events.append((k,p))))
    call=(lambda:brain.deep_plan(context)) if mode=='DEEP' else ((lambda:brain.shadow_audit(context)) if mode=='SHADOW' else (lambda:brain.fast_decide(context,'ENTRY')))
    with pytest.raises(ValueError):call()
    assert len(events)==1 and events[0][0]=='MODEL_CALL_FAILED'
    value=events[0][1]['ai']
    assert value['response_status']=='incomplete' and value['incomplete_reason']=='max_output_tokens'
    assert value['estimated_cost_usd']=='0.1034' and value['usage']['output_tokens']==2048
    assert 'decision' not in value and 'synthetic-secret' not in json.dumps(events)


def test_broad_context_feedback_flexible_horizon_and_baseline(setup_cycle):
    args,_,_=setup_cycle;items=tuple(observation('stock-'+str(n),'S'+str(n)) for n in range(12))
    u=universe(items);args['controller'].refresh(u)
    context=build_alpha_context(u,args['controller'].state,NOW,recent_outcomes=('strategy=momentum; net=-10',))
    assert len(context.instruments)==12 and context.recent_outcomes
    d=parse_alpha_decision(json.dumps(open_raw(NOW,iid='stock-1',symbol='S1')))
    assert validate_alpha_semantics(d,context,NOW).open.next_review_at-NOW==timedelta(minutes=17)
    snapshot=baseline_snapshot(context);later=context.model_copy(update={'as_of':NOW+timedelta(minutes=5),'expires_at':NOW+timedelta(minutes=6)})
    result=evaluate_baseline(snapshot,later)
    assert result and len(result['benchmarks'])==5 and all('gross_return_bps' in x for x in result['benchmarks'])
    assert all(x['entry_reference']=='ASK' and x['entry_usd']=='100.00' for x in snapshot['benchmarks'])


def test_session_dossier_reaches_deep_and_fast_context_without_trade_authority(setup_cycle):
    args,_,_=setup_cycle;u=universe((observation('stock-b','BBB'),))
    args['controller'].refresh(u)
    dossier={'scope':'SESSION_OPPORTUNITY_EVIDENCE_NO_EXECUTION_AUTHORITY','symbol':'BBB',
        'first_detected_at':(NOW-timedelta(seconds=4)).isoformat(),
        'latest_screen_evidence':{'source':'TOP_GAINERS','screen_at':NOW.isoformat(),'rank':2,
            'percent_per_minute':'3.4','relative_volume_10d':'6.1'},
        'quote_sequence':[{'at':NOW.isoformat(),'bid':'99.99','ask':'100.00'}],
        'prior_astra_decisions':[{'action':'ABSTAIN','best_executable_return_bps':'250'}]}
    context=build_alpha_context(u,args['controller'].state,NOW,opportunity_dossiers={'stock-b':dossier})
    market=json.loads(context.instruments[0].market_context)
    assert market['session_opportunity_dossier']==dossier
    assert market['detection_evidence']['percent_per_minute']=='3.4'
    assert market['detection_evidence']['first_detected_at']==(NOW-timedelta(seconds=4)).isoformat()
    assert 'order' not in json.dumps(dossier).lower() and context.focal_instrument_id is None


def test_live_sized_opportunity_dossier_is_compacted_at_model_boundary(setup_cycle):
    args,_,_=setup_cycle;item=observation('stock-b','MSFT');u=universe((item,))
    args['controller'].refresh(u)
    quote={'at':NOW.isoformat(),'quote_at':NOW.isoformat(),'bid':'100.00','ask':'100.01',
        'bid_size':'100','ask_size':'100','volume':'2000000','relative_volume_10d':'3.1',
        'short_window_change_ratio':'0.08','short_window_minutes':3,'bar_first_at':NOW.isoformat(),
        'bar_last_at':NOW.isoformat(),'bar_return_bps':'800','regime_label':'CONTINUATION'}
    decision={'at':NOW.isoformat(),'action':'ABSTAIN','reason':'evidence '*180,'ask':'100.01',
        'quote_at':NOW.isoformat(),'later_observations':4,'best_executable_return_bps':'900',
        'worst_executable_return_bps':'-100'}
    dossier={'scope':'SESSION_OPPORTUNITY_EVIDENCE_NO_EXECUTION_AUTHORITY','symbol':'MSFT',
        'instrument_id':'stock-b','as_of':NOW.isoformat(),'attempts':8,'current_status':'RESULT_READY',
        'current_reason':'MODEL_COMPLETED','quote_sequence':[quote]*6,'prior_astra_decisions':[decision]*4}
    context=build_alpha_context(u,args['controller'].state,NOW,opportunity_dossiers={'stock-b':dossier})
    value=context.instruments[0].market_context;parsed=json.loads(value)
    assert len(value)<=1900
    compact=parsed['session_opportunity_dossier']
    assert compact['instrument_id']=='stock-b' and compact['quote_sequence'][-1]['ask']=='100.01'
    assert len(compact['quote_sequence'])<6 and len(compact['prior_astra_decisions'])<4


def test_post_model_validation_does_not_drop_open_for_verbose_live_window(setup_cycle):
    args,_,_=setup_cycle
    item=replace(observation('stock-b','MSFT'),live_windows={'ticks':'x'*3000})
    u=universe((item,))
    args['controller'].refresh(u)
    compact=build_alpha_context(u,args['controller'].state,NOW)
    assert len(compact.instruments[0].market_context)<=1900
    fresh=build_alpha_context(u,args['controller'].state,NOW,include_market_context=False)
    assert fresh.instruments[0].market_context is None
    assert fresh.instruments[0].quote_at==item.quote_at
    assert fresh.reconciliation_ready and fresh.settlement_known


def test_full_stream_windows_fit_model_context_without_discarding_candidate(setup_cycle):
    args,_,_=setup_cycle
    row={'event_count':100,'change_bps':'150.123','tick_volume':'9500','first_price':'12.34',
        'last_price':'12.90','high_price':'13.00','low_price':'12.30','source_end_at':NOW.isoformat(),
        'source_start_at':(NOW-timedelta(seconds=60)).isoformat(),'bid':'12.90','ask':'12.91',
        'bid_size':'100000','ask_size':'100000'}
    item=replace(observation('stock-b','BBB'),live_windows={str(n):row for n in (1,5,10,60)},
        stream_material_signature=(('quote',345,346,3,48,51),(1,12,150),(5,15,400),(10,16,500),(60,70,1500)))
    u=universe((item,));args['controller'].refresh(u)
    value=build_alpha_context(u,args['controller'].state,NOW).instruments[0].market_context
    assert len(value)<=1900 and set(json.loads(value)['live_windows'])=={'1','5','10','60'}


def test_entry_prompt_prohibits_fabricated_ev_and_arbitrary_large_size():
    text=FAST_ENTRY_PROMPT+DEEP_ALPHA_PROMPT
    for term in ('never let invented scenario weights','minimum executable exploratory size',
            'not proof of total executable liquidity','prior ABSTAIN grants no inertia',
            'concrete, falsifiable confirmation'):
        assert term in text
    assert open_raw(NOW,quantity='1')['open']['desired_quantity']=='1'


def test_runner_age_alone_does_not_spend_another_fast_model_call(setup_cycle):
    args,_,_=setup_cycle;u=universe((observation('stock-b','BBB'),))
    args['controller'].refresh(u)
    runner={'symbol':'BBB','instrument_id':'stock-b','percent_per_minute':'1.2',
        'screen_at':NOW.isoformat(),'quote_at':NOW.isoformat(),'screen_age_seconds':5,
        'quote_age_seconds':1,'current':True}
    first=build_alpha_context(u,args['controller'].state,NOW,observed_top_runners=(json.dumps(runner),))
    runner['screen_age_seconds']=6;runner['quote_age_seconds']=2
    second=first.model_copy(update={'observed_top_runners':(json.dumps(runner),)})
    assert fast_context_signature(first)==fast_context_signature(second)
    runner['percent_per_minute']='2.2'
    assert fast_context_signature(first)!=fast_context_signature(first.model_copy(
        update={'observed_top_runners':(json.dumps(runner),)}))


def test_alpha_objective_rewards_net_profit_and_meaningful_deployment():
    combined=(DEEP_ALPHA_PROMPT+' '+FAST_ENTRY_PROMPT+' '+FAST_POSITION_PROMPT).lower()
    assert 'maximize long-run realized net profit' in combined
    assert 'api/model cost' in combined and 'sample confidence' in combined
    assert 'abstain has no intrinsic reward' in combined
    assert 'hard constraint' in combined
    assert 'trade count has no reward' in combined and 'must not force trades' in combined
    assert 'create, select, refine, combine, reuse, or retire strategies' in combined
    assert 'at least 10% realized net return per trading day' in combined
    assert 'guarantee' in combined
    assert 'ev = sum(probability_i * net_payoff_i)' in combined
    assert all(term in combined for term in ('broker fees','spread','slippage','api/model cost','opportunity cost','tail scenarios'))
    assert 'no setup, strategy family, indicator, target, holding period, or trade cadence is' in combined
    assert 'strategy labels are descriptive metadata, never admission rules' in combined
    assert 'aal overnight safety-recovery profit is not' in combined
    assert 'single-instrument, long-only' in combined and 'atomic pairs' in combined and 'multi-leg' in combined
    assert 'as optional inspiration' in DEEP_ALPHA_PROMPT.lower()
    assert all(term in DEEP_ALPHA_PROMPT.lower() for term in ('co-movement','divergence or convergence',
        'market-neutral','sufficient independent samples','regime stability','transaction-cost viability',
        'borrow and short constraints','timing mismatch','correlation breakdown','multiple-testing',
        'data-snooping','false discovery'))
    assert 'rather than a required pairs/stat-arbitrage algorithm' in DEEP_ALPHA_PROMPT.lower()
    assert 'unexecutable pair as research only' in DEEP_ALPHA_PROMPT.lower()
    assert 'must never output it as an executable action' in DEEP_ALPHA_PROMPT.lower()
    assert 'ticker or company familiarity and market-cap prestige are irrelevant' in DEEP_ALPHA_PROMPT.lower()
    assert 'large, mid-sized, small, and lesser-known eligible' in DEEP_ALPHA_PROMPT.lower()
    assert 'discovery surface and cheap rank' in DEEP_ALPHA_PROMPT.lower()
    assert all(term in DEEP_ALPHA_PROMPT.lower() for term in ('measurable dislocation',
        'volatility relative to spread and liquidity','probability-weighted payoff after all costs',
        'data quality','usable capacity'))
    for prompt in (DEEP_ALPHA_PROMPT,FAST_ENTRY_PROMPT,FAST_POSITION_PROMPT):
        guidance=prompt.lower()
        assert 'plausible' in guidance and 'realized net dollars' in guidance
        assert 'credible loss' in guidance and 'tail outcomes' in guidance
        assert 'capital' in guidance and 'time' in guidance
        assert 'alternative' in guidance
    assert 'tiny positive scalp' in FAST_ENTRY_PROMPT.lower()
    assert 'a modest early' in FAST_POSITION_PROMPT.lower()
    assert 'never hold merely' in FAST_POSITION_PROMPT.lower()


def test_deep_cost_control_ignores_clock_only_changes(setup_cycle):
    args,_,_=setup_cycle;u=universe((observation('stock-b','BBB'),observation('stock-c','CCC')))
    args['controller'].refresh(u);context=build_alpha_context(u,args['controller'].state,NOW,recent_outcomes=('memory',))
    later=context.model_copy(update={'as_of':NOW+timedelta(minutes=30),'expires_at':NOW+timedelta(minutes=31)})
    assert deep_context_signature(context)==deep_context_signature(later)


def test_fast_cost_control_requires_market_or_account_change(setup_cycle):
    args,_,_=setup_cycle;u=universe((observation('stock-b','BBB'),))
    args['controller'].refresh(u);context=build_alpha_context(u,args['controller'].state,NOW)
    later=context.model_copy(update={'as_of':NOW+timedelta(minutes=5),'expires_at':NOW+timedelta(minutes=6)})
    assert fast_context_signature(context)==fast_context_signature(later)
    noise=context.model_copy(update={'instruments':(
        context.instruments[0].model_copy(update={'bid_usd':D('100.01')}),)})
    assert fast_context_signature(context)==fast_context_signature(noise)
    changed=context.model_copy(update={'instruments':(
        context.instruments[0].model_copy(update={'bid_usd':D('101.00'),'ask_usd':D('101.01')}),)})
    assert fast_context_signature(context)!=fast_context_signature(changed)


def test_fast_position_request_contains_original_thesis_and_current_plan(setup_cycle):
    args,_,_=setup_cycle;u=universe((observation('stock-b','BBB'),))
    args['controller'].refresh(u)
    state=args['controller'].state
    state['positions']['stock-b']={'quantity':'100','basis_usd':'10000','mark_usd':'10100',
        'stop_usd':'98','open_risk_usd':'300','protected_quantity':'100','originating_orders':()}
    original=AlphaManagedPlan(plan_id='plan-b',instrument_id='stock-b',symbol='BBB',episode_id='episode-b',
        strategy_label='opening acceleration',thesis='Volume-backed acceleration should persist while bids replenish.',
        quantity='100',entry_limit_usd='100',original_stop_usd='97',active_stop_usd='97',target_usd='106',
        entry_expires_at=NOW+timedelta(minutes=1),next_review_at=NOW+timedelta(minutes=3),
        original_latest_exit_at=NOW+timedelta(hours=2),latest_exit_at=NOW+timedelta(hours=1))
    current=original.model_copy(update={'active_stop_usd':D('98'),'target_usd':D('105'),
        'next_review_at':NOW+timedelta(minutes=4),'version':3})
    context=build_alpha_context(u,state,NOW,instrument_ids=('stock-b',),
        position_plans={'stock-b':current},original_position_plans={'stock-b':original})
    body=fast_alpha_request(context,mode='POSITION',hard_policy={})
    payload=json.loads(body['input'][1]['content']);position=payload['context']['positions'][0]
    assert position['strategy_label']=='opening acceleration'
    assert position['original_thesis']==original.thesis
    assert position['entry_limit_usd']=='100'
    assert position['original_stop_usd']=='97' and position['original_target_usd']=='106'
    assert position['active_stop_usd']=='98' and position['target_usd']=='105'
    assert datetime.fromisoformat(position['next_review_at'].replace('Z','+00:00'))==current.next_review_at
    assert position['plan_version']==3


def test_shadow_auditor_is_structured_bound_and_non_authoritative(monkeypatch,setup_cycle):
    args,_,_=setup_cycle;u=universe((observation('stock-b','BBB'),observation('stock-c','CCC')))
    args['controller'].refresh(u);context=build_alpha_context(u,args['controller'].state,NOW)
    response=api_response({'action':'OPEN','preferred':{'instrument_id':'stock-b','symbol':'BBB',
        'probability_positive':'0.61','expected_gain_bps':'45','expected_loss_bps':'20','horizon_minutes':5},
        'reasoning':'Positive probability-weighted payoff after the observed spread.'})
    client=AstraClient('synthetic-key');seen=[]
    monkeypatch.setattr(client,'_request',lambda *call:(seen.append(call) or response))
    result=client.shadow_audit(context,{})
    assert result['audit']['action']=='OPEN' and result['reasoning']=='low'
    body=seen[0][2]
    assert body['reasoning']['effort']=='low' and body['max_output_tokens']==768 and 'tools' not in body
    assert all(key not in result for key in ('nonce','permit','order'))


def test_shadow_result_scores_same_context_without_authority(setup_cycle):
    args,_,_=setup_cycle;u=universe((observation('stock-b','BBB'),observation('stock-c','CCC')))
    args['controller'].refresh(u);context=build_alpha_context(u,args['controller'].state,NOW)
    audit={'audit':{'action':'OPEN','preferred':{'instrument_id':'stock-b','symbol':'BBB'}}}
    snap=shadow_snapshot(audit,context,{'focus':[{'instrument_id':'stock-c','symbol':'CCC','rank':1}]},'audit-1')
    snap.update(astra_action='ABSTAIN',astra_instrument_id='stock-c',astra_symbol='CCC',astra_entry_ask_usd='100')
    later_instruments=tuple(x.model_copy(update={'bid_usd':D('101')}) if x.instrument_id=='stock-b' else x
        for x in context.instruments)
    later=context.model_copy(update={'as_of':NOW+timedelta(minutes=5),'expires_at':NOW+timedelta(minutes=6),
        'instruments':later_instruments})
    result=evaluate_shadow(snap,later)
    assert D(result['shadow_observed_return_bps'])>0 and result['astra_observed_return_bps']=='0'
    assert result['winner']=='SHADOW' and 'counterfactual' in result['score_note']


def test_empty_deep_plan_replans_in_ten_minutes_without_accelerating_normal_plan():
    assert not deep_plan_due({'focus':[]},NOW,NOW+timedelta(minutes=9,seconds=59))
    assert deep_plan_due({'focus':[]},NOW,NOW+timedelta(minutes=10))
    assert not deep_plan_due({'focus':[{'symbol':'AAPL'}]},NOW,NOW+timedelta(minutes=29,seconds=59))
    assert deep_plan_due({'focus':[{'symbol':'AAPL'}]},NOW,NOW+timedelta(minutes=30))


def test_broad_cheap_scan_covers_hundreds_but_details_only_shortlist(monkeypatch):
    from threading import Barrier,Lock,get_ident
    monotonic={'now':100.0}
    monkeypatch.setattr('autonomous_trader.mvp.context.time.monotonic',lambda:monotonic['now'])
    calls=[];rank_index={rank:n for n,rank in enumerate(TOP_ACTIVE_RANK_TYPES)}
    screen_barrier=Barrier(10);screen_threads=set();screen_lock=Lock()
    class Reader:
        def read(self,op,*,symbols=('AAPL',),rank_type='VOLUME',direction='DESC'):
            calls.append((op,tuple(symbols),rank_type,direction));status=200
            if op in (Operation.TOP_ACTIVE,Operation.GAINERS_LOSERS):
                with screen_lock:screen_threads.add(get_ident())
                screen_barrier.wait(timeout=2)
            if op==Operation.ACCOUNTS:body=[{'account_id':'paper','account_type':'CASH','account_class':'INDIVIDUAL_CASH'}]
            elif op==Operation.BALANCE:body={'account_currency_assets':[{'currency':'USD','settled_cash':'1000000',
                'unsettled_cash':'0','net_liquidation_value':'1000000'}]}
            elif op==Operation.POSITIONS:body=[]
            elif op==Operation.OPEN_ORDERS:body={'data':[],'pagination_key':None}
            elif op==Operation.TOP_ACTIVE:
                n=rank_index[rank_type]
                if rank_type=='TURNOVER':status=417;body={}
                else:body=[{'symbol':f'S{n}{i:03d}','currency':'USD','exchange_code':'NSQ','volume':'250000',
                    'relative_volume_10d':'3.5','market_value':'75000000','change_ratio':'1.5','amplitude':'8.2'} for i in range(60)]
            elif op==Operation.GAINERS_LOSERS:
                prefix={'MIN_3':'G3','MIN_5':'G5','DAY_1':'GD'}[rank_type]+('U' if direction=='DESC' else 'D')
                body=[{'symbol':f'{prefix}{i:03d}','currency':'USD','exchange_code':'NSQ','volume':'250000',
                    'relative_volume_10d':'3.5','market_value':'75000000',
                    'change_ratio':'3' if direction=='DESC' and rank_type=='MIN_3' and i==59 else
                        ('1.5' if direction=='DESC' else '-1.5'),'amplitude':'8.2'} for i in range(60)]
            elif op==Operation.INSTRUMENTS:body={'data':[{'symbol':s,'instrument_id':'id-'+s,'category':'US_STOCK',
                'sub_category':'ADR','currency':'USD','status':'OC','exchange_code':'NSQ','lot_size':'1'} for s in symbols]}
            elif op==Operation.HISTORY_BATCH:body={'result':[{'symbol':s,'instrument_id':'id-'+s,'result':[
                {'time':NOW.isoformat(),'open':'10','high':'10.02','low':'9.99','close':'10.01','volume':'100000'}]} for s in symbols]}
            elif op==Operation.SNAPSHOT:body=[{'symbol':s,'instrument_id':'id-'+s,'bid':'1.99','ask':'2.05',
                'bid_size':'5000','ask_size':'4000','quote_time':str(int(NOW.timestamp()*1000)),
                'volume':'250000','list_status':'LISTED','lot_size':'1'} for s in symbols]
            return SimpleNamespace(status_code=status,body=body,received_at=NOW.isoformat())
    source=SandboxSource.__new__(SandboxSource);source.credentials=SimpleNamespace(account_id='paper')
    source.readers={op:Reader() for op in Operation};source._universe_symbols=();source._universe_at=0
    source._discovery_generation=0;source.discovery_stats={};source._parallel_discovery=True
    source._reader_factory=lambda:Reader()
    result=source.refresh({},portfolio=True)
    stats=source.discovery_stats
    assert (stats['generation'],stats['observed_rows'],stats['unique_symbols'],stats['eligible_symbols'],
        stats['shortlisted_symbols'])==(1,540,540,540,MAX_CANDIDATES)
    assert len(screen_threads)==10 and stats['discovery_interval_seconds']==15
    assert stats['rank_sources']==[
        'GAINERS_LOSERS:MIN_3:DESC','GAINERS_LOSERS:MIN_3:ASC',
        'GAINERS_LOSERS:MIN_5:DESC','GAINERS_LOSERS:MIN_5:ASC',
        'GAINERS_LOSERS:DAY_1:DESC','GAINERS_LOSERS:DAY_1:ASC',
        'TOP_ACTIVE:RELATIVE_VOLUME_10D:DESC','TOP_ACTIVE:AMPLITUDE:DESC','TOP_ACTIVE:VOLUME:DESC']
    assert stats['unsupported_rank_sources']==['TOP_ACTIVE:TURNOVER:DESC']
    assert len(stats['shortlist'])==MAX_CANDIDATES
    assert all(row['cheap_rank_reasons'] for row in stats['shortlist'])
    assert len(result.candidates)==MAX_CANDIDATES
    assert any(x.change_ratio<0 for x in result.candidates)
    assert all(x.entry_eligible and x.volume==D('250000') for x in result.candidates)
    assert all((x.bid_size,x.ask_size,x.relative_volume_10d)==(D('5000'),D('4000'),D('3.5')) for x in result.candidates)
    alpha=build_alpha_context(result,{'positions':{},'orders':{},'reservations':{},'cash':{
        'liquidation_equity_usd':'1000000','settled_usd':'1000000'},'day_start_equity':'1000000',
        'reconciliation':{'ready':True,'settlement_known':True},'session':{
        'opens_at':(NOW-timedelta(hours=1)).isoformat(),'closes_at':(NOW+timedelta(hours=5)).isoformat()}},NOW)
    first=alpha.instruments[0]
    # A wide but valid two-sided quote is alpha/cost evidence, not broken data.
    # It remains visible to Astra; bounded LIMIT execution and hard risk remain
    # independent authority boundaries.
    assert first.reference_price_usd==D('2.02') and first.spread_bps==D('301.507538')
    assert (first.volume,first.relative_volume_10d,first.bid_size,first.ask_size)==(D('250000'),D('3.5'),D('5000'),D('4000'))
    market_context=json.loads(first.market_context)
    assert market_context['cheap_discovery_sources']
    assert market_context['market_value']=='75000000' and market_context['change_ratio']=='3' and market_context['amplitude']=='8.2'
    detailed={s for op,s,_,_ in calls if op in (Operation.INSTRUMENTS,Operation.HISTORY_BATCH,Operation.SNAPSHOT) for s in s}
    assert len(detailed)==MAX_CANDIDATES
    assert {rank for op,_,rank,_ in calls if op==Operation.TOP_ACTIVE}=={'RELATIVE_VOLUME_10D','AMPLITUDE','VOLUME','TURNOVER'}
    assert {rank for op,_,rank,_ in calls if op==Operation.GAINERS_LOSERS}=={'MIN_3','MIN_5','DAY_1'}
    assert {direction for op,_,_,direction in calls if op==Operation.GAINERS_LOSERS}=={'ASC','DESC'}
    assert len(stats['top_runners'])==10 and all(row['window_minutes'] in (3,5) for row in stats['top_runners'])
    assert len(stats['screen_runners'])==10
    assert stats['screen_runners'][0]['symbol']=='G3U059'
    assert stats['screen_runners'][0]['percent_per_minute']=='100.000000'
    assert {row['symbol'] for row in stats['screen_runners']} <= {x.ticker for x in result.candidates}
    assert 'G3U059' in {x.ticker for x in result.candidates}
    assert stats['day_gainers'][0]['symbol']=='GDU000'
    assert stats['day_gainers'][0]['source']=='GAINERS_LOSERS:DAY_1:DESC'
    assert stats['day_gainer_reserved_symbols']==[f'GDU{i:03d}' for i in range(8)]
    assert {'G3U059',*[f'GDU{i:03d}' for i in range(8)]} <= {x.ticker for x in result.candidates}
    assert stats['shortlist'][0]['symbol']=='G3U059'
    assert any(x.symbol=='G3U059' for x in alpha.instruments)
    account_index=next(i for i,x in enumerate(calls) if x[0]==Operation.ACCOUNTS)
    assert next(i for i,x in enumerate(calls) if x[0]==Operation.TOP_ACTIVE)<account_index
    assert max(i for i,x in enumerate(calls) if x[0] in (Operation.INSTRUMENTS,Operation.HISTORY_BATCH))<account_index
    assert any(i>account_index and x[0]==Operation.SNAPSHOT for i,x in enumerate(calls))
    assert stats['detailed_candidates']==MAX_CANDIDATES and stats['detailed_rejections']=={}
    chosen=result.candidates[0].ticker;before=len([1 for op,_,_,_ in calls if op in (Operation.TOP_ACTIVE,Operation.GAINERS_LOSERS)])
    focused=source.refresh_focus({},chosen)
    assert focused.select(chosen,NOW).ticker==chosen and len(focused.candidates)==1
    assert len([1 for op,_,_,_ in calls if op in (Operation.TOP_ACTIVE,Operation.GAINERS_LOSERS)])==before
    # Cache age is measured from screen start, not completion, so network time
    # cannot stretch the configured start-to-start discovery cadence.
    monotonic['now']=114.999;source.refresh({},portfolio=True)
    assert len([1 for op,_,_,_ in calls if op in (Operation.TOP_ACTIVE,Operation.GAINERS_LOSERS)])==before
    monotonic['now']=115.0;source.refresh({},portfolio=True)
    assert len([1 for op,_,_,_ in calls if op in (Operation.TOP_ACTIVE,Operation.GAINERS_LOSERS)])==before+10


def test_final_quote_refresh_prefers_active_shortlisted_names_over_stale_first_ten():
    symbols=tuple(f'S{i:02d}' for i in range(20));snapshots=[]
    class Reader:
        def read(self,op,*,symbols=('AAPL',),**_):
            if op==Operation.INSTRUMENTS:
                body={'data':[{'symbol':s,'instrument_id':'id-'+s,'category':'US_STOCK',
                    'sub_category':'COMMON_STOCK','currency':'USD','status':'OC','exchange_code':'NSQ',
                    'lot_size':'1'} for s in symbols]}
            elif op==Operation.HISTORY_BATCH:
                body={'result':[{'symbol':s,'instrument_id':'id-'+s,'result':[
                    {'time':NOW.isoformat(),'open':'2','high':'2.02','low':'1.99','close':'2',
                     'volume':'100000'}]} for s in symbols]}
            elif op==Operation.SNAPSHOT:
                snapshots.append(tuple(symbols))
                final=len(snapshots)>2
                quote_at=NOW if final else NOW-timedelta(seconds=60 if symbols[0]=='S00' else 3)
                body=[{'symbol':s,'instrument_id':'id-'+s,'bid':'1.99','ask':'2.00',
                    'quote_time':str(int(quote_at.timestamp()*1000)),'volume':'100000',
                    'list_status':'LISTED','lot_size':'1'} for s in symbols]
            elif op==Operation.ACCOUNTS:
                body=[{'account_id':'paper','account_type':'CASH','account_class':'INDIVIDUAL_CASH'}]
            elif op==Operation.BALANCE:
                body={'account_currency_assets':[{'currency':'USD','settled_cash':'1000000',
                    'unsettled_cash':'0','net_liquidation_value':'1000000'}]}
            elif op==Operation.POSITIONS:body=[]
            elif op==Operation.OPEN_ORDERS:body={'data':[],'pagination_key':None}
            else:raise AssertionError(op)
            return SimpleNamespace(status_code=200,body=body,received_at=NOW.isoformat())
    source=SandboxSource.__new__(SandboxSource)
    source.credentials=SimpleNamespace(account_id='paper')
    source.readers={op:Reader() for op in Operation}
    source._reader_factory=lambda:Reader()
    source._parallel_discovery=False
    source._universe_symbols=symbols
    source._universe_at=__import__('time').monotonic()
    source._discovery_generation=0
    source.discovery_stats={}
    observed=source.refresh({},portfolio=True)
    assert snapshots[:2]==[symbols[:10],symbols[10:]]
    assert snapshots[2]==symbols[10:]
    assert {item.ticker for item in observed.eligible(NOW)}==set(symbols[10:])


def focused_source(*,status='OC',instrument_id='focus-id',bid='9.99',snapshot=True):
    class Reader:
        def read(self,op,*,symbols=('AAPL',),**_):
            if op==Operation.ACCOUNTS:body=[{'account_id':'paper','account_type':'CASH','account_class':'INDIVIDUAL_CASH'}]
            elif op==Operation.BALANCE:body={'account_currency_assets':[{'currency':'USD','settled_cash':'1000000',
                'unsettled_cash':'0','net_liquidation_value':'1000000'}]}
            elif op==Operation.POSITIONS:body=[]
            elif op==Operation.OPEN_ORDERS:body={'data':[],'pagination_key':None}
            elif op==Operation.INSTRUMENTS:body={'data':[{'symbol':'FOCUS','instrument_id':'focus-id',
                'category':'US_STOCK','sub_category':'COMMON_STOCK','currency':'USD','status':status,'exchange_code':'NSQ','lot_size':'1'}]}
            elif op==Operation.HISTORY_BATCH:body={'result':[{'symbol':'FOCUS','instrument_id':'focus-id','result':[
                {'time':NOW.isoformat(),'open':'10','high':'10.02','low':'9.98','close':'10','volume':'100000'}]}]}
            elif op==Operation.SNAPSHOT:
                body=[] if not snapshot else [{'symbol':'FOCUS','instrument_id':instrument_id,'bid':bid,'ask':'10.01',
                    'quote_time':str(int(NOW.timestamp()*1000)),'volume':'100000','list_status':'LISTED','lot_size':'1'}]
            return SimpleNamespace(status_code=200,body=body,received_at=NOW.isoformat())
    source=SandboxSource.__new__(SandboxSource);source.credentials=SimpleNamespace(account_id='paper')
    source.readers={op:Reader() for op in Operation};source._universe_symbols=();source._universe_at=0
    source._discovery_generation=0;source.discovery_stats={};source._discovery_metrics={};source._discovery_provenance={}
    return source


def test_single_symbol_focus_stops_after_first_fresh_final_snapshot():
    source=focused_source();reader=source.readers[Operation.SNAPSHOT];calls=[]
    class CountingReader:
        def read(self,op,**kwargs):
            calls.append(tuple(kwargs['symbols']))
            return reader.read(op,**kwargs)
    source.readers[Operation.SNAPSHOT]=CountingReader()
    focused=source.refresh_focus({},'FOCUS')
    assert focused.select('FOCUS',NOW).ticker=='FOCUS'
    assert calls==[('FOCUS',),('FOCUS',)]


def test_focused_refresh_parallelizes_independent_reads_before_final_quote():
    from threading import Barrier,Lock,get_ident
    source=focused_source();source._parallel_discovery=True
    base=source.readers[Operation.ACCOUNTS];barrier=Barrier(6);lock=Lock()
    completed=set();threads=set();snapshot_after_batch=[]
    class ParallelReader:
        def read(self,op,**kwargs):
            barrier.wait(timeout=2)
            with lock:
                completed.add(op);threads.add(get_ident())
            return base.read(op,**kwargs)
    source._reader_factory=lambda:ParallelReader()
    source._focus_reader_factory=lambda:ParallelReader()
    snapshot=source.readers[Operation.SNAPSHOT]
    class FinalQuoteReader:
        def read(self,op,**kwargs):
            snapshot_after_batch.append(completed=={
                Operation.INSTRUMENTS,Operation.HISTORY_BATCH,Operation.ACCOUNTS,
                Operation.BALANCE,Operation.POSITIONS,Operation.OPEN_ORDERS})
            return snapshot.read(op,**kwargs)
    source.readers[Operation.SNAPSHOT]=FinalQuoteReader()
    focused=source.refresh_focus({},'FOCUS')
    assert focused.select('FOCUS',NOW).ticker=='FOCUS'
    assert len(threads)==6 and snapshot_after_batch==[True]


def test_single_symbol_focus_retries_stale_final_snapshots_without_accepting_them():
    source=focused_source();reader=source.readers[Operation.SNAPSHOT];calls=[]
    class StaleFinalReader:
        def read(self,op,**kwargs):
            calls.append(tuple(kwargs['symbols']))
            response=reader.read(op,**kwargs)
            if len(calls)>1:response.body[0]['quote_time']=str(int((NOW-timedelta(seconds=3)).timestamp()*1000))
            return response
    source.readers[Operation.SNAPSHOT]=StaleFinalReader()
    focused=source.refresh_focus({},'FOCUS')
    assert calls==[('FOCUS',)]*4
    with pytest.raises(ValueError,match='^STALE_MARKET_OR_ACCOUNT$'):
        focused.select('FOCUS',NOW)


def test_focus_that_loses_only_entry_eligibility_maps_to_bounded_selection_error():
    focused=focused_source(status='CLOSED').refresh_focus({},'FOCUS')
    assert len(focused.candidates)==1 and not focused.candidates[0].entry_eligible
    with pytest.raises(ValueError,match='^INELIGIBLE_OR_ILLIQUID_STOCK$'):focused.select('FOCUS',NOW)


def test_focused_quote_remains_usable_across_serialized_account_reads_and_stale_bars_are_evidence():
    at=NOW+timedelta(seconds=12)
    item=replace(observation('focus-id','FOCUS'),quote_at=at-timedelta(milliseconds=400),received_at=NOW,
        quote_received_at=at-timedelta(milliseconds=300),bars=({'time':(NOW-timedelta(minutes=40)).isoformat(),
            'open':'10','high':'10.1','low':'9.9','close':'10','volume':'1'},))
    focused=UniverseObservation((item,),NOW,D('1000000'),D(0),D('1000000'),{},(),'focus-evidence')
    focused.require_fresh(at)
    assert focused.select('FOCUS',at).instrument_id=='focus-id'
    with pytest.raises(ValueError,match='^STALE_MARKET_OR_ACCOUNT$'):
        focused.select('FOCUS',NOW+timedelta(seconds=31))


@pytest.mark.parametrize(('source','error'),[
    (lambda:focused_source(instrument_id='wrong-id'),'FOCUSED_INSTRUMENT_IDENTITY_MISMATCH'),
    (lambda:focused_source(bid='bad'),'FOCUSED_INSTRUMENT_PAYLOAD_INVALID'),
    (lambda:focused_source(snapshot=False),'FOCUSED_INSTRUMENT_PAYLOAD_INCOMPLETE')])
def test_focus_identity_and_payload_failures_do_not_map_to_entry_invalidation(source,error):
    with pytest.raises(ValueError,match='^'+error+'$'):source().refresh_focus({},'FOCUS')


def test_open_admission_uses_genuinely_fresh_post_astra_mark(setup_cycle):
    args,_,_=setup_cycle;controller=args['controller']
    pre=observation('stock-b','BBB');other=observation('stock-c','CCC')
    controller.refresh(universe((pre,other)))
    after_astra=NOW+timedelta(seconds=20);controller.clock=lambda:after_astra
    post=replace(pre,quote_at=after_astra-timedelta(milliseconds=500),received_at=after_astra,
        quote_received_at=after_astra,evidence_id='post-astra-fresh')
    refreshed=universe((post,));controller.refresh(refreshed)
    context=build_alpha_context(refreshed,controller.state,after_astra)
    decision=AlphaDecision.model_validate(open_raw(after_astra))
    validate_alpha_semantics(decision,context,after_astra,maximum_quote_age_ms=5000)
    intent,plan=translate_open(decision,context,controller.account_id,after_astra,'post-astra-open',
        capability_version=controller.state['config']['capabilities']['version'],market_regime='TRENDING',model_cost_usd='1.25')
    nonce=controller.admit(intent,plan)
    state=controller.state
    assert datetime.fromisoformat(state['marks']['stock-b']['observed_at'])==post.quote_at
    assert state['orders'][intent.intent_id]['phase']=='RESERVED'
    assert state['outbox'][intent.intent_id]['status']=='READY'
    assert state['outbox'][intent.intent_id]['nonce']==nonce
    assert plan.market_regime=='TRENDING' and plan.initial_model_cost_usd==D('1.25')
    assert plan.original_latest_exit_at==context.liquidation_at


def test_two_positions_share_portfolio_loop_and_one_stop_does_not_flat_other(setup_cycle):
    args,broker,_=setup_cycle;controller=args['controller'];gateway=args['gateway'];session=args['calendar'].session(NOW.date())
    items=[observation('stock-b','BBB'),observation('stock-c','CCC')];plans={}
    for item in items:
        positions={iid:p['quantity'] for iid,p in controller.state['positions'].items()}
        cash=controller.state['cash'];u=replace(universe(items,positions),settled=D(cash['settled_usd']),
            unsettled=D(cash['unsettled_usd']),equity=D(cash['liquidation_equity_usd']));controller.refresh(u)
        assert controller.state['reconciliation']['ready'],(controller.state['reconciliation'],controller.state['cash'],positions)
        context=build_alpha_context(u,controller.state,NOW)
        d=AlphaDecision.model_validate(open_raw(NOW,item.instrument_id,item.ticker));validate_alpha_semantics(d,context,NOW)
        intent,plan=translate_open(d,context,controller.account_id,NOW,'decision-'+item.ticker,
            capability_version=controller.state['config']['capabilities']['version'])
        nonce=controller.admit(intent,plan);gateway.dispatch(intent,nonce,item,controller,session,lambda:NOW);plans[plan.plan_id]=plan
        controller.send(Protection(instrument_id=item.instrument_id,quantity='1',stop_usd='95',evidence_id='test-protection-'+item.ticker))
    positions={iid:p['quantity'] for iid,p in controller.state['positions'].items()}
    stopped=observation('stock-b','BBB','94',positions=positions);healthy=observation('stock-c','CCC','100',positions=positions)
    cash=controller.state['cash'];u=replace(universe((stopped,healthy),positions),settled=D(cash['settled_usd']),
        unsettled=D(cash['unsettled_usd']),equity=D(cash['liquidation_equity_usd']));controller.refresh(u)
    class Store:
        def alpha_plan(self,pid):return plans[pid].model_dump(mode='json')
    events=[];manager=AlphaPortfolioManager(Store(),review=lambda *_:None,emit=lambda k,p:events.append((k,p)),
        exit_manager=LocalPositionManager(monitor_dry_run=True,emit=lambda k,p:events.append((k,p))))
    result=manager.tick(u,controller,gateway,session,lambda:NOW)
    assert result['reason']=='STOP' and set(controller.state['positions'])=={'stock-c'}
    assert len(broker.orders)==3 and any(k=='ALPHA_OUTCOME' for k,_ in events)
    outcome=next(p for k,p in events if k=='ALPHA_OUTCOME')
    assert {'realized_net_after_all_known_costs_usd','return_on_capital','holding_seconds','capital_deployed_usd',
        'capital_time_usd_seconds','maximum_favorable_excursion_usd','maximum_adverse_excursion_usd',
        'win_loss','model_cost_usd','market_regime'}<=set(outcome)


def test_closed_market_alpha_acceptance_calls_no_model_broker_or_source(tmp_path):
    class Store:
        def runtime(self):return {}
        def heartbeat(self,_):pass
    closed=datetime(2026,9,15,21,tzinfo=timezone.utc);engine=Engine(tmp_path,RuntimeConfig(),Store(),clock=lambda:closed)
    engine.controller=SimpleNamespace(state={'positions':{}});engine.alpha_manager=SimpleNamespace()
    engine.source=SimpleNamespace(refresh_portfolio=lambda *_:(_ for _ in ()).throw(AssertionError('closed market read')))
    engine.brain_factory=lambda:(_ for _ in ()).throw(AssertionError('closed market model call'))
    engine.gateway=SimpleNamespace(dispatch=lambda *_:(_ for _ in ()).throw(AssertionError('closed market order')))
    engine.initialized=True  # Exercise the closed-session step, not dependency startup.
    engine.state['last_error']='INSUFFICIENT_ELIGIBLE_UNIVERSE'
    engine.step()
    assert engine.state['status']=='RUNNING' and engine.state['last_error'] is None
    assert engine.state['activity']=='Waiting for regular entry session'
    assert engine.state['activity']=='Waiting for regular entry session' and engine.state['next_evaluation']


def test_one_fresh_candidate_can_reach_alpha_but_zero_fresh_candidates_fail():
    one=universe((observation('stock-b','BBB'),))
    one.require_fresh(NOW)
    assert [item['symbol'] for item in one.selection_payload(NOW)]==['BBB']

    stale_item=replace(one.candidates[0],quote_at=NOW-timedelta(seconds=3))
    none_fresh=replace(one,candidates=(stale_item,))
    with pytest.raises(ValueError,match='INSUFFICIENT_ELIGIBLE_UNIVERSE'):
        none_fresh.require_fresh(NOW)


def test_wide_spread_is_visible_to_astra_but_stale_quote_still_fails():
    item=replace(observation('wide-id','WIDE'),ask=D('101.50'))
    item.require_fresh(NOW)
    context=build_alpha_context(universe((item,)),{'positions':{},'orders':{},'reservations':{},'cash':{
        'liquidation_equity_usd':'1000000','settled_usd':'1000000'},'day_start_equity':'1000000',
        'reconciliation':{'ready':True,'settlement_known':True},'session':{
        'opens_at':(NOW-timedelta(hours=1)).isoformat(),'closes_at':(NOW+timedelta(hours=5)).isoformat()}},NOW)
    assert context.instruments[0].spread_bps>D('20')
    with pytest.raises(ValueError,match='STALE_MARKET_OR_ACCOUNT'):
        replace(item,quote_at=NOW-timedelta(seconds=3)).require_fresh(NOW)


def test_transient_error_schedules_real_retry(tmp_path):
    class Store:
        def runtime(self):return {}
        def heartbeat(self,_):pass
        def emit(self,*_,**__):pass
    engine=Engine(tmp_path,RuntimeConfig(),Store(),clock=lambda:NOW,poll=30)
    engine.record_error(ValueError('INSUFFICIENT_ELIGIBLE_UNIVERSE'))
    assert engine.next_evaluation==NOW+timedelta(seconds=30)
    assert engine.state['next_evaluation']==(NOW+timedelta(seconds=30)).isoformat()


def test_transient_state_retry_does_not_accelerate_existing_alpha_schedule(tmp_path):
    class Store:
        def runtime(self):return {}
        def heartbeat(self,_):pass
        def emit(self,*_,**__):pass
    engine=Engine(tmp_path,RuntimeConfig(),Store(),clock=lambda:NOW,poll=30)
    engine.next_evaluation=NOW+timedelta(minutes=5)
    engine.record_error(ValueError('INSUFFICIENT_ELIGIBLE_UNIVERSE'))
    assert engine.next_evaluation==NOW+timedelta(minutes=5)
    assert engine.state['next_evaluation']==(NOW+timedelta(minutes=5)).isoformat()


def test_broad_context_uses_completed_snapshot_time_across_reconciliation(monkeypatch,tmp_path):
    class Store:
        def runtime(self):return {}
        def heartbeat(self,_):pass
        def emit(self,*_,**__):pass
        def recent_outcomes(self):return ()
    engine=Engine(tmp_path,RuntimeConfig(mode=ExecutionMode.DRY_RUN),Store(),clock=lambda:NOW)
    state={'positions':{},'orders':{},'reservations':{},'latches':{
        'daily':False,'drawdown':False,'incident':False,'terminated':False},
        'cash':{'liquidation_equity_usd':'1000000','settled_usd':'1000000'},'day_start_equity':'1000000',
        'policy_hash':'test-policy','config':{'capabilities':{'version':'mvp-local-monitor-v1'}},
        'reconciliation':{'ready':True,'settlement_known':True,'valid_until':(NOW+timedelta(hours=6)).isoformat()},'session':{
            'label':NOW.date().isoformat(),
            'opens_at':(NOW-timedelta(minutes=30)).isoformat(),'closes_at':(NOW+timedelta(hours=6)).isoformat()}}
    class Controller:
        account_id='test'
        @property
        def state(self):return state
        def lease(self):return state
    item=replace(observation('edge-id','EDGE'),quote_at=NOW-timedelta(milliseconds=1500))
    observed=universe((item,))
    engine.controller=Controller();engine.source=SimpleNamespace(refresh_portfolio=lambda _:observed)
    engine.deep_plan={'focus':[]};engine.deep_plan_at=NOW;engine.next_evaluation=NOW
    times=iter((NOW,NOW+timedelta(milliseconds=250),NOW+timedelta(milliseconds=250),
        NOW+timedelta(milliseconds=250),NOW+timedelta(milliseconds=250)))
    engine.clock=lambda:next(times,NOW+timedelta(milliseconds=250))
    monkeypatch.setattr('autonomous_trader.monitoring.runner.refresh_session',lambda *_args,**_kwargs:state)
    engine._run_candidates=lambda session:None  # Isolate broad snapshot timing from asynchronous queue.
    engine._alpha_step(NOW,engine.calendar.session(NOW.date()))
    assert engine.state['status']=='RUNNING'
    assert len(engine.opportunities.jobs)==1


def test_focused_instrument_survives_reconciliation_delay_with_fresh_exact_symbol_read(monkeypatch,tmp_path):
    class Store:
        def runtime(self):return {}
        def heartbeat(self,_):pass
        def emit(self,*_,**__):pass
        def recent_outcomes(self):return ()
    engine=Engine(tmp_path,RuntimeConfig(),Store(),clock=lambda:NOW)
    session=engine.calendar.session(NOW.date());current={'at':NOW}
    state={'positions':{},'orders':{},'reservations':{},'latches':{
        'daily':False,'drawdown':False,'incident':False,'terminated':False},
        'cash':{'liquidation_equity_usd':'1000000','settled_usd':'1000000'},'day_start_equity':'1000000',
        'policy_hash':'test-policy','config':{'capabilities':{'version':'mvp-local-monitor-v1'}},
        'reconciliation':{'ready':True,'settlement_known':True,'valid_until':session.closes_at.isoformat()},'session':{
            'label':session.label.isoformat(),
            'opens_at':session.opens_at.isoformat(),'closes_at':session.closes_at.isoformat()}}
    selected=observation('focus-id','FOCUS');other=observation('other-id','OTHER')
    broad=universe((selected,other))
    class Controller:
        account_id='test'
        @property
        def state(self):return state
        def lease(self):return state
        def refresh(self,observed):
            if len(observed.candidates)==1:current['at']+=timedelta(milliseconds=250)
            return state
    class Source:
        def refresh_portfolio(self,_):return broad
        def refresh_focus(self,_,symbol):
            assert symbol=='FOCUS'
            item=replace(selected,quote_at=current['at']-timedelta(milliseconds=1900),
                received_at=current['at'],quote_received_at=current['at'])
            return replace(universe((item,)),received_at=current['at'])
        def refresh_comparative_focus(self,_,symbol,alternatives):
            assert symbol=='FOCUS' and alternatives==['OTHER']
            items=(replace(selected,quote_at=current['at']-timedelta(milliseconds=1900),
                received_at=current['at'],quote_received_at=current['at']),
                replace(other,quote_at=current['at']-timedelta(milliseconds=1900),
                    received_at=current['at'],quote_received_at=current['at']))
            return replace(universe(items),received_at=current['at'])
    class Brain:
        def fast_decide(self,context,mode):
            assert mode=='ENTRY' and {x.symbol for x in context.instruments}=={'FOCUS','OTHER'}
            return {'decision':{'schema_version':'1.0','action':'ABSTAIN','reason':'No positive net edge now.',
                    'open':None,'hold':None,'modify':None,'close':None},'schema_valid':True,'semantics_valid':True,
                    'model':MODEL,'reasoning':'medium','latency_ms':1,
                    'usage':{'input_tokens':100,'output_tokens':20},'estimated_cost_usd':'0.001',
                    'model_work_id':'test-portfolio-claim'}
        def portfolio_decide(self,context):return self.fast_decide(context,'ENTRY')
    engine.clock=lambda:current['at'];engine.controller=Controller();engine.source=Source()
    engine.alpha_manager=SimpleNamespace(plans=lambda _:{})
    engine.brain_factory=lambda:Brain();engine.gateway=SimpleNamespace()
    engine.deep_plan={'regime_label':'TEST','regime_view':'Test.','focus':[{'instrument_id':'focus-id',
        'symbol':'FOCUS','rank':1,'strategy_label':'test','thesis':'Fresh exact-symbol evidence.','horizon':'intraday'}]}
    engine.deep_plan_at=NOW;engine.next_evaluation=NOW;events=[];engine.emit=lambda k,p:events.append((k,p))
    monkeypatch.setattr('autonomous_trader.monitoring.runner.refresh_session',lambda controller,observed,*args,**kwargs:controller.refresh(observed))
    engine.config=RuntimeConfig(opportunity_workers=1)
    engine._alpha_step(NOW,session)
    for future,_,_ in engine.opportunities.futures.values():future.result(timeout=2)
    engine.opportunities.calls=[NOW.isoformat()]*engine.config.opportunity_calls_per_window
    engine._run_candidates(session)
    assert [kind for kind,_ in events]==['ALPHA_FOCUS_SELECTED','ALPHA_DECISION']
    assert events[-1][1]['ai']['decision']['action']=='ABSTAIN'
    assert engine.state['status']=='RUNNING' and engine.state['last_error'] is None


def test_overdue_holding_is_supervised_before_broad_universe_or_alpha_even_during_retry(tmp_path):
    session=__import__('autonomous_trader.mvp.config',fromlist=['reviewed_calendar']).reviewed_calendar().session(NOW.date())
    now=session.liquidation_at
    class Store:
        def runtime(self):return {}
        def heartbeat(self,_):pass
        def recent_outcomes(self):raise AssertionError('entry feedback must not run before mandatory exit')
    engine=Engine(tmp_path,RuntimeConfig(),Store(),clock=lambda:now)
    state={'positions':{'held-id':{'quantity':'7'}},'orders':{},'settlements':{},
        'cash':{'settled_usd':'1000','unsettled_usd':'0'},'reconciliation':{'ready':True},
        'session':{'label':session.label.isoformat()},'latches':{}}
    class Controller:
        account_id='test'
        @property
        def state(self):return state
        def lease(self):return state
        def refresh(self,_):return state
        def refresh_for_exit(self,_):return state
    held=UniverseObservation((observation('held-id','HELD',positions={'held-id':'7'}),),now,D('1000'),D('0'),D('1000'),
        {'held-id':'7'},(),'held-only')
    calls=[]
    class Source:
        def refresh_held(self,_):calls.append('held');return replace(held,received_at=now,
            candidates=(replace(held.candidates[0],quote_at=now,received_at=now,quote_received_at=now),))
        def refresh_portfolio(self,_):raise AssertionError('broad universe must not precede mandatory exit')
    class Manager:
        def tick(self,*_):calls.append('exit');state['positions']={};return {'exited':'held-id','reason':'ABSOLUTE_LATEST_EXIT'}
    engine.controller=Controller();engine.source=Source();engine.alpha_manager=Manager();engine.gateway=SimpleNamespace()
    engine.consecutive_universe_insufficiencies=1;engine.universe_retry_at=now+timedelta(seconds=30)
    engine._alpha_step(now,session)
    assert calls==['held','exit'] and engine.state['activity']=='Mandatory held-position exit completed'


def test_overdue_alpha_position_exits_exact_owned_quantity(setup_cycle):
    args,broker,_=setup_cycle;controller=args['controller'];item=observation('stock-b','BBB')
    initial=universe((item,observation('stock-c','CCC')));controller.refresh(initial)
    context=build_alpha_context(initial,controller.state,NOW)
    decision=AlphaDecision.model_validate(open_raw(NOW,quantity='7'))
    intent,plan=translate_open(decision,context,controller.account_id,NOW,'deadline-exact-share',
        capability_version=controller.state['config']['capabilities']['version'])
    nonce=controller.admit(intent,plan);args['gateway'].dispatch(intent,nonce,item,controller,args['calendar'].session(NOW.date()),lambda:NOW)
    after=plan.latest_exit_at;controller.clock=lambda:after
    held=replace(item,quote_at=after-timedelta(seconds=7),received_at=after,quote_received_at=after,
        bars=tuple({**row,'time':after.isoformat()} for row in item.bars),
        positions={item.instrument_id:'7'},settled=D(controller.state['cash']['settled_usd']),
        unsettled=D(controller.state['cash']['unsettled_usd']),equity=D(controller.state['cash']['liquidation_equity_usd']))
    held_universe=replace(universe((held,),positions={item.instrument_id:'7'}),received_at=after,
        settled=held.settled,unsettled=held.unsettled,equity=held.equity);controller.refresh(held_universe)
    class Store:
        def alpha_plan(self,_):return plan.model_dump(mode='json')
    manager=AlphaPortfolioManager(Store(),review=lambda *_:None,emit=lambda *_:None,
        exit_manager=LocalPositionManager(monitor_dry_run=True))
    result=manager.tick(held_universe,controller,args['gateway'],args['calendar'].session(NOW.date()),lambda:after)
    assert result['reason']=='ABSOLUTE_LATEST_EXIT' and not controller.state['positions']
    exit_order=list(broker.orders.values())[-1]
    assert len(broker.orders)==2 and exit_order.side=='SELL_OWNED' and exit_order.quantity==D('7')


def test_carried_position_requires_priority_astra_reassessment_before_old_plan_can_exit(setup_cycle):
    args,broker,_=setup_cycle;controller=args['controller'];item=observation('stock-b','BBB')
    initial=universe((item,observation('stock-c','CCC')));controller.refresh(initial)
    context=build_alpha_context(initial,controller.state,NOW)
    decision=AlphaDecision.model_validate(open_raw(NOW,quantity='7'))
    intent,plan=translate_open(decision,context,controller.account_id,NOW,'carried-priority-review',
        capability_version=controller.state['config']['capabilities']['version'])
    nonce=controller.admit(intent,plan);args['gateway'].dispatch(intent,nonce,item,controller,args['calendar'].session(NOW.date()),lambda:NOW)
    next_session=args['calendar'].session(NOW.date()+timedelta(days=3));now=next_session.opens_at+timedelta(seconds=10)
    controller.clock=lambda:now
    # Both the old target and old deadline are true, but neither has authority in the new session.
    held=replace(item,bid=D('105'),ask=D('105.01'),quote_at=now,received_at=now,quote_received_at=now,
        bars=tuple({**row,'time':now.isoformat()} for row in item.bars),positions={item.instrument_id:'7'},
        settled=D(controller.state['cash']['settled_usd']),unsettled=D(controller.state['cash']['unsettled_usd']),
        equity=D(controller.state['cash']['liquidation_equity_usd']))
    held_universe=replace(universe((held,),positions={item.instrument_id:'7'}),received_at=now,
        settled=held.settled,unsettled=held.unsettled,equity=held.equity);controller.refresh(held_universe)
    class Store:
        def alpha_plan(self,_):return plan.model_dump(mode='json')
    reviews=[];events=[]
    def review(_,review_plan):
        reviews.append(review_plan)
        return ({'decision':{'schema_version':'1.0','action':'HOLD','reason':'Current evidence supports holding.',
            'open':None,'hold':{'position_id':'position-'+item.instrument_id,
                'next_review_at':(now+timedelta(minutes=2)).isoformat()},'modify':None,'close':None},
            'estimated_cost_usd':'0.01'},None)
    manager=AlphaPortfolioManager(Store(),review=review,emit=lambda k,p:events.append((k,p)),
        exit_manager=LocalPositionManager(monitor_dry_run=True))
    first=manager.tick(held_universe,controller,args['gateway'],next_session,lambda:now)
    assert first=={'positions_monitored':1} and len(broker.orders)==1
    manager.tick(held_universe,controller,args['gateway'],next_session,lambda:now)
    assert len(broker.orders)==1 and reviews[0].latest_exit_at==next_session.liquidation_at
    assert any(k=='ALPHA_POSITION_REVIEW_REQUESTED' and p['cross_session_reassessment'] for k,p in events)
    assert any(k=='ALPHA_POSITION_REVIEW' and p['action']=='HOLD' for k,p in events)


def test_astra_target_touch_requests_review_instead_of_automatic_exit(setup_cycle):
    args,broker,_=setup_cycle;controller=args['controller'];item=observation('stock-b','BBB')
    initial=universe((item,observation('stock-c','CCC')));controller.refresh(initial)
    context=build_alpha_context(initial,controller.state,NOW)
    raw=open_raw(NOW,quantity='7');raw['open']['target_usd']='101'
    decision=AlphaDecision.model_validate(raw)
    intent,plan=translate_open(decision,context,controller.account_id,NOW,'target-astra-authority',
        capability_version=controller.state['config']['capabilities']['version'])
    nonce=controller.admit(intent,plan);args['gateway'].dispatch(intent,nonce,item,controller,args['calendar'].session(NOW.date()),lambda:NOW)
    touched=replace(item,bid=D('101'),ask=D('101.01'),positions={item.instrument_id:'7'},
        settled=D(controller.state['cash']['settled_usd']),unsettled=D(controller.state['cash']['unsettled_usd']),
        equity=D(controller.state['cash']['liquidation_equity_usd']))
    touched_universe=replace(universe((touched,),positions={item.instrument_id:'7'}),settled=touched.settled,
        unsettled=touched.unsettled,equity=touched.equity);controller.refresh(touched_universe)
    class Store:
        def alpha_plan(self,_):return plan.model_dump(mode='json')
    events=[]
    def review(*_):
        return ({'decision':{'schema_version':'1.0','action':'HOLD','reason':'Current evidence supports holding.',
            'open':None,'hold':{'position_id':'position-'+item.instrument_id,
                'next_review_at':(NOW+timedelta(minutes=2)).isoformat()},'modify':None,'close':None},
            'estimated_cost_usd':'0.01'},None)
    manager=AlphaPortfolioManager(Store(),review=review,emit=lambda k,p:events.append((k,p)),
        exit_manager=LocalPositionManager(monitor_dry_run=True))
    manager.tick(touched_universe,controller,args['gateway'],args['calendar'].session(NOW.date()),lambda:NOW)
    assert len(broker.orders)==1 and controller.state['positions']
    assert any(k=='ALPHA_POSITION_REVIEW_REQUESTED' and p['target_reassessment'] for k,p in events)
    manager.tick(touched_universe,controller,args['gateway'],args['calendar'].session(NOW.date()),lambda:NOW)
    assert len(broker.orders)==1 and controller.state['positions']
    assert any(k=='ALPHA_POSITION_REVIEW' and p['action']=='HOLD' for k,p in events)


def test_held_stream_wiggle_waits_for_astra_review_horizon_and_stale_is_reported(setup_cycle):
    from concurrent.futures import Future
    args,broker,_=setup_cycle;controller=args['controller'];item=observation('stock-b','BBB')
    initial=universe((item,observation('stock-c','CCC')));controller.refresh(initial)
    context=build_alpha_context(initial,controller.state,NOW)
    intent,plan=translate_open(AlphaDecision.model_validate(open_raw(NOW,quantity='7')),context,
        controller.account_id,NOW,'stream-position-review',capability_version=controller.state['config']['capabilities']['version'])
    nonce=controller.admit(intent,plan);args['gateway'].dispatch(intent,nonce,item,controller,args['calendar'].session(NOW.date()),lambda:NOW)
    held=replace(item,positions={item.instrument_id:'7'},stream_material_signature=(('quote',1),),
        settled=D(controller.state['cash']['settled_usd']),unsettled=D(controller.state['cash']['unsettled_usd']),
        equity=D(controller.state['cash']['liquidation_equity_usd']))
    held_universe=replace(universe((held,),positions=held.positions),settled=held.settled,unsettled=held.unsettled,equity=held.equity)
    controller.refresh(held_universe)
    class Store:
        def alpha_plan(self,_):return plan.model_dump(mode='json')
    events=[];requests=[]
    manager=AlphaPortfolioManager(Store(),review=lambda *_:None,emit=lambda k,p:events.append((k,p)),
        exit_manager=LocalPositionManager(monitor_dry_run=True))
    manager._submit_review=lambda *args:(requests.append(args) or Future())
    session=args['calendar'].session(NOW.date())
    manager.tick(held_universe,controller,args['gateway'],session,lambda:NOW)
    assert not requests
    changed=replace(held_universe,candidates=(replace(held,stream_material_signature=(('quote',2),)),))
    manager.tick(changed,controller,args['gateway'],session,lambda:NOW)
    manager.tick(changed,controller,args['gateway'],session,lambda:NOW)
    assert not requests and len(broker.orders)==1
    due=plan.next_review_at
    due_held=replace(held,stream_material_signature=(('quote',2),),quote_at=due,received_at=due,quote_received_at=due)
    due_universe=replace(changed,candidates=(due_held,),received_at=due)
    manager.tick(due_universe,controller,args['gateway'],session,lambda:due)
    assert len(requests)==1 and len(broker.orders)==1
    assert any(k=='ALPHA_POSITION_REVIEW_REQUESTED' and p['scheduled_reassessment'] for k,p in events)
    stale=replace(due_universe,candidates=(replace(due_held,quote_at=due-timedelta(seconds=7)),))
    manager.tick(stale,controller,args['gateway'],session,lambda:due)
    assert len(requests)==1 and any(k=='POSITION_MARKET_DATA_FAILURE' for k,p in events)


def test_tightened_stop_may_lock_profit_above_entry():
    raw={'plan_id':'plan','instrument_id':'stock','symbol':'STOCK','episode_id':'episode',
        'strategy_label':'astra-managed','thesis':'Current evidence supports the position.','quantity':'10',
        'entry_limit_usd':'79.50','original_stop_usd':'78.92','active_stop_usd':'79.70',
        'target_usd':'80.65','entry_expires_at':(NOW+timedelta(minutes=1)).isoformat(),
        'next_review_at':(NOW+timedelta(minutes=3)).isoformat(),
        'original_latest_exit_at':(NOW+timedelta(minutes=5)).isoformat(),
        'latest_exit_at':(NOW+timedelta(minutes=5)).isoformat()}
    assert AlphaManagedPlan.model_validate(raw).active_stop_usd==D('79.70')


def test_due_durable_stop_exits_before_invalid_alpha_plan_validation(setup_cycle):
    args,broker,_=setup_cycle;controller=args['controller'];item=observation('stock-b','BBB')
    initial=universe((item,observation('stock-c','CCC')));controller.refresh(initial)
    context=build_alpha_context(initial,controller.state,NOW)
    decision=AlphaDecision.model_validate(open_raw(NOW,quantity='7'))
    intent,plan=translate_open(decision,context,controller.account_id,NOW,'invalid-plan-safety-exit',
        capability_version=controller.state['config']['capabilities']['version'])
    nonce=controller.admit(intent,plan);args['gateway'].dispatch(intent,nonce,item,controller,args['calendar'].session(NOW.date()),lambda:NOW)
    high=replace(item,bid=D('102'),ask=D('102.01'),positions={item.instrument_id:'7'},
        settled=D(controller.state['cash']['settled_usd']),unsettled=D(controller.state['cash']['unsettled_usd']),
        equity=D(controller.state['cash']['liquidation_equity_usd']))
    controller.refresh(replace(universe((high,),positions={item.instrument_id:'7'}),settled=high.settled,
        unsettled=high.unsettled,equity=high.equity))
    controller.send(Protection(instrument_id=item.instrument_id,quantity='7',stop_usd='101',evidence_id='tightened-profit-stop'))
    below=replace(item,bid=D('100.50'),ask=D('100.51'),positions={item.instrument_id:'7'},
        settled=D(controller.state['cash']['settled_usd']),unsettled=D(controller.state['cash']['unsettled_usd']),
        equity=D(controller.state['cash']['liquidation_equity_usd']))
    held=replace(universe((below,),positions={item.instrument_id:'7'}),settled=below.settled,
        unsettled=below.unsettled,equity=below.equity);controller.refresh(held)
    invalid=plan.model_dump(mode='json');invalid['target_usd']='99'
    class Store:
        def alpha_plan(self,_):return invalid
    events=[];manager=AlphaPortfolioManager(Store(),review=lambda *_:(_ for _ in ()).throw(
        AssertionError('model work must not precede due safety exit')),emit=lambda k,p:events.append((k,p)),
        exit_manager=LocalPositionManager(monitor_dry_run=True,emit=lambda k,p:events.append((k,p))))
    result=manager.tick(held,controller,args['gateway'],args['calendar'].session(NOW.date()),lambda:NOW)
    assert result['reason']=='STOP' and not controller.state['positions']
    assert len(broker.orders)==2 and list(broker.orders.values())[-1].quantity==D('7')
    assert any(k=='ALPHA_OUTCOME_DEFERRED' for k,_ in events)


def test_terminal_canceled_exits_do_not_block_one_exact_share_safety_exit():
    item=observation('stock-b','BBB');sent=[];dispatched=[]
    intent=SimpleNamespace(instrument_id=item.instrument_id,account_id='account',plan_id='plan',episode_id='episode',
        capability_version='capability')
    class Controller:
        account_id='account'
        state={'positions':{item.instrument_id:{'quantity':'7'}},
            'orders':{'old-1':{'final':True,'phase':'CANCELED'},'old-2':{'final':True,'phase':'CANCELED'}}}
        def send(self,command):
            sent.append(command);return {'outbox':{command.intent.intent_id:{'nonce':'one-use'}}}
    class Gateway:
        def dispatch(self,exit_intent,nonce,*_):
            dispatched.append((exit_intent,nonce));return {'status':'FILLED','filled_price':str(item.bid)}
    manager=LocalPositionManager(monitor_dry_run=True)
    result=manager._exit_owned(intent,item,Gateway(),Controller(),
        __import__('autonomous_trader.mvp.config',fromlist=['reviewed_calendar']).reviewed_calendar().session(NOW.date()),
        lambda:NOW,'STOP',['STOP'])
    assert result['status']=='FILLED' and len(sent)==len(dispatched)==1
    assert dispatched[0][0].side=='SELL_OWNED' and dispatched[0][0].quantity==D('7')


def test_nonterminal_or_unknown_order_blocks_duplicate_safety_exit():
    item=observation('stock-b','BBB')
    intent=SimpleNamespace(instrument_id=item.instrument_id,account_id='account',plan_id='plan',episode_id='episode',
        capability_version='capability')
    class Controller:
        account_id='account'
        state={'positions':{item.instrument_id:{'quantity':'7'}},'orders':{'uncertain':{'final':False,'phase':'UNKNOWN'}}}
    with pytest.raises(ValueError,match='EXIT_ALREADY_PENDING'):
        LocalPositionManager(monitor_dry_run=True)._exit_owned(intent,item,SimpleNamespace(),Controller(),
            __import__('autonomous_trader.mvp.config',fromlist=['reviewed_calendar']).reviewed_calendar().session(NOW.date()),
            lambda:NOW,'STOP',['STOP'])
