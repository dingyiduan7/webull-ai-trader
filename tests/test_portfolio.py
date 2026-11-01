from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from decimal import Decimal as D
import pytest
from hypothesis import given, settings, HealthCheck, strategies as st
from autonomous_trader.execution import OrderState as S
from autonomous_trader.portfolio import (AdmissionError, OfflineAccount, Episode, Reservation,
                                         invariant_violations, PortfolioView)
from autonomous_trader.risk import Reason, position_risk
from autonomous_trader.numeric import add


def buy(account, intent, event, *, quantity="100", fees="0", fee_allowance="0", key="buy-1", price="100"):
    order = intent(key=key, quantity=quantity, price=price)
    account.reserve(order, expected_version=account.version, fee_allowance_usd=D(fee_allowance))
    account.transition(order.intent_id, S.SUBMITTING)
    account.apply(order.intent_id, event(order, kind="FILL", filled=quantity, price=price, fees=fees))
    return order


def test_concurrent_buys_recheck_version(account, intent):
    orders = [intent(key="buy-a", symbol="a", quantity="2000"), intent(key="buy-b", symbol="b", quantity="2000")]
    account.settled_usd = D("300000")
    def reserve(order):
        try:
            account.reserve(order, expected_version=0)
            return True
        except ValueError:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(reserve, orders)) == 1
    other = next(x for x in orders if x.intent_id not in account.orders)
    with pytest.raises(AdmissionError, match="INSUFFICIENT_SETTLED_CASH"):
        account.reserve(other, expected_version=account.version)
    assert account.available_settled_usd == D("100000")


def test_five_stocks_include_pending_orders(account, intent):
    for i in range(5):
        order = intent(key=str(i), symbol=str(i), quantity="100")
        account.reserve(order, expected_version=account.version)
    with pytest.raises(AdmissionError, match="POSITION_LIMIT"):
        account.reserve(intent(key="six", symbol="six"), expected_version=account.version)


def test_full_account_exposure_is_allowed_within_all_limits(account, intent):
    for i in range(5):
        account.reserve(intent(key=str(i), symbol=str(i), quantity="2000", stop="99"), expected_version=account.version)
    assert account.available_settled_usd == 0
    assert add(*(r.exposure_usd for r in account.reservations.values())) == D("1000000")
    assert invariant_violations(account.view()) == ()


def test_price_drift_detected_and_sell_still_allowed(account, intent, event):
    order = buy(account, intent, event, quantity="3000")
    p = account.positions[order.episode_id]
    account.positions[order.episode_id] = type(p)(**{**p.model_dump(), "mark_usd": D("140"), "protected_quantity": p.quantity})
    issues = invariant_violations(account.view())
    assert Reason.EXPOSURE_LIMIT in {v.code for v in issues}
    account.new_risk_enabled = False
    account.reserve(intent(key="reduce", side="SELL_OWNED", quantity="2000"), expected_version=account.version)


def test_aggregate_stock_exposure_and_no_episode_splitting(account, intent):
    account.reserve(intent(quantity="3500", stop="99"), expected_version=0)
    with pytest.raises(AdmissionError, match="EXPOSURE_LIMIT"):
        account.reserve(intent(key="second", quantity="501"), expected_version=account.version)
    with pytest.raises(ValueError, match="fragmentation"):
        account.reserve(intent(key="split", quantity="1", episode="new-episode"), expected_version=account.version)


def test_open_risk_cannot_sum_five_maximal_trades(account, intent):
    # Owner limits 2026-10-16: 10% planned loss per position, 20% across positions -> two maximal trades fit.
    for i in range(2):
        account.reserve(intent(key=str(i), symbol=str(i), quantity="2000", stop="50"), expected_version=account.version)
    with pytest.raises(AdmissionError, match="OPEN_RISK"):
        account.reserve(intent(key="4", symbol="4", quantity="2000", stop="50"), expected_version=account.version)


def test_per_episode_limit_inclusive_and_costs_count(account, intent):
    account.reserve(intent(quantity="2000", stop="50"), expected_version=0)
    fresh = OfflineAccount(account_id=account.account_id, settled_usd=D("1000000"), equity_usd=D("1000000"), start_of_day_equity_usd=D("1000000"), calendar=account.calendar)
    with pytest.raises(AdmissionError, match="EPISODE_RISK"):
        fresh.reserve(intent(quantity="2000", stop="50"), expected_version=0, fee_allowance_usd=D("0.01"))


