"""Causal, bounded outcome projection for later portfolio decisions."""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json


NONVALIDATING_EXITS={
    'SAFETY_LATCH','OPEN_POSITION_OUTSIDE_SESSION','RECOVERY','FORCED_LIQUIDATION',
    'LEGACY_UNRECORDED',
}


def _at(value):
    if isinstance(value,datetime):return value
    try:return datetime.fromisoformat(str(value).replace('Z','+00:00'))
    except (TypeError,ValueError):return None


def _decimal(value):
    try:return Decimal(str(value))
    except (TypeError,ValueError,InvalidOperation):return None


def _number(value):
    if value is None:return 'UNKNOWN'
    text=format(Decimal(value).quantize(Decimal('.000001')),'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def _prior(event_at,payload,as_of,field=None):
    event_time=_at(event_at)
    value_time=_at(payload.get(field)) if field else event_time
    return event_time is not None and value_time is not None and event_time<as_of and value_time<as_of


def _horizon(decision):
    start=_at(decision.get('stated_horizon_start'));end=_at(decision.get('stated_horizon_end'))
    if start is None or end is None or end<start:return 'UNKNOWN'
    return str(int((end-start).total_seconds()))


def _mean(total,count):
    return 'UNKNOWN' if not count else _number(total/count)


def project_measured_feedback(decisions,outcomes,model_results,*,as_of,max_groups=8):
    """Return one deterministic JSON memory row using evidence strictly before ``as_of``.

    Decision and model-result inputs are ``(event_at, payload)`` pairs. Outcomes
    are corrected canonical payloads; their own ``closed_at`` is authoritative.
    """
    if as_of.tzinfo is None:raise ValueError('MEASURED_FEEDBACK_AS_OF_MUST_BE_AWARE')
    if not 1<=max_groups<=8:raise ValueError('MEASURED_FEEDBACK_GROUP_LIMIT')
    as_of=as_of.astimezone(timezone.utc)
    decision_by_plan={}
    for event_at,payload in decisions:
        if payload.get('action')!='OPEN' or not _prior(event_at,payload,as_of,'model_response_at'):continue
        plan_id=payload.get('outcome_plan_id')
        if not plan_id:continue
        prior=decision_by_plan.get(plan_id)
        if prior is None or str(payload.get('model_response_at'))<str(prior.get('model_response_at')):
            decision_by_plan[plan_id]=payload
    result_by_id={}
    for event_at,payload in model_results:
        if not _prior(event_at,payload,as_of):continue
        result_id=payload.get('id')
        if result_id and result_id not in result_by_id:result_by_id[result_id]=payload

    deduplicated={}
    for row in outcomes:
        if not _prior(row.get('closed_at'),row,as_of,'closed_at'):continue
        identity=(row.get('plan_id'),row.get('closed_at'))
        prior=deduplicated.get(identity)
        if prior is None or (prior.get('evidence_quality')!='FULL_RUNTIME'
                and row.get('evidence_quality')=='FULL_RUNTIME'):
            deduplicated[identity]=row

    groups={}
    for row in deduplicated.values():
        decision=decision_by_plan.get(row.get('plan_id'))
        entry_plan=(decision or {}).get('entry_exit_plan')
        strategy=entry_plan.get('strategy_label') if isinstance(entry_plan,dict) else None
        strategy=str(strategy or row.get('strategy_label') or 'UNCLASSIFIED')[:120]
        regime=str((decision or {}).get('regime_label') or row.get('market_regime') or 'UNCLASSIFIED')[:120]
        horizon=_horizon(decision) if decision else 'UNKNOWN'
        key=(strategy,horizon,regime)
        g=groups.setdefault(key,{'latest':'','samples':0,'comparable':0,'wins':0,'losses':0,'flat':0,
            'broker_net':Decimal(0),'gross':Decimal(0),'broker_fees':Decimal(0),
            'all_cost_net':Decimal(0),'model_cost':Decimal(0),'unknown_costs':0,
            'capital':Decimal(0),'capital_count':0,'capital_time':Decimal(0),'capital_time_count':0,
            'mfe':Decimal(0),'mfe_count':0,'mae':Decimal(0),'mae_count':0,
            'slippage':Decimal(0),'slippage_count':0,'spread':Decimal(0),'spread_count':0,
            'nonvalidating':0,'completeness':set()})
        g['samples']+=1;g['latest']=max(g['latest'],str(row.get('closed_at') or ''))
        gross=_decimal(row.get('realized_gross_usd'));fees=_decimal(row.get('broker_fees_usd'))
        broker_net=_decimal(row.get('realized_net_after_broker_fees_usd'))
        for name,value in (('gross',gross),('broker_fees',fees),('broker_net',broker_net)):
            if value is None:g['completeness'].add('UNKNOWN_'+name.upper())
            else:g[name]+=value

        result=result_by_id.get((decision or {}).get('model_work_result_id'))
        entry_cost=_decimal(result.get('cost_usd')) if result else None
        attributed_cost=_decimal(row.get('model_cost_usd'))
        all_cost_net=_decimal(row.get('realized_net_after_all_known_costs_usd'))
        cost_complete=(decision is not None and result is not None and entry_cost is not None
            and attributed_cost is not None and all_cost_net is not None and broker_net is not None)
        if cost_complete and attributed_cost>=entry_cost:
            # The completed outcome already contains entry plus later review costs.
            # The canonical result proves the entry component; never add it twice.
            g['model_cost']+=attributed_cost;g['all_cost_net']+=all_cost_net
        else:
            g['unknown_costs']+=1;g['completeness'].add('UNKNOWN_MODEL_COST')
            if cost_complete:g['completeness'].add('MODEL_COST_LINK_MISMATCH')
        if decision is not None:
            if row.get('strategy_label') and row.get('strategy_label')!=strategy:
                g['completeness'].add('STRATEGY_LABEL_MISMATCH')
            if row.get('market_regime') and row.get('market_regime')!=regime:
                g['completeness'].add('REGIME_LABEL_MISMATCH')

        classification=str(row.get('win_loss') or '')
        if classification=='WIN':g['wins']+=1
        elif classification=='LOSS':g['losses']+=1
        else:g['flat']+=1
        quality=row.get('evidence_quality')=='FULL_RUNTIME'
        origin=str(row.get('failure_origin') or '')
        nonvalidating=(not quality or 'INFRASTRUCTURE' in origin or 'SAFETY' in origin
            or str(row.get('exit_reason') or '') in NONVALIDATING_EXITS or decision is None)
        if nonvalidating:g['nonvalidating']+=1
        else:g['comparable']+=1

        for field,total,count in (
            ('capital_deployed_usd','capital','capital_count'),
            ('capital_time_usd_seconds','capital_time','capital_time_count'),
            ('maximum_favorable_excursion_usd','mfe','mfe_count'),
            ('maximum_adverse_excursion_usd','mae','mae_count'),
            ('measured_slippage_usd','slippage','slippage_count'),
            ('measured_spread_bps','spread','spread_count')):
            value=_decimal(row.get(field))
            if value is not None:g[total]+=value;g[count]+=1

    entries=[]
    for (strategy,horizon,regime),g in sorted(groups.items(),key=lambda item:(item[1]['latest'],item[0]),reverse=True):
        cost_net='UNKNOWN' if g['unknown_costs'] else _number(g['all_cost_net'])
        roc='UNKNOWN'
        if not g['unknown_costs'] and g['capital_count']==g['samples'] and g['capital']:
            roc=_number(g['all_cost_net']/g['capital'])
        capital_day='UNKNOWN'
        if not g['unknown_costs'] and g['capital_time_count']==g['samples'] and g['capital_time']:
            capital_day=_number(g['all_cost_net']/(g['capital_time']/Decimal(86400)))
        complete=(not g['completeness'] and g['capital_count']==g['samples']
            and g['mfe_count']==g['samples'] and g['mae_count']==g['samples'])
        entries.append({'strategy':strategy,'planned_horizon_seconds':horizon,'regime':regime,
            'sample_count':g['samples'],'comparable_closed_trades':g['comparable'],
            'status':'EXPLORATORY' if g['comparable']<10 else 'MEASURED_NO_EDGE_CLAIM',
            'wins':g['wins'],'losses':g['losses'],'flat':g['flat'],
            'realized_net_after_broker_fees_usd':_number(g['broker_net']),
            'realized_net_after_broker_and_known_model_costs_usd':cost_net,
            'known_model_cost_usd':_number(g['model_cost']) if not g['unknown_costs'] else 'UNKNOWN',
            'unknown_cost_count':g['unknown_costs'],'return_on_deployed_capital':roc,
            'net_usd_per_capital_day':capital_day,'mean_mfe_usd':_mean(g['mfe'],g['mfe_count']),
            'mean_mae_usd':_mean(g['mae'],g['mae_count']),
            'actual_measured_slippage_usd':_mean(g['slippage'],g['slippage_count']),
            'actual_measured_spread_bps':_mean(g['spread'],g['spread_count']),
            'nonvalidating_safety_recovery_legacy':g['nonvalidating'],
            'evidence_completeness':'COMPLETE' if complete else 'PARTIAL',
            'completeness_issues':sorted(g['completeness']),'latest_closed_at':g['latest']})

    header={'memory_type':'measured_portfolio_feedback','as_of':as_of.isoformat(),
        'scope':'PRIOR_CLOSED_TRADES_ONLY','learning_claim':'MEASURED_FEEDBACK_NOT_REINFORCEMENT_LEARNING',
        'edge_claim':'NONE','groups':entries[:max_groups]}
    value=json.dumps(header,sort_keys=True,separators=(',',':'))
    while len(value)>1950 and header['groups']:
        header['groups'].pop();value=json.dumps(header,sort_keys=True,separators=(',',':'))
    return value if header['groups'] else None
