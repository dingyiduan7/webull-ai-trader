from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal as D
from uuid import uuid4
import pytest
from psycopg import sql

from autonomous_trader.contracts import OrderIntent
from autonomous_trader.reference import Session, NEW_YORK
from autonomous_trader.durable.ledger import Ledger, LocalDatabase, migrate, grant_writer
from autonomous_trader.durable.models import (CreateAccount, Mark, Reconcile, Admit, ConsumePermit,
    OrderObservation, Execution, FinalizeOrder, Protection)


@pytest.fixture
def db_config():
    schema = "m2_test_" + uuid4().hex[:16]
    admin = LocalDatabase(database="postgres", user="m2_admin", schema=schema)
    # Missing PostgreSQL fails the required integration suite; never silently skip.
    migrate(admin)
    grant_writer(admin, "m2_writer")
    try:
        yield admin, LocalDatabase(database="postgres", user="m2_writer", schema=schema)
    finally:
        try:
            with admin.connect() as connection:
                connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
        except Exception:
            # Preserve the original test result if a crash-boundary test has
            # intentionally left PostgreSQL unavailable during teardown.
            pass


@dataclass
class Harness:
    ledger: Ledger
    account_id: str
    at: object
    token: int
    holder: str = "worker-a"

    @property
    def state(self):
        return self.ledger.snapshot(self.account_id)

    def send(self, command, event_id=None, **kw):
        return self.ledger.execute(self.account_id, command, event_id=event_id or uuid4().hex,
            expected_version=kw.get("expected_version", self.state["version"]), holder=kw.get("holder", self.holder),
            token=kw.get("token", self.token), at=kw.get("at", self.at))

    def reconcile(self, **updates):
        s = self.state
        values = dict(settled_usd=s["cash"]["settled_usd"], unsettled_usd=s["cash"]["unsettled_usd"],
            positions=tuple((symbol, p["quantity"]) for symbol, p in s["positions"].items()),
            outstanding_intent_ids=tuple(k for k, o in s["orders"].items() if not o["final"]),
            evidence_id=uuid4().hex, settlement_known=True)
        values.update(updates)
        return self.send(Reconcile(**values))

    def admit(self, intent, *, fee="0", exit_cost="0", stress="0"):
        s = self.send(Admit(intent=intent, policy_hash=self.state['policy_hash'] if 'economic_policy' in self.state else None,
            fee_allowance_usd=fee, exit_cost_usd=exit_cost,
            stress_allowance_usd=stress, latest_exit_at=Session.model_validate(self.state["session"]).liquidation_at, max_holding_seconds=600))
        return s["outbox"][intent.intent_id]["nonce"]

    def dispatch(self, intent, nonce):
        self.send(ConsumePermit(nonce=nonce))
        self.send(OrderObservation(intent_id=intent.intent_id, status="ACKNOWLEDGED", evidence_id=uuid4().hex))

    def fill(self, intent, quantity=None, price=None, fee="0", execution_id=None):
        execution_id = execution_id or uuid4().hex
        day = self.at.astimezone(NEW_YORK).date()
        days = self.state["config"]["settlement_days"]
        settlement_date = next(d for d in days if d > day.isoformat()) if intent.side == "SELL_OWNED" else None
        self.send(Execution(execution_id=execution_id, intent_id=intent.intent_id, quantity=quantity or intent.quantity,
            price_usd=price or intent.limit_price_usd, fee_usd=fee, executed_at=self.at, settlement_date=settlement_date,
            evidence_id="fixture-execution"))
        return execution_id

    def finalize(self, intent, status="FILLED"):
        s = self.state
        order = s["orders"][intent.intent_id]
        return self.send(FinalizeOrder(intent_id=intent.intent_id, status=status,
            execution_ids=tuple(k for k, x in s["executions"].items() if x["intent_id"] == intent.intent_id),
            total_quantity=order["filled_quantity"], total_fees_usd=order["fees_usd"], evidence_id="complete-fixture-detail"))

    def trade(self, intent, *, price=None, fee="0"):
        nonce = self.admit(intent, fee=fee)
        self.dispatch(intent, nonce)
        execution_id = self.fill(intent, price=price, fee=fee)
        self.finalize(intent)
        if intent.side == "BUY":
            p = self.state["positions"][intent.instrument_id]
            self.send(Protection(instrument_id=intent.instrument_id, quantity=p["quantity"], stop_usd=p["stop_usd"], evidence_id="offline-protection"))
        self.reconcile()
        return execution_id


@pytest.fixture
def harness_factory(db_config, now, calendar, context):
    def create(balance="1000000", count=6, ledger=None, account_prefix="account-"):
        service = ledger or Ledger(db_config[1])
        instruments = tuple(type(context.instrument)(**{**context.instrument.model_dump(), "instrument_id": f"stock-{i}", "ticker": f"S{i}"}) for i in range(count))
        days = []
        day = now.date()
        for offset in range(30):
            candidate = day + timedelta(days=offset)
            if calendar.settlement_day(candidate):
                days.append(candidate)
        account_id = account_prefix + uuid4().hex
        command = CreateAccount(account_id=account_id, experiment_id="experiment-" + account_id, owner_id="owner",
            initial_settled_usd=balance, session=calendar.session(now.date()), instruments=instruments,
            capabilities=context.capabilities, settlement_days=tuple(days), mark_max_age_seconds=3600, reconciliation_max_age_seconds=3600)
        service.create(command, event_id=uuid4().hex, at=now)
        s = service.acquire_leader(account_id, "worker-a", event_id=uuid4().hex, at=now, ttl_seconds=60)
        h = Harness(service, account_id, now, s["leader"]["token"])
        for instrument in instruments:
            h.send(Mark(instrument_id=instrument.instrument_id, bid_usd="100", observed_at=now, source_id="offline-fixture"))
        h.reconcile()
        return h
    return create


@pytest.fixture
def h(harness_factory):
    return harness_factory()


@pytest.fixture
def order_factory(intent):
    def create(h, key=None, symbol="stock-0", quantity="100", price="100", stop="95", side="BUY"):
        order = intent(key=key or uuid4().hex, symbol=symbol, quantity=quantity, price=price, stop=stop, side=side)
        return OrderIntent(**{**order.model_dump(), "account_id": h.account_id})
    return create
