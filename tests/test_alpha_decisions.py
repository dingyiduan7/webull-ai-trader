from datetime import timedelta
from decimal import Decimal
import json

import pytest

from autonomous_trader.contracts import (
    AlphaContext, AlphaInstrumentContext, AlphaPositionContext, DeepAlphaPlan,
)
from autonomous_trader.decisions import (
    DecisionError, parse_alpha_decision, parse_trade_decision, validate_alpha_semantics,
)
from autonomous_trader.decision_api.astra_probe import (
    deep_alpha_request, fast_alpha_request, stamp_alpha_decision,
)
from autonomous_trader.mvp.context import adapt_alpha_context


def alpha_context(now, *, position=False):
    instrument=AlphaInstrumentContext(instrument_id='stock-a',symbol='AAA',quote_at=now,
        bid_usd='99.99',ask_usd='100.00',price_increment_usd='0.01',quantity_increment='1',
        bars=(),volume='1000000',volatility='0.01',price_movement='0.02')
    positions=()
    if position:
        positions=(AlphaPositionContext(position_id='position-a',instrument_id='stock-a',symbol='AAA',
            quantity='100',average_entry_usd='98',mark_usd='99.99',active_stop_usd='95',target_usd='105',
            latest_exit_at=now.replace(hour=19,minute=30),unrealized_pnl_usd='199'),)
    return AlphaContext(as_of=now,expires_at=now+timedelta(seconds=30),
        session_open=now.replace(hour=13,minute=30),session_close=now.replace(hour=20),
        liquidation_at=now.replace(hour=19,minute=55),instruments=(instrument,),positions=positions,
        settled_cash_usd='100000',remaining_risk_usd='5000',reconciliation_ready=True,settlement_known=True)


def open_decision(now, **updates):
    payload=dict(instrument_id='stock-a',symbol='AAA',strategy_label='breakout',
        thesis='Fresh strength may continue.',desired_notional_usd=None,desired_quantity='100',
        entry_limit_usd='100',entry_expires_at=(now+timedelta(minutes=3)).isoformat(),
        hard_stop_usd='95',latest_exit_at=now.replace(hour=19,minute=50).isoformat(),
        target_usd=None,next_review_at=(now+timedelta(minutes=7)).isoformat())
    payload.update(updates)
    return {'schema_version':'1.0','action':'OPEN','reason':'Bounded long thesis.','open':payload,
        'hold':None,'modify':None,'close':None}


def decision(action, payload=None):
    value={'schema_version':'1.0','action':action,'reason':'Current evidence supports this intent.',
        'open':None,'hold':None,'modify':None,'close':None}
    if payload is not None:value[action.lower()]=payload
    return value


def parsed(value):
    return parse_alpha_decision(json.dumps(value))


def test_valid_abstain_is_fresh_and_has_no_payload(now):
    d=parsed(decision('ABSTAIN'))
    assert validate_alpha_semantics(d,alpha_context(now),now).action=='ABSTAIN'
    invalid=decision('ABSTAIN');invalid['open']=open_decision(now)['open']
    with pytest.raises(DecisionError,match='payload'):parsed(invalid)


def test_open_is_compact_target_optional_and_horizon_variable(now):
    raw=open_decision(now,latest_exit_at=(now+timedelta(hours=2,minutes=17)).isoformat())
    d=parsed(raw)
    assert validate_alpha_semantics(d,alpha_context(now),now).open.target_usd is None
    serialized=json.dumps(raw)
    for removed in ('expected_gross','expected_net','probability','predicate','provenance','policy_hash'):
        assert removed not in serialized


@pytest.mark.parametrize('fault',('missing_stop','stale','outside_session','instrument','grid'))
def test_unsafe_open_rejected(now,fault):
    raw=open_decision(now);context=alpha_context(now);when=now
    if fault=='missing_stop':del raw['open']['hard_stop_usd']
    elif fault=='stale':when=now+timedelta(seconds=31)
    elif fault=='outside_session':
        when=now.replace(hour=13);context=context.model_copy(update={'as_of':when,'expires_at':when+timedelta(seconds=30),
            'instruments':(context.instruments[0].model_copy(update={'quote_at':when}),)})
    elif fault=='instrument':raw['open']['instrument_id']='unknown'
    elif fault=='grid':raw['open']['entry_limit_usd']='100.005'
    if fault=='missing_stop':
        with pytest.raises(DecisionError):parsed(raw)
    else:
        with pytest.raises(DecisionError):validate_alpha_semantics(parsed(raw),context,when)


