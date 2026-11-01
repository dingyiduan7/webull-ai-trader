from datetime import timedelta
from decimal import Decimal
from uuid import uuid4
import pytest
from pydantic import ValidationError
from autonomous_trader.economic_policy import (SandboxEconomics, PolicyProposal,
    AuthorizeSandboxEconomics, ReleaseSandboxPreferences, AuthorizeDailyDownsideBudget, AuthorizeModelBudgetCap,
    ReviseSandboxEconomics,
    account_fingerprint, effective_policy, daily_downside_budget)
from autonomous_trader.durable.ledger import Ledger
from autonomous_trader.durable.models import Mark, Incident, Protection, RefreshPermit, NewSession
from autonomous_trader.durable.projection import Rejected, reduce_event, digest
from autonomous_trader.reference import Session


def enable(h):
    h.ledger.execution_scope='SANDBOX'
    return h.send(AuthorizeSandboxEconomics())


def change(h, *, release=(), **policy):
    s=h.state
    return ReviseSandboxEconomics(proposal=PolicyProposal(based_revision=s['economic_policy']['revision'],
        based_policy_hash=s['policy_hash'],policy=SandboxEconomics(**(s['economic_policy']['policy']|policy)),
        release_halts=release,reason='Fresh paper-account economics',evidence='Current reconciled account',
        expected_incremental_value_usd='100'),context_hash='fixture-context',context_at=h.at,response_at=h.at,
        account_fingerprint=account_fingerprint(s),model='gpt-6-astra',reasoning='medium',decision_id=uuid4().hex)


def test_scope_defaults_replay_and_version(h):
    before=h.state
    assert h.ledger.rebuild(h.account_id)==before
    with pytest.raises(Rejected,match='SANDBOX_POLICY_SCOPE_REQUIRED'):h.send(AuthorizeSandboxEconomics())
    enable(h);command=change(h,stock_exposure='0.7',trade_risk='0.1',open_risk='0.3',maximum_positions=8,calls_per_300s=40)
    s=h.send(command)
    assert effective_policy(s).stock_exposure==Decimal('.7')
    assert s['economic_policy']['revision']==2
    assert h.ledger.rebuild(h.account_id)==s
    assert Ledger(h.ledger.config).snapshot(h.account_id)['policy_hash']==s['policy_hash']
    with pytest.raises(Rejected,match='STALE_POLICY_VERSION'):h.send(command)


def test_sandbox_economic_defaults_replay_by_envelope_version(h):
    h.ledger.execution_scope='SANDBOX'
    before=h.state
    current=h.send(AuthorizeSandboxEconomics())
    event=h.ledger.journal(h.account_id)[-1]
    assert event['economic_policy_default_version']==1
    assert effective_policy(current).model_budget_usd==Decimal('125')

    legacy=dict(event)
    legacy.pop('economic_policy_default_version')
    historical=reduce_event(before,legacy)
    assert effective_policy(historical).model_budget_usd==Decimal('100')


def test_higher_policy_applies_at_actual_admission(h,order_factory):
    enable(h)
    order=order_factory(h,quantity='5000',stop='99')
    with pytest.raises(Rejected,match='STOCK_EXPOSURE_LIMIT'):h.admit(order)
    h.send(change(h,stock_exposure='0.7',trade_risk='0.1',open_risk='0.3'))
    h.admit(order)
    assert h.state['reservations'][order.intent_id]['cash_usd']=='500000'
    with pytest.raises(Rejected,match='POLICY_ORDER_AUTHORITY_ACTIVE'):h.send(change(h,stock_exposure='0.8'))


@pytest.mark.parametrize('values',[{'account_exposure':'1.01'},{'stock_exposure':'1.01'},
    {'trade_risk':'.6','open_risk':'.5'},{'daily_loss':'0'},{'maximum_positions':10},
    {'calls_per_300s':121},{'model_budget_usd':'276'},{'margin':True},{'drawdown':'NaN'}])
def test_immutable_geometry(values):
    with pytest.raises(ValidationError):SandboxEconomics(**values)


def test_incident_unknown_settlement_and_stale_context_cannot_be_released(h):
    enable(h);cmd=change(h)
    h.at+=timedelta(seconds=91);h.reconcile()
    with pytest.raises(Rejected,match='STALE_POLICY_CONTEXT'):h.send(cmd)
    h.reconcile(settlement_known=False)
    with pytest.raises(Rejected,match='UNKNOWN_SETTLEMENT'):h.send(change(h))
    h.reconcile();h.send(Incident(reason='BROKEN_DATA',evidence_id='fixture'))
    with pytest.raises(Rejected,match='RECONCILING'):h.send(change(h))
    with pytest.raises(ValidationError):change(h,release=('incident',))


