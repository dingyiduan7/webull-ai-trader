"""Pure precision and integrity proof for a flat REAL cash checkpoint."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from hashlib import sha256
import json
from pathlib import Path
import re


CENT=Decimal('.01')
HALF_CENT=Decimal('.005')
CHECKPOINT_SCHEMA='REAL_FLAT_CHECKPOINT_V1'
CHECKPOINT_SCHEMA_V2='REAL_FLAT_CHECKPOINT_V2'
CHECKPOINT_SCHEMA_V3='REAL_FLAT_CHECKPOINT_V3'
V2_FILENAME='flat-checkpoint-v2-20260930.json'
BASE_CHECKPOINT_NAME='flat-checkpoint-20260929T145844Z.json'
BASE_CHECKPOINT_SHA256='EFD33BEEE04020794D380E187613A087E059DC91A2C3974F51BBA31B616BC7BB'
ACCOUNT_DIAGNOSTIC_NAME='account-cash-diagnostic-20260930T161140Z.json'
ACCOUNT_DIAGNOSTIC_SHA256='AD252F7124C84E05FD7F6356FDFF60A3FC3D1B283343E86E66E3CB514AF9411B'
CASH_ACTIVITY_DIAGNOSTIC_NAME='cash-activity-diagnostic-20260930T163954Z.json'
CASH_ACTIVITY_DIAGNOSTIC_SHA256='56DFDB9433E75F0BFC2794904270E15A6CD8EA8A6A0E801C130F5BBBFDEDD0BB'
ADJUSTMENT_TYPE='UNATTRIBUTED_NON_TRADING_BROKER_ADJUSTMENT'


def number(value):
    try:
        result=Decimal(str(value))
        if not result.is_finite():raise InvalidOperation
        return result
    except (InvalidOperation,TypeError,ValueError):
        raise ValueError('INVALID_FLAT_CHECKPOINT_NUMERIC') from None


def rounded(value):
    return number(value).quantize(CENT,rounding=ROUND_HALF_UP)


def prove_flat_cash(*,settled,unsettled,cash_balance,equity,precise_cash,fills):
    settled,unsettled,cash_balance,equity,precise_cash=map(number,
        (settled,unsettled,cash_balance,equity,precise_cash))
    if not fills:raise ValueError('LIQUIDATION_FILL_EVIDENCE_REQUIRED')
    gross=Decimal(0);fees=Decimal(0);reported_net=Decimal(0);per_fill_display_net=Decimal(0)
    broker_nets=[]
    for fill in fills:
        quantity,price,fee=map(number,(fill['quantity'],fill['filled_price'],fill['fees_usd']))
        if quantity<=0 or price<=0 or fee<0:raise ValueError('LIQUIDATION_FILL_EVIDENCE_INVALID')
        fill_gross=quantity*price;fill_net=fill_gross-fee
        gross+=fill_gross;fees+=fee;reported_net+=fill_net;per_fill_display_net+=rounded(fill_net)
        broker_net=fill.get('broker_net_usd')
        if broker_net is not None:broker_nets.append(number(broker_net))
    if broker_nets and len(broker_nets)!=len(fills):
        raise ValueError('PARTIAL_BROKER_NET_EVIDENCE')
    precise_unsettled=precise_cash-settled
    difference=precise_unsettled-reported_net
    conditions=dict(flat_equity=(equity==cash_balance),display_cash=(cash_balance==settled+unsettled),
        precise_cash_rounds=(rounded(precise_cash)==cash_balance),
        reported_net_rounds=(rounded(reported_net)==unsettled),
        precise_unsettled_rounds=(rounded(precise_unsettled)==unsettled),
        sub_half_cent_difference=(abs(difference)<HALF_CENT),
        broker_net_complete=(not broker_nets or len(broker_nets)==len(fills)),
        broker_net_matches=(not broker_nets or abs(sum(broker_nets,Decimal(0))-precise_unsettled)<HALF_CENT))
    if not all(conditions.values()):raise ValueError('UNSETTLED_PROCEEDS_UNRECONCILED')
    return dict(gross_usd=str(gross),fees_usd=str(fees),reported_net_exact_usd=str(reported_net),
        per_fill_rounded_net_sum_usd=str(per_fill_display_net),precise_unsettled_evidence_usd=str(precise_unsettled),
        broker_display_unsettled_usd=str(unsettled),precision_difference_usd=str(difference),
        broker_net_fields='COMPLETE' if broker_nets else 'UNAVAILABLE',conditions=conditions)


def checkpoint_digest(path):
    return sha256(Path(path).read_bytes()).hexdigest().upper()


def _validate_v1(value, *, account_binding):
    if not isinstance(value,dict) or value.get('schema')!=CHECKPOINT_SCHEMA or value.get('environment')!='REAL_READ_ONLY':
        raise ValueError('REAL_FLAT_CHECKPOINT_SCHEMA')
    account=value.get('account',{});recent=value.get('recent_orders',{});cash=value.get('cash',{})
    local=value.get('local_integrity',{});authority=value.get('authority',{});liquidation=value.get('liquidation',{})
    if (account.get('type')!='INDIVIDUAL_CASH' or account.get('sole_account') is not True
            or account.get('binding_matches_observation_journal') is not True):
        raise ValueError('REAL_FLAT_CHECKPOINT_ACCOUNT')
    if value.get('positions_count')!=0 or value.get('open_order_count')!=0:
        raise ValueError('REAL_FLAT_CHECKPOINT_NOT_FLAT')
    if (recent.get('all_terminal') is not True or recent.get('ambiguous_count')!=0
            or not isinstance(recent.get('history_count'),int) or recent['history_count']<1):
        raise ValueError('REAL_FLAT_CHECKPOINT_ORDER_HISTORY')
    fills=liquidation.get('fills')
    proof=prove_flat_cash(settled=cash.get('settled_usd'),unsettled=cash.get('unsettled_usd'),
        cash_balance=cash.get('cash_balance_usd'),equity=cash.get('equity_usd'),
        precise_cash=cash.get('precise_cash_evidence_usd'),fills=fills)
    if (cash.get('totals_reconciled') is not True or proof!=liquidation.get('precision_proof')
            or number(liquidation.get('total_fees_usd'))!=number(proof['fees_usd'])):
        raise ValueError('REAL_FLAT_CHECKPOINT_CASH')
    if (local.get('observation_replay_verified') is not True or local.get('observation_ledger_version')!=1
            or local.get('writer_locks')!=0 or local.get('original_execution_approved') is not False
            or local.get('recovery_execution_approved') is not False
            or local.get('real_strategy_schema_present') is not False
            or local.get('judicator_real_go_present') is not False):
        raise ValueError('REAL_FLAT_CHECKPOINT_LOCAL_INTEGRITY')
    if authority!={'runner_started':False,'astra_called':False,'broker_mutations':0,'strategy_authority_enabled':False}:
        raise ValueError('REAL_FLAT_CHECKPOINT_AUTHORITY')
    observed=value.get('observed_at')
    if not isinstance(observed,str):raise ValueError('REAL_FLAT_CHECKPOINT_TIME')
    # The binding is deliberately absent from the sanitized artifact. It is
    # bound through the replay-verified observation ledger and GO receipt.
    if not isinstance(account_binding,str) or not account_binding.startswith('real-'):
        raise ValueError('REAL_ACCOUNT_BINDING_REQUIRED')
    return value


def _artifact(root,name,digest_value):
    if Path(name).name!=name:raise ValueError('REAL_FLAT_CHECKPOINT_V2_ARTIFACT')
    path=Path(root)/'.local'/'real-readiness'/name
    if not path.is_file() or checkpoint_digest(path)!=digest_value:
        raise ValueError('REAL_FLAT_CHECKPOINT_V2_ARTIFACT')
    try:return json.loads(path.read_text(encoding='utf-8'))
    except (OSError,ValueError):raise ValueError('REAL_FLAT_CHECKPOINT_V2_ARTIFACT') from None


def _validate_v2(value, *, account_binding, root):
    if root is None:raise ValueError('REAL_FLAT_CHECKPOINT_V2_ROOT_REQUIRED')
    if (not isinstance(value,dict) or value.get('schema')!=CHECKPOINT_SCHEMA_V2
            or value.get('environment')!='REAL_READ_ONLY'
            or value.get('account_binding')!=account_binding
            or not re.fullmatch(r'[0-9a-f]{64}',str(value.get('build_sha256','')))):
        raise ValueError('REAL_FLAT_CHECKPOINT_V2_SCHEMA')
    account=value.get('account',{});cash=value.get('cash',{});recent=value.get('recent_orders',{})
    local=value.get('local_integrity',{});continuity=value.get('continuity',{})
    if account!={'type':'INDIVIDUAL_CASH','sole_account':True,'binding_matches_observation_journal':True}:
        raise ValueError('REAL_FLAT_CHECKPOINT_V2_ACCOUNT')
    if value.get('positions_count')!=0 or value.get('open_order_count')!=0:
        raise ValueError('REAL_FLAT_CHECKPOINT_V2_NOT_FLAT')
    if (recent.get('all_terminal') is not True or recent.get('ambiguous_count')!=0
            or recent.get('partial_or_unknown_count')!=0
            or not isinstance(recent.get('history_count'),int) or recent['history_count']<0):
        raise ValueError('REAL_FLAT_CHECKPOINT_V2_ORDER_HISTORY')
    expected_cash={'settled_usd':'505.44','unsettled_usd':'0.00','cash_balance_usd':'505.44',
        'equity_usd':'505.44','totals_reconciled':True}
    if cash!=expected_cash:raise ValueError('REAL_FLAT_CHECKPOINT_V2_CASH')
    expected_refs={
        'base_checkpoint':{'name':BASE_CHECKPOINT_NAME,'sha256':BASE_CHECKPOINT_SHA256},
        'account_diagnostic':{'name':ACCOUNT_DIAGNOSTIC_NAME,'sha256':ACCOUNT_DIAGNOSTIC_SHA256},
        'cash_activity_diagnostic':{'name':CASH_ACTIVITY_DIAGNOSTIC_NAME,'sha256':CASH_ACTIVITY_DIAGNOSTIC_SHA256}}
    for key,expected in expected_refs.items():
        if continuity.get(key)!=expected:raise ValueError('REAL_FLAT_CHECKPOINT_V2_ARTIFACT')
    expected_continuity={'base_total_usd':'505.35','current_total_usd':'505.44','adjustment_usd':'0.09',
        'adjustment_type':ADJUSTMENT_TYPE,'cash_activity_net_usd':'0',
        'cash_activity_match':False,'trading_profit_usd':'0','realized_pnl_effect_usd':'0'}
    if any(continuity.get(k)!=v for k,v in expected_continuity.items()):
        raise ValueError('REAL_FLAT_CHECKPOINT_V2_CONTINUITY')
    owner=value.get('owner_authorization')
    if owner!={'scope':'REAL_FLAT_CHECKPOINT_V2_SUPERSESSION','decision':'AUTHORIZED',
            'provenance':'OWNER_VIA_PROJECT_COMMAND_CENTER'}:
        raise ValueError('REAL_FLAT_CHECKPOINT_V2_OWNER_AUTHORIZATION')
    if (local.get('observation_replay_verified') is not True or local.get('observation_ledger_version')!=1
            or local.get('writer_locks')!=0 or local.get('real_strategy_schema_present') is not False
            or local.get('judicator_real_go_present') is not False):
        raise ValueError('REAL_FLAT_CHECKPOINT_V2_LOCAL_INTEGRITY')
    if value.get('authority') is not False:raise ValueError('REAL_FLAT_CHECKPOINT_V2_AUTHORITY')
    if not isinstance(value.get('observed_at'),str):raise ValueError('REAL_FLAT_CHECKPOINT_V2_TIME')

    base=_artifact(root,BASE_CHECKPOINT_NAME,BASE_CHECKPOINT_SHA256)
    _validate_v1(base,account_binding=account_binding)
    account_diagnostic=_artifact(root,ACCOUNT_DIAGNOSTIC_NAME,ACCOUNT_DIAGNOSTIC_SHA256)
    activity=_artifact(root,CASH_ACTIVITY_DIAGNOSTIC_NAME,CASH_ACTIVITY_DIAGNOSTIC_SHA256)
    diagnostic_cash={'settled_usd':'505.44','unsettled_usd':'0.00',
        'cash_balance_usd':'505.44','equity_usd':'505.44'}
    if (account_diagnostic.get('schema')!='REAL_ACCOUNT_CASH_MISMATCH_DIAGNOSTIC_V1'
            or account_diagnostic.get('account_binding')!=account_binding
            or account_diagnostic.get('flat_checkpoint_sha256')!=BASE_CHECKPOINT_SHA256
            or account_diagnostic.get('current_cash')!=diagnostic_cash
            or account_diagnostic.get('account',{}).get('flat') is not True
            or account_diagnostic.get('account',{}).get('positions_count')!=0
            or account_diagnostic.get('account',{}).get('open_nonfinal_order_count')!=0
            or account_diagnostic.get('delta',{}).get('cash_balance_usd')!='0.09'
            or account_diagnostic.get('authority',{}).get('readiness_evidence') is not False):
        raise ValueError('REAL_FLAT_CHECKPOINT_V2_ACCOUNT_DIAGNOSTIC')
    if (activity.get('schema')!='REAL_CASH_ACTIVITY_DIAGNOSTIC_V1'
            or activity.get('account_binding')!=account_binding
            or activity.get('raw_exact_net_usd')!='0'
            or activity.get('expected_checkpoint_delta_usd')!='0.09'
            or activity.get('net_matches_checkpoint_delta') is not False
            or activity.get('authority',{}).get('readiness_evidence') is not False):
        raise ValueError('REAL_FLAT_CHECKPOINT_V2_CASH_ACTIVITY_DIAGNOSTIC')
    return value


def _validate_v3(value, *, account_binding):
    """Fresh read-only bootstrap of a flat, fully settled account (scripts/real_bootstrap.py).

    Unlike V1/V2 it carries no liquidation or historical-continuity proof: it
    is only valid when nothing is unsettled, so the opening ledger needs no
    receivable provenance. Exact cash is the ledger's starting point.
    """
    if (not isinstance(value,dict) or value.get('environment')!='REAL_READ_ONLY'
            or not isinstance(account_binding,str) or not account_binding.startswith('real-')
            or value.get('account_binding')!=account_binding):
        raise ValueError('REAL_FLAT_CHECKPOINT_V3_SCHEMA')
    required={'schema','environment','account_binding','observed_at','account','positions_count',
        'open_order_count','cash','local_integrity','authority','created_by'}
    if set(value)!=required or value.get('created_by')!='scripts/real_bootstrap.py':
        raise ValueError('REAL_FLAT_CHECKPOINT_V3_SCHEMA')
    if value.get('account')!={'type':'INDIVIDUAL_CASH','sole_account':True}:
        raise ValueError('REAL_FLAT_CHECKPOINT_V3_ACCOUNT')
    if value.get('positions_count')!=0 or value.get('open_order_count')!=0:
        raise ValueError('REAL_FLAT_CHECKPOINT_V3_NOT_FLAT')
    cash=value.get('cash')
    if not isinstance(cash,dict) or set(cash)!={'settled_usd','unsettled_usd','cash_balance_usd','equity_usd','totals_reconciled'}:
        raise ValueError('REAL_FLAT_CHECKPOINT_V3_CASH')
    settled,unsettled,balance,equity=(number(cash[k]) for k in ('settled_usd','unsettled_usd','cash_balance_usd','equity_usd'))
    if (cash['totals_reconciled'] is not True or unsettled!=0 or settled<=0
            or not settled==balance==equity):
        raise ValueError('REAL_FLAT_CHECKPOINT_V3_CASH')
    local=value.get('local_integrity')
    if (not isinstance(local,dict) or local.get('observation_replay_verified') is not True
            or type(local.get('observation_ledger_version')) is not int or local['observation_ledger_version']<1):
        raise ValueError('REAL_FLAT_CHECKPOINT_V3_LOCAL_INTEGRITY')
    if value.get('authority') is not False:raise ValueError('REAL_FLAT_CHECKPOINT_V3_AUTHORITY')
    if not isinstance(value.get('observed_at'),str):raise ValueError('REAL_FLAT_CHECKPOINT_V3_TIME')
    return value


def validate_checkpoint(value, *, account_binding, root=None):
    """Validate a sanitized flat checkpoint without granting authority."""
    if isinstance(value,dict) and value.get('schema')==CHECKPOINT_SCHEMA_V3:
        return _validate_v3(value,account_binding=account_binding)
    if isinstance(value,dict) and value.get('schema')==CHECKPOINT_SCHEMA_V2:
        return _validate_v2(value,account_binding=account_binding,root=root)
    return _validate_v1(value,account_binding=account_binding)


def load_checkpoint(path, *, account_binding, expected_sha256=None):
    path=Path(path)
    try:value=json.loads(path.read_text(encoding='utf-8'))
    except (OSError,ValueError):raise ValueError('REAL_FLAT_CHECKPOINT_REQUIRED') from None
    actual=checkpoint_digest(path)
    if expected_sha256 is not None and actual!=str(expected_sha256).upper():
        raise ValueError('REAL_FLAT_CHECKPOINT_DIGEST')
    root=path.resolve().parents[2] if len(path.resolve().parents)>2 else None
    return validate_checkpoint(value,account_binding=account_binding,root=root),actual
