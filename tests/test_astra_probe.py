from datetime import timedelta
from decimal import Decimal
import json
import pytest
from autonomous_trader.decision_api.astra_probe import AstraClient, MODEL, structured_schema, estimated_cost, request_body, build_context
from autonomous_trader.contracts import payload_hash
from autonomous_trader.sandbox.secrets import PreflightError


def abstain(proposal):
    return dict(proposal,action='ABSTAIN',entry_plan=None)


def response(proposal):
    return {'model':MODEL,'status':'completed','usage':{'input_tokens':1000,'output_tokens':100,
        'input_tokens_details':{'cached_tokens':100}},'output':[{'type':'message','content':[
        {'type':'output_text','text':json.dumps(proposal)}]}]}


def test_schema_retains_canonical_shape_and_three_actions():
    from jsonschema import Draft202012Validator
    s=structured_schema();Draft202012Validator.check_schema(s)
    assert set(s['required'])==set(s['properties'])
    assert s['properties']['action']['enum']==['PROPOSE_ENTRY','ABSTAIN','REQUEST_DATA']
    assert s['additionalProperties'] is False


def test_no_model_substitution(monkeypatch):
    client=AstraClient('synthetic-key')
    monkeypatch.setattr(client,'_request',lambda *a:{'data':[{'id':'gpt-5.6-sol'}]})
    with pytest.raises(PreflightError,match='ASTRA_UNAVAILABLE'):client.verify_model()


def test_valid_independent_validation_and_one_call(context,registry,proposal,now,monkeypatch):
    client=AstraClient('synthetic-key');seen=[]
    def call(*args):seen.append(args);return response(abstain(proposal))
    monkeypatch.setattr(client,'_request',call)
    result=client.decide(context,registry,clock=lambda:now)
    assert result['schema_valid'] and result['semantics_valid'] and result['no_trade']
    assert result['decision']['action']=='ABSTAIN'
    assert result['estimated_cost_usd']=='0.0141'
    with pytest.raises(PreflightError):client.decide(context,registry,clock=lambda:now)
    assert len(seen)==1
    request=request_body(context)
    assert request['store'] is False and 'tools' not in request
    assert request['text']['format']['strict'] is True


def test_valid_target_fraction_remains_json_number_for_cycle_reparse(context,registry,proposal,now,monkeypatch):
    from autonomous_trader.decisions import parse_trade_decision
    proposal['entry_plan']['targets']=[{'price_usd':'105','fraction_of_initial_quantity':1}]
    client=AstraClient('synthetic-key')
    monkeypatch.setattr(client,'_request',lambda *args:response(proposal))
    result=client.decide(context,registry,clock=lambda:now)
    assert result['schema_valid'] and result['semantics_valid']
    assert result['decision']['entry_plan']['targets'][0]['fraction_of_initial_quantity']==1
    assert not isinstance(result['decision']['entry_plan']['targets'][0]['fraction_of_initial_quantity'],str)
    assert parse_trade_decision(json.dumps(result['decision'])).entry_plan.targets[0].fraction_of_initial_quantity==1


@pytest.mark.parametrize('fault',['schema','semantics','timeout','stale','refusal','wrong_model'])
def test_failures_are_no_trade(fault,context,registry,proposal,now,monkeypatch):
    client=AstraClient('synthetic-key');data=response(abstain(proposal))
    if fault=='schema':data['output'][0]['content'][0]['text']='{}'
    if fault=='semantics':data=response(dict(abstain(proposal),context_hash='incorrect'))
    if fault=='refusal':data['output'][0]['content']=[{'type':'refusal','refusal':'no'}]
    if fault=='wrong_model':data['model']='gpt-5.6-sol'
    def call(*args):
        if fault=='timeout':raise PreflightError('OPENAI_ERROR_OR_TIMEOUT_NO_RETRY')
        return data
    monkeypatch.setattr(client,'_request',call)
    clocks=iter([now,now+timedelta(seconds=100)]) if fault=='stale' else None
    result=client.decide(context,registry,clock=(lambda:next(clocks)) if clocks else lambda:now)
    assert result['no_trade'] and not result['semantics_valid'] and result['decision'] is None


