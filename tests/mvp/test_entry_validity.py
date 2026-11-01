"""Admission timing cannot extend a model deadline or defeat exact refresh."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime,timedelta
from types import SimpleNamespace
import json
from decimal import Decimal
import pytest
from test_cycle import NOW,setup_cycle
from test_alpha_reset import open_raw,api_response
from test_opportunity_pipeline import engine_fixture,result
from autonomous_trader.decision_api.astra_probe import AstraClient,fast_alpha_request
from autonomous_trader.mvp.config import ExecutionMode,RuntimeConfig
from autonomous_trader.mvp.gateway import ExecutionGateway
from autonomous_trader.sandbox.secrets import PreflightError


def prepare(setup_cycle,tmp_path):
    e,store,broker,context,versions,session=engine_fixture(setup_cycle,tmp_path)
    job=next(iter(e.opportunities.jobs.values()));job['status']='RESULT_READY'
    return e,store,broker,context,versions,session,job,open_raw(NOW,job['instrument_id'],job['symbol'])


def test_sufficient_window_reaches_admission_without_rebasing(setup_cycle,tmp_path):
    e,store,broker,c,_,s,j,d=prepare(setup_cycle,tmp_path);original=deepcopy(d)
    e._candidate_result(j,c,result(d),s)
    assert d==original and len(broker.orders)==1
    assert {'ALPHA_PLAN_CREATED','INTENT_APPROVED'} <= {k for k,_,_ in store.events}
    broker_event=next(payload for kind,payload,_ in store.events if kind=='BROKER_RESULT')
    trace=broker_event['latency_trace']
    assert all(trace[key] for key in ('model_handled_at','post_model_refresh_started_at',
        'post_model_refresh_completed_at','post_model_market_source_at','post_model_market_received_at',
        'post_model_window_updated_at','pre_order_validated_at','risk_admitted_at','order_submit_started_at','terminal_at'))
    intent=next(iter(e.controller.state['orders'].values()))['intent']
    assert datetime.fromisoformat(intent['deadline'])==datetime.fromisoformat(d['open']['entry_expires_at'])


def test_verbose_live_evidence_does_not_block_fresh_open_admission(setup_cycle,tmp_path):
    e,store,broker,c,_,s,j,d=prepare(setup_cycle,tmp_path)
    read=e.source.refresh_focus
    def verbose_refresh(state,symbol):
        observed=read(state,symbol)
        return replace(observed,candidates=tuple(replace(item,live_windows={'ticks':'x'*3000})
            for item in observed.candidates))
    e.source.refresh_focus=verbose_refresh
    e._candidate_result(j,c,result(d),s)
    assert j['reason'].startswith('BROKER_') and len(broker.orders)==1
    assert {'ALPHA_PLAN_CREATED','INTENT_APPROVED','BROKER_RESULT'} <= {k for k,_,_ in store.events}


def test_late_broad_snapshot_cannot_regress_focused_mark_or_block_account_reconciliation(setup_cycle,tmp_path):
    e,_,broker,c,_,s,j,d=prepare(setup_cycle,tmp_path)
    current=e.controller.state['marks'][j['instrument_id']]
    snapshot=e.source.refresh_focus(e.controller.state,j['symbol'])
    delayed=replace(snapshot,candidates=tuple(replace(x,quote_at=x.quote_at-timedelta(seconds=4),bid=x.bid-Decimal('1'))
        for x in snapshot.candidates))
    e.controller.refresh(delayed)
    assert e.controller.state['marks'][j['instrument_id']]==current
    assert e.controller.state['reconciliation']['ready'] and not broker.orders
    with pytest.raises(ValueError,match='STALE_MARKET_OR_ACCOUNT'):delayed.select(j['symbol'],NOW)


def test_moved_executable_ask_requires_astra_reassessment_without_raising_its_limit(setup_cycle,tmp_path):
    e,store,broker,c,_,s,j,d=prepare(setup_cycle,tmp_path)
    original_ask=next(x.ask_usd for x in c.instruments if x.instrument_id==j['instrument_id'])
    d['open']['entry_limit_usd']=str(original_ask)
    original=deepcopy(d);read=e.source.refresh_focus
    def moved(state,symbol):
        u=read(state,symbol)
        return replace(u,candidates=tuple(replace(x,bid=original_ask+Decimal('.01'),ask=original_ask+Decimal('.02')) for x in u.candidates))
    e.source.refresh_focus=moved
    e._candidate_result(j,c,result(d),s)
    assert j['status']=='PENDING' and j['reason']=='ENTRY_PRICE_MOVED_REASSESS'
    assert not broker.orders and d==original
    assert datetime.fromisoformat(j['not_before'])==NOW
    assert not any(k=='INTENT_APPROVED' for k,_,_ in store.events)


@pytest.mark.parametrize(('fill_on_detail','expected_status'),[(8,'FILLED'),(None,'CANCELLED')])
@pytest.mark.parametrize('with_held',[False,True])
def test_flat_buy_respects_astra_expiry_after_six_details(setup_cycle,tmp_path,monkeypatch,fill_on_detail,expected_status,with_held):
    e,store,_,context,_,session,job,decision=prepare(setup_cycle,tmp_path)
    if with_held:
        from autonomous_trader.durable.models import Protection
        e._candidate_result(job,context,result(decision),session)
        p=e.controller.state['positions'][job['instrument_id']]
        e.controller.send(Protection(instrument_id=job['instrument_id'],quantity=p['quantity'],stop_usd=p['stop_usd'],evidence_id='held-test'))
        job=list(e.opportunities.jobs.values())[1];job['status']='RESULT_READY'
        decision=open_raw(NOW,job['instrument_id'],job['symbol'])
    decision['open']['entry_expires_at']=(NOW+timedelta(seconds=45)).isoformat()
    calls={'detail':0,'cancel':0};current={'at':NOW}
    e.clock=lambda:current['at'];e.controller.clock=e.clock
    def response(body):
        return SimpleNamespace(status_code=200,body=body,path='/synthetic',received_at=current['at'].isoformat(),headers={},header_names=())
    class Transport:
        _account_id='synthetic-account'
        def place(self,intent,symbol):
            self.intent,self.symbol=intent,symbol
            return response({'client_order_id':intent.client_order_id,'order_id':'broker-entry'})
        def detail(self,client_id):
            calls['detail']+=1
            current['at']+=timedelta(milliseconds=3200)
            filled=calls['detail']==fill_on_detail
            return response({'client_order_id':client_id,'orders':[{
                'order_id':'broker-entry','client_order_id':client_id,'symbol':self.symbol,'side':'BUY',
                'order_type':'LIMIT','time_in_force':'DAY','support_trading_session':'CORE',
                'total_quantity':str(self.intent.quantity),'limit_price':str(self.intent.limit_price_usd),
                'filled_quantity':str(self.intent.quantity) if filled else '0',
                'filled_price':str(self.intent.limit_price_usd) if filled else '0',
                'filled_time_at':current['at'].isoformat() if filled else None,
                'status':'FILLED' if filled else ('CANCELLED' if calls['cancel'] else 'SUBMITTED'),
                'fees':[],'commission':{}}]})
        def cancel(self,client_id):
            calls['cancel']+=1
            assert current['at']>=datetime.fromisoformat(decision['open']['entry_expires_at'])
            return response({'client_order_id':client_id,'order_id':'broker-entry'})
    monkeypatch.setattr('autonomous_trader.mvp.gateway.sandbox_ledger_id',lambda _:e.controller.account_id)
    gateway=ExecutionGateway(RuntimeConfig(ExecutionMode.SANDBOX),transport=Transport())
    gateway._capture=lambda _:None;e.gateway=gateway
    e._candidate_result(job,context,result(decision),session)
    if with_held:
        assert job['reason']=='BROKER_WORKING' and calls['cancel']==0
        assert any(r['instrument_id']==job['instrument_id'] for r in e.controller.state['reservations'].values())
        # Reconstruct the gateway: accepted orders are resumed from durable
        # authority, not submitted again and not lost with an in-memory timer.
        resumed=ExecutionGateway(RuntimeConfig(ExecutionMode.SANDBOX),transport=gateway.transport)
        resumed._capture=lambda _:None
        for _ in range(20):
            terminal=resumed.service_working_entries(e.controller,session,e.clock)
            if terminal:break
        assert terminal[0]['broker']['status']==expected_status
        assert e.controller.state['positions'].get(list(e.opportunities.jobs.values())[0]['instrument_id'])
    assert calls['detail']>=8 and calls['cancel']==(0 if fill_on_detail else 1)
    assert job['reason']==('BROKER_WORKING' if with_held else 'BROKER_'+expected_status)
    assert {'INTENT_APPROVED','BROKER_RESULT'} <= {kind for kind,_,_ in store.events}
    assert (e.controller.state['positions'].get(job['instrument_id'],{}).get('quantity')=='1')==(expected_status=='FILLED')


def test_expired_window_dropped_before_refresh_or_authority(setup_cycle,tmp_path):
    e,store,broker,c,versions,s,j,d=prepare(setup_cycle,tmp_path)
    d['open']['entry_expires_at']=NOW.isoformat();original=deepcopy(d)
    e._candidate_result(j,c,result(d),s)
    assert j['reason']=='ENTRY_EXPIRED_BEFORE_ADMISSION' and d==original and not versions and not broker.orders
    assert not any(k in ('ALPHA_PLAN_CREATED','INTENT_APPROVED') for k,_,_ in store.events)


def test_model_owned_short_window_reaches_admission_if_refresh_finishes_in_time(setup_cycle,tmp_path):
    e,store,broker,c,_,s,j,d=prepare(setup_cycle,tmp_path);current={'at':NOW};e.clock=lambda:current['at']
    e.controller.clock=e.clock;d['open']['entry_expires_at']=(NOW+timedelta(seconds=40)).isoformat()
    original=deepcopy(d);read=e.source.refresh_focus
    def delayed(state,symbol):
        u=read(state,symbol);current['at']=NOW+timedelta(seconds=35)
        return replace(u,received_at=current['at'],candidates=tuple(replace(
            x,quote_at=current['at'],received_at=current['at']) for x in u.candidates))
    e.source.refresh_focus=delayed
    e._candidate_result(j,c,result(d),s)
    assert d==original and len(broker.orders)==1 and j['reason'].startswith('BROKER_')
    assert {'ALPHA_PLAN_CREATED','INTENT_APPROVED'} <= {k for k,_,_ in store.events}


def test_expiry_during_exact_refresh_has_precise_drop(setup_cycle,tmp_path):
    e,store,broker,c,_,s,j,d=prepare(setup_cycle,tmp_path);current={'at':NOW};e.clock=lambda:current['at']
    original=deepcopy(d);read=e.source.refresh_focus
    def delayed(state,symbol):
        u=read(state,symbol);current['at']=NOW+timedelta(seconds=121)
        return replace(u,received_at=current['at'],candidates=tuple(replace(x,quote_at=current['at'],received_at=current['at']) for x in u.candidates))
    e.source.refresh_focus=delayed;e.controller.refresh=lambda _:None
    e._candidate_result(j,c,result(d),s)
    assert j['reason']=='ENTRY_EXPIRED_BEFORE_ADMISSION' and not broker.orders and d==original
    assert not any(k=='ALPHA_PLAN_CREATED' for k,_,_ in store.events)


def test_real_entry_ineligibility_still_drops(setup_cycle,tmp_path):
    e,_,broker,c,_,s,j,d=prepare(setup_cycle,tmp_path);read=e.source.refresh_focus
    def ineligible(state,symbol):
        u=read(state,symbol)
        return replace(u,candidates=tuple(replace(x,entry_eligible=False) for x in u.candidates))
    e.source.refresh_focus=ineligible;e._candidate_result(j,c,result(d),s)
    assert j['reason']=='INELIGIBLE_OR_ILLIQUID_STOCK' and not broker.orders


def test_entry_request_exposes_buffer_and_abstain_is_unaffected(monkeypatch,setup_cycle,tmp_path):
    e,_,broker,c,versions,s,j,_=prepare(setup_cycle,tmp_path)
    body=fast_alpha_request(c,mode='ENTRY',hard_policy={});p=json.loads(body['input'][1]['content'])
    assert p['entry_validity']=={'context_expires_at':c.expires_at.isoformat(),'model_owns_deadline':True,
        'runtime_never_extends_deadline':True,'must_remain_future_through_refresh_admission_and_dispatch':True}
    ai=result();client=AstraClient('synthetic-key')
    monkeypatch.setattr(client,'_request',lambda *args:api_response({'decision':{**ai['decision'],'defer_to':None}}))
    valid=client.fast_decide(c,mode='ENTRY',hard_policy={},clock=lambda:NOW)
    valid['model_work_id']='test-portfolio-claim'
    e._candidate_result(j,c,valid,s)
    assert j['reason']=='ABSTAIN' and not broker.orders and not versions


def test_short_model_window_is_accepted_without_coercion_while_still_future(monkeypatch,setup_cycle,tmp_path):
    _,_,_,c,_,_,_,d=prepare(setup_cycle,tmp_path)
    d['open']['entry_expires_at']=(NOW+timedelta(seconds=40)).isoformat();original=deepcopy(d)
    client=AstraClient('synthetic-key')
    monkeypatch.setattr(client,'_request',lambda *args:api_response({'decision':{**d,'defer_to':None}}))
    value=client.fast_decide(c,mode='ENTRY',hard_policy={},clock=lambda:NOW)
    assert value['decision']['action']=='OPEN'
    assert d==original and client.last_call['validation_stage']=='SEMANTICS'


def test_model_completion_after_its_entry_expiry_fails_closed(monkeypatch,setup_cycle,tmp_path):
    _,_,_,c,_,_,_,d=prepare(setup_cycle,tmp_path)
    d['open']['entry_expires_at']=(NOW+timedelta(seconds=40)).isoformat()
    client=AstraClient('synthetic-key')
    monkeypatch.setattr(client,'_request',lambda *args:api_response({'decision':{**d,'defer_to':None}}))
    with pytest.raises(PreflightError,match='^ENTRY_EXPIRED_BEFORE_ADMISSION$'):
        client.fast_decide(c,mode='ENTRY',hard_policy={},clock=lambda:NOW+timedelta(seconds=41))


def test_semantic_rejection_is_precise_not_opaque(setup_cycle,tmp_path):
    e,_,broker,c,_,s,j,d=prepare(setup_cycle,tmp_path)
    d['open']['next_review_at']=NOW.isoformat()
    e._candidate_result(j,c,result(d),s)
    assert j['reason']=='ALPHA_SEMANTIC_NEXT_REVIEW' and not broker.orders


def test_translation_failure_has_safe_stage_not_private_text(monkeypatch,setup_cycle,tmp_path):
    e,_,broker,c,_,s,j,d=prepare(setup_cycle,tmp_path)
    def fail(*args,**kwargs):raise ValueError('private synthetic validation detail')
    monkeypatch.setattr('autonomous_trader.monitoring.runner.translate_open',fail)
    e._candidate_result(j,c,result(d),s)
    assert j['reason']=='ALPHA_TRANSLATION_INVALID' and not broker.orders
