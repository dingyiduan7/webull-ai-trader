"""Bounded REAL account-only certification outside market hours. No quotes or authority."""
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json, os, re, sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))

from autonomous_trader.durable.ledger import LocalDatabase
from autonomous_trader.durable.projection import (MAX_ENTRY_SUBMISSIONS_PER_SESSION,
    MAX_ENTRY_SUBMISSIONS_PER_EXCHANGE_WEEK)
from autonomous_trader.monitoring.model_budget import (REAL_MONTHLY_COST_CONTROL,REAL_MONTHLY_COST_SUSPENSION_ID,
    HARD_CALLS_PER_300S,
    MAX_NON_POSITION_CALLS_PER_300S,MAX_PORTFOLIO_CALLS_PER_SESSION,
    MAX_PORTFOLIO_CALLS_PER_EXCHANGE_WEEK,MAX_POSITION_CALLS_PER_SESSION,
    MAX_POSITION_CALLS_PER_EXCHANGE_WEEK,MAX_REAL_CALLS_PER_SESSION,
    MAX_REAL_CALLS_PER_EXCHANGE_WEEK)
from autonomous_trader.real_preflight import (REAL_HTTP_HOST,RealReadOnly,Operation,require_success,
    select_single_individual_cash)
from autonomous_trader.real_flat_checkpoint import checkpoint_digest
from autonomous_trader.real_readiness import account_readiness
from autonomous_trader.real_secrets import load_real_credentials
from autonomous_trader.real_state import RealObservationLedger,binding
from autonomous_trader.real_trading import RealGate,build_digest,CAPABILITY_VERSION

CHECKPOINT_NAME='flat-checkpoint-v2-20260930.json'
CHECKPOINT_SHA256='0ADC634776FFC6D532A55831427199F43DA87DD6B36C47C91DEC8E0058B0A82F'
ACCOUNT_OPERATIONS=(Operation.ACCOUNTS,Operation.BALANCE,Operation.POSITIONS,Operation.OPEN_ORDERS)
ACCOUNT_PATHS=('/trading/accounts/list','/trading/assets/balances/get',
    '/trading/assets/positions/list','/trading/orders/open-orders/list')


def strategy_schema_present(db):
    with db.connect() as connection:
        connection.execute('SET TRANSACTION READ ONLY')
        return bool(connection.execute("SELECT to_regnamespace('real_trader') IS NOT NULL").fetchone()[0])


def project_checkpoint(root):
    path=Path(root).resolve()/'.local'/'real-readiness'/CHECKPOINT_NAME
    if not path.is_file():raise ValueError('REAL_FLAT_CHECKPOINT_REQUIRED')
    if checkpoint_digest(path)!=CHECKPOINT_SHA256:raise ValueError('REAL_FLAT_CHECKPOINT_DIGEST')
    return path


def certification_tool_hashes(root):
    return {'after_hours_script_sha256':sha256(Path(__file__).read_bytes()).hexdigest(),
        'market_open_script_sha256':sha256((Path(root)/'scripts'/'webull_real_readiness_packet.py').read_bytes()).hexdigest()}


def settlement_continuity(current,checkpoint):
    prior=checkpoint['cash'];old_settled=Decimal(prior['settled_usd']);old_unsettled=Decimal(prior['unsettled_usd'])
    baseline=old_settled+old_unsettled
    if (current.settled_usd+current.unsettled_usd!=baseline
            or current.equity_usd!=Decimal(prior['equity_usd'])):
        raise ValueError('REAL_FLAT_CHECKPOINT_CASH_CHANGED')
    settled_delta=current.settled_usd-old_settled
    if settled_delta<0 or old_unsettled-current.unsettled_usd!=settled_delta:
        raise ValueError('REAL_SETTLEMENT_TRANSITION_INVALID')
    return 'UNCHANGED' if settled_delta==0 else 'EXPECTED_SETTLEMENT_EVOLUTION'