def test_expired_context_never_calls_api(context,registry,now,monkeypatch):
    client=AstraClient('synthetic-key')
    monkeypatch.setattr(client,'_request',lambda *a:pytest.fail('stale context must not be sent'))
    assert client.decide(context,registry,clock=lambda:now+timedelta(minutes=2))['result']=='STALE_CONTEXT'


def test_cost_is_decimal_and_counts_cached_input():
    assert Decimal(estimated_cost({'input_tokens':100,'output_tokens':10}))==Decimal('.0015')
    with pytest.raises(PreflightError):estimated_cost({'input_tokens':1,'output_tokens':2,'input_tokens_details':{'cached_tokens':3}})
    assert Decimal(estimated_cost({'input_tokens':5164,'output_tokens':633,
        'input_tokens_details':{'cached_tokens':0,'cache_write_tokens':5161}}))==Decimal('0.0961925')


def test_observation_projection_does_not_invent_capabilities(now):
    from pathlib import Path
    from autonomous_trader.sandbox.http import HttpObservation
    from autonomous_trader.sandbox.sanitize import Sanitizer
    root=Path(__file__).parent/'fixtures'
    observations={}
    for name in ('accounts','balance','positions','orders','instruments','snapshot'):
        data=json.loads((root/'webull_m3b/20260911T193156104102Z'/('after_'+name+'.json')).read_text())
        observations[name]=HttpObservation(data['path'],200,now.isoformat(),{},data['body'])
    data=json.loads((root/'webull_sandbox/20260911T180959106398Z/history_batch.json').read_text())
    for n,bar in enumerate(data['body']['result'][0]['result']):bar['time']=(now-timedelta(minutes=n+1)).isoformat()
    observations['bars']=HttpObservation(data['path'],200,now.isoformat(),{},data['body'])
    observations['snapshot'].body[0]['quote_time']=str(int(now.timestamp()*1000))
    account=next(a['account_id'] for a in observations['accounts'].body if a['account_class']=='INDIVIDUAL_CASH')
    c,registry=build_context(observations,account,Sanitizer(),now)
    assert c.account.balances.settled_usd==Decimal('999667.24')
    assert c.account.balances.unsettled_usd==Decimal('332.73')
    assert not c.account.balances.settlement_known and not c.capabilities.protection_mechanisms
    assert not c.instrument.tradable and c.warnings
    assert registry.maximum_market_age_ms==2000
    assert set(c.allowed_actions)=={'PROPOSE_ENTRY','ABSTAIN','REQUEST_DATA'}
    observations['positions'].body.append({'quantity':'1'})
    with pytest.raises(PreflightError):build_context(observations,account,Sanitizer(),now)


def test_actual_astra_decision_replays_at_original_validation_time():
    from pathlib import Path
    from autonomous_trader.contracts import DecisionContext
    from autonomous_trader.decisions import parse_trade_decision, validate_semantics, SemanticRegistry
    r=json.loads((Path(__file__).parents[1]/'docs/astra-decision-result.json').read_text())
    c=DecisionContext.model_validate(r['context'])
    registry=SemanticRegistry(strategy_bindings=((c.strategy_id,c.strategy_version,c.strategy_artifact_hash),),
        policy_hashes=(c.policy_hash,),source_ids=('WEBULL_SANDBOX_READ_ONLY',),execution_policy_ids=(),
        partial_fill_policy_ids=(),trailing_rules=(),calibration_ids=(),maximum_market_age_ms=2000,maximum_account_age_ms=10000)
    d=parse_trade_decision(json.dumps(r['decision']))
    assert validate_semantics(d,c,registry,c.as_of+timedelta(milliseconds=r['latency_ms'])).action=='ABSTAIN'
    assert r['decision_requests']==1 and r['broker_order_calls']==0 and r['no_trade']
    assert estimated_cost(r['usage'])==r['estimated_cost_usd']
