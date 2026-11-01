"""Observation-only service identity; never an execution approval receipt."""
from datetime import timedelta
from .recovery_reasons import FLAT_RECOVERABLE_REASONS
from .real_daily import load_authority, session_entitlement, digest
from .mvp.config import reviewed_calendar
from .reference import NEW_YORK

MARKET_UNAVAILABLE = frozenset({'STALE_REAL_QUOTE','STALE_REAL_CONTEXT',
    'REAL_MARKET_COVERAGE_STALE','REAL_COVERAGE_SNAPSHOT','INVALID_REAL_QUOTE',
    'NO_AFFORDABLE_REAL_CANDIDATES','NO_FRESH_REAL_PORTFOLIO_CONTEXT',
    'NO_ELIGIBLE_REAL_PORTFOLIO_CONTEXT',
    # First minutes after the open: no 1-minute bars yet, so current coverage cannot be proven. Waiting mode
    # grants no trading authority; it only retries until fresh bars exist.
    'REAL_MARKET_COVERAGE_UNVERIFIED'})

def market_unavailable(exc):
    return type(exc) is ValueError and str(exc) in MARKET_UNAVAILABLE

def service_receipt(root,now,*,recovery=False,gate_type=None):
    from .real_trading import RealGate
    # Validate current persistent authority first; never fall back from revoked GO.
    gate_type=gate_type or RealGate
    try:return gate_type.offline_receipt(root,now,recovery=recovery)
    except ValueError:
        authority=load_authority(root,now)
        session=reviewed_calendar().session(now.astimezone(NEW_YORK).date())
        if not session or not session.contains(now):raise
        return dict(verdict='OBSERVE_ONLY',scope='REAL_OBSERVATION_ONLY_V1',service_only=True,
            account_binding=authority['account_binding'],build_sha256=authority['build_sha256'],
            flat_checkpoint_sha256=authority['flat_checkpoint_sha256'],overnight_enabled=authority['scope'].get('overnight') is True,
            market_session_label=session.label.isoformat(),issued_at=session.opens_at.isoformat(),
            expires_at=session.closes_at.isoformat(),recovery_not_after=session.closes_at.isoformat(),
            daily_authority_sha256=digest(authority))

def clean_flat(state):
    return not (state['positions'] or state['reservations'] or state.get('incidents')
        or any(state['latches'].values())
        or any(not row['final'] or row.get('phase')=='UNKNOWN' for row in state['orders'].values()))

def recoverable_flat_incident(state):
    incidents=state.get('incidents',{})
    return bool(incidents and state['latches'].get('incident')
        and all(row.get('reason') in FLAT_RECOVERABLE_REASONS
            and row.get('pending_correction') is False for row in incidents.values())
        and not any(state['latches'].get(k,False) for k in ('daily','drawdown','terminated'))
        and not state['positions'] and not state['reservations']
        and not any(not row['final'] or row.get('phase')=='UNKNOWN' for row in state['orders'].values()))

def market_authority(root,receipt,now):
    authority=load_authority(root,now)
    if authority['build_sha256']!=receipt['build_sha256'] or authority['account_binding']!=receipt['account_binding']:
        raise ValueError('REAL_ACCOUNT_BINDING_MISMATCH')
    evidence=session_entitlement(authority,now,root)
    return dict(session_label=receipt['market_session_label'],entitlement_evidence_sha256=digest(evidence))


def resolution_baseline(state,checkpoint):
    """Cash an incident's flat account must be back at before the incident may clear.

    The flat checkpoint anchors the ledger's opening. Once the ledger has journaled real fills
    after that opening, the anchor is the ledger's own cash: it changes only through journaled
    broker executions, fees and settlements (a Reconcile never writes cash), so after any trade the
    old checkpoint figures are history, not a target. Before any fill the checkpoint still binds.
    Returns (settled, unsettled, equity) or None when no usable anchor exists (fail closed)."""
    from decimal import Decimal
    try:
        traded=any(row.get('final') and Decimal(str(row.get('filled_quantity','0')))>0
            for row in state['orders'].values())
        if traded:
            cash=state['cash']
            return (Decimal(cash['settled_usd']),Decimal(cash['unsettled_usd']),Decimal(cash['liquidation_equity_usd']))
        cash=checkpoint['cash']
        return (Decimal(str(cash['settled_usd'])),Decimal(str(cash['unsettled_usd'])),Decimal(str(cash['equity_usd'])))
    except (KeyError,TypeError,ValueError,ArithmeticError):return None