@pytest.mark.parametrize('latch',['daily','drawdown','terminated'])
def test_economic_latch_release_preserves_history(h,latch):
    enable(h)
    # Exercise reducer at an exactly reconciled historic loss checkpoint without
    # pretending these synthetic values were broker observations.
    s=h.state;s['latches'][latch]=True
    cmd=change(h,release=(latch,),daily_loss='0.8',drawdown='0.8',experiment_death='0.99')
    cmd=cmd.model_copy(update={'account_fingerprint':account_fingerprint(s)})
    event=dict(h.ledger.journal(h.account_id)[-1],command=cmd.model_dump(mode='json'),
        sequence=s['version']+1,event_id=uuid4().hex,policy_scope='SANDBOX')
    result=reduce_event(s,event)
    assert not result['latches'][latch]
    for key in ('historic_high_water','segments','executions','daily_realized','restarts'):
        assert result[key]==s[key]
    assert result['policy_history'][-1]['decision']['proposal']['release_halts']==[latch]


def test_post_response_reconciliation_required(h):
    enable(h);cmd=change(h)
    h.at+=timedelta(seconds=1)
    cmd=cmd.model_copy(update={'response_at':h.at})
    with pytest.raises(Rejected,match='POST_RESPONSE_RECONCILIATION'):h.send(cmd)
    h.reconcile();h.send(cmd)


def test_closed_session_policy_cannot_release(h):
    enable(h);h.at+=timedelta(days=1);cmd=change(h)
    with pytest.raises(Rejected,match='CLOSED'):h.send(cmd)


def test_active_holdings_cannot_be_invalidated_by_tighter_policy(h,order_factory):
    enable(h);h.send(change(h,stock_exposure='0.8',trade_risk='0.1',open_risk='0.4'))
    h.trade(order_factory(h,quantity='3000'))
    with pytest.raises(Rejected,match='POLICY_CONFLICTS_WITH_ACTIVE_EXPOSURE'):
        h.send(change(h,stock_exposure='0.2'))
    assert effective_policy(h.state).stock_exposure==Decimal('0.8')


def test_real_loss_halts_released_without_resetting_loss_history(h,order_factory):
    enable(h);h.send(change(h,stock_exposure='1',trade_risk='1',open_risk='1'))
    h.trade(order_factory(h,quantity='9900',stop='1'))
    h.send(Mark(instrument_id='stock-0',bid_usd='1',observed_at=h.at,source_id='loss-fixture'))
    assert all(h.state['latches'][name] for name in ('daily','drawdown','terminated'))
    h.trade(order_factory(h,quantity='9900',price='1',side='SELL_OWNED'))
    before=h.state
    with pytest.raises(Rejected,match='POLICY_RELEASE_THRESHOLD_STILL_BREACHED'):
        h.send(change(h,release=('daily','drawdown','terminated')))
    after=h.send(change(h,release=('daily','drawdown','terminated'),daily_loss='1',drawdown='1',experiment_death='1'))
    assert not any(after['latches'].values())
    assert after['daily_realized']==before['daily_realized']
    assert after['historic_high_water']==before['historic_high_water']
    assert after['cash']==before['cash']
    assert h.ledger.rebuild(h.account_id)==after


def test_owner_preference_release_preserves_history_and_replays(h):
    enable(h);before=h.state
    after=h.send(ReleaseSandboxPreferences())
    policy=effective_policy(after)
    assert (policy.stock_exposure,policy.trade_risk,policy.open_risk)==(1,1,1)
    assert (policy.maximum_positions,policy.deep_min_interval_seconds,policy.calls_per_300s)==(9,0,120)
    assert (policy.daily_loss,policy.drawdown,policy.experiment_death,policy.model_budget_usd)==(
        Decimal('.05'),Decimal('.20'),Decimal('.90'),125)
    for key in ('cash','historic_high_water','segments','executions','daily_realized','restarts','latches'):
        assert after[key]==before[key]
    assert after['policy_history'][:-1]==before['policy_history']
    assert after['economic_policy']['revision']==2
    assert after['economic_policy']['origin']=='OWNER_PREFERENCES_RELEASED'
    assert after['policy_hash']!=before['policy_hash']
    assert h.ledger.rebuild(h.account_id)==after
    with pytest.raises(Rejected,match='PREFERENCES_ALREADY_RELEASED'):
        h.send(ReleaseSandboxPreferences())


def test_owner_model_budget_cap_migrates_durably(h):
    enable(h)
    h.send(change(h,model_budget_usd='100'))
    before=h.state
    after=h.send(AuthorizeModelBudgetCap())
    assert effective_policy(after).model_budget_usd==Decimal('275')
    assert after['economic_policy']['model_budget_cap_authorization_id']=='owner-model-budget-cap-275-20260925'
    assert after['economic_policy']['model_budget_cap_authorized'] is True
    assert after['economic_policy']['origin']=='OWNER_MODEL_BUDGET_CAP'
    assert after['economic_policy']['revision']==before['economic_policy']['revision']+1
    assert after['cash']==before['cash'] and after['latches']==before['latches']
    assert h.ledger.rebuild(h.account_id)==after
    with pytest.raises(Rejected,match='MODEL_BUDGET_CAP_ALREADY_AUTHORIZED'):
        h.send(AuthorizeModelBudgetCap())


