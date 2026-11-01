from types import SimpleNamespace
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import json
import pytest
from test_real_daily import fixture,NOW
from autonomous_trader import real_daily as daily
from autonomous_trader.real_waiting import service_receipt,clean_flat
from autonomous_trader.real_trading import RealGate,RealController
from autonomous_trader.real_runner import RealRunner
from scripts import run_real_daily as launcher
from scripts import run_real_trader as child
from scripts import run_real_supervisor as supervisor

def test_observe_receipt_never_grants_entry_and_requires_current_go(tmp_path,monkeypatch):
 a,save,bundle=fixture(tmp_path,monkeypatch)
 receipt=service_receipt(tmp_path,NOW)
 assert receipt['service_only'] and receipt['verdict']=='OBSERVE_ONLY'
 (tmp_path/'saved-waiting.json').write_text(json.dumps(receipt))
 with pytest.raises(ValueError):RealGate.offline_receipt(tmp_path,NOW)
 with pytest.raises(ValueError,match='ENTRY_AUTHORITY_REQUIRED'):
  RealGate(tmp_path,a['account_binding']).approved(receipt,NOW)
 a['revoked']=True;save()
 with pytest.raises(ValueError):service_receipt(tmp_path,NOW)

def make_runner(tmp_path,monkeypatch):
 a,_,bundle=fixture(tmp_path,monkeypatch);p,state,samples=bundle(NOW)
 account=dict(account_binding=a['account_binding'],observed_at=NOW,received_at=NOW,positions=(),open_client_ids=(),
  cash=dict(settled_cash='500',unsettled_cash='0',cash_balance='500',net_liquidation_value='500'))
 controller=SimpleNamespace(state=state,account_id=a['account_binding'],waiting_for_market_data=True,recovery_only=False)
 reconciled=[];controller.refresh_flat_recovery=lambda value:reconciled.append(value)
 runner=RealRunner.__new__(RealRunner);runner.controller=controller;runner.clock=lambda:NOW
 runner.source=SimpleNamespace(symbols=('FNGR','AMOD'),account_observation=lambda:account)
 runner.emit=lambda *a,**k:None;runner.pending=None
 failures=[];runner._reconciliation_failure=lambda exc,stage:failures.append((str(exc),stage)) or 'INCIDENT'
 runner._incident=lambda code:failures.append((code,'INCIDENT')) or 'INCIDENT'
 runner.worker=SimpleNamespace(submit=lambda *a:pytest.fail('No paid call while waiting'))
 return runner,a,p,state,samples,account,reconciled,failures

def test_stale_runner_waits_then_full_readable_certificate_promotes(tmp_path,monkeypatch):
 runner,a,p,state,samples,account,reconciled,failures=make_runner(tmp_path,monkeypatch)
 # Preserve both candidates; stale FNGR must not be removed to obtain certification.
 i=p.instruments[0].model_copy(update={'ticker':'FNGR'})
 j=i.model_copy(update={'instrument_id':'instrument-b','ticker':'AMOD'})
 q=p.context.instruments[0].model_copy(update={'symbol':'FNGR'})
 q2=q.model_copy(update={'instrument_id':'instrument-b','symbol':'AMOD'})
 p=replace(p,instruments=(i,j),context=p.context.model_copy(update={'instruments':(q,q2)}),
  quote_received_at={'instrument-a':NOW,'instrument-b':NOW},market_data_evidence={**p.market_data_evidence,'nasdaq_symbols':['FNGR','AMOD']})
 stale=[True]
 def prepare():
  rows=[dict(symbol=s,instrument_id={'FNGR':'instrument-a','AMOD':'instrument-b'}.get(s,s),
   bid='3.64',ask='3.66',quote_time=int((NOW-timedelta(days=1) if stale[0] and s in ('FNGR','AMOD') else NOW).timestamp()*1000))
   for s in ('FNGR','AMOD','AAPL','QQQ')]
  updated=launcher.aligned_preparation_packet(p,rows,NOW,NOW)
  daily.persist_certificate(tmp_path,daily.certificate(a,updated,state,samples,NOW,tmp_path))
  value,_=daily.current_certificate(tmp_path,a,NOW)
  daily.decode_packet(value['packet']).validate(NOW,entry=True)
  runner.authority_receipt=RealGate.offline_receipt(tmp_path,NOW)
  return updated
 runner.prepare_waiting_market=prepare
 runner._advance_packet=lambda packet,session,prepared=False: 'MARKET_DATA_READY' if prepared else pytest.fail('fresh packet not handed off')
 assert runner._wait_for_market_data()=='WAITING_FOR_MARKET_DATA'
 assert runner.source.symbols==('FNGR','AMOD') and reconciled and not failures
 with pytest.raises(ValueError,match='MARKET_DATA_NOT_READY'):runner._start(None,'ENTRY')
 with pytest.raises(ValueError,match='MARKET_DATA_NOT_READY'):
  RealController.admit(runner.controller,SimpleNamespace(side='BUY'),None)
 assert child.runtime_status(runner,{'result':'WAITING_FOR_MARKET_DATA'})=='WAITING_FOR_MARKET_DATA'
 stale[0]=False
 assert runner._wait_for_market_data()=='MARKET_DATA_READY'
 assert not runner.controller.waiting_for_market_data and runner.authority_receipt['verdict']=='GO'
 assert runner.source.symbols==('FNGR','AMOD') and not failures