def account_only_packet(reader,db,root,clock=lambda:datetime.now(timezone.utc)):
    started_at=clock()
    project_checkpoint(root)
    state=RealObservationLedger(db).verify_replay()
    if (not state or state.get('entry_authority') is not False or state.get('overnight_enabled') is not False
            or state.get('incident_latched') or not state.get('observation_reconciled')
            or state.get('managed_positions') or state.get('reservations')):
        raise ValueError('REAL_LOCAL_AUTHORITY_STATE_INVALID')
    gate=RealGate(root,state['account_binding'],db)
    checkpoint,digest=gate.checkpoint(CHECKPOINT_SHA256)
    if digest!=CHECKPOINT_SHA256:raise ValueError('REAL_FLAT_CHECKPOINT_DIGEST')
    if (Path(root)/'.local'/'real-readiness'/'judicator-real-go.json').exists() or strategy_schema_present(db):
        raise ValueError('REAL_AUTHORITY_MUST_REMAIN_DISABLED')

    account_ob=require_success(Operation.ACCOUNTS,reader.read(Operation.ACCOUNTS))
    account=select_single_individual_cash(account_ob.body)
    if binding(account['account_id'])!=state['account_binding']:
        raise ValueError('REAL_ACCOUNT_BINDING_MISMATCH')
    balance_ob=require_success(Operation.BALANCE,reader.read(Operation.BALANCE,account_id=account['account_id']))
    positions_ob=require_success(Operation.POSITIONS,reader.read(Operation.POSITIONS,account_id=account['account_id']))
    orders_ob=require_success(Operation.OPEN_ORDERS,reader.read(Operation.OPEN_ORDERS,account_id=account['account_id']))
    if (not isinstance(orders_ob.body,dict) or not isinstance(orders_ob.body.get('data'),list)
            or orders_ob.body.get('pagination_key')):
        raise ValueError('REAL_OPEN_ORDER_PAGINATION_UNRESOLVED')
    checked_at=clock()
    received=[datetime.fromisoformat(x.received_at) for x in (account_ob,balance_ob,positions_ob,orders_ob)]
    if (any(at.tzinfo is None or at<started_at or at>checked_at for at in received)
            or not 0<=(checked_at-min(received)).total_seconds()<=30):
        raise ValueError('REAL_ACCOUNT_OBSERVATION_STALE')
    if (max(received)-min(received)).total_seconds()>60:raise ValueError('REAL_ACCOUNT_READ_WINDOW_EXCEEDED')
    current=account_readiness(balance_ob.body,positions_ob.body,orders_ob.body,min(received))
    if (current.account_type!='INDIVIDUAL_CASH' or current.external_holdings or current.open_order_count
            or not current.account_totals_match):
        raise ValueError('REAL_CURRENT_ACCOUNT_NOT_FLAT')
    transition=settlement_continuity(current,checkpoint)
    contacted=tuple(path for method,path in reader._http.contacted if method=='GET')
    if contacted!=ACCOUNT_PATHS:raise ValueError('REAL_ACCOUNT_ONLY_ROUTE_VIOLATION')
    return {
        'schema':'REAL_AFTER_HOURS_ACCOUNT_PACKET_V1','environment':'REAL_ACCOUNT_READ_ONLY_AFTER_HOURS',
        'created_at':checked_at.isoformat(),'build_sha256':build_digest(root),'flat_checkpoint_sha256':digest,
        'account_binding':state['account_binding'],
        'certification_tools':certification_tool_hashes(root),
        'authentication':{'token_valid_for_bounded_reads':True,'host':REAL_HTTP_HOST,
            'signed_get_count':len(contacted),'sandbox_contacted':False},
        'account':{'type':'INDIVIDUAL_CASH','sole_account_binding_verified':True,'flat':True,
            'positions_count':0,'open_nonfinal_order_count':0},
        'cash':{'settled_usd':str(current.settled_usd),'unsettled_usd':str(current.unsettled_usd),
            'equity_usd':str(current.equity_usd),'checkpoint_total_usd':str(Decimal(checkpoint['cash']['settled_usd'])+
                Decimal(checkpoint['cash']['unsettled_usd'])),'continuity':transition},
        'checkpoint_handoff':{'location':'PROJECT_ROOT/.local/real-readiness/'+CHECKPOINT_NAME,
            'exact_hash_verified':True,'account_binding_verified':True,'checkpoint_flat':True,
            'current_broker_flat':True,'prior_observation_external_quantity_count':len(state.get('external_quantities',{})),
            'observation_journal_mutated':False},
        'market_data':{'requested':False,'discovery_requested':False,'quote_freshness':'DEFERRED_TO_CORE_SESSION',
            'nasdaq_basic_entitlement':'DEFERRED_TO_CORE_SESSION'},
        'limits':{'entry_submissions_per_session':None,
            'round_trips_per_exchange_week':10,'trade_limit_control':'RESERVED_ENTRY_EXIT_ROUND_TRIP_V1',
            'portfolio_calls_per_session':MAX_PORTFOLIO_CALLS_PER_SESSION,
            'portfolio_calls_per_exchange_week':MAX_PORTFOLIO_CALLS_PER_EXCHANGE_WEEK,
            'position_calls_per_session':MAX_POSITION_CALLS_PER_SESSION,
            'position_calls_per_exchange_week':MAX_POSITION_CALLS_PER_EXCHANGE_WEEK,
            'astra_calls_per_session':MAX_REAL_CALLS_PER_SESSION,
            'astra_calls_per_exchange_week':MAX_REAL_CALLS_PER_EXCHANGE_WEEK,
            'model_calls_per_300_seconds':HARD_CALLS_PER_300S,
            'non_position_calls_per_300_seconds':MAX_NON_POSITION_CALLS_PER_300S,
            'monthly_model_cost_cap_enabled':False,'monthly_model_cost_cap_usd':None,
            'monthly_model_cost_control':REAL_MONTHLY_COST_CONTROL,
            'monthly_model_cost_suspension_id':REAL_MONTHLY_COST_SUSPENSION_ID},
        'protective_exit':{'capability_version':CAPABILITY_VERSION,'same_day_required':True,
            'overnight_authority':False,'status':'BUILD_BOUNDARY_PRESENT; LIVE_QUOTE_CHECK_REQUIRED_DURING_CORE_SESSION'},
        'authority':{'production_enabled':False,'judicator_go_present':False,'real_writer_schema_present':False,
            'entry_authority':False,'runner_started_by_check':False,'scheduler_started_by_check':False,
            'astra_called':False,'broker_mutations':0,'ledger_mutations':0}}


