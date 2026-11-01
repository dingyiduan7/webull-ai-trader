from datetime import datetime,timezone,timedelta
from decimal import Decimal as D
from types import SimpleNamespace
from pathlib import Path
from uuid import uuid4
import json,threading,urllib.request,urllib.error
import pytest
import socket
_ORIGINAL_CONNECT=socket.socket.connect
_ORIGINAL_CREATE=socket.create_connection
from test_cycle import setup_cycle,SyntheticBrain,NOW
from autonomous_trader.mvp.config import RuntimeConfig,ExecutionMode,reviewed_calendar
from autonomous_trader.mvp.context import Observation
from autonomous_trader.mvp.gateway import LocalPositionManager
from autonomous_trader.monitoring.runner import Engine,recover_plan,next_entry_time
from autonomous_trader.monitoring.views import (DashboardReader,account_view,risk_view,trades_from_state,
    historic_trade,decision_index,bounded_series)
from autonomous_trader.monitoring.server import DashboardServer
from autonomous_trader.monitoring.store import compact_performance_memory,compact_shadow_memory,compact_baseline_memory

class MemoryStore:
    def __init__(self):self.events=[];self.last={}
    def runtime(self):return self.last
    def heartbeat(self,p):self.last=dict(p)
    def emit(self,k,p,**kw):self.events.append((k,p,kw))
    def plan(self,did):return next((p['ai']['decision'] for k,p,_ in self.events if k=='ASTRA_DECISION' and p['ai'].get('decision',{}).get('decision_id')==did),None)

@pytest.fixture
def engine(setup_cycle,tmp_path):
    args,broker,ledger=setup_cycle;store=MemoryStore();e=Engine(tmp_path,RuntimeConfig(),store,clock=lambda:NOW,interval=300)
    e.controller=args['controller'];e.gateway=args['gateway'];e.monitor=SimpleNamespace(ready=True,manage=lambda *a:None)
    e.brain_factory=lambda:SyntheticBrain(action='ABSTAIN')
    e.quote=D('99.99')
    class Source:
        def refresh(self,state):
            return Observation('913256135',e.quote,e.quote+D('.01'),e.clock(),e.clock(),D(state['cash']['settled_usd']),D(state['cash']['unsettled_usd']),D(state['cash']['liquidation_equity_usd']),{k:v['quantity'] for k,v in state['positions'].items()},tuple(k for k,v in state['orders'].items() if not v['final']),uuid4().hex)
    e.source=Source();e.controller.clock=lambda:e.clock()
    e.initialized=True
    return e,broker,store

def test_closed_to_open_calls_existing_cycle_without_approval(engine,monkeypatch):
    e,broker,store=engine
    e.clock=lambda:datetime(2026,9,11,13,15,tzinfo=timezone.utc)
    # Fresh fixture logical time cannot move backward; advance from Friday to Monday premarket.
    e.clock=lambda:datetime(2026,9,14,13,15,tzinfo=timezone.utc)
    e.step();assert not store.events and not broker.orders
    assert e.state['activity']=='Waiting for regular entry session'
    calls=[]
    def cycle(**kwargs):
        calls.append(kwargs);return {'astra_action':'ABSTAIN','blocker':None}
    monkeypatch.setattr('autonomous_trader.monitoring.runner.run_trading_cycle',cycle)
    e.clock=lambda:datetime(2026,9,14,13,30,tzinfo=timezone.utc)
    e.step();assert len(calls)==1
    e.step();assert len(calls)==1
    assert e.controller.state['session']['label']=='2026-09-14'

def test_entry_decision_persisted_then_restore_stop_without_new_astra(engine):
    e,broker,store=engine;e.brain_factory=lambda:SyntheticBrain()
    e.step();assert len(broker.orders)==1
    intent,plan=recover_plan(e.controller.state,store)
    assert intent.quantity==1 and plan.protection.initial_stop_usd==99
    e.quote=D('98');e.monitor=LocalPositionManager(monitor_dry_run=True,sleep=lambda _:None)
    e.brain_factory=lambda:pytest.fail('Existing holding must not call Astra')
    e.step();assert len(broker.orders)==2 and not e.controller.state['positions']
    assert len([1 for k,_,_ in store.events if k=='ASTRA_DECISION'])==1

