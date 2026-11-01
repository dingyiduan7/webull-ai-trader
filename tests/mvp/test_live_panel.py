from copy import deepcopy
from datetime import datetime,timedelta,timezone
from decimal import Decimal as D
from autonomous_trader.monitoring.live_panel import project_panel

NOW=datetime(2026,9,24,18,tzinfo=timezone.utc)

def fixture():
    r={'system':{'heartbeat':NOW.isoformat(),'postgres':'CONNECTED','market':'OPEN','pipeline':{}},
        'account':{'daily_realized':D('8'),'reconciliation':{'ready':True,'settlement_known':True,
            'valid_until':(NOW+timedelta(seconds=10)).isoformat()}},'risk':{'latches':{}},
        'positions':[],'trades':[],'api':{'day_known_cost':D('2'),'unknown_cost_calls':0}}
    s={'orders':{},'executions':{},'permits':{},'positions':{},'instruments':{}}
    e={'rows':[],'discovery':(NOW,{'unique_symbols':1000,'shortlisted_symbols':30}),
        'scan_totals':(10,10000,300),'jobs':{},'metrics':{},'outcomes':[],'missed':None}
    return r,s,e

def test_services_connected_cannot_hide_budget_block():
    r,s,e=fixture();r['system'].update(webull='CONNECTED',openai='CONNECTED',status='RUNNING')
    e['rows']=[('denied',NOW,'ALPHA_DEEP_PLAN_FAILED',{'reason':'MODEL_MONTHLY_BUDGET_EXHAUSTED'})]
    p=project_panel(r,s,[],e,NOW)
    assert p['status']=='BLOCKED' and any('MODEL_MONTHLY_BUDGET_EXHAUSTED' in x for x in p['blockers'])

def test_successful_provider_claim_supersedes_older_budget_denial():
    r,s,e=fixture();e['rows']=[('denied',NOW-timedelta(seconds=3),'ALPHA_DEEP_PLAN_FAILED',{'reason':'MODEL_MONTHLY_BUDGET_EXHAUSTED'}),
        ('claim',NOW,'MODEL_WORK_CLAIM',{'call_type':'FAST_ENTRY'})]
    assert project_panel(r,s,[],e,NOW)['status']=='SCANNING'

def test_submit_attempt_is_not_broker_acceptance_and_cancel_is_not_fill():
    r,s,e=fixture();s['instruments']={'i':{'ticker':'TEST'}}
    s['orders']={'o':{'intent':{'intent_id':'o','instrument_id':'i','plan_id':'p','side':'BUY','quantity':'2','limit_price_usd':'10'},'phase':'UNKNOWN','final':False}}
    s['permits']={'nonce':{'intent_id':'o'}}
    journal=[{'logical_at':NOW.isoformat(),'command':{'kind':'CONSUME','nonce':'nonce'}}]
    p=project_panel(r,s,journal,e,NOW)
    assert p['funnel']['submission_attempts']==1 and p['funnel']['submitted']==p['funnel']['filled']==0
    assert 'BROKER_ORDER_STATE_UNCERTAIN' in p['blockers']
    journal.append({'logical_at':NOW.isoformat(),'command':{'kind':'ORDER_OBSERVATION','intent_id':'o','status':'ACKNOWLEDGED'}})
    s['orders']['o'].update(phase='CANCELED',final=True)
    p=project_panel(r,s,journal,e,NOW)
    assert p['funnel']['submitted']==1 and p['funnel']['filled']==0


def test_no_fee_double_counting_or_invented_latency():
    r,s,e=fixture();s['executions']={'x':{'intent_id':'sell','executed_at':NOW.isoformat(),'fee_usd':'1'}}
    s['orders']={'sell':{'intent':{'side':'SELL_OWNED'},'final':True}}
    p=project_panel(r,s,[],e,NOW)
    assert p['economics']['broker_fees_today']==1
    assert p['economics']['net_after_known_costs']==6
    assert not p['latency']


def test_position_excursions_are_loaded_from_matching_durable_plan():
    r,s,e=fixture();r['positions']=[{'ticker':'TEST'}]
    s['positions']={'i':{'originating_orders':['o']}};s['instruments']={'i':{'ticker':'TEST'}}
    s['orders']={'o':{'intent':{'plan_id':'p','side':'BUY'},'final':True}}
    e['metrics']={'p':{'maximum_favorable_excursion_usd':'50','maximum_adverse_excursion_usd':'4'}}
    p=project_panel(r,s,[],e,NOW)
    assert r['positions'][0]['mfe']=='50' and r['positions'][0]['mae']=='4' and p['status']=='MANAGING'


