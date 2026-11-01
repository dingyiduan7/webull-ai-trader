"""Only fixed stage codes and numeric billing metadata may survive rejection."""
import json
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
import pytest
from test_cycle import NOW,setup_cycle
from test_alpha_reset import observation,universe,open_raw,api_response
from autonomous_trader.mvp.alpha import build_alpha_context
from autonomous_trader.decision_api.astra_probe import AstraClient
from autonomous_trader.monitoring.runner import ObservedBrain


@pytest.mark.parametrize('failure,expected,stage',[
    ('json','ALPHA_JSON_INVALID','JSON'),
    ('length','ALPHA_REASON_TOO_LONG','CANONICAL_PARSE'),
    ('payload','ALPHA_ACTION_PAYLOAD_MISMATCH','CANONICAL_PARSE'),
    ('both_sizes','ALPHA_SIZE_PAYLOAD_MISMATCH','CANONICAL_PARSE'),
    ('neither_size','ALPHA_SIZE_PAYLOAD_MISMATCH','CANONICAL_PARSE'),
    ('routing','INVALID_COMPARISON_ROUTING','ROUTING'),
    ('expiry','ENTRY_EXPIRED_BEFORE_ADMISSION','SEMANTICS'),
    ('freshness','ALPHA_SEMANTIC_STALE_CONTEXT','SEMANTICS'),
    ('shape','REFUSAL_OR_OUTPUT_SHAPE','OUTPUT_SHAPE'),
])
def test_safe_fast_failure_stage_and_billing_survive(monkeypatch,setup_cycle,failure,expected,stage):
    args,_,_=setup_cycle;u=universe((observation('stock-b','BBB'),))
    args['controller'].refresh(u);context=build_alpha_context(u,args['controller'].state,NOW)
    d=open_raw(NOW);d['defer_to']=None
    if failure=='length':d['reason']='synthetic-private-value'*100
    if failure=='payload':d['action']='ABSTAIN'
    if failure=='both_sizes':d['open']['desired_notional_usd']=100
    if failure=='neither_size':d['open']['desired_quantity']=None
    if failure=='routing':d['defer_to']={'instrument_id':'private-value','symbol':'PRIVATE'}
    if failure=='expiry':d['open']['entry_expires_at']=NOW.isoformat()
    response=api_response({'decision':d})
    if failure=='json':response['output'][0]['content'][0]['text']='not-json-synthetic-private-value'
    if failure=='shape':response['output'][0]['content']=[]
    times=iter([NOW,NOW+timedelta(seconds=91)] if failure=='freshness' else [NOW,NOW])
    clock=lambda:next(times,NOW)
    client=AstraClient('synthetic-private-value');events=[]
    # The secret-output check must remain distinct from incidental private text.
    client._key='different-synthetic-key'
    monkeypatch.setattr(client,'_request',lambda *args:deepcopy(response))
    brain=ObservedBrain(client,SimpleNamespace(clock=clock,update=lambda **kw:None,
        emit=lambda k,p:events.append((k,p))))
    if failure=='length':
        value=brain.fast_decide(context,'ENTRY')
        assert value['decision']['action']=='OPEN' and len(value['decision']['reason'])==1000
        return
    with pytest.raises(ValueError,match='^'+expected+'$'):brain.fast_decide(context,'ENTRY')
    assert len(events)==1 and events[0][0]=='MODEL_CALL_FAILED'
    event=events[0][1]
    assert event['failure_code']==expected and event['ai']['validation_stage']==stage
    assert event['ai']['estimated_cost_usd'] is not None
    assert 'private-value' not in json.dumps(event) and 'decision' not in event['ai']


def test_arbitrary_exception_text_is_never_a_failure_code():
    events=[]
    def fail(*args,**kwargs):raise ValueError('secret arbitrary text')
    brain=ObservedBrain(SimpleNamespace(fast_decide=fail,last_call={'usage':None}),
        SimpleNamespace(clock=lambda:NOW,update=lambda **kw:None,emit=lambda k,p:events.append((k,p))))
    with pytest.raises(ValueError,match='^ASTRA_FAST_DECISION_FAILED$'):brain.fast_decide(None,'ENTRY')
    assert events[0][1]['failure_code']=='ASTRA_FAST_DECISION_FAILED'
    assert 'secret' not in json.dumps(events)


def test_deep_compaction_preserves_model_budget_and_candidate_capacity(setup_cycle):
    from autonomous_trader.decision_api.astra_probe import deep_alpha_request,MODEL
    args,_,_=setup_cycle;u=universe((observation('stock-b','BBB'),))
    args['controller'].refresh(u);context=build_alpha_context(u,args['controller'].state,NOW)
    body=deep_alpha_request(context,{})
    schema=body['text']['format']['schema']
    assert body['model']==MODEL and body['reasoning']['effort']=='medium' and body['max_output_tokens']==2048
    assert schema['properties']['regime_view']['maxLength']==350
    assert schema['$defs']['DeepFocusItem']['properties']['thesis']['maxLength']==240
    assert schema['properties']['focus']['maxItems']==20