def test_pending_deposits_and_provisional_buying_power_are_ineligible(account, intent):
    account.settled_usd = D("100")
    account.pending_deposits_usd = D("1000000")
    account.provisional_buying_power_usd = D("1000000")
    with pytest.raises(AdmissionError, match="INSUFFICIENT_SETTLED_CASH"):
        account.reserve(intent(quantity="2"), expected_version=0)
    account.settlement_known = False
    with pytest.raises(AdmissionError, match="UNKNOWN_SETTLEMENT"):
        account.reserve(intent(quantity="1"), expected_version=0)


def test_partial_fill_cancel_fees_and_settlement(account, intent, event):
    entry = intent(quantity="100")
    account.reserve(entry, expected_version=0, fee_allowance_usd=D("2"))
    account.transition(entry.intent_id, S.SUBMITTING)
    account.apply(entry.intent_id, event(entry, filled="40", kind="FILL", price="99", fees="1"))
    assert account.settled_usd == D("996039")
    assert account.reservations[entry.intent_id].cash_usd == D("6001")
    account.transition(entry.intent_id, S.CANCEL_PENDING)
    account.apply(entry.intent_id, event(entry, seq=2, filled="50", kind="FILL", price="99", fees="1"))
    assert account.orders[entry.intent_id].state == S.CANCEL_PENDING
    account.apply(entry.intent_id, event(entry, seq=3, filled="50", kind="CANCELED", fees="1"))
    assert account.reservations[entry.intent_id].cash_usd == D("5001")
    account.confirm_terminal(entry.intent_id, filled=D("50"), fees=D("1"))
    assert account.available_settled_usd == D("995049")
    sell = intent(key="sell", side="SELL_OWNED", quantity="50", price="101")
    account.reserve(sell, expected_version=account.version)
    account.transition(sell.intent_id, S.SUBMITTING)
    sale = event(sell, filled="50", kind="FILL", price="101", fees="1")
    account.apply(sell.intent_id, sale)
    assert account.settled_usd == D("995048")
    assert account.settlement_lots[sale.event_id].amount_usd == D("5050")
    with pytest.raises(ValueError):
        account.confirm_settlement(sale.event_id, date(2026, 9, 11))
    account.confirm_settlement(sale.event_id, date(2026, 9, 14))
    account.confirm_settlement(sale.event_id, date(2026, 9, 14))
    assert account.settled_usd == D("1000098")  # $100 gain minus $2 total fees


def test_no_cash_reuse_after_roundtrip(account, intent, event):
    entry = buy(account, intent, event, quantity="2000")
    account.confirm_terminal(entry.intent_id, filled=D("2000"), fees=D("0"))
    sell = intent(key="sell", side="SELL_OWNED", quantity="2000")
    account.reserve(sell, expected_version=account.version)
    account.transition(sell.intent_id, S.SUBMITTING)
    account.apply(sell.intent_id, event(sell, filled="2000", kind="FILL", price="100"))
    assert account.settled_usd == D("800000")
    assert sum(x.amount_usd for x in account.settlement_lots.values()) == D("200000")


@pytest.mark.parametrize("pending", [S.CANCEL_PENDING, S.REPLACE_PENDING])
def test_sell_races_cannot_oversell(account, intent, event, pending):
    buy(account, intent, event)
    sell = intent(key="sell", quantity="100", side="SELL_OWNED", price="99")
    account.reserve(sell, expected_version=account.version)
    account.transition(sell.intent_id, S.SUBMITTING)
    account.apply(sell.intent_id, event(sell))
    account.transition(sell.intent_id, pending)
    with pytest.raises(ValueError, match="unreserved"):
        account.reserve(intent(key="overlap", quantity="100", side="SELL_OWNED"), expected_version=account.version)
    account.apply(sell.intent_id, event(sell, seq=2, filled="40", kind="FILL", price="99"))
    assert account.reservations[sell.intent_id].shares == D("60")
    assert account.positions[sell.episode_id].quantity == D("60")
    with pytest.raises(ValueError):
        account.reserve(intent(key="overlap2", quantity="1", side="SELL_OWNED"), expected_version=account.version)