def test_closed_session_and_expired_reconciliation_are_explicit():
    r,s,e=fixture();r['system']['market']='CLOSED'
    assert project_panel(r,s,[],e,NOW)['blockers'][0]=='SESSION_CLOSED'
    r['system']['market']='OPEN';r['account']['reconciliation']['valid_until']=(NOW-timedelta(seconds=1)).isoformat()
    r['system']['heartbeat']=(NOW-timedelta(seconds=60)).isoformat()
    assert 'RECONCILIATION_EXPIRED' in project_panel(r,s,[],e,NOW)['blockers']
    r['system']['heartbeat']=NOW.isoformat();r['account']['reconciliation']['ready']=False
    assert 'RECONCILIATION_EXPIRED' in project_panel(r,s,[],e,NOW)['blockers']


def test_lapsed_quote_window_with_live_trader_is_not_blocked():
    # 2026-10-14 snapshot: fresh heartbeat, reconciliation ready, valid_until 30 s in the past, market OPEN.
    r,s,e=fixture();r['system'].update(market='OPEN',heartbeat=NOW.isoformat())
    r['account']['reconciliation'].update(ready=True,valid_until=(NOW-timedelta(seconds=30)).isoformat())
    p=project_panel(r,s,[],e,NOW)
    assert p['status']!='BLOCKED' and 'RECONCILIATION_EXPIRED' not in p['blockers']


def test_unknown_opportunity_metrics_remain_unknown_and_first_seen_is_preserved():
    r,s,e=fixture();e['jobs']={'j':{'symbol':'TEST','current':True,'rank':7,'first_at':NOW.isoformat(),'session':'2026-09-24'}}
    p=project_panel(r,s,[],e,NOW);o=p['opportunities'][0]
    assert o['first_detected']==NOW.isoformat() and o['rank']==7
    assert o['move_percent'] is None and o['spread_bps'] is None and o['quote_age_seconds'] is None


def test_learning_uses_exploratory_samples_and_excludes_recovery_from_strategy_score():
    r,s,e=fixture();base={'strategy_label':'test','market_regime':'test','closed_at':NOW.isoformat(),
        'realized_net_after_all_known_costs_usd':'-5','evidence_quality':'FULL_RUNTIME','exit_reason':'ASTRA_CLOSE'}
    e['outcomes']=[dict(base,plan_id='one'),dict(base,plan_id='two',exit_reason='SAFETY_LATCH')]
    g=project_panel(r,s,[],e,NOW)['learning']['groups'][0]
    assert g['samples']==2 and g['strategy_evidence_samples']==1 and g['sample_confidence']=='EXPLORATORY'
    assert g['net_after_all_known_costs_usd']=='-5'


def test_known_order_is_sent_and_model_work_is_deciding():
    r,s,e=fixture();r['system']['pipeline']={'in_flight':1}
    assert project_panel(r,s,[],e,NOW)['status']=='ASTRA DECIDING'
    s['orders']={'o':{'intent':{'side':'BUY'},'phase':'ACKNOWLEDGED','final':False}}
    assert project_panel(r,s,[],e,NOW)['status']=='ORDER SENT'


def test_heartbeat_committed_during_snapshot_read_is_not_stale():
    r,s,e=fixture();r['system']['heartbeat']=(NOW+timedelta(milliseconds=5)).isoformat()
    assert 'TRADER_HEARTBEAT_STALE' not in project_panel(r,s,[],e,NOW)['blockers']
    r['system']['heartbeat']=(NOW-timedelta(seconds=21)).isoformat()
    assert 'TRADER_HEARTBEAT_STALE' in project_panel(r,s,[],e,NOW)['blockers']


def test_money_path_does_not_equate_scanning_with_fills():
    r,s,e=fixture()
    p=project_panel(r,s,[],e,NOW)
    assert p['money_path']=='NO_FILLS: NO_BUY_EXECUTION_RECORDED'
    assert [x['stage'] for x in p['stages']]==['Scan','Shortlist','Astra','OPEN','Broker','Fill','Manage','Exit','Learning']
    assert p['economics']['api_share_of_profit_percent']==25
    r['account']['daily_realized']=D('0')
    assert project_panel(r,s,[],e,NOW)['economics']['api_share_of_profit_percent'] is None


def test_closed_trade_memory_is_matched_to_plan_when_projection_follows_fill():
    r,s,e=fixture();r['trades']=[{'ticker':'TEST','decision_id':'p','exit_time':NOW.isoformat()}]
    e['metrics']={'p':{'recorded_at':NOW.isoformat()}}
    p=project_panel(r,s,[],e,NOW)
    assert p['funnel']['managed']==p['funnel']['exited']==1
    assert p['learning']['closed_trade_updates'][0]['status']=='MEMORY_UPDATE_NOT_RECORDED'
    e['outcomes']=[{'symbol':'TEST','plan_id':'p','closed_at':(NOW+timedelta(milliseconds=750)).isoformat(),
        'realized_net_after_all_known_costs_usd':'6'}]
    p=project_panel(r,s,[],e,NOW)
    assert p['learning']['closed_trade_updates'][0]['status']=='RECORDED'
    assert p['funnel']['memory_recorded']==1


