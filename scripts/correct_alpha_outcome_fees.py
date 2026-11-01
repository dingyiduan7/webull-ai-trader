"""Append monitoring-only corrections from effective ledger executions; no account writes."""
import argparse
from datetime import date
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from autonomous_trader.durable.ledger import LocalDatabase
from autonomous_trader.durable.projection import digest
from autonomous_trader.mvp.alpha import completed_alpha_trade
from autonomous_trader.monitoring.store import Store


def corrected_payload(original,state):
    plan_id=original['plan_id'];instrument_id=original['instrument_id']
    origins={o['intent']['episode_id'] for o in state['orders'].values()
        if o['intent']['plan_id']==plan_id and o['intent']['side']=='BUY'
        and o['intent']['instrument_id']==instrument_id}
    if len(origins)!=1:raise ValueError('ALPHA_OUTCOME_ORIGIN_AMBIGUOUS')
    trade=completed_alpha_trade(state,plan_id,instrument_id,next(iter(origins)))
    if (Decimal(original['quantity'])!=trade['quantity']
            or Decimal(original['realized_gross_usd'])!=trade['realized_gross_usd']
            or Decimal(original['capital_deployed_usd'])!=trade['capital_deployed_usd']):
        raise ValueError('ALPHA_OUTCOME_LEDGER_MISMATCH')
    corrected=dict(original);fee=trade['broker_fees_usd']
    net=trade['realized_gross_usd']-fee
    economic=net-Decimal(original['model_cost_usd'])
    corrected.update(broker_fees_usd=str(fee),realized_net_after_broker_fees_usd=str(net),
        realized_net_after_all_known_costs_usd=str(economic),
        return_on_capital=str(economic/trade['capital_deployed_usd']),
        win_loss='WIN' if economic>0 else ('LOSS' if economic<0 else 'FLAT'))
    return corrected


def run(day,apply=False):
    store=Store();runtime=store.runtime()
    if runtime.get('mode')!='SANDBOX' or runtime.get('ledger_schema')!='mvp_sandbox':
        raise ValueError('SANDBOX_REQUIRED')
    with LocalDatabase('postgres','mvp_reader',schema='mvp_sandbox').connect() as c:
        c.execute('SET TRANSACTION READ ONLY')
        state,state_hash=c.execute('SELECT state,state_hash FROM accounts WHERE account_id=%s',
            (runtime['account_id'],)).fetchone()
        if digest(state)!=state_hash:raise ValueError('CHECKPOINT_INTEGRITY_FAILURE')
    with LocalDatabase('postgres','mvp_reader',schema='mvp_monitor').connect() as c:
        c.execute('SET TRANSACTION READ ONLY')
        originals=c.execute("SELECT id,at,payload FROM events WHERE kind='ALPHA_OUTCOME' AND mode='SANDBOX' ORDER BY at,id").fetchall()
        existing={r[0]:r[1] for r in c.execute("SELECT id,payload FROM events WHERE kind='ALPHA_OUTCOME_CORRECTION' AND mode='SANDBOX'")}
    changes=[]
    for event_id,at,original in originals:
        if date.fromisoformat(original['closed_at'][:10])!=day:continue
        corrected=corrected_payload(original,state)
        if corrected['broker_fees_usd']==original['broker_fees_usd']:continue
        correction_id='alpha-outcome-fees-'+sha256(event_id.encode()).hexdigest()
        payload={'summary':'Ledger execution fee attribution correction',
            'original_event_id':event_id,'reason':'EPISODE_FEE_SPILL','replacement':corrected}
        if correction_id in existing:
            if existing[correction_id]!=payload:raise ValueError('CONFLICTING_ALPHA_OUTCOME_CORRECTION')
            continue
        changes.append((correction_id,at,payload))
    if apply:
        for event_id,at,payload in changes:
            store.emit('ALPHA_OUTCOME_CORRECTION',payload,mode='SANDBOX',at=at,event_id=event_id)
    return {'date':day.isoformat(),'corrections':len(changes),'applied':apply,
        'plans':[p['replacement']['plan_id'] for _,_,p in changes]}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--day',type=date.fromisoformat,required=True)
    parser.add_argument('--apply',action='store_true');args=parser.parse_args()
    print(run(args.day,args.apply))