def clean_json(value,forbidden):
    text=json.dumps(value,indent=2,ensure_ascii=False)
    lowered=text.lower()
    if any(item and item in text for item in forbidden):raise ValueError('REAL_SECRET_SANITIZATION_FAILURE')
    if any(word in lowered for word in ('app_secret','app key','authorization','access_token','x-signature')):
        raise ValueError('REAL_SECRET_SANITIZATION_FAILURE')
    return text


def write_packet(root,value,forbidden,now):
    text=clean_json(value,forbidden);folder=Path(root)/'.local'/'real-readiness';folder.mkdir(parents=True,exist_ok=True)
    path=folder/('after-hours-account-packet-'+now.strftime('%Y%m%dT%H%M%SZ')+'.json')
    temporary=path.with_suffix('.tmp')
    with temporary.open('w',encoding='utf-8') as output:
        output.write(text);output.flush();os.fsync(output.fileno())
    os.replace(temporary,path)
    return path


def main():
    prepare=RealGate.offline_account_preparation(ROOT,datetime.now(timezone.utc))
    credentials=load_real_credentials(ROOT)
    db=LocalDatabase('postgres','m2_admin',schema='real_readiness')
    value=account_only_packet(RealReadOnly(credentials),db,ROOT)
    if (value['build_sha256']!=prepare['build_sha256']
            or value['flat_checkpoint_sha256']!=prepare['flat_checkpoint_sha256']
            or value['account_binding']!=prepare['account_binding']):
        raise ValueError('JUDICATOR_REAL_ACCOUNT_PREPARATION_INVALID')
    now=datetime.fromisoformat(value['created_at'])
    write_packet(ROOT,value,(credentials.app_key,credentials.app_secret,credentials.account_id,
        credentials.access_token),now)
    print(json.dumps({'status':'REAL_AFTER_HOURS_ACCOUNT_PACKET_VALID','positions':0,'open_orders':0,
        'broker_mutations':0,'astra_calls':0},separators=(',',':')))


def cli():
    try:main();return 0
    except Exception as exc:
        code=str(exc)
        if not re.fullmatch(r'[A-Z][A-Z0-9_]{2,79}',code):code='REAL_AFTER_HOURS_ACCOUNT_CHECK_FAILED'
        print(json.dumps({'status':code},separators=(',',':')))
        return 2


if __name__=='__main__':raise SystemExit(cli())
