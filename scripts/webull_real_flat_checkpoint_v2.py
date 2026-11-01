"""Create one Judicator-authorized REAL flat-checkpoint supersession; never trade."""
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit
import json
import os
import sys

import requests
from webull.trade.request.v3.get_order_history_request_v2 import OrderHistoryRequestV2

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))

from autonomous_trader.durable.ledger import LocalDatabase
from autonomous_trader.real_flat_checkpoint import (CHECKPOINT_SCHEMA_V2,V2_FILENAME,
    BASE_CHECKPOINT_NAME,BASE_CHECKPOINT_SHA256,ACCOUNT_DIAGNOSTIC_NAME,ACCOUNT_DIAGNOSTIC_SHA256,
    CASH_ACTIVITY_DIAGNOSTIC_NAME,CASH_ACTIVITY_DIAGNOSTIC_SHA256,ADJUSTMENT_TYPE,
    checkpoint_digest,load_checkpoint,validate_checkpoint)
from autonomous_trader.real_preflight import (REAL_HTTP_HOST,RealReadOnly,RealReadOnlyHttp,Operation,
    require_success,select_single_individual_cash)
from autonomous_trader.real_readiness import account_readiness
from autonomous_trader.real_secrets import load_real_credentials
from autonomous_trader.real_state import RealObservationLedger,binding
from autonomous_trader.real_trading import RealGate,build_digest
from autonomous_trader.sandbox.sanitize import Sanitizer

FINAL=frozenset(('FILLED','CANCELLED','FAILED','REJECTED','EXPIRED'))


