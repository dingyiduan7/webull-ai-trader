from datetime import date, datetime, time, timezone
from decimal import Decimal as D
import json
import pytest

from autonomous_trader.contracts import *
from autonomous_trader.decisions import SemanticRegistry
from autonomous_trader.reference import FixtureCalendar
from autonomous_trader.portfolio import OfflineAccount


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    import socket
    def forbidden(*args, **kwargs):
        raise AssertionError("M1 tests must remain offline")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.fixture
def now():
    return datetime(2026, 9, 11, 14, 0, tzinfo=timezone.utc)


@pytest.fixture
def calendar():
    # Deliberately synthetic fixture: exchange and settlement closures differ.
    return FixtureCalendar(date(2026, 1, 1), date(2026, 12, 31),
        exchange_holidays={date(2026, 4, 3), date(2026, 11, 26)},
        settlement_holidays={date(2026, 9, 7), date(2026, 11, 11), date(2026, 11, 26)},
        early_closes={date(2026, 11, 27): time(13)})


@pytest.fixture
def instrument():
    return Instrument(instrument_id="stock-a", ticker="AAA", country="US", asset_type="STOCK",
        listing_exchange="TEST_US_EXCHANGE", liquid=True, tradable=True,
        quantity_increment="1", price_increment_usd="0.01", minimum_quantity="1")


@pytest.fixture
def registry():
    return SemanticRegistry(strategy_bindings=(("strategy", "1", "artifact"),), policy_hashes=("policy",),
        source_ids=("fixture",), execution_policy_ids=("protect", "exit"), partial_fill_policy_ids=("partial",),
        trailing_rules=(("none", "NONE"), ("trail", "AMOUNT"), ("percent", "PERCENT"), ("feature", "FEATURE_RULE")),
        calibration_ids=("calibration",), maximum_market_age_ms=2000, maximum_account_age_ms=10000)


@pytest.fixture
def context(now, calendar, instrument):
    session = calendar.session(now.date())
    account = AccountSnapshot(account_id="offline-account", environment="OFFLINE", account_type="CASH",
        balances=CashBalances(settled_usd="1000000", reserved_usd="0", unsettled_usd="0", settlement_known=True),
        settlement_semantics="CONFIRMED_SETTLED_ONLY", positions=(), outstanding_order_ids=(), observed_at=now,
        reconciliation_version="0", broker_equity_usd="1000000", liquidation_equity_usd="1000000", economic_equity_usd="1000000")
    return DecisionContext(context_id="context", decision_id="decision", opportunity_id="opportunity", account=account,
        strategy_id="strategy", strategy_version="1", strategy_artifact_hash="artifact", prompt_version="1", policy_hash="policy",
        as_of=now, expires_at=now.replace(minute=1), instrument=instrument,
        capabilities=CapabilityManifest(version="sim-v1", entry_order_types=("LIMIT", "STOP_LIMIT"), protection_mechanisms=("BROKER_STOP_MARKET", "BROKER_STOP_LIMIT")),
        session_label=now.date(), session_open=session.opens_at, session_close=session.closes_at, liquidation_at=session.liquidation_at,
        market=MarketEvent(instrument_id="stock-a", event_type="QUOTE", bid_usd="99.99", ask_usd="100", trade_price_usd=None,
            quantity=None, conditions=(), source_event_at=now, received_at=now, entitled=True, delay_ms=0, quality_flags=(), raw_reference="quote"),
        features=(Feature(feature_id="bid", source_id="fixture", value="99.99", value_type="DECIMAL", usable_at=now),),
        evidence=(Evidence(evidence_id="evidence", source_id="fixture", published_at=now, received_at=now, usable_at=now, revision="1", text="Synthetic evidence"),),
        warnings=(), prior_decision_ids=(), memory_evidence_ids=(), allowed_actions=("ABSTAIN", "REQUEST_DATA", "PROPOSE_ENTRY", "PROPOSE_AMENDMENT", "PROPOSE_EXIT"),
        evaluation_horizon_seconds=600, expected_cost_usd="1", expected_latency_ms=0, review_reason="fixture")


