"""Persistent reviewed scope and fresh, non-transferable daily REAL evidence.

The owner supplies the persistent authorization. This module can issue a
technical certificate, never expand the approved account/build/policy scope.
"""
from .runtime_storage import data_root
from datetime import datetime, timedelta, date
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
import json
import os

from .contracts import AlphaContext, Instrument, payload_hash
from .risk import POLICY
from .real_state import digest
from .mvp.config import reviewed_calendar
from .reference import NEW_YORK

AUTHORITY_NAME='judicator-real-persistent.json'
POINTER_NAME='current-real-session.json'
RECOVERABLE='REAL_ACCOUNT_RECONCILIATION_FAILED'
from .recovery_reasons import FLAT_RECOVERABLE_REASONS as RECOVERABLE_REASONS


# Multi-day holding switch (owner decision 2026-10-14). Part of the signed charter scope: flipping it
# requires the owner to re-sign (scripts/owner_sign_authorization.py), so code alone cannot enable it.
OVERNIGHT_HOLDING=True  # owner decision 2026-10-16: on, for high-upside positions only (carry.CARRY_MIN_UPSIDE)


def scope():
    from .monitoring.model_budget import (REAL_MONTHLY_COST_CONTROL,REAL_MONTHLY_COST_SUSPENSION_ID,
        MAX_REAL_CALLS_PER_SESSION,MAX_REAL_CALLS_PER_EXCHANGE_WEEK)
    return dict(capital_usd='500',risk_policy_sha256=payload_hash(POLICY),entries_per_session=None,
        round_trips_per_exchange_week=None,trade_limit_control='RESERVED_ENTRY_EXIT_ROUND_TRIP_V1',
        astra_calls_per_session=MAX_REAL_CALLS_PER_SESSION,
        astra_calls_per_week=MAX_REAL_CALLS_PER_EXCHANGE_WEEK,
        monthly_model_cost_cap_enabled=False,monthly_model_cost_cap_usd=None,
        monthly_model_cost_control=REAL_MONTHLY_COST_CONTROL,
        monthly_model_cost_suspension_id=REAL_MONTHLY_COST_SUSPENSION_ID,
        overnight=OVERNIGHT_HOLDING,environment='REAL',account_type='INDIVIDUAL_CASH')


def folder(root):return data_root(root)/'.local/real-readiness'


def read_json(path):
    try:return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError,ValueError):raise ValueError('REAL_DAILY_EVIDENCE_UNAVAILABLE') from None


def load_authority(root,now):
    """File-only guard: must precede DB, secrets, process locks or broker access."""
    from .real_owner_authorization import verify_owner_authorization
    value=verify_owner_authorization(root)
    return validate_unsigned_request(root,value,now)


def validate_unsigned_request(root,value,now):
    """Technical preparation only. This function NEVER establishes owner approval.

    The owner console uses it before prompting, so a malformed/expired/wrong-build
    request does not consume a genuine owner signature that startup cannot use.
    Runtime entrypoints must call load_authority, including signature verification.
    """
    from .real_trading import build_digest
    from .real_flat_checkpoint import load_checkpoint
    required={'schema','verdict','authority_id','account_binding','build_sha256','scope',
        'flat_checkpoint_sha256','entitlement_evidence','issued_at','expires_at','revoked'}
    try:
        issued=datetime.fromisoformat(value['issued_at']);expires=datetime.fromisoformat(value['expires_at'])
        valid=(set(value)==required and value['schema']=='REAL_PERSISTENT_AUTHORITY_V1'
            and value['verdict']=='GO' and value['revoked'] is False
            and isinstance(value['authority_id'],str) and bool(value['authority_id'])
            and value['build_sha256']==build_digest(root) and value['scope']==scope()
            and issued.tzinfo is not None and expires.tzinfo is not None and issued<=now<expires)
    except (KeyError,TypeError,ValueError):valid=False
    if not valid:raise ValueError('REAL_PERSISTENT_AUTHORITY_INVALID')
    matches=[p for p in folder(root).glob('flat-checkpoint-*.json')
        if sha256(p.read_bytes()).hexdigest().upper()==value['flat_checkpoint_sha256']]
    if len(matches)!=1:raise ValueError('REAL_FLAT_CHECKPOINT_REQUIRED')
    load_checkpoint(matches[0],account_binding=value['account_binding'],
        expected_sha256=value['flat_checkpoint_sha256'])
    # The original portal evidence/observation time is preserved. New session
    # bindings do not assert that the portal was freshly inspected each day.
    session_entitlement(value,now,root)
    return value


