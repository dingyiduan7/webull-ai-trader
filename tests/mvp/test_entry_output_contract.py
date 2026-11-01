"""The entry wire schema enforces action payloads without repairing model choices."""
from copy import deepcopy
import json
import pytest
from jsonschema import Draft202012Validator
from test_cycle import NOW,setup_cycle
from test_alpha_reset import observation,universe,open_raw,api_response
from autonomous_trader.mvp.alpha import build_alpha_context
from autonomous_trader.decision_api.astra_probe import fast_alpha_request,AstraClient
from autonomous_trader.decisions import parse_alpha_decision
from autonomous_trader.sandbox.secrets import PreflightError


@pytest.fixture
def entry_context(setup_cycle):
    args,_,_=setup_cycle;u=universe((observation('stock-b','BBB'),))
    args['controller'].refresh(u)
    return build_alpha_context(u,args['controller'].state,NOW)


@pytest.mark.parametrize('action,notional,quantity,valid',[
    ('OPEN',None,1,True),('OPEN',100,None,True),
    ('OPEN',100,1,False),('OPEN',None,None,False),
    ('OPEN',0,None,False),('OPEN',None,-1,False),
    ('ABSTAIN',None,None,True),
])
def test_entry_wire_exactly_one_size(entry_context,action,notional,quantity,valid):
    request=fast_alpha_request(entry_context,mode='ENTRY',hard_policy={})
    schema=request['text']['format']['schema'];Draft202012Validator.check_schema(schema)
    d={**open_raw(NOW),'defer_to':None,'action':action}
    if action=='ABSTAIN':d['open']=None
    else:d['open'].update(desired_notional_usd=notional,desired_quantity=quantity)
    assert Draft202012Validator(schema).is_valid({'decision':d}) is valid
    assert 'anyOf' not in schema and request['max_output_tokens']==1536


@pytest.mark.parametrize('action,field',[
    ('ABSTAIN','open'),('ABSTAIN','hold'),('ABSTAIN','modify'),('ABSTAIN','close'),
    ('OPEN','hold'),('OPEN','modify'),('OPEN','close'),('OPEN','defer_to'),
])
def test_entry_wire_inactive_payloads_are_null(entry_context,action,field):
    schema=fast_alpha_request(entry_context,mode='ENTRY',hard_policy={})['text']['format']['schema']
    d={**open_raw(NOW),'defer_to':None,'action':action};d['open']['desired_quantity']=1
    if action=='ABSTAIN':d['open']=None
    assert Draft202012Validator(schema).is_valid({'decision':d})
    d[field]=deepcopy(open_raw(NOW)['open']) if field=='open' else {}
    assert not Draft202012Validator(schema).is_valid({'decision':d})


@pytest.mark.parametrize('chosen',['desired_quantity','desired_notional_usd'])
def test_unwrap_preserves_model_size_and_canonical_decision(monkeypatch,entry_context,chosen):
    d={**open_raw(NOW),'defer_to':None}
    d['open'].update(desired_quantity=None,desired_notional_usd=None);d['open'][chosen]=100
    client=AstraClient('synthetic-key')
    monkeypatch.setattr(client,'_request',lambda *args:api_response({'decision':d}))
    result=client.fast_decide(entry_context,mode='ENTRY',hard_policy={},clock=lambda:NOW)
    expected={k:v for k,v in d.items() if k!='defer_to'}
    assert result['decision']==parse_alpha_decision(json.dumps(expected)).model_dump(mode='json')
    assert result['decision']['open'][chosen]=='100'
    assert result['schema_valid'] and result['semantics_valid']


def test_flat_or_extra_envelope_rejected_without_inference(monkeypatch,entry_context):
    client=AstraClient('synthetic-key');d={**open_raw(NOW),'defer_to':None}
    for raw in (d,{'decision':d,'extra':None},{'decision':open_raw(NOW)}):
        monkeypatch.setattr(client,'_request',lambda *args:api_response(raw))
        with pytest.raises(PreflightError,match='^ALPHA_ENTRY_ENVELOPE_INVALID$'):
            client.fast_decide(entry_context,mode='ENTRY',hard_policy={},clock=lambda:NOW)
