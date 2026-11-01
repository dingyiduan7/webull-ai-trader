from datetime import timedelta
from decimal import Decimal as D
import pytest
from autonomous_trader.contracts import RiskPermit, payload_hash
from autonomous_trader.invariants import DispatchConsumption, InvariantSuite
from autonomous_trader.execution import OrderState as S
from autonomous_trader.portfolio import AdmissionError


def permit_for(order, now):
    return RiskPermit(intent_hash=payload_hash(order), policy_hash="policy", reservation_ids=(order.intent_id,),
        account_state_version="1", account_id=order.account_id, environment="OFFLINE", expires_at=now + timedelta(seconds=30), nonce="nonce", fencing_token=5)


def test_permit_one_use_survives_simulated_restart(intent, now, tmp_path):
    order = intent()
    permit = permit_for(order, now)
    state = DispatchConsumption(account_id=order.account_id, policy_hash="policy", fencing_token=5, lease_expires_at=now + timedelta(seconds=60))
    kwargs = dict(account_state_version="1", reservation_ids=(order.intent_id,), audit_available=True, reconciled=True)
    state = state.consume(permit, order, now, **kwargs)
    snapshot = tmp_path / "offline-consumption.json"
    snapshot.write_text(state.model_dump_json())
    restored = DispatchConsumption.model_validate_json(snapshot.read_text())
    with pytest.raises(ValueError, match="consumed"):
        restored.consume(permit, order, now, **kwargs)
    with pytest.raises(ValueError, match="consumed"):
        restored.consume(permit.model_copy(update={"nonce": "fresh-nonce"}), order, now, **kwargs)


@pytest.mark.parametrize("update", [{"fencing_token": 4}, {"intent_hash": "wrong"}, {"account_id": "other"}, {"policy_hash": "other"}, {"account_state_version": "old"}, {"reservation_ids": ()}])
def test_permit_binding_and_fencing(intent, now, update):
    order = intent()
    state = DispatchConsumption(account_id=order.account_id, policy_hash="policy", fencing_token=5, lease_expires_at=now + timedelta(seconds=60))
    permit = permit_for(order, now).model_copy(update=update)
    with pytest.raises(ValueError):
        state.consume(permit, order, now, account_state_version="1", reservation_ids=(order.intent_id,), audit_available=True, reconciled=True)


@pytest.mark.parametrize("audit,reconciled,offset", [(False, True, 0), (True, False, 0), (True, True, 60)])
def test_dispatch_fault_gates(intent, now, audit, reconciled, offset):
    order = intent()
    state = DispatchConsumption(account_id=order.account_id, policy_hash="policy", fencing_token=5, lease_expires_at=now + timedelta(seconds=60))
    with pytest.raises(ValueError):
        state.consume(permit_for(order, now), order, now + timedelta(seconds=offset), account_state_version="1",
                      reservation_ids=(order.intent_id,), audit_available=audit, reconciled=reconciled)


def test_invariant_suite_and_protection_admission_gate(account, intent, event):
    suite = InvariantSuite()
    suite.assert_valid(account.view())
    order = intent()
    account.reserve(order, expected_version=0)
    account.transition(order.intent_id, S.SUBMITTING)
    account.apply(order.intent_id, event(order, kind="FILL", filled="50", price="100"))
    with pytest.raises(AssertionError, match="protection"):
        suite.assert_valid(account.view())
    with pytest.raises(AdmissionError, match="UNSUPPORTED_CAPABILITY"):
        account.reserve(intent(key="second", symbol="stock-b"), expected_version=account.version)
    account.acknowledge_protection(order.episode_id, D("50"), certified=True)
    suite.assert_valid(account.view())


def test_bad_fill_rolls_back_projection_keeps_event_and_reservations(account, intent, event):
    order = intent()
    account.reserve(order, expected_version=0)
    account.transition(order.intent_id, S.SUBMITTING)
    invalid = event(order, kind="FILL", filled="100", price="101")
    with pytest.raises(ValueError):
        account.apply(order.intent_id, invalid)
    assert not account.new_risk_enabled
    assert account.orders[order.intent_id].state == S.UNKNOWN
    assert account.settled_usd == D("1000000")
    assert account.positions == {}
    assert account.reservations[order.intent_id].cash_usd == D("10000")
    assert invalid in account.journal


def test_account_and_uncertain_orders_do_not_free_reservations(account, intent):
    order = intent()
    account.reserve(order, expected_version=0)
    account.transition(order.intent_id, S.SUBMITTING)
    account.transition(order.intent_id, S.UNKNOWN)
    assert not account.new_risk_enabled
    with pytest.raises(ValueError):
        account.confirm_terminal(order.intent_id, filled=D("0"), fees=D("0"))
    assert account.reservations[order.intent_id].cash_usd == D("10000")