def test_hold_requires_owned_reconciled_position(now):
    d=parsed(decision('HOLD',{'position_id':'position-a','next_review_at':(now+timedelta(minutes=5)).isoformat()}))
    assert validate_alpha_semantics(d,alpha_context(now,position=True),now).action=='HOLD'
    with pytest.raises(DecisionError,match='position'):validate_alpha_semantics(d,alpha_context(now),now)


@pytest.mark.parametrize('changes',(
    {'requested_stop_usd':'96'},
    {'target_action':'SET','requested_target_usd':'106'},
    {'target_action':'REMOVE'},
    {'requested_next_review_at':'2026-09-11T14:10:00Z'},
    {'requested_latest_exit_at':'2026-09-11T19:30:00Z'},
    {'requested_latest_exit_at':'2026-09-11T19:45:00Z'},
))
def test_bounded_modifications_allowed(now,changes):
    payload={'position_id':'position-a','requested_stop_usd':None,'target_action':'KEEP',
        'requested_target_usd':None,'requested_next_review_at':None,'requested_latest_exit_at':None}|changes
    assert validate_alpha_semantics(parsed(decision('MODIFY',payload)),alpha_context(now,position=True),now).action=='MODIFY'


@pytest.mark.parametrize('changes',(
    {'requested_latest_exit_at':'2026-09-11T19:56:00Z'},
    {'requested_stop_usd':'94'},
    {'requested_quantity':'101'},
))
def test_risk_increasing_or_unsupported_modifications_rejected(now,changes):
    payload={'position_id':'position-a','requested_stop_usd':None,'target_action':'KEEP',
        'requested_target_usd':None,'requested_next_review_at':None,'requested_latest_exit_at':None}|changes
    with pytest.raises(DecisionError):validate_alpha_semantics(parsed(decision('MODIFY',payload)),alpha_context(now,position=True),now)


def test_close_is_full_reconciled_position_only(now):
    d=parsed(decision('CLOSE',{'position_id':'position-a','full_close':True}))
    assert validate_alpha_semantics(d,alpha_context(now,position=True),now).close.full_close
    for payload in ({'position_id':'unknown','full_close':True},
                    {'position_id':'position-a','full_close':False},
                    {'position_id':'position-a','full_close':True,'quantity':'1'}):
        with pytest.raises(DecisionError):validate_alpha_semantics(parsed(decision('CLOSE',payload)),alpha_context(now,position=True),now)


def test_legacy_v1_parser_remains_available(proposal):
    assert parse_trade_decision(json.dumps(proposal)).schema_version=='1.0'


def test_deep_plan_and_fast_boundary_have_no_execution_or_credentials(now):
    context=alpha_context(now)
    plan=DeepAlphaPlan(regime_label='MIXED',regime_view='Mixed tape; retain only clear setups.',focus=())
    assert plan.focus==()
    deep=deep_alpha_request(context,{'max_position_exposure':'0.20'})
    fast=fast_alpha_request(context,mode='ENTRY',hard_policy={'max_position_exposure':'0.20'})
    for body in (deep,fast):
        assert 'tools' not in body
        text=json.dumps(body).lower()
        for forbidden in ('api_key','app_secret','authorization','client_order_id','submit_order'):
            assert forbidden not in text
    assert deep['text']['format']['name']=='DeepAlphaPlan'
    branches=fast['text']['format']['schema']['properties']['decision']['anyOf']
    assert [x['properties']['action']['enum'] for x in branches]==[['ABSTAIN'],['OPEN']]


def test_service_stamps_provenance_after_model_output(now):
    context=alpha_context(now);d=parsed(decision('ABSTAIN'))
    envelope=stamp_alpha_decision(d,context,created_at=now,policy_version='policy-v1',
        capability_version='capability-v1',model='gpt-6-astra',reasoning='medium',
        usage={'input_tokens':100,'output_tokens':20},estimated_cost_usd='0.002')
    assert envelope.decision.action=='ABSTAIN' and envelope.context_hash
    assert 'model' not in type(d).model_fields and envelope.model=='gpt-6-astra'


def test_existing_context_adapter_is_read_only_shape(context):
    adapted=adapt_alpha_context((context,),remaining_risk_usd='500')
    assert adapted.instruments[0].instrument_id==context.instrument.instrument_id
    assert adapted.settled_cash_usd==context.account.balances.settled_usd
    assert adapted.reconciliation_ready and adapted.settlement_known


def test_closed_market_boundary_constructs_abstain_without_order_authority(now):
    closed=now.replace(hour=21)
    context=alpha_context(closed)
    assert validate_alpha_semantics(parsed(decision('ABSTAIN')),context,closed).action=='ABSTAIN'
    body=fast_alpha_request(context,mode='ENTRY',hard_policy={'regular_session_only':True})
    assert 'tools' not in body and body['store'] is False