@pytest.mark.parametrize('bad',['cash','broker-order','broker-position','local-position','local-order','latch'])
def test_account_or_exposure_failure_never_enters_market_wait(tmp_path,monkeypatch,bad):
 runner,a,p,state,samples,account,reconciled,failures=make_runner(tmp_path,monkeypatch)
 if bad=='cash':account['cash']['settled_cash']='499'
 if bad=='broker-order':account['open_client_ids']=('unknown',)
 if bad=='broker-position':account['positions']=({'symbol':'FNGR','quantity':'1'},)
 if bad=='local-position':state['positions']['instrument-a']={'quantity':'1'}
 if bad=='local-order':state['orders']['x']={'final':False,'phase':'UNKNOWN'}
 if bad=='latch':state['latches']['incident']=True
 runner.prepare_waiting_market=lambda:pytest.fail('Account failure must not prepare market')
 assert runner._wait_for_market_data()=='INCIDENT'
 assert not runner.controller.waiting_for_market_data and failures
 assert not reconciled

def test_supervisor_accepts_owned_waiting_heartbeat_without_production(tmp_path,monkeypatch):
 a,_,_=fixture(tmp_path,monkeypatch);r=service_receipt(tmp_path,NOW)
 h=dict(schema='REAL_CHILD_HEARTBEAT_V1',pid=123,at=NOW.isoformat(),status='WAITING_FOR_MARKET_DATA',
  mode='REAL',production_enabled=False,account_id=a['account_binding'],ledger_schema='real_trader',
  activity='Supervised REAL runner',recovery_only=False,authority_build_sha256='build',
  market_session_label=r['market_session_label'],reconciliation_ready=True,unresolved=False,
  last_step_result='WAITING_FOR_MARKET_DATA',last_step_at=NOW.isoformat())
 assert supervisor._heartbeat_matches(h,r,False,NOW)
 h['production_enabled']=True
 assert not supervisor._heartbeat_matches(h,r,False,NOW)

def test_empty_fresh_candidate_set_waits_without_account_incident_then_resumes(tmp_path,monkeypatch):
 runner,a,p,state,samples,account,reconciled,failures=make_runner(tmp_path,monkeypatch)
 def unavailable():raise ValueError('NO_FRESH_REAL_PORTFOLIO_CONTEXT')
 runner.prepare_waiting_market=unavailable
 for _ in range(3):assert runner._wait_for_market_data()=='WAITING_FOR_MARKET_DATA'
 assert len(reconciled)==3 and not failures and runner.controller.waiting_for_market_data
 runner.prepare_waiting_market=lambda:p
 runner._advance_packet=lambda packet,session,prepared=False:'RESUMED' if prepared else 'BAD'
 assert runner._wait_for_market_data()=='RESUMED' and not failures

@pytest.mark.parametrize('reason',['REAL_TRANSPORT_UNCERTAIN_NO_RETRY','REAL_ACCOUNT_BINDING_MISMATCH','REAL_RECOVERY_ORDER_UNCERTAIN'])
def test_real_uncertainty_is_not_reclassified_as_candidate_wait(tmp_path,monkeypatch,reason):
 runner,*rest=make_runner(tmp_path,monkeypatch)
 def unsafe():raise ValueError(reason)
 runner.prepare_waiting_market=unsafe
 assert runner._wait_for_market_data()=='INCIDENT'
 assert not runner.controller.waiting_for_market_data

def test_empty_candidate_code_never_bypasses_held_position_safety(tmp_path,monkeypatch):
 runner,a,p,state,samples,account,reconciled,failures=make_runner(tmp_path,monkeypatch)
 state['positions']['instrument-a']={'quantity':'1'}
 runner.prepare_waiting_market=lambda:pytest.fail('Held position cannot enter flat wait')
 assert runner._market_failure_or_incident(ValueError('NO_FRESH_REAL_PORTFOLIO_CONTEXT'),'SOURCE_REFRESH')=='INCIDENT'
