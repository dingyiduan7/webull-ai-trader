"""Broker-truth cash (owner decision 2026-10-18: "use only broker's truth as the true balance").

Webull's settled and unsettled cash are the account's true balance. Before any comparison of broker cash with
the ledger, the ledger is brought to the broker's figures:

1. due sale lots the broker no longer shows as unsettled are confirmed as settled, one lot at a time, oldest
   settlement date first (ordinary SETTLE events, so lot bookkeeping stays exact and nothing stays stuck);
2. any remaining difference is journaled as one CASH_ADOPT event (logged, never hidden). The ledger treats it
   as a cash flow, not performance: P&L is untouched and the risk reference points (day start, high-water
   marks) move with it, so an adoption never trips a loss, drawdown or termination latch by itself.

Only when the broker evidence is valid (bound to this account, fresh, finite, internally consistent) and the
account is quiet on BOTH sides: no open/unknown ledger orders or reservations, no broker open orders, and broker
positions exactly equal to ledger positions. Positions are never adopted: a position mismatch remains an
incident. Never places, cancels or modifies orders.
"""
from datetime import date, datetime
from decimal import Decimal

from .durable.projection import settlement_observation_date

ZERO = Decimal(0)


def quiet(state):
    return not state.get('reservations') and all(
        o['final'] and o.get('phase') != 'UNKNOWN' for o in state.get('orders', {}).values())


def _ledger_positions(state):
    return {iid: Decimal(str(row['quantity'])) for iid, row in state.get('positions', {}).items()
            if Decimal(str(row['quantity'])) != 0}


def eligible(state, broker_positions, broker_open_ids):
    """Quiet on both sides and broker positions identical to the ledger's (by instrument id)."""
    if broker_positions is None or broker_open_ids is None:
        return False
    observed = {str(k): Decimal(str(v)) for k, v in dict(broker_positions).items() if Decimal(str(v)) != 0}
    return quiet(state) and not tuple(broker_open_ids) and observed == _ledger_positions(state)


def plan(state, broker_settled, broker_unsettled, now, *, broker_positions=None, broker_open_ids=None):
    """(due_lot_keys_to_confirm, settled_delta, unsettled_delta) bringing the ledger to the broker, or None when
    not eligible. Deltas are broker minus ledger after the confirmations."""
    if not eligible(state, broker_positions, broker_open_ids):
        return None
    s, u = Decimal(state['cash']['settled_usd']), Decimal(state['cash']['unsettled_usd'])
    bs, bu = Decimal(str(broker_settled)), Decimal(str(broker_unsettled))
    if not (bs.is_finite() and bu.is_finite()) or bs < 0 or bu < 0:
        return None
    if bs == s and bu == u:
        return [], ZERO, ZERO
    outstanding = [(v['settlement_date'], k, Decimal(v['outstanding_usd']))
                   for k, v in state.get('settlements', {}).items() if Decimal(v['outstanding_usd']) > 0]
    receivables = sum((x for _, _, x in outstanding), ZERO)
    adopted_unsettled = u - receivables          # unsettled carried by earlier adoptions
    try:
        due = settlement_observation_date(state, now).isoformat()
        days = set(state['config']['settlement_days'])
    except (KeyError, TypeError, ValueError):
        due, days = None, set()
    confirm, remaining = [], receivables
    for day, key, amount in sorted(outstanding):
        if due is None or day > due or day not in days:
            break
        # Confirm (oldest first) while the broker's unsettled is below what would remain with this lot
        # still outstanding: the broker no longer holds it; any residual cent is adopted, never left stuck.
        if bu - adopted_unsettled < remaining:
            confirm.append(key)
            remaining -= amount
        else:
            break
    confirmed = receivables - remaining
    return confirm, bs - (s + confirmed), bu - (u - confirmed)


def valid_observation(account, account_id, now, max_age_seconds=10):
    """The broker account read is bound to this account, fresh and internally consistent."""
    try:
        if account.get('account_binding') != account_id:
            return False
        observed, received = account.get('observed_at'), account.get('received_at', account.get('observed_at'))
        if (not isinstance(observed, datetime) or not isinstance(received, datetime)
                or not observed <= received <= now or not 0 <= (now - observed).total_seconds() <= max_age_seconds):
            return False
        cash = account['cash']
        settled, unsettled = Decimal(cash['settled_cash']), Decimal(cash['unsettled_cash'])
        total = Decimal(cash.get('cash_balance', settled + unsettled))
        return (all(v.is_finite() and v >= 0 for v in (settled, unsettled, total))
                and settled + unsettled == total)
    except Exception:
        return False


def apply(controller, broker_settled, broker_unsettled, evidence_id, *, broker_positions, broker_open_ids, emit=None):
    """Journal settlements and the adoption so the ledger equals the broker. Returns evidence or None."""
    from .durable.models import CashAdoption, ConfirmSettlement
    state = controller.state
    if (Decimal(str(broker_settled)) == Decimal(state['cash']['settled_usd'])
            and Decimal(str(broker_unsettled)) == Decimal(state['cash']['unsettled_usd'])):
        return None                        # already equal: nothing to journal
    planned = plan(state, broker_settled, broker_unsettled, controller.clock(),
                   broker_positions=broker_positions, broker_open_ids=broker_open_ids)
    if planned is None:
        return None
    confirm, ds, du = planned
    if not confirm and ds == 0 and du == 0:
        return None
    before = dict(settled=state['cash']['settled_usd'], unsettled=state['cash']['unsettled_usd'])
    controller.lease()
    for key in confirm:
        lot = controller.state['settlements'][key]
        controller.send(ConfirmSettlement(execution_id=key, amount_usd=lot['amount_usd'],
                                          effective_on=date.fromisoformat(lot['settlement_date']), evidence_id=evidence_id))
    if ds != 0 or du != 0:
        controller.send(CashAdoption(settled_delta_usd=ds, unsettled_delta_usd=du,
                                     broker_settled_usd=Decimal(str(broker_settled)),
                                     broker_unsettled_usd=Decimal(str(broker_unsettled)), evidence_id=evidence_id))
    evidence = dict(ledger_before=before, broker=dict(settled=str(broker_settled), unsettled=str(broker_unsettled)),
                    settlements_confirmed=list(confirm), adopted_settled_delta=str(ds), adopted_unsettled_delta=str(du),
                    evidence_id=evidence_id)
    if emit:
        try:
            emit('REAL_BROKER_TRUTH_CASH', evidence)
        except Exception:
            pass
    return evidence