def test_missing_persisted_plan_blocks_recovery(engine):
    e,broker,store=engine;e.brain_factory=lambda:SyntheticBrain();e.step();store.events=[]
    with pytest.raises(ValueError,match='PERSISTED_VALIDATED_PLAN_MISSING'):e.step()
    assert len(broker.orders)==1

def test_after_cutoff_still_manages_holding(engine):
    e,broker,store=engine;e.brain_factory=lambda:SyntheticBrain();e.step()
    e.clock=lambda:NOW.replace(hour=19,minute=56)
    calls=[];e.monitor=SimpleNamespace(manage=lambda *args:calls.append(args))
    e.step();assert len(calls)==1 and len(broker.orders)==1

def test_closed_holding_never_dispatches(engine):
    e,broker,_=engine;e.brain_factory=lambda:SyntheticBrain();e.step();e.clock=lambda:NOW.replace(hour=21)
    e.step();assert e.state['last_error']=='OPEN_POSITION_OUTSIDE_SESSION' and len(broker.orders)==1

def test_uncertain_order_blocks_new_brain(engine):
    e,broker,_=engine
    # Durable incident is enough to block entries; never clear it in the scheduler.
    e.controller.lease();e.controller.incident('TEST_UNKNOWN_BROKER_STATE')
    e.brain_factory=lambda:pytest.fail('Latched account must not call model')
    e.step();assert e.state['status']=='ERROR' and not broker.orders

def test_runner_error_does_not_die(engine):
    e,_,store=engine
    e.step=lambda:(_ for _ in ()).throw(ValueError('STALE_ACCOUNT'))
    class Stop:
        count=0
        def is_set(self):return self.count>=2
        def clear(self):pass
        def wait(self,_):self.count+=1
    stop=Stop();e.wake_event=stop;e.run(stop);assert len([k for k,_,_ in store.events if k=='ERROR'])==2
    assert store.last['status']=='ERROR'

def test_runner_generic_error_records_only_safe_exception_class(engine):
    e,_,store=engine
    e.record_error(TypeError('unsafe arbitrary detail'))
    assert e.state['last_error']=='RUNNER_STEP_FAILED TYPEERROR'
    assert 'unsafe' not in store.events[-1][1]['summary']

def test_risk_and_pnl_views_use_m2_state(engine):
    e,_,_=engine;e.step();s=e.controller.state
    a=account_view(s,'2026-09-11');r=risk_view(s)
    assert a['eligible_cash']==D('1000000') and a['experiment_pnl']==0
    assert [row['limit_percent'] for row in r['rows']]==[100,40,20,5,20,90]   # open risk 20% (owner 2026-10-16)
    assert r['planned_trade_limit_percent']==D('10')
    assert r['daily_downside_active'] is False
    assert r['aggregate_preference_ceiling_percent']==D('20')

def test_trade_analytics_use_effective_fifo_and_fees(engine):
    e,_,store=engine;e.brain_factory=lambda:SyntheticBrain();e.step();e.quote=D('98');e.monitor=LocalPositionManager(monitor_dry_run=True,sleep=lambda _:None);e.step()
    trades=trades_from_state(e.controller.state,{})
    assert len(trades)==1 and trades[0]['quantity']==1 and trades[0]['net']==D('-2.20')
    assert trades[0]['fees']==0

def test_historical_certification_trade_not_fake():
    t=historic_trade(Path(__file__).parents[2]);assert t['net']==D('-.03') and t['fees']==D('.02')

def test_dashboard_accepts_historical_null_decision_evidence():
    decision={'decision_id':'valid-id','action':'ABSTAIN'}
    assert decision_index(({'decision':None},{'decision':decision},None))=={'valid-id':decision}

def test_dashboard_bounds_chart_payload_and_preserves_endpoints():
    rows=[{'at':str(i),'equity':str(i)} for i in range(2400)]
    bounded=bounded_series(rows,600)
    assert len(bounded)==600 and bounded[0]==rows[0] and bounded[-1]==rows[-1]