def test_detection_to_fill_uses_linked_execution_not_broker_ack():
    r,s,e=fixture();start=NOW-timedelta(seconds=20);detected=NOW-timedelta(seconds=25)
    e['rows']=[('d',NOW-timedelta(seconds=5),'ALPHA_DECISION',{'decision_id':'p','ai':{'decision':{'action':'OPEN','open':{'instrument_id':'i','symbol':'TEST'}},'latency_ms':15000},'latency_trace':{'model_started_at':start.isoformat(),'hot_signal_at':detected.isoformat(),'model_handled_at':(NOW-timedelta(seconds=5)).isoformat()}})]
    s['instruments']={'i':{'ticker':'TEST'}}
    s['orders']={'o':{'intent':{'instrument_id':'i','plan_id':'p','side':'BUY','quantity':'1','limit_price_usd':'10'},'phase':'FILLED','final':True}}
    s['permits']={'n':{'intent_id':'o'}}
    s['executions']={'x':{'intent_id':'o','executed_at':NOW.isoformat(),'fee_usd':'0'}}
    journal=[{'logical_at':(NOW-timedelta(seconds=4)).isoformat(),'command':{'kind':'CONSUME','nonce':'n'}}]
    p=project_panel(r,s,journal,e,NOW)
    assert p['latency'][0]['total_s']==25 and p['latency'][0]['order_to_fill_s']==4
    assert p['money_path']=='FILLS_RECORDED'


def test_owner_budget_increase_supersedes_historical_denial():
    r,s,e=fixture();s['economic_policy']={'origin':'OWNER_MODEL_BUDGET_CAP','effective_at':NOW.isoformat()}
    e['rows']=[('denied',NOW-timedelta(seconds=1),'ALPHA_DEEP_PLAN_FAILED',{'reason':'MODEL_MONTHLY_BUDGET_EXHAUSTED'})]
    p=project_panel(r,s,[],e,NOW)
    assert not any('BUDGET_EXHAUSTED' in b for b in p['blockers'])
    assert p['last_model_denial']['reason']=='MODEL_MONTHLY_BUDGET_EXHAUSTED'


def test_real_pipeline_decisions_and_activity_are_shown():
    """2026-10-19: in REAL mode the panel showed NO DECISION / NO_RECENT_DISCOVERY_EVIDENCE while trading."""
    r,s,e=fixture();e['discovery']=None;e['scan_totals']=(0,0,0)
    e['real_decisions']=[('d1',NOW-timedelta(seconds=60),{'action':'ABSTAIN','reason':'No clear edge.','symbol':None,'candidates':8}),
        ('d2',NOW-timedelta(seconds=10),{'action':'OPEN','reason':'VWAP reclaim on volume.','symbol':'ABC','candidates':8})]
    e['real_activity']=NOW-timedelta(seconds=10)
    p=project_panel(r,s,[],e,NOW)
    assert p['status']=='SCANNING' and 'NO_RECENT_DISCOVERY_EVIDENCE' not in p['blockers']
    assert p['decision_counts']['ABSTAIN']==1 and p['decision_counts']['OPEN']==1
    assert p['decisions'][0]['symbol']=='ABC' and p['funnel']['scanned']==8 and p['funnel']['sent_to_astra']==2


def test_real_market_data_wait_is_not_a_block():
    r,s,e=fixture();e['discovery']=None;e['real_wait']=NOW-timedelta(seconds=30)
    p=project_panel(r,s,[],e,NOW)
    assert p['status']=='WAITING FOR MARKET DATA' and not p['blockers']


def test_two_brain_flow_rows():
    r,s,e=fixture();e['discovery']=None
    t1,t2=NOW-timedelta(seconds=90),NOW-timedelta(seconds=20)
    e['real_decisions']=[('d1',t1,{'action':'ABSTAIN','reason':'No edge.','candidates':8,'snapshot':t1.isoformat(),'symbols':['AAA','BBB']}),
        ('d2',t2,{'action':'ABSTAIN','reason':'Opus liked ABC; Astra disagreed.','candidates':8,'snapshot':t2.isoformat(),'symbols':['ABC'],'decision_id':'x2'})]
    e['real_dual']=[(t2,{'snapshot':t2.isoformat(),'opus':'OPEN','opus_instrument':'abc','astra':'ABSTAIN','astra_reason':'Volume pace below 1.','consensus':'ABSTAIN'})]
    s['instruments']={'abc':{'ticker':'ABC'}}
    p=project_panel(r,s,[],e,NOW);f=p['flow']
    assert f[0]['opus']=='OPEN' and f[0]['opus_symbol']=='ABC' and f[0]['astra']=='ABSTAIN' and f[0]['outcome']=='VETOED BY ASTRA'
    assert f[1]['opus']=='ABSTAIN' and f[1]['astra']=='NOT ASKED' and f[1]['outcome']=='NO TRADE' and f[1]['shortlist']==['AAA','BBB']