def session_entitlement(authority,now,root):
    from .real_trading import validate_realtime_entitlement
    label=now.astimezone(NEW_YORK).date().isoformat()
    original=authority['entitlement_evidence']
    if (original.get('account_binding')!=authority['account_binding']
            or original.get('build_sha256')!=authority['build_sha256']):
        raise ValueError('REAL_ENTITLEMENT_UNVERIFIED')
    value={**original,'session_label':label}
    return validate_realtime_entitlement(value,dict(account_binding=authority['account_binding'],
        build_sha256=authority['build_sha256'],market_session_label=label),now,root)


def eligible_incidents(state,today=None):
    """`today` (a date): a daily-loss latch from an earlier session is not a block, because the ledger
    resets it at that day's NEW_SESSION; drawdown/termination latches always block."""
    incidents=state.get('incidents',{})
    if any(v.get('reason') not in RECOVERABLE_REASONS or v.get('pending_correction') is not False for v in incidents.values()):
        raise ValueError('REAL_DAILY_UNRESOLVED_INCIDENT')
    stale_daily=today is not None and state['session']['label']<today.isoformat()
    if (any(state['latches'].get(k,False) for k in (('drawdown','terminated') if stale_daily else ('daily','drawdown','terminated')))
            or bool(state['latches'].get('incident'))!=bool(incidents)):
        raise ValueError('REAL_DAILY_SAFETY_LATCH')
    return tuple(sorted(incidents))


def policy_authorized(state,authority):
    """The ledger runs under the owner-signed risk policy, or the owner re-signed a new policy that the
    running code implements exactly (the next normal start journals that revision in the ledger)."""
    signed=authority['scope']['risk_policy_sha256']
    return state['policy_hash']==signed or (signed==payload_hash(POLICY) and state['policy_hash'] in PRIOR_REAL_POLICY_SHA256)


# REAL policy hashes the owner signed before (ledger may still record one until the revision is journaled).
PRIOR_REAL_POLICY_SHA256=frozenset({payload_hash(POLICY.model_copy(update={'trade_risk':Decimal('0.015'),
    'open_risk':Decimal('0.05')}))})


def carried_positions_allowed(state,authority):
    """Positions may exist at a session start only when the signed charter authorizes overnight holding."""
    return bool(state['positions']) and authority['scope'].get('overnight') is True


def verify_flat_account(state,packet,authority,now):
    """Check existing accounting; an exact due transition is journaled later by the controller.

    With charter-authorized overnight holding the account may carry positions: every position quantity
    must then equal the ledger exactly and cash buckets must reconcile exactly (equity moves with marks)."""
    from .durable.projection import settlement_observation_date
    carried=carried_positions_allowed(state,authority)
    packet.validate(now,flat=not carried)
    if state['account_id']!=authority['account_binding'] or packet.account_binding!=state['account_id']:
        raise ValueError('REAL_ACCOUNT_BINDING_MISMATCH')
    if not policy_authorized(state,authority):raise ValueError('REAL_DAILY_POLICY_MISMATCH')
    if ((state['positions'] and not carried) or state['reservations'] or packet.open_client_ids
            or any(not r['final'] or r.get('phase')=='UNKNOWN' for r in state['orders'].values())):
        raise ValueError('REAL_DAILY_UNRESOLVED_EXPOSURE_OR_ORDER')
    if carried and dict(packet.positions)!={iid:Decimal(row['quantity']) for iid,row in state['positions'].items()}:
        raise ValueError('REAL_DAILY_CARRY_POSITION_MISMATCH')
    if not state['reconciliation']['settlement_known']:raise ValueError('REAL_DAILY_SETTLEMENT_UNKNOWN')
    ids=eligible_incidents(state,now.astimezone(NEW_YORK).date());cash=state['cash']
    from .broker_truth import plan as broker_plan
    planned=broker_plan(state,packet.settled,packet.unsettled,now,broker_positions=packet.positions,
        broker_open_ids=packet.open_client_ids)
    if planned is not None:
        # Broker-truth (owner 2026-10-18): on a quiet account the broker's cash is the balance; the trader
        # journals the settlements/adoption at its first refresh. Only the broker packet's own arithmetic is checked.
        if packet.settled<0 or packet.unsettled<0 or (not carried and packet.equity!=packet.settled+packet.unsettled):
            raise ValueError('REAL_DAILY_CASH_MISMATCH')
        return dict(ledger_version=state['version'],state_sha256=digest(state),incident_ids=list(ids),
            settlement_ids=list(planned[0]),settlement_known=True,positions=len(packet.positions),nonfinal_orders=0,
            settled_usd=str(packet.settled),unsettled_usd=str(packet.unsettled),equity_usd=str(packet.equity),
            broker_truth=dict(settled_delta=str(planned[1]),unsettled_delta=str(planned[2])))
    settled=Decimal(cash['settled_usd']);unsettled=Decimal(cash['unsettled_usd'])
    lots=[(key,row) for key,row in state['settlements'].items() if Decimal(row['outstanding_usd'])>0]
    outstanding=sum((Decimal(row['outstanding_usd']) for _,row in lots),Decimal(0))
    if outstanding!=unsettled:raise ValueError('REAL_DAILY_SETTLEMENT_MISMATCH')
    delta=packet.settled-settled
    due=[]
    if delta:
        effective=settlement_observation_date(state,now)
        if (delta<=0 or delta!=outstanding or packet.unsettled!=unsettled-delta
                or any(date.fromisoformat(row['settlement_date'])>effective
                    or row['settlement_date'] not in state['config']['settlement_days'] for _,row in lots)):
            raise ValueError('SETTLEMENT_CHANGE_UNRECONCILED')
        due=[key for key,_ in lots]
    elif packet.unsettled!=unsettled:raise ValueError('REAL_DAILY_CASH_MISMATCH')
    if packet.settled+packet.unsettled!=settled+unsettled:raise ValueError('REAL_DAILY_CASH_MISMATCH')
    if not carried and (packet.equity!=Decimal(cash['liquidation_equity_usd']) or packet.equity!=settled+unsettled):
        raise ValueError('REAL_DAILY_CASH_MISMATCH')
    return dict(ledger_version=state['version'],state_sha256=digest(state),incident_ids=list(ids),
        settlement_ids=due,settlement_known=True,positions=len(packet.positions),nonfinal_orders=0,
        settled_usd=str(packet.settled),unsettled_usd=str(packet.unsettled),equity_usd=str(packet.equity))