def test_http_get_only_host_bound_and_automatic_refresh(monkeypatch):
    def local_connect(sock,address):
        assert address[0]=='127.0.0.1'
        return _ORIGINAL_CONNECT(sock,address)
    monkeypatch.setattr(socket.socket,'connect',local_connect)
    monkeypatch.setattr(socket,'create_connection',_ORIGINAL_CREATE)
    class Reader:
        def snapshot(self):return {'system':{'status':'RUNNING'},'test':True}
    server=DashboardServer(Reader(),0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    base=f'http://127.0.0.1:{server.server_port}'
    try:
        with urllib.request.urlopen(base+'/api/snapshot') as r:assert json.load(r)['test']
        with urllib.request.urlopen(base) as r:
            html=r.read().decode();assert 'setTimeout(refresh,2000)' in html and 'textContent' in html and 'innerHTML' not in html
            assert 'new AbortController()' in html and 'AbortSignal.timeout' not in html and 'rows.at(' not in html
            assert '<details>' in html and 'Details &amp; history' in html and 'id="status"' in html
            assert 'function brief(' in html and 'Not enough time left to safely complete a new entry' in html
            assert 'Dashboard cannot reach its server' in html and 'Market closed' in html
            assert 'US liquid-stock MVP universe' not in html and 'M2' not in html and 'M3' not in html
        for req in (urllib.request.Request(base+'/api/snapshot',method='POST'),urllib.request.Request(base,headers={'Host':'malicious.example'})):
            with pytest.raises(urllib.error.HTTPError) as exc:urllib.request.urlopen(req)
            assert exc.value.code in (403,405)
        with pytest.raises(urllib.error.HTTPError):urllib.request.urlopen(base+'/.secrets/webull-sandbox.clixml')
    finally:server.shutdown();server.server_close()

def test_denver_session_holiday_and_weekend():
    cal=reviewed_calendar()
    assert next_entry_time(cal,datetime(2026,9,12,13,tzinfo=timezone.utc))==datetime(2026,9,14,13,30,tzinfo=timezone.utc)
    assert cal.session(datetime(2026,11,26).date()) is None

def test_scheduler_xml_and_no_production():
    import xml.etree.ElementTree as ET
    root=Path(__file__).parents[2];text=(root/'.local/mvp/task-preview.xml').read_text(encoding='utf-8-sig')
    task=ET.fromstring(text);ns={'t':'http://schemas.microsoft.com/windows/2004/02/mit/task'}
    assert task.findtext('t:Settings/t:WakeToRun',namespaces=ns)=='true'
    assert task.findtext('t:Settings/t:RestartOnFailure/t:Count',namespaces=ns)=='999'
    assert task.findtext('t:Settings/t:ExecutionTimeLimit',namespaces=ns)=='PT0S'
    times=[x.text for x in task.findall('t:Triggers/t:CalendarTrigger/t:StartBoundary',ns)]
    assert times==['2026-01-01T13:15:00Z','2026-01-01T14:15:00Z']
    args=task.findtext('t:Actions/t:Exec/t:Arguments',namespaces=ns)
    assert '-Mode SANDBOX' in args and '-Wait' in args and '-Scheduled' in args
    assert 'Password'==task.findtext('t:Principals/t:Principal/t:LogonType',namespaces=ns)
    assert 'OPENAI_API_KEY=' not in text and 'APP_SECRET=' not in text
    launcher=(root/'scripts/start_ai_trader.ps1').read_text()
    assert '} while ($Wait)' in launcher and 'Start-Sleep -Seconds 60' in launcher

def test_monitor_store_is_append_only_and_reader_cannot_write(setup_cycle):
    from autonomous_trader.monitoring.store import provision
    from autonomous_trader.durable.ledger import LocalDatabase
    import psycopg
    args,_,_=setup_cycle
    store=provision(args['controller'].ledger.config.schema)
    eid='offline-monitor-test-'+uuid4().hex
    store.emit('TEST',{'summary':'Offline permissions check'},mode='TEST',event_id=eid)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with store.db.connect() as c:c.execute('DELETE FROM events WHERE id=%s',(eid,))
    reader=LocalDatabase('postgres','mvp_reader',schema='mvp_monitor')
    with reader.connect() as c:assert c.execute('SELECT count(*) FROM events WHERE id=%s',(eid,)).fetchone()[0]==1
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with reader.connect() as c:c.execute("INSERT INTO events VALUES('forbidden',now(),'TEST','TEST','{}')")


def test_durable_position_metrics_and_dashboard_all_model_categories(setup_cycle):
    from autonomous_trader.monitoring.store import provision
    args,_,_=setup_cycle;store=provision(args['controller'].ledger.config.schema);mode='WEEK2_'+uuid4().hex
    metric={'summary':'checkpoint','plan_id':'plan-week2','instrument_id':'iid-week2','max_bid_usd':'105',
        'min_bid_usd':'98','maximum_favorable_excursion_usd':'50','maximum_adverse_excursion_usd':'20',
        'position_model_cost_usd':'.25','exit_reason':None}
    store.emit('ALPHA_POSITION_METRICS',metric,mode=mode,at=NOW)
    assert store.alpha_metrics('plan-week2','iid-week2')['max_bid_usd']=='105'
    store.emit('ASTRA_SELECTION',{'selection':{'estimated_cost_usd':'2'}},mode=mode,at=NOW)
    store.emit('SHADOW_AUDIT',{'audit_id':'shadow-week2','shadow':{'estimated_cost_usd':'1','audit':{
        'action':'ABSTAIN','preferred':None,'reasoning':'No positive payoff.'}}},mode=mode,at=NOW)
    assert store.shadow_audit('shadow-week2')['action']=='ABSTAIN'
    store.emit('MODEL_CALL_FAILED',{'ai':{'estimated_cost_usd':'.25'}},mode=mode,at=NOW)
    store.heartbeat({'status':'RUNNING','mode':mode,'ledger_schema':None,'account_id':None})
    snapshot=DashboardReader(Path(__file__).parents[2]).snapshot(NOW)
    assert snapshot['api']['total_known_cost']==D('3.25')
    assert snapshot['api']['by_call_type']=={'SHADOW_AUDIT':D('1'),'ASTRA_SELECTION':D('2'),'MODEL_CALL_FAILED':D('.25')}


def test_compact_performance_memory_keeps_economics_and_small_sample_exploratory():
    rows=[{'strategy_label':'adaptive setup','market_regime':'TRENDING','thesis':'current evidence','closed_at':f'2026-09-1{n}T20:00:00+00:00',
        'realized_net_after_broker_fees_usd':str(value+1),'realized_net_after_all_known_costs_usd':str(value),
        'return_on_capital':str(D(value)/1000),'holding_seconds':'300','maximum_favorable_excursion_usd':'20',
        'maximum_adverse_excursion_usd':'10','model_cost_usd':'1','capital_deployed_usd':'1000',
        'capital_time_usd_seconds':'300000','win_loss':'WIN' if value>0 else 'LOSS',
        'evidence_quality':'FULL_RUNTIME','exit_reason':'ASTRA_CLOSE'} for n,value in ((1,D('10')),(2,D('-4')))]
    calls=[{'ai':{'estimated_cost_usd':'2'}},{'deep':{'estimated_cost_usd':'3'}},
        {'selection':{'estimated_cost_usd':'4'}},{'shadow':{'estimated_cost_usd':'1'}}]
    memory=compact_performance_memory(rows,calls);portfolio=json.loads(memory[0]);group=json.loads(memory[1])['groups'][0]
    assert portfolio['completed_trades']==2 and portfolio['all_known_api_model_cost_usd']=='10'
    assert portfolio['economic_net_after_all_known_model_cost_usd']=='-2'
    assert group['samples']==2 and group['sample_confidence']=='EXPLORATORY'
    assert group['strategy_evidence_samples']==2 and group['nonvalidating_safety_recovery_or_legacy_samples']==0
    assert group['net_after_all_known_costs_usd']=='6' and group['wins']==1 and group['losses']==1
    assert group['model_cost_scope']=='PER_TRADE_COMPLETE'


def test_safety_recovery_and_legacy_profit_remain_economic_but_never_validate_strategy():
    rows=[{'plan_id':'aal-recovery','strategy_label':'descriptive-only','market_regime':'UNCLASSIFIED',
        'closed_at':'2026-09-17T13:30:12+00:00','realized_net_after_broker_fees_usd':'4495.30',
        'realized_net_after_all_known_costs_usd':'4495.30','return_on_capital':'.03579',
        'holding_seconds':'63702','maximum_favorable_excursion_usd':'0','maximum_adverse_excursion_usd':'0',
        'model_cost_usd':'0','capital_deployed_usd':'125600','capital_time_usd_seconds':'8000000000',
        'win_loss':'WIN','evidence_quality':'LEDGER_LEGACY_NO_EXCURSION','exit_reason':'LEGACY_UNRECORDED'}]
    memory=compact_performance_memory(rows);portfolio=json.loads(memory[0]);group=json.loads(memory[1])['groups'][0]
    assert portfolio['realized_net_after_broker_fees_usd']=='4495.3'
    assert group['samples']==1 and group['strategy_evidence_samples']==0
    assert group['nonvalidating_safety_recovery_or_legacy_samples']==1 and group['sample_confidence']=='NONE'


def test_shadow_missed_opportunity_memory_is_compact_and_non_authoritative():
    rows=[{'audit_id':f'a-{n}','started_at':NOW.isoformat(),'evaluated_at':(NOW+timedelta(minutes=5)).isoformat(),
        'shadow_action':'OPEN','shadow_symbol':'AAA','shadow_observed_return_bps':str(n),
        'astra_action':'ABSTAIN','astra_symbol':'BBB','astra_observed_return_bps':'0','winner':'SHADOW'} for n in range(20)]
    value=compact_shadow_memory(rows);parsed=json.loads(value)
    assert parsed['memory_type']=='shadow_opportunity_evidence'
    assert parsed['sample_policy']=='exploratory_until_repeated' and len(parsed['observations'])<=12
    assert len(value)<=1900 and all('order' not in row for row in parsed['observations'])


def test_baseline_missed_opportunity_memory_is_executable_side_and_exploratory():
    rows=[{'started_at':NOW.isoformat(),'evaluated_at':(NOW+timedelta(minutes=5)).isoformat(),
        'benchmarks':[{'name':'simple_momentum','symbol':'ARTL','entry_usd':'10.32','entry_reference':'ASK',
            'exit_usd':'12.25','gross_return_bps':'1870.15'},
            {'name':'top_mover_fixed_horizon','symbol':'ARTL','entry_usd':'10.32','entry_reference':'ASK',
            'exit_usd':'12.25','gross_return_bps':'1870.15'}]}]
    parsed=json.loads(compact_baseline_memory(rows))
    assert parsed['memory_type']=='missed_opportunity_counterfactuals'
    assert len(parsed['observations'])==1
    assert parsed['observations'][0]['entry_reference']=='ASK'
    assert parsed['observations'][0]['observed_executable_return_bps']=='1870.15'
    assert 'excludes slippage' in parsed['sample_policy']


def test_historical_orders_come_from_recorded_rest():
    from autonomous_trader.monitoring.views import historic_orders
    rows=historic_orders(Path(__file__).parents[2]);assert len(rows)==3
    assert sum(D(r['filled']) for r in rows)==2
    assert [r['status'] for r in rows]==['CANCELLED','FILLED','FILLED']

def test_validated_plan_survives_new_postgres_store_instance(engine):
    from autonomous_trader.monitoring.store import provision,Store
    e,broker,memory=engine;e.brain_factory=lambda:SyntheticBrain();e.step()
    store=provision(e.controller.ledger.config.schema)
    event=next(p for k,p,_ in memory.events if k=='ASTRA_DECISION')
    store.emit('ASTRA_DECISION',event,mode='TEST',event_id='persisted-plan-'+uuid4().hex)
    intent,plan=recover_plan(e.controller.state,Store())
    assert plan.protection.initial_stop_usd==99 and intent.quantity==1
    assert len(broker.orders)==1
