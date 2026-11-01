"""Prepare the existing portfolio decision contract from REAL evidence only.

No provider or order client is imported. This is the disabled real composition
boundary, not a second strategy or risk controller.
"""
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
import json

from .contracts import AlphaContext, AlphaInstrumentContext
from .risk import POLICY


@dataclass(frozen=True)
class RealCandidate:
    context: AlphaInstrumentContext
    session_label: str
    received_at: object
    host: str
    instrument_certified: bool
    minimum_order_quantity: Decimal
    entry_cost_allowance_usd: Decimal | None


def prepare_portfolio_context(ledger, account_binding, candidates, session, now, *, recent_outcomes=()):
    """One current portfolio packet, <=8 instruments, zero per-candidate calls.

The supplied quality/financial evidence is retained for Astra to evaluate.
No market-cap, low-price, indicator or profit-guarantee filter is applied.
"""
    state = ledger.snapshot()
    if not state or state['environment'] != 'REAL_READ_ONLY' or state['account_binding'] != account_binding:
        raise ValueError('REAL_ACCOUNT_BINDING_MISMATCH')
    from datetime import datetime
    observed = datetime.fromisoformat(state['observed_at'])
    if not 0 <= (now-observed).total_seconds() <= 30 or not state['observation_reconciled']:
        raise ValueError('REAL_ACCOUNT_NOT_RECONCILED')
    if session is None or not session.permits_entry(now):
        raise ValueError('REGULAR_SESSION_REQUIRED')
    settled = Decimal(state['cash']['settled_usd'])
    equity = Decimal(state['cash']['equity_usd'])
    budget = min(Decimal('500'), settled)
    stock_budget = min(budget, equity*POLICY.stock_exposure)
    retained, excluded, seen = [], [], set()
    for candidate in candidates:
        item = AlphaInstrumentContext.model_validate(candidate.context.model_dump())
        reason = None
        if candidate.host != 'api.webull.com':
            raise ValueError('REAL_MARKET_PROVENANCE_REQUIRED')
        if item.instrument_id in seen or item.symbol in seen: raise ValueError('DUPLICATE_REAL_CANDIDATE')
        seen.update((item.instrument_id, item.symbol))
        if candidate.session_label != session.label.isoformat(): reason = 'PRIOR_SESSION_CANDIDATE'
        elif item.symbol in state['external_quantities']: reason = 'EXTERNAL_HOLDING_NO_AUTHORITY'
        elif not candidate.instrument_certified: reason = 'REAL_INSTRUMENT_CAPABILITY_UNVERIFIED'
        elif not (session.opens_at <= item.quote_at <= now and
                  observed <= candidate.received_at <= now): reason = 'STALE_REAL_QUOTE'
        elif item.bid_usd > item.ask_usd: reason = 'INVALID_QUOTE'
        elif (not candidate.minimum_order_quantity.is_finite() or candidate.minimum_order_quantity <= 0 or
              candidate.minimum_order_quantity % item.quantity_increment != 0): reason = 'INVALID_MINIMUM_QUANTITY'
        elif (candidate.entry_cost_allowance_usd is None or not candidate.entry_cost_allowance_usd.is_finite()
              or candidate.entry_cost_allowance_usd < 0): reason = 'REAL_ORDER_COST_UNBOUNDED'
        elif item.ask_usd*candidate.minimum_order_quantity + candidate.entry_cost_allowance_usd > stock_budget:
            reason = 'MINIMUM_ORDER_UNAFFORDABLE'
        if reason:
            excluded.append({'symbol': item.symbol, 'reason': reason})
            continue
        gaps = [name for name in ('daily_research', 'sector_context', 'volume', 'relative_volume_10d',
                                 'bid_size', 'ask_size') if getattr(item, name) is None]
        if not item.bars: gaps.append('recent_bars')
        # Existing financial/research strings remain intact. Missing evidence
        # is represented explicitly rather than turning it into a quality score.
        metadata = {'market_provenance': 'REAL_HTTP', 'host': candidate.host,
            'received_at': candidate.received_at.isoformat(), 'evidence_gaps': gaps,
            'source_market_context': item.market_context,
            'affordable_notional_ceiling_usd': str(stock_budget-candidate.entry_cost_allowance_usd),
            'minimum_order_quantity': str(candidate.minimum_order_quantity),
            'entry_cost_allowance_usd': str(candidate.entry_cost_allowance_usd),
            'earnings_evidence': 'See financial context when supplied; otherwise unavailable'}
        encoded = json.dumps(metadata, separators=(',', ':'))
        retained.append(AlphaInstrumentContext.model_validate({**item.model_dump(), 'market_context': encoded}))
    if not retained: raise ValueError('NO_ELIGIBLE_REAL_PORTFOLIO_CONTEXT')
    from .real_triggers import MAX_SHORTLIST
    excluded.extend({'symbol': item.symbol, 'reason': 'SHORTLIST_CAPACITY'} for item in retained[MAX_SHORTLIST:])
    retained = retained[:MAX_SHORTLIST]
    context = AlphaContext(as_of=now, expires_at=now+timedelta(seconds=30),
        session_open=session.opens_at, session_close=session.closes_at, liquidation_at=session.liquidation_at,
        instruments=tuple(retained), positions=(), settled_cash_usd=budget,
        remaining_risk_usd='0', reconciliation_ready=False, settlement_known=True,
        recent_outcomes=recent_outcomes, execution_cost_evidence=json.dumps({
            'environment': 'REAL_READ_ONLY', 'equity_usd': str(equity),
            'broker_fees': 'Per-candidate allowances are bounds, not a certified production fee schedule',
            'model_cost': 'Decision frequency is separate from trade counts. Retain atomic cost accounting/reservations, rolling spend-rate controls and meaningful-context deduplication; monthly dollar cap is temporarily suspended.',
            'authority': 'PRODUCTION_DISABLED; preparation only; no risk permit'}, separators=(',', ':')))
    return context, tuple(excluded)