def encode_packet(packet):
    return dict(account_binding=packet.account_binding,observed_at=packet.observed_at.isoformat(),
        settled=str(packet.settled),unsettled=str(packet.unsettled),equity=str(packet.equity),
        positions={k:str(v) for k,v in packet.positions.items()},open_client_ids=list(packet.open_client_ids),
        instruments=[i.model_dump(mode='json') for i in packet.instruments],
        context=packet.context.model_dump(mode='json'),evidence_id=packet.evidence_id,
        quote_received_at={k:v.isoformat() for k,v in packet.quote_received_at.items()},
        market_data_evidence=packet.market_data_evidence,host=packet.host,account_type=packet.account_type)


def decode_packet(value):
    from .real_trading import RealPacket
    return RealPacket(**{**value,'observed_at':datetime.fromisoformat(value['observed_at']),
        'settled':Decimal(value['settled']),'unsettled':Decimal(value['unsettled']),'equity':Decimal(value['equity']),
        'positions':{k:Decimal(v) for k,v in value['positions'].items()},'open_client_ids':tuple(value['open_client_ids']),
        'instruments':tuple(Instrument.model_validate(i) for i in value['instruments']),
        'context':AlphaContext.model_validate(value['context']),
        'quote_received_at':{k:datetime.fromisoformat(v) for k,v in value['quote_received_at'].items()}})


def validate_coverage(samples,created,label):
    if len(samples)!=2 or {s['asset_type'] for s in samples}!={'STOCK','ETF'}:
        raise ValueError('REAL_MARKET_COVERAGE_UNVERIFIED')
    for s in samples:
        if (s['session_label']!=label or s['exchange'] not in ('NAS','NMS','NSQ')
                or s['snapshot_http_status']!=200 or s['bars_http_status']!=200
                or s['delayed'] is not False or s['bars_real_time_required'] is not True
                or s['delay_evidence'] not in ('EXPLICIT_RESPONSE',
                    'DOCUMENTED_PRODUCTION_OPENAPI_SUBSCRIPTION_PLUS_CURRENT_EFFECTIVENESS')):
            raise ValueError('REAL_MARKET_COVERAGE_UNVERIFIED')
        for field,limit in (('quote_at',10),('quote_received_at',10),('bar_at',180)):
            at=datetime.fromisoformat(s[field])
            if not (at.tzinfo is not None and at<=created
                    and (limit is None or (created-at).total_seconds()<=limit)
                    and (field=='bar_at' or at>=reviewed_calendar().session(date.fromisoformat(label)).opens_at)):
                raise ValueError('REAL_MARKET_COVERAGE_STALE')


def certificate(authority,packet,state,samples,now,root):
    session=packet.validate(now,flat=not carried_positions_allowed(state,authority),entry=True)
    packet.validate_market_data_authority(now)
    proof=verify_flat_account(state,packet,authority,now)
    entitlement=session_entitlement(authority,now,root)
    if packet.market_data_evidence['entitlement_evidence_sha256']!=digest(entitlement):
        raise ValueError('REAL_ENTITLEMENT_UNVERIFIED')
    validate_coverage(samples,now,session.label.isoformat())
    return dict(schema='REAL_SESSION_CERTIFICATE_V1',authority_sha256=digest(authority),
        build_sha256=authority['build_sha256'],account_binding=authority['account_binding'],
        market_session_label=session.label.isoformat(),created_at=now.isoformat(),
        expires_at=min(session.closes_at,datetime.fromisoformat(authority['expires_at'])).isoformat(),
        flat_checkpoint_sha256=authority['flat_checkpoint_sha256'],entitlement_evidence=entitlement,
        coverage_samples=samples,packet=encode_packet(packet),ledger_proof=proof,
        broker_mutations=0,astra_calls=0)


