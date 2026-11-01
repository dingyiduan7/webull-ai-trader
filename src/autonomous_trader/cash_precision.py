"""Bounded, explained reconciliation of broker cash against the ledger.

Incident 2026-10-15: the ledger books a sale's regulatory fee (here $0.01) out of
the proceeds; the broker's cash had not yet reflected that fee, and (after close)
had already moved the sale proceeds from unsettled to settled. Exact equality then
refused every check: post-close, clean restart, settlement detection and exits.

This module never widens a tolerance and never edits the ledger. It only answers one
question: "is the broker's cash exactly the ledger's cash plus a surplus that the ledger's
own recorded fees fully explain, in a bucket arrangement the ledger itself can explain?"
If so it returns broker figures reduced by that surplus (conservative: the surplus is not
spendable and is not profit) plus evidence. Anything else is returned unchanged so the
existing exact checks keep failing closed.

Rules (all required):
- the broker total exceeds the ledger total by d, 0 < d <= the ledger's cumulative recorded
  fees (broker below ledger is never adjusted);
- after removing d from one bucket, (settled, unsettled) equals the ledger's own figures or the
  ledger's figures after settling some subset of lots that are already due;
- no negative balance results.
"""
from datetime import timedelta
from decimal import Decimal
from itertools import combinations

from .durable.projection import settlement_observation_date

CENT = Decimal('0.01')
MAX_DUE_LOTS = 12


def fee_allowance(state):
    """Cumulative fees the ledger has recorded on orders: the most an unposted-fee surplus can be."""
    return sum((Decimal(str(order.get('fees_usd', '0'))) for order in state.get('orders', {}).values()), Decimal(0))


def _due_lot_amounts(state, now):
    try:
        due = settlement_observation_date(state, now).isoformat()
    except Exception:
        return None
    lots = [Decimal(str(lot['outstanding_usd'])) for lot in state.get('settlements', {}).values()
            if Decimal(str(lot['outstanding_usd'])) > 0 and lot['settlement_date'] <= due]
    return lots if len(lots) <= MAX_DUE_LOTS else None


def _consistent(settled, unsettled, ledger_settled, ledger_unsettled, lots):
    shift = ledger_settled - settled          # settled the ledger would have to gain/lose
    if shift == 0 and unsettled == ledger_unsettled:
        return True
    for size in range(1, len(lots) + 1):
        for subset in combinations(lots, size):
            x = sum(subset, Decimal(0))
            if settled == ledger_settled + x and unsettled == ledger_unsettled - x:
                return True
    return False


def adjust_cash(cash, state, now):
    """Return (cash, evidence). `cash` is the broker's cash mapping (decimal strings); never mutated."""
    try:
        settled, unsettled = Decimal(cash['settled_cash']), Decimal(cash['unsettled_cash'])
        total, equity = Decimal(cash['cash_balance']), Decimal(cash['net_liquidation_value'])
        ledger = state['cash']
        l_settled, l_unsettled = Decimal(ledger['settled_usd']), Decimal(ledger['unsettled_usd'])
    except Exception:
        return cash, None
    if settled + unsettled != total:
        return cash, None
    surplus = total - (l_settled + l_unsettled)
    allowance = fee_allowance(state)
    if surplus <= 0 or surplus > allowance:
        return cash, None
    lots = _due_lot_amounts(state, now)
    if lots is None:
        return cash, None
    for bucket in ('unsettled', 'settled'):
        s2 = settled - (surplus if bucket == 'settled' else 0)
        u2 = unsettled - (surplus if bucket == 'unsettled' else 0)
        if s2 < 0 or u2 < 0:
            continue
        if _consistent(s2, u2, l_settled, l_unsettled, lots):
            adjusted = dict(cash)
            adjusted['settled_cash'], adjusted['unsettled_cash'] = str(s2), str(u2)
            adjusted['cash_balance'] = str(total - surplus)
            adjusted['net_liquidation_value'] = str(equity - surplus)
            evidence = dict(kind='BROKER_CASH_SURPLUS_EXPLAINED_BY_LEDGER_FEES', variance_usd=str(surplus),
                            fee_allowance_usd=str(allowance), bucket=bucket,
                            broker=dict(settled=str(settled), unsettled=str(unsettled), total=str(total), equity=str(equity)),
                            ledger=dict(settled=str(l_settled), unsettled=str(l_unsettled)),
                            due_lots=[str(x) for x in lots])
            return adjusted, evidence
    return cash, None


def make_adjuster(controller, source, clock):
    """Closure for RealMarketSource.cash_adjuster; records the latest evidence on the source."""
    def adjuster(cash):
        adjusted, evidence = adjust_cash(cash, controller.state, clock())
        source.last_cash_variance = evidence
        return adjusted
    return adjuster