def history_read(http,account_id):
    request=OrderHistoryRequestV2();request.set_account_id(account_id)
    parsed=urlsplit(request.get_url())
    if (request.get_method()!='GET' or parsed.scheme or parsed.netloc
            or parsed.path!='/trading/orders/historical-orders/list' or request.get_body_params()):
        raise ValueError('REAL_HISTORY_READ_BOUNDARY')
    wire=http._make_http_response(REAL_HTTP_HOST,request,10,5)
    with requests.Session() as session:
        session.trust_env=False
        prepared=session.prepare_request(requests.Request('GET','https://'+REAL_HTTP_HOST+wire.get_url(),
            headers=wire.get_headers(),data=wire.get_body()))
        with session.send(prepared,allow_redirects=False,verify=True,timeout=(5,10),stream=True) as response:
            if response.status_code!=200:raise ValueError('REAL_HISTORY_READ_FAILED')
            chunks=[];size=0
            for chunk in response.iter_content(65536):
                size+=len(chunk)
                if size>2_000_000:raise ValueError('REAL_HISTORY_RESPONSE_TOO_LARGE')
                chunks.append(chunk)
    try:return json.loads(b''.join(chunks),parse_float=Decimal,
        parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
    except (ValueError,UnicodeError):raise ValueError('REAL_HISTORY_SHAPE') from None


def history_summary(body):
    if not isinstance(body,dict) or not isinstance(body.get('data'),list) or body.get('pagination_key'):
        raise ValueError('REAL_HISTORY_PAGINATION_OR_SHAPE')
    rows=[]
    for group in body['data']:
        if not isinstance(group,dict) or not isinstance(group.get('orders'),list):
            raise ValueError('REAL_HISTORY_SHAPE')
        rows.extend(group['orders'])
    if any(not isinstance(row,dict) or row.get('status') not in FINAL for row in rows):
        raise ValueError('RECENT_ORDER_FINALITY_UNRESOLVED')
    if any(Decimal(str(row.get('filled_quantity','0')))!=0 and row.get('status')!='FILLED' for row in rows):
        raise ValueError('RECENT_PARTIAL_ORDER_UNRESOLVED')
    return {'history_scope':'BROKER_DEFAULT_LAST_7_DAYS_ONE_PAGE','history_count':len(rows),
        'all_terminal':True,'ambiguous_count':0,'partial_or_unknown_count':0}


def build_checkpoint(*,observed_at,build_sha256,account_binding,recent_orders,ledger_version):
    return {'schema':CHECKPOINT_SCHEMA_V2,'environment':'REAL_READ_ONLY',
        'observed_at':observed_at.isoformat(),'build_sha256':build_sha256,'account_binding':account_binding,
        'account':{'type':'INDIVIDUAL_CASH','sole_account':True,'binding_matches_observation_journal':True},
        'positions_count':0,'open_order_count':0,'recent_orders':recent_orders,
        'cash':{'settled_usd':'505.44','unsettled_usd':'0.00','cash_balance_usd':'505.44',
            'equity_usd':'505.44','totals_reconciled':True},
        'continuity':{
            'base_checkpoint':{'name':BASE_CHECKPOINT_NAME,'sha256':BASE_CHECKPOINT_SHA256},
            'account_diagnostic':{'name':ACCOUNT_DIAGNOSTIC_NAME,'sha256':ACCOUNT_DIAGNOSTIC_SHA256},
            'cash_activity_diagnostic':{'name':CASH_ACTIVITY_DIAGNOSTIC_NAME,
                'sha256':CASH_ACTIVITY_DIAGNOSTIC_SHA256},
            'base_total_usd':'505.35','current_total_usd':'505.44','adjustment_usd':'0.09',
            'adjustment_type':ADJUSTMENT_TYPE,'cash_activity_net_usd':'0','cash_activity_match':False,
            'trading_profit_usd':'0','realized_pnl_effect_usd':'0'},
        'owner_authorization':{'scope':'REAL_FLAT_CHECKPOINT_V2_SUPERSESSION','decision':'AUTHORIZED',
            'provenance':'OWNER_VIA_PROJECT_COMMAND_CENTER'},
        'local_integrity':{'observation_replay_verified':True,'observation_ledger_version':ledger_version,
            'writer_locks':0,'real_strategy_schema_present':False,'judicator_real_go_present':False},
        'authority':False}


def main():
    started=datetime.now(timezone.utc)
    prepare=RealGate.offline_checkpoint_preparation(ROOT,started)
    folder=ROOT/'.local'/'real-readiness';target=folder/V2_FILENAME
    if target.exists() or any(folder.glob('flat-checkpoint-v2-*.json')):
        raise ValueError('REAL_FLAT_CHECKPOINT_V2_ALREADY_EXISTS')
    base,_=load_checkpoint(folder/BASE_CHECKPOINT_NAME,account_binding=prepare['account_binding'],
        expected_sha256=BASE_CHECKPOINT_SHA256)
    if checkpoint_digest(folder/ACCOUNT_DIAGNOSTIC_NAME)!=ACCOUNT_DIAGNOSTIC_SHA256:
        raise ValueError('REAL_FLAT_CHECKPOINT_V2_ARTIFACT')
    if checkpoint_digest(folder/CASH_ACTIVITY_DIAGNOSTIC_NAME)!=CASH_ACTIVITY_DIAGNOSTIC_SHA256:
        raise ValueError('REAL_FLAT_CHECKPOINT_V2_ARTIFACT')

    credentials=load_real_credentials(ROOT)
    reader=RealReadOnly(credentials);history_http=RealReadOnlyHttp(credentials)
    db=LocalDatabase('postgres','m2_admin',schema='real_readiness')
    state=RealObservationLedger(db).verify_replay()
    if (not state or state.get('account_binding')!=prepare['account_binding'] or state.get('entry_authority') is not False
            or state.get('overnight_enabled') is not False or state.get('incident_latched')
            or state.get('managed_positions') or state.get('reservations')):
        raise ValueError('REAL_LOCAL_AUTHORITY_STATE_INVALID')
    with db.connect() as connection:
        connection.execute('SET TRANSACTION READ ONLY')
        locks=connection.execute("SELECT objid FROM pg_locks WHERE locktype='advisory' AND classid=481516 "
            "AND objid IN (902,903,904,905) AND granted").fetchall()
        real_schema=connection.execute("SELECT to_regnamespace('real_trader')").fetchone()[0]
    if locks or real_schema or (folder/'judicator-real-go.json').exists():
        raise ValueError('REAL_AUTHORITY_MUST_REMAIN_DISABLED')

    accounts=require_success(Operation.ACCOUNTS,reader.read(Operation.ACCOUNTS))
    account=select_single_individual_cash(accounts.body);account_id=account['account_id']
    if binding(account_id)!=prepare['account_binding']:raise ValueError('REAL_ACCOUNT_BINDING_MISMATCH')
    observations={op:require_success(op,reader.read(op,account_id=account_id))
        for op in (Operation.BALANCE,Operation.POSITIONS,Operation.OPEN_ORDERS)}
    checked=datetime.now(timezone.utc)
    received=[datetime.fromisoformat(x.received_at) for x in (accounts,*observations.values())]
    if (any(at.tzinfo is None or at<started or at>checked for at in received)
            or not 0<=(checked-min(received)).total_seconds()<=30):
        raise ValueError('REAL_ACCOUNT_OBSERVATION_STALE')
    orders=observations[Operation.OPEN_ORDERS]
    if orders.body.get('pagination_key'):raise ValueError('REAL_OPEN_ORDER_PAGINATION_UNRESOLVED')
    current=account_readiness(observations[Operation.BALANCE].body,observations[Operation.POSITIONS].body,
        orders.body,min(received))
    if (current.account_type!='INDIVIDUAL_CASH' or current.external_holdings or current.open_order_count
            or not current.account_totals_match or current.settled_usd!=Decimal('505.44')
            or current.unsettled_usd!=Decimal('0.00') or current.equity_usd!=Decimal('505.44')):
        raise ValueError('REAL_FLAT_CHECKPOINT_V2_CURRENT_ACCOUNT_MISMATCH')
    recent=history_summary(history_read(history_http,account_id))
    value=build_checkpoint(observed_at=min(received),build_sha256=build_digest(ROOT),
        account_binding=prepare['account_binding'],recent_orders=recent,ledger_version=state['version'])
    validate_checkpoint(value,account_binding=prepare['account_binding'],root=ROOT)
    sanitizer=Sanitizer(tuple(x for x in (credentials.app_key,credentials.app_secret,
        credentials.account_id,credentials.access_token,account_id) if x))
    text=json.dumps(value,indent=2);sanitizer.assert_clean(text)
    folder.mkdir(parents=True,exist_ok=True);temporary=target.with_suffix('.tmp')
    with temporary.open('x',encoding='utf-8') as output:
        output.write(text);output.flush();os.fsync(output.fileno())
    try:os.rename(temporary,target)
    except Exception:
        temporary.unlink(missing_ok=True);raise
    print(json.dumps({'status':'REAL_FLAT_CHECKPOINT_V2_CREATED','path':str(target.relative_to(ROOT)),
        'sha256':checkpoint_digest(target),'settled_usd':'505.44','unsettled_usd':'0.00',
        'equity_usd':'505.44','positions':0,'open_orders':0,'authority':False,'broker_mutations':0}))


if __name__=='__main__':
    try:main()
    except Exception as exc:
        code=str(exc) if isinstance(exc,ValueError) and str(exc).isupper() else 'REAL_FLAT_CHECKPOINT_V2_FAILED'
        print(json.dumps({'status':'BLOCKED','blocker':code}))
        raise SystemExit(2)