@pytest.fixture
def proposal(context, now):
    condition = {"join": "ALL", "predicates": [{"feature_id": "bid", "source_id": "fixture", "operator": "GT", "value": "90", "persistence_ms": 0}]}
    return dict(schema_version="1.0", decision_id="decision", context_id="context", context_hash=payload_hash(context),
        opportunity_id="opportunity", portfolio_state_version="0", policy_hash="policy", capability_version="sim-v1",
        strategy_id="strategy", strategy_version="1", strategy_artifact_hash="artifact", created_at=now.isoformat(),
        valid_until=context.expires_at.isoformat(), action="PROPOSE_ENTRY", amendment=None, exit_request=None, data_request=None,
        entry_plan=dict(instrument_id="stock-a", ticker="AAA", direction="LONG", entry_condition=condition,
            entry_order_type="LIMIT", session="CORE", time_in_force="DAY", maximum_entry_usd="100", limit_price_usd="100", trigger_price_usd=None,
            entry_expires_at=context.expires_at.isoformat(), sizing={"unit": "SHARES", "amount": "100", "minimum_acceptable_quantity": "1"},
            protection=dict(initial_stop_usd="95", trigger_reference="BID", trigger_source_id="fixture", execution_policy_id="protect", mechanism="BROKER_STOP_MARKET", stop_limit_usd=None),
            targets=[], trailing=dict(rule_id="none", mode="NONE", activation_rule=None, distance=None),
            max_holding_seconds=600, latest_exit_at=context.liquidation_at.isoformat(), invalidation=condition,
            partial_fill_policy_id="partial", max_spread_bps="20", strategy_episode_id="episode-a"),
        forecast=None, evidence_ids=["evidence"], contradictions=[], missing_data=[], reasoning_summary="Offline proposal", review_triggers=[])


@pytest.fixture
def intent(now):
    def make(key="buy-1", symbol="stock-a", quantity="100", price="100", stop="95", side="BUY", episode=None):
        return OrderIntent(intent_id=key, client_order_id="client-" + key, account_id="offline-account", environment="OFFLINE",
            instrument_id=symbol, side=side, quantity=quantity, order_type="LIMIT", session="CORE", time_in_force="DAY",
            limit_price_usd=price, trigger_price_usd=None, maximum_entry_usd=price if side == "BUY" else None,
            protection=Protection(initial_stop_usd=stop, trigger_reference="BID", trigger_source_id="fixture", execution_policy_id="protect", mechanism="BROKER_STOP_MARKET", stop_limit_usd=None) if side == "BUY" else None,
            plan_id="plan-" + symbol, episode_id=episode or "episode-" + symbol, deadline=now.replace(hour=19), capability_version="sim-v1")
    return make


@pytest.fixture
def account(calendar):
    return OfflineAccount(account_id="offline-account", settled_usd=D("1000000"), equity_usd=D("1000000"), start_of_day_equity_usd=D("1000000"), calendar=calendar)


@pytest.fixture
def event(now):
    def make(order, seq=1, filled="0", kind="ACK", price=None, fees="0", at=None):
        return BrokerEvent(broker_order_id="sim-" + order.client_order_id, client_order_id=order.client_order_id,
            event_id=order.intent_id + "-event-" + str(seq), kind=kind, sequence=seq, cumulative_filled_quantity=filled,
            leaves_quantity=None, fill_price_usd=price, fees_usd=fees, received_at=at or now, raw_reference="raw-fixture")
    return make


@pytest.fixture
def position(now, calendar):
    return Position(position_id="position", instrument_id="stock-a", episode_id="episode-a", plan_id="plan", plan_version="1",
        quantity="100", average_entry_usd="80", mark_usd="100", active_stop_usd="90", first_fill_at=now,
        latest_exit_at=calendar.session(now.date()).liquidation_at, max_holding_seconds=600, protected_quantity="100")
