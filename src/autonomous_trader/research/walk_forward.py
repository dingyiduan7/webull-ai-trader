"""Read-only chronological evaluation of immutable portfolio decisions."""
from datetime import datetime,timezone
from decimal import Decimal,InvalidOperation
import json

from ..monitoring.measured_feedback import NONVALIDATING_EXITS,project_measured_feedback

BASELINE_RULES=frozenset({'CASH_ZERO_V1','EQUAL_NOTIONAL_CUTOFF_ASK_TO_HORIZON_BID_V1'})


def _at(value):
    if isinstance(value,datetime):parsed=value
    else:
        try:parsed=datetime.fromisoformat(str(value).replace('Z','+00:00'))
        except (TypeError,ValueError):return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else None


def _decimal(value):
    try:return Decimal(str(value))
    except (TypeError,ValueError,InvalidOperation):return None


def _number(value):
    if value is None:return 'UNKNOWN'
    text=format(Decimal(value).quantize(Decimal('.000001')),'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def _dedupe(rows,key):
    result={};duplicates=0
    for event_at,payload in rows:
        identity=payload.get(key)
        if not identity:continue
        prior=result.get(identity)
        if prior is not None:
            if prior[1]!=payload:raise ValueError('WALK_FORWARD_DUPLICATE_CONFLICT:'+str(identity))
            duplicates+=1;continue
        result[identity]=(_at(event_at),payload)
    return result,duplicates


def _drawdown(rows):
    total=Decimal(0);peak=Decimal(0);worst=Decimal(0)
    for row in sorted(rows,key=lambda item:item['at']):
        if row['all_cost_net'] is None:return None
        total+=row['all_cost_net'];peak=max(peak,total);worst=max(worst,peak-total)
    return worst


def _summary(rows):
    complete=bool(rows) and all(row['all_cost_net'] is not None for row in rows)
    broker_complete=all(row['broker_net'] is not None for row in rows)
    trades=[row for row in rows if row['trade']]
    known_trades=[row for row in trades if row['all_cost_net'] is not None]
    wins=sum(row['all_cost_net']>0 for row in known_trades);losses=sum(row['all_cost_net']<0 for row in known_trades)
    net=sum((row['all_cost_net'] for row in rows),Decimal(0)) if complete else None
    capital_complete=all(row['capital'] is not None for row in trades)
    capital=sum((row['capital'] for row in trades),Decimal(0)) if capital_complete else None
    time_complete=all(row['capital_time'] is not None for row in trades)
    capital_time=sum((row['capital_time'] for row in trades),Decimal(0)) if time_complete else None
    return {'completed_decisions':len(rows),'completed_trades':len(trades),'trade_wins':wins,'trade_losses':losses,
        'trade_win_rate':_number(Decimal(wins)/len(known_trades)) if known_trades else 'UNKNOWN',
        'net_after_broker_fees_usd':(_number(sum((row['broker_net'] for row in rows),Decimal(0)))
            if broker_complete else 'UNKNOWN'),
        'net_after_broker_and_known_model_costs_usd':_number(net),
        'unknown_cost_count':sum(row['all_cost_net'] is None for row in rows),
        'maximum_drawdown_after_known_costs_usd':_number(_drawdown(rows)) if complete else 'UNKNOWN',
        'return_on_deployed_capital':(_number(net/capital)
            if complete and capital_complete and capital else 'UNKNOWN'),
        'net_usd_per_capital_day':(_number(net/(capital_time/Decimal(86400)))
            if complete and time_complete and capital_time else 'UNKNOWN')}


def evaluate_walk_forward(decisions,outcomes,model_results,baselines,*,split_at,as_of):
    """Evaluate decisions in ``[split_at, as_of)`` without accessing external state.

    Decisions, model results, and baselines are ``(event_at, payload)`` pairs.
    Outcomes are corrected canonical payloads carrying ``closed_at``.
    """
    split_at=_at(split_at);as_of=_at(as_of)
    if split_at is None or as_of is None or split_at>=as_of:raise ValueError('WALK_FORWARD_WINDOW_INVALID')
    decision_map,decision_duplicates=_dedupe(decisions,'decision_id')
    result_map,result_duplicates=_dedupe(model_results,'id')
    baseline_map,baseline_duplicates=_dedupe(baselines,'decision_id')

    evaluation={}
    for identity,(event_at,payload) in decision_map.items():
        response=_at(payload.get('model_response_at'));snapshot=_at(payload.get('decision_snapshot_at'))
        if event_at is None or response is None or snapshot is None:continue
        if event_at<response or response<snapshot:raise ValueError('WALK_FORWARD_DECISION_TIME_INVALID')
        if event_at<as_of and split_at<=response<as_of:evaluation[identity]=payload

    outcomes_by_plan={};duplicate_outcomes=0;future_outcomes=0
    for payload in outcomes:
        plan=payload.get('plan_id');closed=_at(payload.get('closed_at'))
        if not plan or closed is None:continue
        if closed>=as_of:future_outcomes+=1;continue
        prior=outcomes_by_plan.get(plan)
        if prior is not None:
            if prior==payload:duplicate_outcomes+=1;continue
            prior_full=prior.get('evidence_quality')=='FULL_RUNTIME';current_full=payload.get('evidence_quality')=='FULL_RUNTIME'
            if prior_full!=current_full:
                outcomes_by_plan[plan]=prior if prior_full else payload;duplicate_outcomes+=1;continue
            raise ValueError('WALK_FORWARD_OUTCOME_CONFLICT:'+str(plan))
        outcomes_by_plan[plan]=payload

    actual=[];used_plans=set();unresolved_open=0;partial=0;nonvalidating=0
    for decision_id,decision in sorted(evaluation.items(),key=lambda item:item[1]['model_response_at']):
        response=_at(decision['model_response_at']);result=result_map.get(decision.get('model_work_result_id'))
        result_payload=result[1] if result and result[0] is not None and result[0]<as_of else None
        entry_cost=_decimal((result_payload or {}).get('cost_usd'))
        if decision.get('action')=='ABSTAIN':
            actual.append({'decision_id':decision_id,'at':response,'trade':False,'broker_net':Decimal(0),
                'all_cost_net':-entry_cost if entry_cost is not None else None,
                'capital':Decimal(0),'capital_time':Decimal(0),'nonvalidating':False})
            continue
        if decision.get('action')!='OPEN':partial+=1;continue
        plan=decision.get('outcome_plan_id');outcome=outcomes_by_plan.get(plan)
        if outcome is None:unresolved_open+=1;continue
        used_plans.add(plan);closed=_at(outcome.get('closed_at'))
        if closed is None or closed<response:partial+=1;continue
        broker_net=_decimal(outcome.get('realized_net_after_broker_fees_usd'))
        all_net=_decimal(outcome.get('realized_net_after_all_known_costs_usd'))
        attributed=_decimal(outcome.get('model_cost_usd'))
        complete=(entry_cost is not None and attributed is not None and attributed>=entry_cost and all_net is not None)
        invalid=(outcome.get('evidence_quality')!='FULL_RUNTIME'
            or any(token in str(outcome.get('failure_origin') or '') for token in ('INFRASTRUCTURE','SAFETY'))
            or str(outcome.get('exit_reason') or '') in NONVALIDATING_EXITS)
        nonvalidating+=invalid;partial+=broker_net is None
        actual.append({'decision_id':decision_id,'at':closed,'trade':True,'broker_net':broker_net,
            'all_cost_net':all_net if complete else None,'capital':_decimal(outcome.get('capital_deployed_usd')),
            'capital_time':_decimal(outcome.get('capital_time_usd_seconds')),'nonvalidating':invalid})

    baseline=[];invalid_baselines=0
    for decision_id,decision in evaluation.items():
        record=baseline_map.get(decision_id)
        if record is None:continue
        event_at,payload=record;defined=_at(payload.get('defined_at'));evaluated=_at(payload.get('evaluated_at'))
        snapshot=_at(decision.get('decision_snapshot_at'));response=_at(decision.get('model_response_at'))
        if (event_at is None or defined is None or evaluated is None or event_at<evaluated or event_at>=as_of
                or defined>snapshot or evaluated<response or evaluated>=as_of
                or payload.get('rule_id') not in BASELINE_RULES):
            invalid_baselines+=1;continue
        gross=_decimal(payload.get('gross_pnl_usd'));fees=_decimal(payload.get('broker_fees_usd'))
        model_cost=_decimal(payload.get('model_cost_usd'));capital=_decimal(payload.get('capital_deployed_usd'))
        if (payload.get('rule_id')=='CASH_ZERO_V1'
                and any(value!=0 for value in (gross,fees,model_cost,capital)
                    if value is not None)):
            invalid_baselines+=1;continue
        broker_net=gross-fees if gross is not None and fees is not None else None
        baseline.append({'decision_id':decision_id,'at':evaluated,'trade':bool(capital and capital>0),
            'broker_net':broker_net,'all_cost_net':broker_net-model_cost if broker_net is not None and model_cost is not None else None,
            'capital':capital,'capital_time':_decimal(payload.get('capital_time_usd_seconds')),'nonvalidating':False})

    orphan=[]
    for plan,outcome in outcomes_by_plan.items():
        if plan in used_plans:continue
        closed=_at(outcome.get('closed_at'))
        if closed is None or closed<split_at:continue
        orphan.append({'at':closed,'trade':True,
            'broker_net':_decimal(outcome.get('realized_net_after_broker_fees_usd')),
            'all_cost_net':_decimal(outcome.get('realized_net_after_all_known_costs_usd')),
            'capital':_decimal(outcome.get('capital_deployed_usd')),
            'capital_time':_decimal(outcome.get('capital_time_usd_seconds'))})

    matched={row['decision_id'] for row in actual}&{row['decision_id'] for row in baseline}
    actual_matched=[row for row in actual if row['decision_id'] in matched]
    baseline_matched=[row for row in baseline if row['decision_id'] in matched]
    actual_matched_summary=_summary(actual_matched);baseline_matched_summary=_summary(baseline_matched)
    actual_net=_decimal(actual_matched_summary['net_after_broker_and_known_model_costs_usd'])
    baseline_net=_decimal(baseline_matched_summary['net_after_broker_and_known_model_costs_usd'])
    difference=_number(actual_net-baseline_net) if matched and actual_net is not None and baseline_net is not None else 'UNKNOWN'
    training=project_measured_feedback(decisions,outcomes,model_results,as_of=split_at)
    reasons=[]
    if len(actual)<10:reasons.append('FEWER_THAN_10_COMPLETED_DECISIONS')
    if any(row['all_cost_net'] is None for row in actual):reasons.append('UNKNOWN_MODEL_OR_API_COSTS')
    if nonvalidating:reasons.append('LEGACY_RECOVERY_OR_INFRASTRUCTURE_OUTCOMES')
    if len(matched)<len(actual):reasons.append('INCOMPLETE_BASELINE_MATCHING')
    return {'report_type':'OFFLINE_WALK_FORWARD_EVALUATION','split_at':split_at.isoformat(),'as_of':as_of.isoformat(),
        'training_evidence_at_split':json.loads(training) if training else None,
        'evaluation':_summary(actual),'baseline':_summary(baseline),
        'matched_comparison':{'sample_count':len(matched),'net_difference_usd':difference,
            'evaluation':actual_matched_summary,'baseline':baseline_matched_summary},
        'unmatched_outcome_economics':_summary(orphan),
        'actions':{'decisions':len(evaluation),'open':sum(row.get('action')=='OPEN' for row in evaluation.values()),
            'abstain':sum(row.get('action')=='ABSTAIN' for row in evaluation.values()),'unresolved_open':unresolved_open},
        'evidence_quality':{'partial_outcomes':partial,'unmatched_outcomes':len(orphan),
            'legacy_recovery_or_infrastructure_nonvalidating':nonvalidating,
            'duplicate_decisions_ignored':decision_duplicates,'duplicate_model_results_ignored':result_duplicates,
            'duplicate_outcomes_ignored':duplicate_outcomes,'duplicate_baselines_ignored':baseline_duplicates,
            'future_outcomes_excluded':future_outcomes,'invalid_or_post_cutoff_baselines':invalid_baselines},
        'uncertainty':{'status':'INSUFFICIENT' if len(actual)<10 else 'DESCRIPTIVE_ONLY','reasons':reasons},
        'claim_boundary':'DESCRIPTIVE_OFFLINE_COMPARISON_ONLY_NO_PROFITABILITY_OR_LEARNING_CLAIM'}
