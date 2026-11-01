"""Order reducer and deterministic simulated broker. No external adapters."""
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from .contracts import Record, OrderIntent, BrokerEvent, Nonnegative, UTC, utc
from .numeric import ZERO, add


class OrderState(StrEnum):
    DRAFT = "DRAFT"
    RESERVED = "RESERVED"
    SUBMITTING = "SUBMITTING"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    WORKING = "WORKING"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    REPLACE_PENDING = "REPLACE_PENDING"
    UNKNOWN = "UNKNOWN"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


TERMINAL = frozenset({OrderState.FILLED, OrderState.CANCELED, OrderState.REJECTED, OrderState.EXPIRED})
ACTIVE = frozenset({OrderState.ACKNOWLEDGED, OrderState.WORKING, OrderState.PARTIALLY_FILLED,
                    OrderState.CANCEL_PENDING, OrderState.REPLACE_PENDING, OrderState.UNKNOWN})
TRANSITIONS = {
    OrderState.DRAFT: {OrderState.RESERVED},
    OrderState.RESERVED: {OrderState.SUBMITTING},
    OrderState.SUBMITTING: {OrderState.ACKNOWLEDGED, OrderState.UNKNOWN, OrderState.REJECTED},
    **{s: set(ACTIVE) | set(TERMINAL) for s in ACTIVE},
}


class Order(Record):
    intent: OrderIntent
    state: OrderState = OrderState.DRAFT
    cumulative_filled: Nonnegative = ZERO
    cumulative_fees_usd: Nonnegative = ZERO
    first_fill_at: UTC | None = None
    last_sequence: int = -1
    events: tuple[BrokerEvent, ...] = ()
    final_reconciled: bool = False

    def evolve(self, **updates):
        return type(self).model_validate({**self.model_dump(), **updates})

    def transition(self, state: OrderState):
        if state not in TRANSITIONS.get(self.state, set()):
            raise ValueError(f"invalid transition {self.state} -> {state}")
        if state == OrderState.FILLED and self.cumulative_filled != self.intent.quantity:
            raise ValueError("cannot mark unfilled order FILLED")
        return self.evolve(state=state)

    def apply(self, event: BrokerEvent):
        if event.client_order_id != self.intent.client_order_id:
            raise ValueError("event order binding mismatch")
        prior = next((x for x in self.events if x.event_id == event.event_id), None)
        if prior is not None:
            if prior != event:
                raise ValueError("conflicting duplicate event")
            return self
        if self.state in {OrderState.DRAFT, OrderState.RESERVED}:
            raise ValueError("broker event before submission")
        events = (*self.events, event)
        if event.sequence <= self.last_sequence or event.kind == "CORRECTION":
            return self.evolve(state=OrderState.UNKNOWN, events=events, final_reconciled=False)
        filled = event.cumulative_filled_quantity
        if filled is None:
            filled = self.cumulative_filled
        if not self.cumulative_filled <= filled <= self.intent.quantity:
            return self.evolve(state=OrderState.UNKNOWN, events=events, final_reconciled=False)
        if ((filled > self.cumulative_filled and event.fill_price_usd is None)
                or (event.kind == "FILL" and filled == 0)):
            return self.evolve(state=OrderState.UNKNOWN, events=events, final_reconciled=False)
        if event.leaves_quantity is not None and add(filled, event.leaves_quantity) > self.intent.quantity:
            raise ValueError("broker leaves exceeds order size")
        if event.fees_usd < self.cumulative_fees_usd:
            return self.evolve(state=OrderState.UNKNOWN, events=events, final_reconciled=False)
        states = {"ACK": OrderState.ACKNOWLEDGED, "UNKNOWN": OrderState.UNKNOWN,
                  "CANCELED": OrderState.CANCELED, "REJECTED": OrderState.REJECTED, "EXPIRED": OrderState.EXPIRED}
        state = states.get(event.kind, OrderState.PARTIALLY_FILLED)
        if filled == self.intent.quantity:
            state = OrderState.FILLED
        elif event.kind == "FILL" and self.state in {OrderState.CANCEL_PENDING, OrderState.REPLACE_PENDING}:
            state = self.state
        # An ACK never undoes an outstanding cancel/replace, nor proves finality.
        elif event.kind == "ACK" and self.state in {OrderState.CANCEL_PENDING, OrderState.REPLACE_PENDING,
                                                   OrderState.PARTIALLY_FILLED, OrderState.WORKING}:
            state = self.state
        if self.state in TERMINAL and state not in TERMINAL:
            state = OrderState.UNKNOWN
        if self.final_reconciled:
            return self.evolve(state=OrderState.UNKNOWN, events=events, final_reconciled=False)
        return self.evolve(state=state, cumulative_filled=filled, cumulative_fees_usd=event.fees_usd,
                           first_fill_at=self.first_fill_at or (event.received_at if filled > 0 else None),
                           last_sequence=event.sequence, events=events, final_reconciled=False)

    def reconcile_terminal(self, *, cumulative_filled: Decimal, cumulative_fees_usd: Decimal):
        if self.state not in TERMINAL or cumulative_filled != self.cumulative_filled or cumulative_fees_usd != self.cumulative_fees_usd:
            raise ValueError("terminal detail and complete fill reconciliation required")
        return self.evolve(final_reconciled=True)