def test_owner_preference_release_requires_sandbox_clean_reconciliation(h,order_factory):
    with pytest.raises(Rejected,match='SANDBOX_POLICY_SCOPE_REQUIRED'):
        h.send(ReleaseSandboxPreferences())
    enable(h)
    h.reconcile(settlement_known=False)
    with pytest.raises(Rejected,match='UNKNOWN_SETTLEMENT'):
        h.send(ReleaseSandboxPreferences())
    h.reconcile()
    h.admit(order_factory(h))
    with pytest.raises(Rejected,match='POLICY_ORDER_AUTHORITY_ACTIVE'):
        h.send(ReleaseSandboxPreferences())


@pytest.mark.parametrize('fault',['expired_reconciliation','incident','daily'])
def test_owner_preference_release_cannot_clear_safety_or_reconciliation(h,fault):
    enable(h);state=h.state
    if fault=='expired_reconciliation':state['reconciliation']['valid_until']=(h.at-timedelta(seconds=1)).isoformat()
    else:state['latches'][fault]=True
    event=dict(h.ledger.journal(h.account_id)[-1],command=ReleaseSandboxPreferences().model_dump(mode='json'),
        sequence=state['version']+1,event_id=uuid4().hex,policy_scope='SANDBOX')
    with pytest.raises(Rejected,match='RECONCILING|SAFETY_LATCHED'):
        reduce_event(state,event)


def test_released_preferences_admit_larger_astra_allocation(h,order_factory):
    enable(h);h.send(ReleaseSandboxPreferences())
    # 40% allocation and 10% planned loss exceed all three former preferences.
    order=order_factory(h,quantity='4000',stop='75')
    h.admit(order)
    assert h.state['reservations'][order.intent_id]['cash_usd']=='400000'
    # Settled account assets still bound aggregate admission.
    with pytest.raises(Rejected,match='CASH|ACCOUNT_EXPOSURE'):
        h.admit(order_factory(h,symbol='stock-1',quantity='7000',stop='75'))


def test_released_preferences_allow_six_positions(h,order_factory):
    enable(h);h.send(ReleaseSandboxPreferences())
    for batch in range(3):
        for offset in range(2):
            i=batch*2+offset
            order=order_factory(h,symbol=f'stock-{i}').model_copy(update={'deadline':h.at+timedelta(hours=1)})
            h.trade(order)
        if batch<2:
            old=Session.model_validate(h.state['session'])
            days=3 if batch==0 else 1
            h.at+=timedelta(days=days)
            h.reconcile()
            session=old.model_copy(update={'label':old.label+timedelta(days=days),
                'opens_at':old.opens_at+timedelta(days=days),'closes_at':old.closes_at+timedelta(days=days)})
            h.send(NewSession(session=session))
            for instrument_id in h.state['instruments']:
                h.send(Mark(instrument_id=instrument_id,bid_usd='100',observed_at=h.at,source_id='session-rollover'))
            h.reconcile()
    assert len(h.state['positions'])==6


@pytest.mark.parametrize('values',[{'daily_loss':'0.1'},{'drawdown':'0.3'},
    {'experiment_death':'0.99'},{'release':('daily',)}])
def test_released_preferences_do_not_delegate_catastrophic_policy(h,values):
    enable(h);h.send(ReleaseSandboxPreferences())
    with pytest.raises(Rejected,match='CATASTROPHIC_POLICY_IMMUTABLE'):
        h.send(change(h,**values))
    assert h.state['economic_policy']['revision']==2


def test_preference_marker_survives_later_valid_revision(h):
    enable(h);h.send(ReleaseSandboxPreferences())
    result=h.send(change(h,stock_exposure='0.8'))
    assert result['economic_policy']['preferences_released']
    assert h.ledger.rebuild(h.account_id)==result
    with pytest.raises(Rejected,match='CATASTROPHIC_POLICY_IMMUTABLE'):
        h.send(change(h,drawdown='0.8'))


def enable_daily_bound(h):
    enable(h);h.send(ReleaseSandboxPreferences())
    return h.send(AuthorizeDailyDownsideBudget())


def test_daily_bound_near_total_loss_rejected_full_capital_tight_stop_allowed(h,order_factory):
    enable_daily_bound(h)
    with pytest.raises(Rejected,match='DAILY_DOWNSIDE_BUDGET'):
        h.admit(order_factory(h,quantity='9999',stop='1'))
    order=order_factory(h,quantity='9999',stop='99')
    h.admit(order,fee='100',exit_cost='100',stress='1000')
    assert h.state['cash']['reserved_usd']=='1000000'
    assert h.state['reservations'][order.intent_id]['risk_usd']=='11199'


