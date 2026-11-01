"""Composable deterministic M1 checks and offline dispatch-consumption reducer."""
from datetime import datetime
from typing import Callable
from .contracts import Record, ID, UTC, RiskPermit, OrderIntent, payload_hash, utc
from .portfolio import PortfolioView, Violation, invariant_violations


class InvariantSuite:
    def __init__(self, checks: tuple[Callable[[PortfolioView], tuple[Violation, ...]], ...] = (invariant_violations,)):
        self.checks = checks

    def evaluate(self, view: PortfolioView) -> tuple[Violation, ...]:
        return tuple(issue for check in self.checks for issue in check(view))

    def assert_valid(self, view: PortfolioView):
        failures = self.evaluate(view)
        if failures:
            raise AssertionError("; ".join(f"{v.code}: {v.detail}" for v in failures))


class DispatchConsumption(Record):
    """Pure reference state, NOT a gateway or durable authority store.

    A consumed permit stays consumed after an uncertain send. Tests serialize
    this state to exercise restart invariants; M2 must persist it transactionally.
    """
    account_id: ID
    policy_hash: ID
    fencing_token: int
    lease_expires_at: UTC
    consumed_nonces: tuple[ID, ...] = ()
    consumed_intent_ids: tuple[ID, ...] = ()

    def consume(self, permit: RiskPermit, intent: OrderIntent, now: datetime, *, account_state_version: str,
                reservation_ids: tuple[str, ...], audit_available: bool, reconciled: bool):
        now = utc(now)
        if not audit_available or not reconciled:
            raise ValueError("dispatch blocked without audit durability and reconciliation")
        if not now < min(self.lease_expires_at, permit.expires_at, intent.deadline):
            raise ValueError("expired lease, intent, or permit")
        if (permit.account_id != self.account_id or intent.account_id != self.account_id
                or permit.policy_hash != self.policy_hash or permit.intent_hash != payload_hash(intent)
                or permit.account_state_version != account_state_version
                or permit.fencing_token != self.fencing_token or permit.environment != intent.environment
                or not permit.reservation_ids or set(permit.reservation_ids) != set(reservation_ids)):
            raise ValueError("permit binding or fencing mismatch")
        if permit.nonce in self.consumed_nonces or intent.intent_id in self.consumed_intent_ids:
            raise ValueError("permit/economic intent already consumed; reconcile instead of resubmitting")
        return type(self)(**{**self.model_dump(), "consumed_nonces": (*self.consumed_nonces, permit.nonce),
                             "consumed_intent_ids": (*self.consumed_intent_ids, intent.intent_id)})
