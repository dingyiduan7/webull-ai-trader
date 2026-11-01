from datetime import datetime,timedelta,timezone

import pytest

from autonomous_trader.research.walk_forward import evaluate_walk_forward


START=datetime(2026,9,1,14,30,tzinfo=timezone.utc)


def decision(at,identity,*,action='OPEN'):
    opened={'strategy_label':'quality evidence'} if action=='OPEN' else None
    return (at+timedelta(seconds=2),{'decision_id':identity,'action':action,
        'decision_snapshot_at':at.isoformat(),'model_response_at':(at+timedelta(seconds=1)).isoformat(),
        'decision_cutoff_at':(at+timedelta(seconds=30)).isoformat(),'model_work_result_id':'work-'+identity,
        'outcome_plan_id':identity if action=='OPEN' else None,'entry_exit_plan':opened,
        'regime_label':'NORMAL','stated_horizon_start':at.isoformat(),
        'stated_horizon_end':(at+timedelta(hours=1)).isoformat() if action=='OPEN' else None})


def outcome(at,identity,*,net='9',broker='10',cost='1',quality='FULL_RUNTIME',
        exit_reason='ASTRA_CLOSE',failure_origin='ASTRA_DECISION'):
    return {'plan_id':identity,'closed_at':at.isoformat(),'strategy_label':'quality evidence','market_regime':'NORMAL',
        'realized_gross_usd':'11','broker_fees_usd':'1','realized_net_after_broker_fees_usd':broker,
        'realized_net_after_all_known_costs_usd':net,'model_cost_usd':cost,
        'capital_deployed_usd':'1000','capital_time_usd_seconds':'3600000',
        'maximum_favorable_excursion_usd':'20','maximum_adverse_excursion_usd':'5',
        'win_loss':'WIN' if not str(net).startswith('-') else 'LOSS','evidence_quality':quality,
        'exit_reason':exit_reason,'failure_origin':failure_origin}


def result(at,identity,cost='.5'):
    return (at,{'id':'work-'+identity,'cost_usd':cost})


def baseline(decision_at,evaluated_at,identity,*,defined_at=None,gross='7',fees='1',cost='0'):
    payload={'decision_id':identity,'rule_id':'EQUAL_NOTIONAL_CUTOFF_ASK_TO_HORIZON_BID_V1',
        'defined_at':(defined_at or decision_at-timedelta(days=1)).isoformat(),
        'evaluated_at':evaluated_at.isoformat(),'gross_pnl_usd':gross,'broker_fees_usd':fees,
        'model_cost_usd':cost,'capital_deployed_usd':'1000','capital_time_usd_seconds':'3600000'}
    return (evaluated_at+timedelta(seconds=1),payload)


def test_chronological_split_prevents_future_leakage_and_compares_predeclared_baseline():
    split=START+timedelta(days=1);as_of=split+timedelta(days=1)
    pre=decision(START,'pre');current=decision(split+timedelta(hours=1),'eval')
    future=decision(as_of+timedelta(hours=1),'future')
    report=evaluate_walk_forward([pre,current,future],
        [outcome(START+timedelta(hours=2),'pre'),outcome(split+timedelta(hours=2),'eval'),
            outcome(as_of+timedelta(hours=2),'future')],
        [result(START+timedelta(seconds=3),'pre'),result(split+timedelta(hours=1,seconds=3),'eval'),
            result(as_of+timedelta(hours=1,seconds=3),'future')],
        [baseline(split+timedelta(hours=1),split+timedelta(hours=2),'eval')],split_at=split,as_of=as_of)
    assert report['actions']=={'decisions':1,'open':1,'abstain':0,'unresolved_open':0}
    assert report['evaluation']['net_after_broker_and_known_model_costs_usd']=='9'
    assert report['baseline']['net_after_broker_and_known_model_costs_usd']=='6'
    assert report['matched_comparison']['net_difference_usd']=='3'
    assert report['training_evidence_at_split']['scope']=='PRIOR_CLOSED_TRADES_ONLY'
    assert report['evidence_quality']['future_outcomes_excluded']==1


def test_identical_duplicates_are_counted_once_and_conflicts_fail():
    split=START;as_of=START+timedelta(days=1);row=decision(START+timedelta(hours=1),'same')
    result_row=result(START+timedelta(hours=1,seconds=3),'same')
    report=evaluate_walk_forward([row,row],[outcome(START+timedelta(hours=2),'same')],
        [result_row,result_row],[],split_at=split,as_of=as_of)
    assert report['actions']['decisions']==1
    assert report['evidence_quality']['duplicate_decisions_ignored']==1
    assert report['evidence_quality']['duplicate_model_results_ignored']==1
    changed=(row[0],{**row[1],'regime_label':'DIFFERENT'})
    with pytest.raises(ValueError,match='WALK_FORWARD_DUPLICATE_CONFLICT'):
        evaluate_walk_forward([row,changed],[],[],[],split_at=split,as_of=as_of)


def test_unknown_model_cost_remains_unknown():
    at=START+timedelta(hours=1)
    report=evaluate_walk_forward([decision(at,'unknown')],[outcome(at+timedelta(hours=1),'unknown')],[],[],
        split_at=START,as_of=START+timedelta(days=1))
    assert report['evaluation']['unknown_cost_count']==1
    assert report['evaluation']['net_after_broker_and_known_model_costs_usd']=='UNKNOWN'
    assert 'UNKNOWN_MODEL_OR_API_COSTS' in report['uncertainty']['reasons']


def test_recovery_economics_are_retained_but_flagged_nonvalidating():
    at=START+timedelta(hours=1)
    row=outcome(at+timedelta(hours=1),'recovery',net='-21',broker='-20',quality='LEDGER_LEGACY_NO_EXCURSION',
        exit_reason='RECOVERY',failure_origin='INFRASTRUCTURE_OR_SAFETY')
    report=evaluate_walk_forward([decision(at,'recovery')],[row],[result(at+timedelta(seconds=3),'recovery')],[],
        split_at=START,as_of=START+timedelta(days=1))
    assert report['evaluation']['net_after_broker_fees_usd']=='-20'
    assert report['evaluation']['net_after_broker_and_known_model_costs_usd']=='-21'
    assert report['evidence_quality']['legacy_recovery_or_infrastructure_nonvalidating']==1


def test_post_cutoff_baseline_is_rejected_and_unmatched_outcome_economics_remain_visible():
    at=START+timedelta(hours=1);close=at+timedelta(hours=1)
    late=baseline(at,close,'eval',defined_at=at+timedelta(seconds=1))
    report=evaluate_walk_forward([decision(at,'eval')],[outcome(close,'eval'),outcome(close,'orphan',net='4',broker='5')],
        [result(at+timedelta(seconds=3),'eval')],[late],split_at=START,as_of=START+timedelta(days=1))
    assert report['baseline']['completed_decisions']==0
    assert report['evidence_quality']['invalid_or_post_cutoff_baselines']==1
    assert report['unmatched_outcome_economics']['net_after_broker_fees_usd']=='5'


def test_zero_sample_is_explicitly_insufficient_and_never_claims_edge():
    report=evaluate_walk_forward([],[],[],[],split_at=START,as_of=START+timedelta(days=1))
    assert report['evaluation']['completed_decisions']==0
    assert report['uncertainty']['status']=='INSUFFICIENT'
    assert 'FEWER_THAN_10_COMPLETED_DECISIONS' in report['uncertainty']['reasons']
    assert report['claim_boundary']=='DESCRIPTIVE_OFFLINE_COMPARISON_ONLY_NO_PROFITABILITY_OR_LEARNING_CLAIM'
