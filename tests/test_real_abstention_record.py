from concurrent.futures import Future
from datetime import timedelta
from types import SimpleNamespace
import pytest
from test_real_daily import fixture,NOW
from autonomous_trader.real_runner import RealRunner


def prepared(tmp_path,monkeypatch):
    _,_,bundle=fixture(tmp_path,monkeypatch);packet,state,_=bundle(NOW)
    context=packet.context.model_copy(update={'reconciliation_ready':True,'settlement_known':True})
    decision={'schema_version':'1.0','action':'ABSTAIN','reason':'Observed payoff does not cover the supplied fee reserve.',
        'open':None,'hold':None,'modify':None,'close':None}
    result={'decision':decision,'model':'gpt-6-astra','reasoning':'medium',
        'usage':{'input_tokens':7839,'output_tokens':649},'latency_ms':21399,
        'estimated_cost_usd':'0.13043','model_work_id':'work-a',
        'model_response_at':(NOW+timedelta(seconds=21.399)).isoformat()}
    future=Future();future.set_result(result)
    r=RealRunner.__new__(RealRunner);r.pending=(future,context,'ENTRY',None)
    r.clock=lambda:NOW+timedelta(seconds=60)
    r.context=lambda p:pytest.fail('ABSTAIN must not acquire current execution context')
    r.gateway=SimpleNamespace(submit=lambda *a:pytest.fail('ABSTAIN cannot submit'))
    r.emit=lambda *a,**kw:None;r.store=object()
    records=[]
    monkeypatch.setattr('autonomous_trader.real_runner.persist_portfolio_decision_evidence',lambda store,record,**kw:records.append(record))
    return r,result,context,records


def test_completed_abstention_retained_after_quote_and_context_expiry(tmp_path,monkeypatch):
    r,result,context,records=prepared(tmp_path,monkeypatch)
    assert r._record_completed_abstention()
    assert r.pending is None and len(records)==1
    record=records[0]
    assert record.action=='ABSTAIN' and record.model_cost_usd==__import__('decimal').Decimal('0.13043')
    assert record.model_response_at==NOW+timedelta(seconds=21.399)
    assert record.decision_snapshot_at==context.as_of
    assert record.candidates[0].quote_at==context.instruments[0].quote_at
    assert record.order_intent_plan_id is None
    assert not r._record_completed_abstention() and len(records)==1


def test_unfinished_or_non_abstention_does_not_take_evidence_only_path(tmp_path,monkeypatch):
    r,result,context,records=prepared(tmp_path,monkeypatch)
    result['decision']['action']='OPEN'
    assert not r._record_completed_abstention() and r.pending and not records
    r.pending=(Future(),context,'ENTRY',None)
    assert not r._record_completed_abstention()


def test_malformed_abstention_still_fails_schema(tmp_path,monkeypatch):
    r,result,context,records=prepared(tmp_path,monkeypatch)
    result['decision']['open']={'unexpected':'value'}
    assert r._record_completed_abstention()
    assert not records and r.pending is None


def test_original_context_must_have_been_valid_when_supplied(tmp_path,monkeypatch):
    r,result,context,records=prepared(tmp_path,monkeypatch)
    # Entry validity window is 10s in the current runtime (deliberately widened from 2s
    # for model latency); a quote older than that must still invalidate the context.
    stale=context.instruments[0].model_copy(update={'quote_at':NOW-timedelta(seconds=11)})
    r.pending=(r.pending[0],context.model_copy(update={'instruments':(stale,)}),'ENTRY',None)
    assert r._record_completed_abstention() and not records


def test_open_still_requires_current_two_second_validation(tmp_path,monkeypatch):
    r,result,context,records=prepared(tmp_path,monkeypatch)
    checks=[];current=object()
    monkeypatch.setattr('autonomous_trader.real_runner.parse_alpha_decision',lambda raw:SimpleNamespace(action='OPEN'))
    r.context=lambda packet:current
    def validate(decision,received,now,**kw):
        checks.append((received,now,kw['maximum_quote_age_ms']))
        raise ValueError('STALE_MARKET')
    monkeypatch.setattr('autonomous_trader.real_runner.validate_alpha_semantics',validate)
    r._apply(object())
    assert checks==[(current,r.clock(),10000)] and not records and r.pending is None
