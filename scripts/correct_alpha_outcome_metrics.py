"""Append corrections for plan-cache spill proven by bound fill/mark evidence."""
import argparse
from datetime import date,datetime,timezone
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from autonomous_trader.durable.ledger import LocalDatabase
from autonomous_trader.durable.projection import digest
from autonomous_trader.monitoring.store import Store,corrected_outcomes


def corrected_metrics(outcome,marks,prior_position_model_cost):
    """Use only marks recorded between this plan's first buy and last sell."""
    if not marks:raise ValueError('ALPHA_OWNED_MARKS_MISSING')
    high=max(Decimal(x) for x in marks);low=min(Decimal(x) for x in marks)
    quantity=Decimal(outcome['quantity']);capital=Decimal(outcome['capital_deployed_usd'])
    prior=Decimal(prior_position_model_cost)
    model=Decimal(outcome['model_cost_usd'])-prior
    if model<0:raise ValueError('ALPHA_MODEL_COST_BINDING_MISMATCH')
    broker_net=Decimal(outcome['realized_net_after_broker_fees_usd']);economic=broker_net-model
    result=dict(outcome)
    result.update(maximum_favorable_excursion_usd=str(max(Decimal(0),quantity*high-capital)),
        maximum_adverse_excursion_usd=str(max(Decimal(0),capital-quantity*low)),
        model_cost_usd=str(model),realized_net_after_all_known_costs_usd=str(economic),
        return_on_capital=str(economic/capital),
        win_loss='WIN' if economic>0 else ('LOSS' if economic<0 else 'FLAT'),
        excursion_attribution='DURABLE_JOURNAL_MARKS_WITHIN_PLAN_FILLS',
        model_cost_attribution='PRIOR_PLAN_CHECKPOINT_REMOVED')
    return result


def run(day,apply=False):
    store=Store();runtime=store.runtime()
    if runtime.get('mode')!='SANDBOX' or runtime.get('ledger_schema')!='mvp_sandbox':
        raise ValueError('SANDBOX_REQUIRED')
    with LocalDatabase('postgres','mvp_reader',schema='mvp_sandbox').connect() as c:
        c.execute('SET TRANSACTION READ ONLY')
        state,state_hash=c.execute('SELECT state,state_hash FROM accounts WHERE account_id=%s',
            (runtime['account_id'],)).fetchone()
        if digest(state)!=state_hash:raise ValueError('CHECKPOINT_INTEGRITY_FAILURE')
        executions=state['executions'];orders=state['orders'];plans={}
        for x in executions.values():
            intent=orders[x['intent_id']]['intent'];key=intent['plan_id']
            info=plans.setdefault(key,{'instrument_id':intent['instrument_id'],'buy':[],'sell':[]})
            if intent['side']=='BUY':info['buy'].append(x['sequence'])
            elif intent['side']=='SELL_OWNED':info['sell'].append(x['sequence'])
        marks={}
        for plan,info in plans.items():
            if not info['buy'] or not info['sell']:continue
            marks[plan]=[r[0] for r in c.execute("SELECT payload->'command'->>'bid_usd' FROM journal WHERE kind='MARK' AND sequence BETWEEN %s AND %s AND payload->'command'->>'instrument_id'=%s ORDER BY sequence",
                (min(info['buy']),max(info['sell']),info['instrument_id']))]
    with LocalDatabase('postgres','mvp_reader',schema='mvp_monitor').connect() as c:
        c.execute('SET TRANSACTION READ ONLY')
        originals=c.execute("SELECT id,payload FROM events WHERE kind='ALPHA_OUTCOME' AND mode='SANDBOX' ORDER BY at,id").fetchall()
        corrections=[r[0] for r in c.execute("SELECT payload FROM events WHERE kind='ALPHA_OUTCOME_CORRECTION' AND mode='SANDBOX' ORDER BY at,id")]
        existing={r[0]:r[1] for r in c.execute("SELECT id,payload FROM events WHERE kind='ALPHA_OUTCOME_CORRECTION' AND mode='SANDBOX'")}
        metric_rows=c.execute("SELECT payload FROM events WHERE kind='ALPHA_POSITION_METRICS' AND mode='SANDBOX' ORDER BY at,id").fetchall()
    metrics={}
    for (row,) in metric_rows:metrics.setdefault(row['plan_id'],[]).append(row)
    effective=dict(zip((event_id for event_id,_ in originals),corrected_outcomes(originals,corrections)))
    changes=[]
    for event_id,original in originals:
        if (original.get('evidence_quality')!='FULL_RUNTIME'
                or date.fromisoformat(original['closed_at'][:10])!=day):continue
        plan=original['plan_id'];info=plans.get(plan);rows=metrics.get(plan)
        if not info or not rows or not marks.get(plan):continue
        observed_high=max(Decimal(x) for x in marks[plan]);observed_low=min(Decimal(x) for x in marks[plan])
        first=rows[0]
        # The first checkpoint must be impossible for this position alone.
        if Decimal(first['max_bid_usd'])<=observed_high and Decimal(first['min_bid_usd'])>=observed_low:
            continue
        earlier=[(min(p['buy']),pid) for pid,p in plans.items()
            if p['instrument_id']==info['instrument_id'] and p['buy'] and min(p['buy'])<min(info['buy'])]
        if not earlier:raise ValueError('PRIOR_ALPHA_PLAN_MISSING')
        prior_plan=max(earlier)[1];prior_rows=metrics.get(prior_plan)
        if not prior_rows:raise ValueError('PRIOR_ALPHA_METRICS_MISSING')
        prior_cost=prior_rows[-1]['position_model_cost_usd']
        # A reused cache carries the previous plan's total into this plan.
        if Decimal(first['position_model_cost_usd'])<Decimal(prior_cost):
            raise ValueError('PRIOR_ALPHA_MODEL_COST_MISMATCH')
        replacement=corrected_metrics(effective[event_id],marks[plan],prior_cost)
        correction_id='alpha-outcome-metrics-'+sha256(event_id.encode()).hexdigest()
        payload={'summary':'Plan-bound excursion and model-cost attribution correction',
            'original_event_id':event_id,'reason':'PLAN_METRIC_CACHE_SPILL','replacement':replacement}
        if correction_id in existing:
            if existing[correction_id]!=payload:raise ValueError('CONFLICTING_ALPHA_OUTCOME_CORRECTION')
            continue
        changes.append((correction_id,payload))
    if apply:
        for event_id,payload in changes:
            store.emit('ALPHA_OUTCOME_CORRECTION',payload,mode='SANDBOX',
                at=datetime.now(timezone.utc),event_id=event_id)
    return {'date':day.isoformat(),'corrections':len(changes),'applied':apply,
        'plans':[p['replacement']['plan_id'] for _,p in changes]}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--day',type=date.fromisoformat,required=True)
    parser.add_argument('--apply',action='store_true');args=parser.parse_args()
    print(run(args.day,args.apply))