def persist_certificate(root,value):
    """Append immutable evidence, then atomically publish its digest, never a new GO."""
    base=folder(root);name='daily-session-'+value['market_session_label']+'-'+digest(value)+'.json'
    with (base/name).open('x',encoding='utf-8') as out:
        json.dump(value,out,sort_keys=True,separators=(',',':'));out.flush();os.fsync(out.fileno())
    pointer={'file':name,'sha256':sha256((base/name).read_bytes()).hexdigest()}
    temporary=base/(POINTER_NAME+'.tmp')
    with temporary.open('w',encoding='utf-8') as out:
        json.dump(pointer,out);out.flush();os.fsync(out.fileno())
    os.replace(temporary,base/POINTER_NAME)
    return pointer


def current_certificate(root,authority,now,*,recovery=False):
    pointer=read_json(folder(root)/POINTER_NAME)
    name=pointer.get('file','')
    if Path(name).name!=name or not name.startswith('daily-session-') or not name.endswith('.json'):
        raise ValueError('REAL_DAILY_CERTIFICATE_INVALID')
    path=folder(root)/name;value=read_json(path)
    try:
        created=datetime.fromisoformat(value['created_at']);expires=datetime.fromisoformat(value['expires_at'])
        session=reviewed_calendar().session(date.fromisoformat(value['market_session_label']))
        if (sha256(path.read_bytes()).hexdigest()!=pointer['sha256']
                or value['schema']!='REAL_SESSION_CERTIFICATE_V1' or value['authority_sha256']!=digest(authority)
                or value['build_sha256']!=authority['build_sha256'] or value['account_binding']!=authority['account_binding']
                or value['flat_checkpoint_sha256']!=authority['flat_checkpoint_sha256']
                or session is None or not session.contains(created) or created>now
                or not created<expires<=min(session.closes_at,datetime.fromisoformat(authority['expires_at']))
                or (not recovery and (not session.contains(now) or now>=expires))
                or (recovery and now>created+timedelta(days=7))
                or value['broker_mutations']!=0 or value['astra_calls']!=0):
            raise ValueError('REAL_DAILY_CERTIFICATE_INVALID')
        packet=decode_packet(value['packet'])
        carried=bool(packet.positions) and authority['scope'].get('overnight') is True
        packet.validate(created,flat=not carried,entry=True)
        packet.validate_market_data_authority(created)
        entitlement=session_entitlement(authority,created,root)
        if (packet.account_binding!=authority['account_binding'] or value['entitlement_evidence']!=entitlement
                or packet.market_data_evidence['entitlement_evidence_sha256']!=digest(entitlement)):
            raise ValueError('REAL_ENTITLEMENT_UNVERIFIED')
        proof=value['ledger_proof']
        if (proof['positions']!=len(packet.positions) or proof['nonfinal_orders']!=0 or proof['settlement_known'] is not True
                or Decimal(proof['settled_usd'])!=packet.settled or Decimal(proof['unsettled_usd'])!=packet.unsettled
                or Decimal(proof['equity_usd'])!=packet.equity):raise ValueError('REAL_DAILY_CERTIFICATE_INVALID')
        validate_coverage(value['coverage_samples'],created,session.label.isoformat())
    except (KeyError,TypeError,OverflowError):raise ValueError('REAL_DAILY_CERTIFICATE_INVALID') from None
    return value,pointer


def daily_receipt(root,now,*,recovery=False):
    authority=load_authority(root,now)
    value,pointer=current_certificate(root,authority,now,recovery=recovery)
    return dict(verdict='GO',scope='REAL_CASH_500_V1',account_binding=authority['account_binding'],
        build_sha256=authority['build_sha256'],flat_checkpoint_sha256=authority['flat_checkpoint_sha256'],
        overnight_enabled=authority['scope'].get('overnight') is True,market_readiness_sha256=pointer['sha256'].upper(),
        market_session_label=value['market_session_label'],issued_at=value['created_at'],expires_at=value['expires_at'],
        recovery_scope='REAL_RECONCILE_REDUCE_ONLY_V1',
        recovery_not_after=min(datetime.fromisoformat(value['created_at'])+timedelta(days=7),
            datetime.fromisoformat(authority['expires_at'])).isoformat(),daily_authority_sha256=digest(authority))