def test_sell_permitted_during_loss_or_cash_halt(account, intent, event):
    buy(account, intent, event)
    account.new_risk_enabled = False
    account.settled_usd = D("0")
    account.reserve(intent(key="exit", side="SELL_OWNED"), expected_version=account.version)


def test_corrections_freeze_without_double_counting(account, intent, event):
    order = buy(account, intent, event)
    prior = account.settled_usd
    correction = event(order, seq=2, filled="0", kind="CORRECTION")
    account.apply(order.intent_id, correction)
    account.apply(order.intent_id, correction)
    assert account.settled_usd == prior
    assert not account.new_risk_enabled
    assert account.orders[order.intent_id].state == S.UNKNOWN
    assert len(account.journal) == 2


def test_partial_protection_and_failure(account, intent, event):
    order = intent(quantity="100")
    account.reserve(order, expected_version=0)
    account.transition(order.intent_id, S.SUBMITTING)
    account.apply(order.intent_id, event(order, kind="FILL", filled="25", price="100"))
    assert account.positions[order.episode_id].protected_quantity == 0
    account.acknowledge_protection(order.episode_id, D("25"), certified=True)
    assert account.positions[order.episode_id].protected_quantity == 25
    account.apply(order.intent_id, event(order, seq=2, kind="FILL", filled="50", price="100"))
    account.acknowledge_protection(order.episode_id, D("25"), certified=False)
    assert not account.new_risk_enabled
    assert account.orders[order.intent_id].state == S.CANCEL_PENDING
    assert "PROTECTION_FAILURE" in account.incidents[-1]


def test_consumed_loss_not_replenished_by_gains(account, intent, event):
    order = buy(account, intent, event)
    account.confirm_terminal(order.intent_id, filled=D("100"), fees=D("0"))
    for key, price in [("loss", "90"), ("gain", "120")]:
        sell = intent(key=key, side="SELL_OWNED", quantity="50", price=price)
        account.reserve(sell, expected_version=account.version)
        account.transition(sell.intent_id, S.SUBMITTING)
        account.apply(sell.intent_id, event(sell, kind="FILL", filled="50", price=price))
    assert account.episodes[order.episode_id].consumed_loss_usd == D("500")
    with pytest.raises(ValueError, match="fragmentation"):
        account.reserve(intent(key="renamed", episode="fresh"), expected_version=account.version)


def test_trailing_stop_giveback(position):
    assert position.average_entry_usd < position.active_stop_usd
    assert position_risk(position) == D("1000")


def test_risk_basis_does_not_expand_after_intraday_gain(account, intent):
    account.equity_usd = D("2000000")
    with pytest.raises(AdmissionError, match="EPISODE_RISK"):
        account.reserve(intent(quantity="2000", stop="40"), expected_version=0)


@settings(max_examples=100, derandomize=True, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.lists(st.tuples(st.integers(1, 100), st.sampled_from(["fill", "cancel_pending", "replace_pending", "duplicate"])), min_size=1, max_size=40))
def test_generated_fill_races_preserve_cash_and_quantities(calendar, intent, event, steps):
    account = OfflineAccount(account_id="offline-account", settled_usd=D("1000000"), equity_usd=D("1000000"), start_of_day_equity_usd=D("1000000"), calendar=calendar)
    order = intent()
    account.reserve(order, expected_version=0)
    account.transition(order.intent_id, S.SUBMITTING)
    account.apply(order.intent_id, event(order))
    last = None
    cumulative = 0
    seq = 1
    for amount, operation in steps:
        current = account.orders[order.intent_id]
        if operation == "duplicate" and last is not None:
            account.apply(order.intent_id, last)
        elif operation in ("cancel_pending", "replace_pending") and current.state not in (S.FILLED, S.UNKNOWN):
            account.transition(order.intent_id, S.CANCEL_PENDING if operation == "cancel_pending" else S.REPLACE_PENDING)
        elif operation == "fill" and cumulative < 100:
            cumulative = min(100, cumulative + amount)
            seq += 1
            last = event(order, seq=seq, kind="FILL", filled=str(cumulative), price="100")
            account.apply(order.intent_id, last)
        owned = account.positions.get(order.episode_id)
        assert (owned.quantity if owned else 0) == cumulative
        assert account.settled_usd == D("1000000") - D(cumulative) * D("100")
        assert account.reservations[order.intent_id].cash_usd == D(100 - cumulative) * D("100")
        assert account.available_settled_usd >= 0
