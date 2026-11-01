import json
from datetime import timedelta

from autonomous_trader.monitoring.measured_feedback import project_measured_feedback


def decision(now,*,plan='plan-1',work='work-1',strategy='adaptive evidence',regime='FAST_MARKET'):
    return (now,{'action':'OPEN','model_response_at':now.isoformat(),'outcome_plan_id':plan,
        'model_work_result_id':work,'regime_label':regime,
        'stated_horizon_start':now.isoformat(),'stated_horizon_end':(now+timedelta(minutes=30)).isoformat(),
        'entry_exit_plan':{'strategy_label':strategy}})


def outcome(now,*,plan='plan-1',closed=10,net='8',broker_net='8.3',model_cost='.3',
        exit_reason='ASTRA_CLOSE',failure_origin='ASTRA_DECISION',quality='FULL_RUNTIME',extra=None):
    value={'plan_id':plan,'closed_at':(now+timedelta(minutes=closed)).isoformat(),
        'strategy_label':'outcome fallback','market_regime':'OUTCOME_REGIME',
        'realized_gross_usd':'9','broker_fees_usd':'.7',
        'realized_net_after_broker_fees_usd':broker_net,
        'realized_net_after_all_known_costs_usd':net,'model_cost_usd':model_cost,
        'capital_deployed_usd':'1000','capital_time_usd_seconds':'1800000',
        'maximum_favorable_excursion_usd':'20','maximum_adverse_excursion_usd':'5',
        'win_loss':'WIN' if float(net)>0 else 'LOSS','exit_reason':exit_reason,
        'failure_origin':failure_origin,'evidence_quality':quality}
    value.update(extra or {})
    return value


def result(now,*,work='work-1',cost='.2'):
    return (now+timedelta(seconds=1),{'id':work,'cost_usd':cost})


def group(value):
    parsed=json.loads(value)
    assert parsed['scope']=='PRIOR_CLOSED_TRADES_ONLY'
    assert parsed['learning_claim']=='MEASURED_FEEDBACK_NOT_REINFORCEMENT_LEARNING'
    assert parsed['edge_claim']=='NONE'
    return parsed['groups'][0]


def test_sparse_group_is_exploratory_and_uses_stable_decision_dimensions(now):
    as_of=now+timedelta(hours=1)
    value=project_measured_feedback([decision(now)],[outcome(now)],[result(now)],as_of=as_of)
    item=group(value)
    assert item['strategy']=='adaptive evidence' and item['regime']=='FAST_MARKET'
    assert item['planned_horizon_seconds']=='1800'
    assert item['sample_count']==item['comparable_closed_trades']==1
    assert item['status']=='EXPLORATORY' and item['realized_net_after_broker_and_known_model_costs_usd']=='8'
    assert item['actual_measured_slippage_usd']==item['actual_measured_spread_bps']=='UNKNOWN'


def test_unknown_canonical_cost_stays_unknown_instead_of_zero(now):
    value=project_measured_feedback([decision(now)],[outcome(now)],[],as_of=now+timedelta(hours=1))
    item=group(value)
    assert item['unknown_cost_count']==1
    assert item['known_model_cost_usd']=='UNKNOWN'
    assert item['realized_net_after_broker_and_known_model_costs_usd']=='UNKNOWN'
    assert 'UNKNOWN_MODEL_COST' in item['completeness_issues']


def test_duplicate_model_result_is_not_double_counted_and_projection_is_restart_stable(now):
    inputs=([decision(now)],[outcome(now,model_cost='.3')],[result(now,cost='.2'),result(now,cost='.2')])
    first=project_measured_feedback(*inputs,as_of=now+timedelta(hours=1))
    second=project_measured_feedback(*inputs,as_of=now+timedelta(hours=1))
    assert first==second and len(first)<=1950
    assert group(first)['known_model_cost_usd']=='0.3'


def test_current_and_future_evidence_cannot_enter_prior_summary(now):
    as_of=now+timedelta(hours=1)
    decisions=[decision(now),decision(as_of,plan='future',work='future-work')]
    outcomes=[outcome(now),outcome(now,plan='future',closed=70,net='100')]
    results=[result(now),result(as_of,work='future-work')]
    item=group(project_measured_feedback(decisions,outcomes,results,as_of=as_of))
    assert item['sample_count']==1 and item['realized_net_after_broker_fees_usd']=='8.3'


def test_safety_recovery_economics_are_retained_but_not_strategy_validation(now):
    row=outcome(now,net='-10',broker_net='-9.7',exit_reason='SAFETY_LATCH',
        failure_origin='INFRASTRUCTURE_OR_SAFETY',quality='LEDGER_LEGACY_NO_EXCURSION')
    item=group(project_measured_feedback([], [row], [],as_of=now+timedelta(hours=1)))
    assert item['realized_net_after_broker_fees_usd']=='-9.7'
    assert item['sample_count']==1 and item['comparable_closed_trades']==0
    assert item['nonvalidating_safety_recovery_legacy']==1 and item['status']=='EXPLORATORY'


def test_actual_spread_and_slippage_only_appear_when_explicitly_measured(now):
    row=outcome(now,extra={'measured_slippage_usd':'1.25','measured_spread_bps':'12.5'})
    item=group(project_measured_feedback([decision(now)],[row],[result(now)],as_of=now+timedelta(hours=1)))
    assert item['actual_measured_slippage_usd']=='1.25'
    assert item['actual_measured_spread_bps']=='12.5'
