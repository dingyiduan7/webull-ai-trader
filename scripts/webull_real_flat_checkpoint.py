"""One bounded REAL read-only flat-account checkpoint. No write route exists."""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from urllib.parse import urlsplit
import json
import os
import sys

import requests
from webull.trade.request.v3.get_order_history_request_v2 import OrderHistoryRequestV2

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))

from autonomous_trader.real_secrets import load_real_credentials
from autonomous_trader.real_preflight import RealReadOnly, RealReadOnlyHttp, Operation, require_success, select_single_individual_cash
from autonomous_trader.real_readiness import account_readiness
from autonomous_trader.real_state import RealObservationLedger, binding
from autonomous_trader.real_liquidation import Journal as OriginalJournal, EXECUTION_APPROVED as ORIGINAL_APPROVED
from autonomous_trader.real_liquidation_recovery import Journal as RecoveryJournal, EXECUTION_APPROVED as RECOVERY_APPROVED
from autonomous_trader.durable.ledger import LocalDatabase
from autonomous_trader.sandbox.sanitize import Sanitizer
from autonomous_trader.real_flat_checkpoint import prove_flat_cash


EXPECTED = dict(ICLN=Decimal('0.05675'), SMH=Decimal('0.00188'), ESGU=Decimal('0.00719'),
                PHO=Decimal('0.01733'), LIT=Decimal('0.01408'))
FINAL = frozenset(('FILLED','CANCELLED','FAILED','REJECTED','EXPIRED'))


def number(value):
    try:
        result=Decimal(str(value))
        if not result.is_finite():raise InvalidOperation
        return result
    except (InvalidOperation,TypeError,ValueError):
        raise ValueError('INVALID_NUMERIC_EVIDENCE') from None


def history_read(http,account_id):
    request=OrderHistoryRequestV2();request.set_account_id(account_id)
    parsed=urlsplit(request.get_url())
    if (request.get_method()!='GET' or parsed.scheme or parsed.netloc
            or parsed.path!='/trading/orders/historical-orders/list' or request.get_body_params()):
        raise ValueError('REAL_HISTORY_READ_BOUNDARY')
    wire=http._make_http_response('api.webull.com',request,10,5)
    with requests.Session() as session:
        session.trust_env=False
        prepared=session.prepare_request(requests.Request('GET','https://api.webull.com'+wire.get_url(),
            headers=wire.get_headers(),data=wire.get_body()))
        with session.send(prepared,allow_redirects=False,verify=True,timeout=(5,10),stream=True) as response:
            if response.status_code!=200:raise ValueError('REAL_HISTORY_READ_FAILED')
            chunks=[];size=0
            for chunk in response.iter_content(65536):
                size+=len(chunk)
                if size>2_000_000:raise ValueError('REAL_HISTORY_RESPONSE_TOO_LARGE')
                chunks.append(chunk)
    return json.loads(b''.join(chunks),parse_float=Decimal,
        parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))


def fee_total(row):
    total=Decimal(0)
    fees=row.get('fees')
    commission=row.get('commission')
    if not isinstance(fees,list) or not isinstance(commission,dict):raise ValueError('FEE_EVIDENCE_UNRESOLVED')
    for fee in fees:
        actual,receivable=number(fee.get('actual_value')),number(fee.get('receivable_value'))
        if actual<0 or actual!=receivable:raise ValueError('FEE_EVIDENCE_UNRESOLVED')
        total+=actual
    if commission:
        actual=number(commission.get('actual_commission'))
        receivable=number(commission.get('receivable_commission'))
        if actual<0 or actual!=receivable:raise ValueError('FEE_EVIDENCE_UNRESOLVED')
        total+=actual
    return total


