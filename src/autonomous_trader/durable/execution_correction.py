"""Build an append-only execution correction from exact broker cash evidence."""
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

from .models import CorrectExecutions, CorrectedExecution, FinalizeOrder


@dataclass(frozen=True)
class SaleCashEvidence:
    execution_id: str
    before_evidence_id: str
    after_evidence_id: str
    settled_before_usd: Decimal
    settled_after_usd: Decimal
    unsettled_before_usd: Decimal
    unsettled_after_usd: Decimal


def correction_from_sale_cash(state,evidence):
    """Return a correction only when the supplied cash proof is exact and unique."""
    if not isinstance(evidence,SaleCashEvidence):raise ValueError('BROKER_CASH_EVIDENCE_REQUIRED')
    execution=state['executions'].get(evidence.execution_id)
    if execution is None:raise ValueError('EXECUTION_EVIDENCE_MISSING')
    order=state['orders'].get(execution['intent_id'])
    if (order is None or order['intent']['side']!='SELL_OWNED' or not order['final']
            or order['phase']!='FILLED'):
        raise ValueError('EXECUTION_EVIDENCE_INCONSISTENT')
    if (evidence.settled_before_usd!=evidence.settled_after_usd
            or Decimal(state['cash']['settled_usd'])!=evidence.settled_after_usd):
        raise ValueError('BROKER_CASH_EVIDENCE_INCONSISTENT')
    quantity=Decimal(execution['quantity']);fee=Decimal(execution['fee_usd'])
    proceeds=evidence.unsettled_after_usd-evidence.unsettled_before_usd
    if quantity<=0 or proceeds<0:raise ValueError('BROKER_CASH_EVIDENCE_INCONSISTENT')
    exact_price=(proceeds+fee)/quantity
    reported_price=Decimal(execution['price_usd'])
    increment=Decimal(state['instruments'][order['intent']['instrument_id']]['price_increment_usd'])
    if (exact_price<=0 or exact_price.quantize(increment,rounding=ROUND_HALF_UP)!=reported_price
            or exact_price<Decimal(order['intent']['limit_price_usd'])):
        raise ValueError('BROKER_CASH_EVIDENCE_INCONSISTENT')
    lot=state['settlements'].get(evidence.execution_id)
    rounded_proceeds=quantity*reported_price-fee
    if (lot is None or Decimal(lot['amount_usd'])!=rounded_proceeds
            or Decimal(state['cash']['unsettled_usd'])-rounded_proceeds!=evidence.unsettled_before_usd
            or Decimal(state['cash']['unsettled_usd'])-evidence.unsettled_after_usd!=rounded_proceeds-proceeds):
        raise ValueError('BROKER_CASH_EVIDENCE_INCONSISTENT')
    execution_ids=tuple(key for key,value in state['executions'].items()
        if value['intent_id']==execution['intent_id'])
    total_quantity=sum((Decimal(state['executions'][key]['quantity']) for key in execution_ids),Decimal(0))
    total_fees=sum((Decimal(state['executions'][key]['fee_usd']) for key in execution_ids),Decimal(0))
    return CorrectExecutions(changes=(CorrectedExecution(execution_id=evidence.execution_id,
        expected_revision=execution['revision'],quantity=execution['quantity'],price_usd=exact_price,
        fee_usd=execution['fee_usd']),),evidence_id=evidence.after_evidence_id,
        terminal_details=(FinalizeOrder(intent_id=execution['intent_id'],status='FILLED',
            execution_ids=execution_ids,total_quantity=total_quantity,total_fees_usd=total_fees,
            evidence_id=evidence.after_evidence_id),))