def test_daily_bound_holdings_pending_buys_and_sells_share_capacity(h,order_factory):
    enable_daily_bound(h)
    h.trade(order_factory(h,quantity='2500',stop='90'))
    h.admit(order_factory(h,symbol='stock-1',quantity='2500',stop='90'))
    # An unfilled owned-share exit provides no risk offset.
    h.admit(order_factory(h,quantity='2500',price='100',side='SELL_OWNED'))
    with pytest.raises(Rejected,match='DAILY_DOWNSIDE_BUDGET'):
        h.admit(order_factory(h,symbol='stock-2',quantity='1',stop='90'))
    assert h.ledger.rebuild(h.account_id)==h.state


def test_daily_bound_reduced_by_realized_loss_with_fees(h,order_factory):
    enable_daily_bound(h)
    h.trade(order_factory(h,quantity='1000',stop='70'),fee='10')
    h.trade(order_factory(h,quantity='1000',price='80',side='SELL_OWNED'),fee='10')
    assert daily_downside_budget(h.state)==Decimal('29980')
    with pytest.raises(Rejected,match='DAILY_DOWNSIDE_BUDGET'):
        h.admit(order_factory(h,symbol='stock-1',quantity='1000',stop='70'))
    h.admit(order_factory(h,symbol='stock-1',quantity='999',stop='70'))


def test_daily_bound_add_and_stop_amendment_cannot_expand_risk(h,order_factory):
    enable_daily_bound(h)
    h.trade(order_factory(h,quantity='2000',stop='80'))
    with pytest.raises(Rejected,match='DAILY_DOWNSIDE_BUDGET'):
        h.admit(order_factory(h,quantity='1000',stop='80'))
    with pytest.raises(Rejected,match='STOP_LOOSENING_OR_GEOMETRY'):
        h.send(Protection(instrument_id='stock-0',quantity='2000',stop_usd='60',evidence_id='unsafe-amend'))
    h.send(Protection(instrument_id='stock-0',quantity='2000',stop_usd='90',evidence_id='safe-amend'))
    h.admit(order_factory(h,quantity='1000',stop='90'))


def test_daily_bound_revalidated_before_dispatch_after_equity_decline(h,order_factory):
    enable_daily_bound(h)
    h.trade(order_factory(h,quantity='1000',stop='90'))
    order=order_factory(h,symbol='stock-1',quantity='1000',stop='60')
    h.admit(order)
    h.send(Mark(instrument_id='stock-0',bid_usd='95',observed_at=h.at,source_id='fresh-decline'))
    h.reconcile()
    with pytest.raises(Rejected,match='RISK_REVALIDATION_FAILED'):
        h.send(RefreshPermit(intent_id=order.intent_id))


def test_daily_bound_is_durable_and_preserved_by_later_policy(h):
    before=enable_daily_bound(h)
    assert before['economic_policy']['revision']==3
    assert before['economic_policy']['daily_downside_budget']
    assert h.ledger.rebuild(h.account_id)==before
    h.send(change(h,stock_exposure='0.9'))
    assert h.state['economic_policy']['daily_downside_budget']
    assert daily_downside_budget(Ledger(h.ledger.config).snapshot(h.account_id))==Decimal('50000')
    with pytest.raises(Rejected,match='DAILY_DOWNSIDE_ALREADY_AUTHORIZED'):
        h.send(AuthorizeDailyDownsideBudget())


def test_owner_budget_increase_preserves_old_event_replay(h):
    enable(h)
    old=h.send(AuthorizeModelBudgetCap(authorization_id='owner-model-budget-cap-125-20260923'))
    assert effective_policy(old).model_budget_usd==Decimal('125')
    middle=h.send(AuthorizeModelBudgetCap(authorization_id='owner-model-budget-cap-225-20260924'))
    assert effective_policy(middle).model_budget_usd==Decimal('225')
    after=h.send(AuthorizeModelBudgetCap())
    assert effective_policy(after).model_budget_usd==Decimal('275')
    ids=[row.get('model_budget_cap_authorization_id') for row in after['policy_history']]
    assert ids[-3:]==['owner-model-budget-cap-125-20260923','owner-model-budget-cap-225-20260924',
        'owner-model-budget-cap-275-20260925']
    assert after['cash']==old['cash'] and after['latches']==old['latches']
    assert h.ledger.rebuild(h.account_id)==after
    with pytest.raises(Rejected,match='MODEL_BUDGET_CAP_ALREADY_AUTHORIZED'):
        h.send(AuthorizeModelBudgetCap(authorization_id='owner-model-budget-cap-125-20260923'))
