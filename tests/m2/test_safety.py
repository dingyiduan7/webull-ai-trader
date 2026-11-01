from datetime import timedelta, date
from decimal import Decimal as D
from hashlib import sha256
import hmac
import json
from uuid import uuid4
import pytest
from autonomous_trader.risk import OwnerRestart, HMACOwnerAuthenticator
from autonomous_trader.durable.ledger import Ledger
from autonomous_trader.durable.models import Mark, Restart, NewSession, Protection
from autonomous_trader.durable.projection import Rejected

TEST_KEY = b"M2-offline-owner-test-key-0000000"


def signed_action(h, *, key=TEST_KEY, **updates):
    s = h.state
    action = OwnerRestart(action_id=uuid4().hex, owner_id="owner", experiment_id=s["experiment_id"],
        segment_id=s["segments"][-1]["segment_id"], expected_equity_usd=s["cash"]["liquidation_equity_usd"],
        issued_at=h.at, expires_at=h.at + timedelta(minutes=1), proof="unsigned")
    action = OwnerRestart(**{**action.model_dump(), **updates})
    payload = json.dumps(action.model_dump(mode="json", exclude={"proof"}), sort_keys=True, separators=(",", ":")).encode()
    return OwnerRestart(**{**action.model_dump(), "proof": hmac.new(key, payload, sha256).hexdigest()})


def mark(h, symbol, price):
    return h.send(Mark(instrument_id=symbol, bid_usd=price, observed_at=h.at, source_id="offline-fixture"))


def test_approved_drawdown_restart_and_permanent_death(harness_factory, order_factory, db_config, calendar):
    h = harness_factory(count=5, ledger=Ledger(db_config[1], owner_authenticator=HMACOwnerAuthenticator("owner", TEST_KEY)))
    for i in range(5):
        if i in (2,4):
            h.at += timedelta(days=3 if i==2 else 1)
            h.reconcile();h.send(NewSession(session=calendar.session(h.at.date())))
            for prior in range(i):mark(h,f"stock-{prior}","100")
        mark(h,f"stock-{i}","100")
        order=order_factory(h, symbol=f"stock-{i}", quantity="2000", stop="99").model_copy(
            update={'deadline':calendar.session(h.at.date()).liquidation_at})
        h.trade(order)
    mark(h, "stock-0", "50")
    mark(h, "stock-1", "50")
    before = h.state
    assert D(before["cash"]["liquidation_equity_usd"]) == 800000
    assert before["latches"]["daily"] and before["latches"]["drawdown"]
    assert h.ledger.status(h.account_id, at=h.at)["mode"] == "DRAWDOWN_HALTED"
    with pytest.raises(Rejected, match="AUTHENTICATED"):
        h.send(Restart(action=signed_action(h, key=b"wrong-key-000000000000000000000000")))
    action = signed_action(h)
    h.send(Restart(action=action))
    after = h.state
    assert not after["latches"]["drawdown"] and after["latches"]["daily"]
    assert after["segments"][0] == before["segments"][0]
    assert D(after["segments"][-1]["reference_usd"]) == 800000
    assert D(after["historic_high_water"]) == 1000000
    assert action.action_id in after["restarts"]
    assert h.ledger.rebuild(h.account_id) == after
    h.at += timedelta(days=3)
    h.reconcile()
    h.send(NewSession(session=calendar.session(h.at.date())))
    assert not h.state["latches"]["daily"]
    for i in range(5):
        mark(h, f"stock-{i}", "10")
    assert D(h.state["cash"]["liquidation_equity_usd"]) == 100000
    assert h.state["latches"]["terminated"]
    with pytest.raises(Rejected, match="RESTART_NOT_PERMITTED"):
        h.send(Restart(action=signed_action(h)))
    for i in range(5):
        mark(h, f"stock-{i}", "100")
    assert h.state["latches"]["terminated"]
    assert Ledger(db_config[1]).status(h.account_id, at=h.at)["mode"] == "TERMINATED"
    assert h.ledger.rebuild(h.account_id) == h.state


def test_trailing_giveback_and_intraday_risk_basis(h, order_factory):
    h.trade(order_factory(h))
    mark(h, "stock-0", "120")
    h.send(Protection(instrument_id="stock-0", quantity="100", stop_usd="110", evidence_id="tightened"))
    assert D(h.state["positions"]["stock-0"]["open_risk_usd"]) == 1000
    assert D(h.state["positions"]["stock-0"]["entry_risk_usd"]) == 0
    with pytest.raises(Rejected, match="EPISODE_RISK"):
        h.admit(order_factory(h, symbol="stock-1", quantity="2000", stop="49.99"))   # > 10% (owner 2026-10-16)


def test_daily_headroom_coupling_is_not_implemented(h, order_factory):
    # Two adverse realized gaps total 4%, while there is still settled cash.
    for i in range(2):
        h.trade(order_factory(h, symbol=f"stock-{i}", quantity="1000", stop="99"))
        h.trade(order_factory(h, symbol=f"stock-{i}", side="SELL_OWNED", quantity="1000", price="80"))
    assert D(h.state["cash"]["realized_net_usd"]) == -40000
    assert not h.state["latches"]["daily"]
    # $13,300 planned loss fits 1.5% of $960k but exceeds $10k unused daily headroom.
    h.admit(order_factory(h, symbol="stock-2", quantity="1900", stop="93"))
    assert D(h.state["cash"]["reserved_usd"]) == 190000


def test_daily_mark_loss_latches_despite_price_recovery(h, order_factory):
    h.trade(order_factory(h, quantity="2000", stop="99"))
    mark(h, "stock-0", "75")
    assert h.state["latches"]["daily"]
    mark(h, "stock-0", "100")
    assert h.state["latches"]["daily"]
    with pytest.raises(Rejected, match="LATCHED"):
        h.admit(order_factory(h, symbol="stock-1"))
    # Risk-reducing reservations remain available during daily halt.
    h.admit(order_factory(h, side="SELL_OWNED", quantity="2000"))