class SimulatedBroker:
    """Script-driven fills. Limit touches do not automatically fill.

    The test fixture retains orders across simulated client restarts. It does
    not assert any real broker's idempotency or event replay guarantees.
    """
    def __init__(self, calendar):
        self.calendar = calendar
        self.orders: dict[str, OrderIntent] = {}
        self.events: dict[str, list[BrokerEvent]] = {}
        self.outage = False
        self.lagged_lists = False
        self.halted_instruments: set[str] = set()

    def submit(self, intent: OrderIntent, now: datetime, *, accepted_timeout=False):
        if self.outage:
            raise TimeoutError("simulated outage: acceptance unknown to caller")
        now = utc(now)
        from .reference import NEW_YORK
        session = self.calendar.session(now.astimezone(NEW_YORK).date())
        if not session or not session.contains(now) or now >= intent.deadline:
            raise ValueError("outside session or expired intent")
        if intent.side == "BUY" and not session.permits_entry(now):
            raise ValueError("scheduled liquidation blocks entry")
        if intent.instrument_id in self.halted_instruments:
            raise ValueError("instrument halted")
        old = self.orders.get(intent.client_order_id)
        if old is not None and old != intent:
            raise ValueError("client ID reused with changed intent")
        if any(x.intent_id == intent.intent_id and x != intent for x in self.orders.values()):
            raise ValueError("economic intent reused with another client ID")
        self.orders[intent.client_order_id] = intent
        self.events.setdefault(intent.client_order_id, [])
        if accepted_timeout:
            raise TimeoutError("accepted but acknowledgement lost")
        return "sim-" + intent.client_order_id

    def detail(self, client_order_id: str):
        if self.outage:
            raise TimeoutError("detail unavailable")
        return self.orders.get(client_order_id), tuple(self.events.get(client_order_id, ()))

    def list_orders(self):
        if self.outage:
            raise TimeoutError("list unavailable")
        return () if self.lagged_lists else tuple(self.orders.values())

    def record(self, event: BrokerEvent):
        if event.client_order_id not in self.orders:
            raise ValueError("unknown client order")
        self.events[event.client_order_id].append(event)

    def cancel(self, client_order_id: str):
        # Requests are allowed after hours; they do not fabricate finality.
        if self.outage:
            raise TimeoutError("cancel uncertain")
        if client_order_id not in self.orders:
            raise ValueError("unknown order")
        return OrderState.CANCEL_PENDING
