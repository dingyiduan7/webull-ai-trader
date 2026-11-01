"""No paid call: retained OPEN survives reasoning only for fresh revalidation."""
import json
from datetime import timedelta
import pytest
from autonomous_trader.decision_api import astra_probe as api
from autonomous_trader.decisions import parse_alpha_decision,validate_alpha_semantics
from test_real_daily import fixture,NOW


def client_context(tmp_path,monkeypatch):
    _,_,bundle=fixture(tmp_path,monkeypatch);packet,_,_=bundle(NOW)
    context=packet.context.model_copy(update={'reconciliation_ready':True,'settlement_known':True})
    decision={'schema_version':'1.0','action':'OPEN','reason':'Retained opportunity',
        'open':dict(instrument_id='instrument-a',symbol='TEST',strategy_label='owner-model',thesis='Model thesis',
            desired_quantity='1',desired_notional_usd=None,entry_limit_usd='10.01',hard_stop_usd='9.99',target_usd='10.50',
            entry_expires_at=(NOW+timedelta(seconds=120)).isoformat(),
            next_review_at=(NOW+timedelta(seconds=60)).isoformat(),latest_exit_at=(NOW+timedelta(seconds=300)).isoformat()),
        'hold':None,'modify':None,'close':None,'defer_to':None}
    client=object.__new__(api.AstraClient);client.model='gpt-6-astra';client.reasoning='medium';client.last_call={}
    client._structured_call=lambda request:(json.dumps({'decision':decision}),40000,{},'0.01')
    monkeypatch.setattr(api,'fast_alpha_request',lambda *a,**k:{})
    times=iter((NOW,NOW+timedelta(seconds=40)))
    return client,context,decision,lambda:next(times)


def test_real_reasoning_does_not_discard_open_before_refreshed_validation(tmp_path,monkeypatch):
    client,context,_,clock=client_context(tmp_path,monkeypatch)
    client.defer_execution_context_validation=True
    result=client.fast_decide(context,mode='ENTRY',hard_policy='',clock=clock)
    assert result['decision']['action']=='OPEN' and result['execution_context_validated'] is False
    decision=parse_alpha_decision(json.dumps(result['decision']))
    current=NOW+timedelta(seconds=40)
    with pytest.raises(ValueError,match='STALE_CONTEXT'):
        validate_alpha_semantics(decision,context,current,maximum_quote_age_ms=None)
    fresh=context.model_copy(update={'as_of':current,'expires_at':current+timedelta(seconds=30),
        'instruments':(context.instruments[0].model_copy(update={'quote_at':current}),)})
    validate_alpha_semantics(decision,fresh,current,maximum_quote_age_ms=None)
    broken=fresh.model_copy(update={'instruments':(fresh.instruments[0].model_copy(update={'quote_at':current+timedelta(seconds=1)}),)})
    with pytest.raises(ValueError,match='STALE_MARKET'):
        validate_alpha_semantics(decision,broken,current,maximum_quote_age_ms=None)


def test_default_client_still_requires_current_original_context(tmp_path,monkeypatch):
    client,context,_,clock=client_context(tmp_path,monkeypatch)
    with pytest.raises(api.PreflightError,match='STALE_CONTEXT'):
        client.fast_decide(context,mode='ENTRY',hard_policy='',clock=clock)


def test_deferred_client_never_extends_astra_entry_deadline(tmp_path,monkeypatch):
    client,context,decision,_=client_context(tmp_path,monkeypatch)
    client.defer_execution_context_validation=True
    decision['open']['entry_expires_at']=(NOW+timedelta(seconds=35)).isoformat()
    times=iter((NOW,NOW+timedelta(seconds=40)))
    with pytest.raises(api.PreflightError,match='ENTRY_EXPIRED'):
        client.fast_decide(context,mode='ENTRY',hard_policy='',clock=lambda:next(times))


def test_completed_open_routes_to_exact_instrument_refresh(tmp_path,monkeypatch):
    from concurrent.futures import Future
    from autonomous_trader.real_runner import RealRunner
    client,context,decision,clock=client_context(tmp_path,monkeypatch)
    client.defer_execution_context_validation=True
    result=client.fast_decide(context,mode='ENTRY',hard_policy='',clock=clock)
    future=Future();future.set_result(result)
    runner=object.__new__(RealRunner);runner.pending=(future,context,'ENTRY',None)
    assert runner._pending_entry_instrument()=='instrument-a'
    runner.pending=(future,context,'POSITION','p')
    assert runner._pending_entry_instrument() is None


def test_context_expired_before_dispatch_never_calls_provider(tmp_path,monkeypatch):
    client,context,_,_=client_context(tmp_path,monkeypatch)
    client.defer_execution_context_validation=True
    client._structured_call=lambda *args,**kwargs:pytest.fail('expired input must not incur model spend')
    with pytest.raises(api.PreflightError,match='STALE_CONTEXT'):
        client.fast_decide(context,mode='ENTRY',hard_policy='',clock=lambda:context.expires_at)
