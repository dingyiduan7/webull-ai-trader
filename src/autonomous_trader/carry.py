"""Multi-day holding (carry) policy — one place for every overnight decision.

Owner decisions 2026-10-14/15: the system is not a day-trading machine. A position may be held from
minutes to days; the decision model chooses each position's selling point and may keep a well-entered
position for a larger move. Carry is enabled only when the owner-signed charter scope says
``overnight: True`` (``real_daily.scope``), so code alone can never switch it on.

Limits that stay in force overnight:
- every carried position keeps its local monitored stop, re-checked from the first quote of the next
  session (there is no broker-resident stop, so an overnight gap can fill below the stop);
- a plan may not extend beyond ``MAX_HOLD_SESSIONS`` sessions after the current one; the model may
  renew the deadline on review, always bounded by the same horizon;
- the ledger enforces the same horizon (``durable.projection`` EXIT_DEADLINE with ``carry_sessions``);
- a carried account is reconciled exactly (cash and every position quantity) after the close and
  before the next open; any mismatch is an incident and blocks new entries, never a forced sale.
"""
from datetime import datetime, time, timedelta
from decimal import Decimal

from .mvp.config import reviewed_calendar
from .reference import NEW_YORK, CalendarUnknown

MAX_HOLD_SESSIONS = 60   # owner-approved 2026-10-20 (was 10): momentum holds need weeks
# Owner decision 2026-10-16: hold overnight only positions with high upside. A plan may outlive today's
# session only with a target at least this far above entry and at least this reward-to-risk to its stop.
CARRY_MIN_UPSIDE = Decimal('0.05')
CARRY_MIN_REWARD_RISK = Decimal('2')


def carry_enabled():
    from .real_daily import scope
    return scope().get('overnight') is True


def future_sessions(label, count):
    """The next `count` regular sessions after `label` (stops early at the end of calendar coverage)."""
    calendar, out, day = reviewed_calendar(), [], label
    while len(out) < count:
        day = day + timedelta(days=1)
        try:
            session = calendar.session(day)
        except CalendarUnknown:
            break
        if session is not None:
            out.append(session)
    return out


def exit_horizon(session, sessions=MAX_HOLD_SESSIONS):
    """Latest permitted exit deadline for a plan created or reviewed in `session`."""
    if not carry_enabled():
        return session.liquidation_at
    later = future_sessions(session.label, sessions)
    return later[-1].liquidation_at if later else session.liquidation_at


def ledger_horizon(label_iso, settlement_days, sessions):
    """Ledger-side bound (no calendar object in the reducer): 16:00 New York on the `sessions`-th
    settlement day after the current session label. Settlement days are a subset of trading days, so
    this bound is never shorter than the calendar horizon by more than bank-only holidays."""
    later = [d for d in settlement_days if d > label_iso]
    if not sessions or not later:
        return None
    day = later[min(sessions, len(later)) - 1]
    return datetime.combine(datetime.fromisoformat(day).date(), time(16), NEW_YORK)


def plan_carries(plan, session):
    """True when this plan is authorized to stay open past this session's liquidation time."""
    # Strictly after today's close: a deadline between liquidation and the close is a same-day plan.
    return bool(session and carry_enabled() and plan.latest_exit_at > session.closes_at)


def broker_positions_by_instrument(account, state):
    """Broker position rows (symbol + quantity) mapped to ledger instrument ids; unknown symbols fail."""
    from decimal import Decimal
    by_ticker = {row['ticker']: iid for iid, row in state['instruments'].items()}
    out = {}
    for row in account.get('positions') or ():
        quantity = Decimal(str(row['quantity']))
        if quantity == 0:
            continue
        iid = row.get('instrument_id') or by_ticker.get(row.get('symbol'))
        if iid is None or str(iid) not in state['instruments'] or str(iid) in out:
            raise ValueError('REAL_CARRY_POSITION_IDENTITY')
        out[str(iid)] = quantity
    return out


def carry_account_values(account, state, account_id, now):
    """Exact account reconciliation with carried positions (no quote/order authority).

    Cash buckets must equal the ledger exactly; every position quantity must equal the ledger; no
    open orders, reservations or unknown orders may exist. Equity is not compared (marks move)."""
    from decimal import Decimal
    if account.get('account_binding') != account_id:
        raise ValueError('REAL_ACCOUNT_BINDING_MISMATCH')
    observed, received = account.get('observed_at'), account.get('received_at')
    if (not isinstance(observed, datetime) or not isinstance(received, datetime)
            or observed.tzinfo is None or received.tzinfo is None or not observed <= received <= now
            or not 0 <= (now - observed).total_seconds() <= 10):
        raise ValueError('STALE_REAL_ACCOUNT')
    if (state['reservations'] or account.get('open_client_ids')
            or any(not row['final'] or row.get('phase') == 'UNKNOWN' for row in state['orders'].values())):
        raise ValueError('REAL_CARRY_RECONCILIATION_NOT_QUIET')
    positions = broker_positions_by_instrument(account, state)
    local = {iid: Decimal(row['quantity']) for iid, row in state['positions'].items()}
    if positions != local:
        raise ValueError('REAL_CARRY_POSITION_MISMATCH')
    cash = account['cash']
    settled, unsettled = Decimal(cash['settled_cash']), Decimal(cash['unsettled_cash'])
    total = Decimal(cash['cash_balance'])
    if (not all(v.is_finite() and v >= 0 for v in (settled, unsettled, total)) or settled + unsettled != total):
        raise ValueError('REAL_CASH_INVALID')
    if settled != Decimal(state['cash']['settled_usd']) or unsettled != Decimal(state['cash']['unsettled_usd']):
        raise ValueError('REAL_CARRY_CASH_MISMATCH')
    return settled, unsettled, positions


def clean_carry(state):
    """Quiet, reconciled account that may hold positions overnight: no incidents, no open/unknown
    orders, no reservations, no risk-halting latch. (The daily latch resets at the next session.)"""
    return not (state['reservations'] or state.get('incidents')
                or any(state['latches'].get(k) for k in ('drawdown', 'terminated', 'incident'))
                or any(not row['final'] or row.get('phase') == 'UNKNOWN' for row in state['orders'].values()))


def snap_exit(latest, session):
    """Clamp a plan deadline into a regular session's [open, liquidation] window. A deadline after one
    session's liquidation but before the next open (e.g. 16:00 or overnight) becomes that session's
    liquidation time, so every deadline fires while stops are watched and orders can execute."""
    if session is None or latest <= session.liquidation_at:
        return latest
    best = session.liquidation_at
    for later in future_sessions(session.label, MAX_HOLD_SESSIONS + 1):
        if latest < later.opens_at:
            break
        best = min(latest, later.liquidation_at)
    return best


def qualifies_for_carry(entry_usd, stop_usd, target_usd):
    """High-upside test for holding past today's session (owner decision 2026-10-16)."""
    if target_usd is None or entry_usd is None or entry_usd <= 0:
        return False
    upside = (target_usd - entry_usd) / entry_usd
    risk = entry_usd - stop_usd if stop_usd is not None else Decimal(0)
    return upside >= CARRY_MIN_UPSIDE and (risk <= 0 or (target_usd - entry_usd) / risk >= CARRY_MIN_REWARD_RISK)
