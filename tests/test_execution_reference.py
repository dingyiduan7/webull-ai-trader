from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
import pytest
from autonomous_trader.execution import Order, OrderState as S, SimulatedBroker
from autonomous_trader.reference import CalendarUnknown, exit_due


def test_invalid_transition_and_terminal_requires_reconciliation(intent, event):
    order = Order(intent=intent())
    with pytest.raises(ValueError):
        order.transition(S.FILLED)
    order = order.transition(S.RESERVED).transition(S.SUBMITTING)
    with pytest.raises(ValueError):
        order.reconcile_terminal(cumulative_filled=D("0"), cumulative_fees_usd=D("0"))
    order = order.apply(event(order.intent, kind="CANCELED"))
    assert not order.final_reconciled
    assert order.reconcile_terminal(cumulative_filled=D("0"), cumulative_fees_usd=D("0")).final_reconciled


def test_timeout_duplicate_and_lagged_list(calendar, intent, now):
    broker = SimulatedBroker(calendar)
    order = intent()
    with pytest.raises(TimeoutError):
        broker.submit(order, now, accepted_timeout=True)
    broker.lagged_lists = True
    assert broker.list_orders() == ()
    assert broker.detail(order.client_order_id)[0] == order
    broker.submit(order, now)
    assert len(broker.orders) == 1
    with pytest.raises(ValueError):
        broker.submit(order.model_copy(update={"quantity": D("101")}), now)
    with pytest.raises(ValueError):
        broker.submit(order.model_copy(update={"client_order_id": "new-id"}), now)


def test_order_restart_roundtrip_and_out_of_order(intent, event):
    order = Order(intent=intent()).transition(S.RESERVED).transition(S.SUBMITTING)
    fill = event(order.intent, seq=2, kind="FILL", filled="40", price="100")
    order = order.apply(fill)
    restored = Order.model_validate_json(order.model_dump_json())
    assert restored == order
    assert restored.apply(fill) == restored
    stale_ack = event(order.intent, seq=1)
    uncertain = restored.apply(stale_ack)
    assert uncertain.state == S.UNKNOWN
    assert uncertain.cumulative_filled == 40


def test_fill_during_cancel_and_late_fill(intent, event):
    order = Order(intent=intent()).transition(S.RESERVED).transition(S.SUBMITTING)
    order = order.apply(event(order.intent)).transition(S.CANCEL_PENDING)
    order = order.apply(event(order.intent, seq=2, kind="FILL", filled="40", price="100"))
    assert order.state == S.CANCEL_PENDING
    order = order.apply(event(order.intent, seq=3, kind="CANCELED", filled="40"))
    order = order.apply(event(order.intent, seq=4, kind="FILL", filled="50", price="100"))
    assert order.state == S.UNKNOWN
    assert not order.final_reconciled


def test_regular_early_dst_and_holiday(calendar):
    regular = calendar.session(date(2026, 9, 11))
    assert regular.liquidation_at.hour == 19 and regular.liquidation_at.minute == 55
    assert not regular.permits_entry(regular.liquidation_at)
    assert regular.contains(regular.liquidation_at)
    assert not regular.contains(regular.closes_at)
    early = calendar.session(date(2026, 11, 27))
    assert early.closes_at.hour == 18 and early.liquidation_at.minute == 55
    assert early.liquidation_at.hour == 17
    assert calendar.session(date(2026, 3, 6)).opens_at.hour == 14
    assert calendar.session(date(2026, 3, 9)).opens_at.hour == 13
    assert calendar.session(date(2026, 11, 26)) is None
    assert calendar.session(date(2026, 9, 12)) is None
    with pytest.raises(CalendarUnknown):
        calendar.session(date(2027, 1, 4))


def test_exchange_and_settlement_holidays_are_independent(calendar):
    assert calendar.next_settlement(date(2026, 9, 4)) == date(2026, 9, 8)
    assert calendar.session(date(2026, 4, 3)) is None
    assert calendar.next_settlement(date(2026, 4, 2)) == date(2026, 4, 3)
    assert calendar.session(date(2026, 11, 11)) is not None
    assert calendar.next_settlement(date(2026, 11, 10)) == date(2026, 11, 12)


def test_hold_starts_first_fill(position, calendar, now):
    session = calendar.session(now.date())
    assert not exit_due(position, session, now + timedelta(seconds=599))
    assert exit_due(position, session, now + timedelta(seconds=600))


def test_simulated_session_halts_outage_and_cancel(calendar, intent, now):
    broker = SimulatedBroker(calendar)
    order = intent()
    broker.halted_instruments.add(order.instrument_id)
    with pytest.raises(ValueError, match="halted"):
        broker.submit(order, now)
    broker.halted_instruments.clear()
    with pytest.raises(ValueError):
        broker.submit(order, now.replace(hour=21))
    broker.submit(order, now)
    assert broker.cancel(order.client_order_id) == S.CANCEL_PENDING
    broker.outage = True
    with pytest.raises(TimeoutError):
        broker.detail(order.client_order_id)


def test_duplicate_event_collision_and_overfill(intent, event):
    order = Order(intent=intent()).transition(S.RESERVED).transition(S.SUBMITTING)
    first = event(order.intent)
    order = order.apply(first)
    with pytest.raises(ValueError, match="conflicting"):
        order.apply(first.model_copy(update={"fees_usd": D("1")}))
    overfill = event(order.intent, seq=2, kind="FILL", filled="101", price="100")
    assert order.apply(overfill).state == S.UNKNOWN


def test_late_ack_preserves_partial_fill_state(intent, event):
    order = Order(intent=intent()).transition(S.RESERVED).transition(S.SUBMITTING)
    order = order.apply(event(order.intent, kind="FILL", filled="40", price="100"))
    order = order.apply(event(order.intent, seq=2, filled="40"))
    assert order.state == S.PARTIALLY_FILLED
    assert order.cumulative_filled == 40


@pytest.mark.parametrize("field,value", [("environment", "LIVE"), ("side", "SHORT"), ("session", "EXTENDED"), ("order_type", "MARKET")])
def test_intents_cannot_express_forbidden_authority(intent, field, value):
    order = intent()
    with pytest.raises(ValueError):
        type(order)(**{**order.model_dump(), field: value})
