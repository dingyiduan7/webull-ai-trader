"""Owner-authorized, append-only repair for the 2026-09-28 SOAR sale."""
import argparse
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))

from autonomous_trader.durable.execution_correction import SaleCashEvidence,correction_from_sale_cash
from autonomous_trader.durable.ledger import Ledger,LocalDatabase
from autonomous_trader.monitoring.store import Store

ACCOUNT_ID='sandbox-f4c2177182715fa2bf7e368ed6676c60'
EXECUTION_ID='3bf33afa48a34460b5d53668ba3350b8'
BEFORE_EVIDENCE_ID='6817db0e3c5b45149cf4b2a1bcbf86b1'
AFTER_EVIDENCE_ID='bd76bd9956e949aa8530e8370a225fb0'
CORRECTION_EVENT_ID='soar-sale-cash-correction-20260928'
OUTCOME_ID='fe87fd591e8d4ad4a612ab3452413848'
OUTCOME_CORRECTION_ID='soar-outcome-cash-correction-20260928'


def _evidence():
    return SaleCashEvidence(execution_id=EXECUTION_ID,before_evidence_id=BEFORE_EVIDENCE_ID,
        after_evidence_id=AFTER_EVIDENCE_ID,settled_before_usd=Decimal('995484.85'),
        settled_after_usd=Decimal('995484.85'),unsettled_before_usd=Decimal('4659.50'),
        unsettled_after_usd=Decimal('8739.43'))


def _load_and_verify():
    with LocalDatabase('postgres','mvp_reader',schema='mvp_sandbox').connect() as c:
        c.execute('SET TRANSACTION READ ONLY')
        state=c.execute('SELECT state FROM accounts WHERE account_id=%s',(ACCOUNT_ID,)).fetchone()[0]
        rows=c.execute("SELECT payload->'command' FROM journal WHERE account_id=%s AND kind='RECONCILE' AND payload->'command'->>'evidence_id'=ANY(%s)",
            (ACCOUNT_ID,[BEFORE_EVIDENCE_ID,AFTER_EVIDENCE_ID])).fetchall()
    observations={row[0]['evidence_id']:row[0] for row in rows}
    expected={BEFORE_EVIDENCE_ID:('995484.85','4659.50',[['950977353','10000']]),
        AFTER_EVIDENCE_ID:('995484.85','8739.43',[])}
    if set(observations)!=set(expected):raise ValueError('SOAR_BROKER_CASH_EVIDENCE_MISSING')
    for key,(settled,unsettled,positions) in expected.items():
        row=observations[key]
        if (row['settled_usd'],row['unsettled_usd'],row['positions'],row['outstanding_intent_ids'])!=(settled,unsettled,positions,[]):
            raise ValueError('SOAR_BROKER_CASH_EVIDENCE_CONFLICT')
    execution=state['executions'].get(EXECUTION_ID)
    if execution is None:raise ValueError('SOAR_EXECUTION_MISSING')
    if execution['revision']==1 and execution['price_usd']=='0.4082':return state,None
    if (execution['revision'],execution['quantity'],execution['price_usd'],execution['fee_usd'])!=(0,'10000','0.41','2.07'):
        raise ValueError('SOAR_EXECUTION_CONFLICT')
    if state['positions'] or any(not order['final'] for order in state['orders'].values()):
        raise ValueError('SOAR_CORRECTION_REQUIRES_FLAT_FINAL_STATE')
    return state,correction_from_sale_cash(state,_evidence())


def _correct_outcome():
    store=Store()
    with LocalDatabase('postgres','mvp_reader',schema='mvp_monitor').connect() as c:
        c.execute('SET TRANSACTION READ ONLY')
        row=c.execute("SELECT payload FROM events WHERE id=%s AND kind='ALPHA_OUTCOME'",(OUTCOME_ID,)).fetchone()
    if row is None or row[0].get('symbol')!='SOAR':raise ValueError('SOAR_OUTCOME_EVIDENCE_MISSING')
    original=row[0];replacement=dict(original)
    economic=Decimal('-20.10')-Decimal(original['model_cost_usd'])
    replacement.update(exit_usd='0.4082',realized_gross_usd='-18.00',
        realized_net_after_broker_fees_usd='-20.10',
        realized_net_after_all_known_costs_usd=str(economic),
        return_on_capital=str(economic/Decimal(original['capital_deployed_usd'])),win_loss='LOSS')
    payload={'summary':'SOAR sale corrected from exact broker cash proceeds',
        'original_event_id':OUTCOME_ID,'reason':'BROKER_ORDER_DETAIL_ROUNDED_FILL_PRICE',
        'replacement':replacement}
    store.emit_idempotent('ALPHA_OUTCOME_CORRECTION',payload,mode='SANDBOX',
        at=datetime.now(timezone.utc),event_id=OUTCOME_CORRECTION_ID)


def run(apply=False):
    state,command=_load_and_verify()
    result={'execution_id':EXECUTION_ID,'reported_price_usd':'0.41','exact_price_usd':'0.4082',
        'gross_proceeds_usd':'4082.00','fees_usd':'2.07','net_proceeds_usd':'4079.93',
        'ledger_overstatement_usd':'18.00','applied':False,'already_applied':command is None}
    if not apply or command is None:return result
    ledger=Ledger(LocalDatabase('postgres','m2_writer',schema='mvp_sandbox'),execution_scope='SANDBOX')
    holder='owner-authorized-soar-correction';now=datetime.now(timezone.utc)
    leased=ledger.acquire_leader(ACCOUNT_ID,holder,event_id='soar-correction-lease-'+now.strftime('%Y%m%d%H%M%S%f'),at=now)
    corrected=ledger.execute(ACCOUNT_ID,command,event_id=CORRECTION_EVENT_ID,
        expected_version=leased['version'],holder=holder,token=leased['leader']['token'],at=now)
    if (corrected['executions'][EXECUTION_ID]['revision']!=1
            or corrected['executions'][EXECUTION_ID]['price_usd']!='0.4082'
            or Decimal(corrected['cash']['unsettled_usd'])!=Decimal('8739.43')):
        raise ValueError('SOAR_CORRECTION_POSTCONDITION_FAILED')
    _correct_outcome();result['applied']=True
    result['reconciliation_ready']=corrected['reconciliation']['ready']
    result['incident_latched']=corrected['latches']['incident']
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--apply',action='store_true')
    print(run(parser.parse_args().apply))