def main():
    credentials=load_real_credentials(ROOT)
    client=RealReadOnly(credentials);http=RealReadOnlyHttp(credentials)
    accounts=require_success(Operation.ACCOUNTS,client.read(Operation.ACCOUNTS))
    selected=select_single_individual_cash(accounts.body);account_id=selected['account_id']
    observations={op:require_success(op,client.read(op,account_id=account_id))
        for op in (Operation.BALANCE,Operation.POSITIONS,Operation.OPEN_ORDERS)}
    balance=observations[Operation.BALANCE];positions=observations[Operation.POSITIONS]
    orders=observations[Operation.OPEN_ORDERS]
    received=min(datetime.fromisoformat(x.received_at) for x in (accounts,balance,positions,orders))
    readiness=account_readiness(balance.body,positions.body,orders.body,received)
    if readiness.external_holdings:raise ValueError('REAL_ACCOUNT_NOT_FLAT')
    if readiness.open_order_count or orders.body.get('pagination_key'):raise ValueError('REAL_OPEN_ORDER_UNRESOLVED')
    if not readiness.account_totals_match:raise ValueError('REAL_ACCOUNT_TOTALS_UNRECONCILED')
    history=history_read(http,account_id)
    if not isinstance(history,dict) or not isinstance(history.get('data'),list) or history.get('pagination_key'):
        raise ValueError('REAL_HISTORY_PAGINATION_OR_SHAPE')
    rows=[]
    for group in history['data']:
        if not isinstance(group,dict) or not isinstance(group.get('orders'),list):raise ValueError('REAL_HISTORY_SHAPE')
        rows.extend(group['orders'])
    if any(not isinstance(row,dict) or row.get('status') not in FINAL for row in rows):
        raise ValueError('RECENT_ORDER_FINALITY_UNRESOLVED')
    if any(number(row.get('filled_quantity','0'))!=0 and row.get('status')!='FILLED' for row in rows):
        raise ValueError('RECENT_PARTIAL_ORDER_UNRESOLVED')
    db=LocalDatabase('postgres','m2_admin',schema='real_readiness')
    state=RealObservationLedger(db).verify_replay()
    if not state or state['account_binding']!=binding(account_id):raise ValueError('REAL_JOURNAL_BINDING_OR_REPLAY')
    original=OriginalJournal(db).events();recovery=RecoveryJournal(db).events()
    original_client={p['client_order_id'] for e,p in original if e=='SUBMISSION_INTENT'}
    today=datetime.now(timezone.utc).date()
    sales=[]
    for row in rows:
        if row.get('status')!='FILLED' or row.get('side')!='SELL' or row.get('symbol') not in EXPECTED:continue
        at=row.get('filled_time_at')
        try:filled_at=datetime.fromisoformat(at.replace('Z','+00:00'))
        except (AttributeError,ValueError):raise ValueError('FILL_TIME_UNRESOLVED') from None
        if filled_at.date()!=today:continue
        quantity=number(row.get('filled_quantity'));price=number(row.get('filled_price'));fees=fee_total(row)
        if quantity!=number(row.get('total_quantity')):raise ValueError('FILL_QUANTITY_UNRESOLVED')
        gross=quantity*price;net=gross-fees
        broker_net=next((row.get(key) for key in ('net_amount','net_proceeds','net_proceeds_amount')
                         if row.get(key) is not None),None)
        sales.append(dict(symbol=row['symbol'],quantity=str(quantity),filled_price=str(price),
            gross_usd=str(gross),fees_usd=str(fees),reported_net_exact_usd=str(net),
            broker_net_usd=None if broker_net is None else str(number(broker_net)),filled_at=filled_at.isoformat(),
            authority='ISOLATED_ICLN_CAMPAIGN' if row.get('client_order_id') in original_client else 'OWNER_MANUAL'))
    if len(sales)!=5 or {x['symbol'] for x in sales}!=set(EXPECTED):raise ValueError('EXACT_LIQUIDATION_FILLS_UNRESOLVED')
    if any(number(x['quantity'])!=EXPECTED[x['symbol']] for x in sales):raise ValueError('EXACT_LIQUIDATION_FILL_QUANTITY')
    total_fees=sum((number(x['fees_usd']) for x in sales),Decimal(0))
    cash=balance.body['account_currency_assets'][0]
    precise=number(cash.get('buying_power'))
    proof=prove_flat_cash(settled=readiness.settled_usd,unsettled=readiness.unsettled_usd,
        cash_balance=readiness.settled_usd+readiness.unsettled_usd,equity=readiness.equity_usd,
        precise_cash=precise,fills=sales)
    with db.connect() as connection:
        locks=connection.execute("SELECT objid FROM pg_locks WHERE locktype='advisory' AND classid=481516 "
            "AND objid IN (902,903,904,905) AND granted").fetchall()
        real_schema=connection.execute("SELECT to_regnamespace('real_trader')").fetchone()[0]
    if locks:raise ValueError('LIQUIDATION_WRITER_STILL_ACTIVE')
    if ORIGINAL_APPROVED or RECOVERY_APPROVED:raise ValueError('LIQUIDATION_EXECUTION_STILL_ENABLED')
    go_path=ROOT/'.local/real-readiness/judicator-real-go.json'
    checkpoint=dict(schema='REAL_FLAT_CHECKPOINT_V1',environment='REAL_READ_ONLY',
        observed_at=received.isoformat(),account=dict(type='INDIVIDUAL_CASH',sole_account=True,
            binding_matches_observation_journal=True),positions_count=0,open_order_count=0,
        recent_orders=dict(history_scope='BROKER_DEFAULT_LAST_7_DAYS_ONE_PAGE',history_count=len(rows),
            all_terminal=True,ambiguous_count=0),cash=dict(settled_usd=str(readiness.settled_usd),
            unsettled_usd=str(readiness.unsettled_usd),cash_balance_usd=str(readiness.settled_usd+readiness.unsettled_usd),
            precise_cash_evidence_usd=str(precise),equity_usd=str(readiness.equity_usd),totals_reconciled=True),
        liquidation=dict(fills=sorted(sales,key=lambda x:x['filled_at']),total_fees_usd=str(total_fees),
            precision_proof=proof),local_integrity=dict(observation_replay_verified=True,
            observation_ledger_version=state['version'],original_liquidation_events=len(original),
            recovery_events=len(recovery),writer_locks=0,original_execution_approved=False,
            recovery_execution_approved=False,real_strategy_schema_present=bool(real_schema),
            judicator_real_go_present=go_path.exists()),authority=dict(runner_started=False,astra_called=False,
            broker_mutations=0,strategy_authority_enabled=False))
    sanitizer=Sanitizer(tuple(v for v in (credentials.app_key,credentials.app_secret,credentials.account_id,
        credentials.access_token,account_id) if v))
    text=json.dumps(checkpoint,indent=2);sanitizer.assert_clean(text)
    path=ROOT/'.local'/'real-readiness'/('flat-checkpoint-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'.json')
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix('.tmp')
    with temporary.open('w',encoding='utf-8') as handle:
        handle.write(text);handle.flush();os.fsync(handle.fileno())
    os.replace(temporary,path)
    print(json.dumps({'status':'REAL_FLAT_CHECKPOINT_COMPLETE','path':str(path.relative_to(ROOT)),
        'settled_usd':str(readiness.settled_usd),'unsettled_usd':str(readiness.unsettled_usd),
        'equity_usd':str(readiness.equity_usd),'positions':0,'open_orders':0,
        'fills':len(sales),'fees_usd':str(total_fees),'precision_proof':proof,
        'fill_aggregates':[{k:x[k] for k in ('symbol','quantity','filled_price','gross_usd','fees_usd','broker_net_usd')}
                           for x in sorted(sales,key=lambda x:x['symbol'])]}))


if __name__=='__main__':
    try:main()
    except Exception as exc:
        code=str(exc) if isinstance(exc,ValueError) and str(exc).isupper() else 'REAL_FLAT_CHECKPOINT_FAILED'
        print(json.dumps({'status':'BLOCKED','blocker':code}))
        raise SystemExit(2)
