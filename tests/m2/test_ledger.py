from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, date, timezone
from decimal import Decimal as D
from uuid import uuid4
import pytest
from hypothesis import given, settings, HealthCheck, strategies as st
from psycopg import errors
from autonomous_trader.durable.models import *
from autonomous_trader.durable.ledger import Ledger, LocalDatabase
from autonomous_trader.durable.execution_correction import SaleCashEvidence,correction_from_sale_cash
from autonomous_trader.durable.projection import (Rejected,digest,risk_violations,settlement_observation_date,
    entry_submission_counts,reduce_event)
from autonomous_trader.reference import Session


def test_durable_trade_fifo_cash_pnl_and_rebuild(h, order_factory):
    entry = order_factory(h, quantity="100")
    buy_id = h.trade(entry, price="99", fee="1")
    s = h.state
    assert D(s["cash"]["settled_usd"]) == D("990099")
    assert D(s["cash"]["unrealized_usd"]) == D("100")
    assert D(s["cash"]["realized_net_usd"]) == D("-1")
    sell = order_factory(h, side="SELL_OWNED", quantity="40", price="101")
    sale_id = h.trade(sell, fee="1")
    s = h.state
    assert D(s["positions"]["stock-0"]["quantity"]) == 60
    assert D(s["cash"]["settled_usd"]) == D("990099")
    assert D(s["cash"]["unsettled_usd"]) == D("4039")
    assert D(s["cash"]["realized_gross_usd"]) == 80
    assert D(s["cash"]["realized_net_usd"]) == 78
    assert D(s["cash"]["unrealized_usd"]) == 60
    assert D(s["cash"]["liquidation_equity_usd"]) == D("1000138")
    with pytest.raises(Rejected, match="SETTLEMENT"):
        h.send(ConfirmSettlement(execution_id=sale_id, amount_usd="4039", effective_on=date(2026, 9, 11), evidence_id="early"))
    h.at += timedelta(days=3)
    h.send(ConfirmSettlement(execution_id=sale_id, amount_usd="4039", effective_on=date(2026, 9, 14), evidence_id="confirmed"))
    h.send(ConfirmSettlement(execution_id=sale_id, amount_usd="4039", effective_on=date(2026, 9, 14), evidence_id="repeat-confirmation"))
    assert D(h.state["cash"]["settled_usd"]) == D("994138")
    assert D(h.state["cash"]["unsettled_usd"]) == 0
    assert h.ledger.rebuild(h.account_id) == h.state


def test_exact_next_business_day_settlement_is_recognizable_only_after_close(h,order_factory):
    h.trade(order_factory(h),price='99')
    sale_id=h.trade(order_factory(h,side='SELL_OWNED'),price='101')
    due=date.fromisoformat(h.state['settlements'][sale_id]['settlement_date'])
    with pytest.raises(Rejected,match='SETTLEMENT'):
        h.send(ConfirmSettlement(execution_id=sale_id,amount_usd='10100',effective_on=due,evidence_id='before-close'))
    h.at=__import__('datetime').datetime.fromisoformat(h.state['session']['closes_at'])+timedelta(seconds=1)
    h.send(ConfirmSettlement(execution_id=sale_id,amount_usd='10100',effective_on=due,evidence_id='after-close-batch'))
    assert D(h.state['cash']['unsettled_usd'])==0


def test_exact_two_lot_broker_settlement_moves_8739_43_without_changing_equity(h,order_factory):
    h.trade(order_factory(h,symbol='stock-0',quantity='50'),price='100')
    first=h.trade(order_factory(h,symbol='stock-0',side='SELL_OWNED',quantity='50',price='93.19'),price='93.19')
    h.trade(order_factory(h,symbol='stock-1',quantity='1'),price='100')
    second=h.trade(order_factory(h,symbol='stock-1',side='SELL_OWNED',quantity='1',price='4079.93'),price='4079.93')
    before=h.state
    assert D(before['cash']['unsettled_usd'])==D('8739.43')
    protected={key:before['cash'][key] for key in ('liquidation_equity_usd','realized_gross_usd','fees_usd','realized_net_usd')}

    h.at=datetime.fromisoformat(before['session']['closes_at'])+timedelta(seconds=1)
    for execution_id in (first,second):
        lot=h.state['settlements'][execution_id]
        h.send(ConfirmSettlement(execution_id=execution_id,amount_usd=lot['amount_usd'],
            effective_on=date.fromisoformat(lot['settlement_date']),evidence_id='exact-broker-settlement-8739-43'))
    after=h.state
    assert D(after['cash']['settled_usd'])==D(before['cash']['settled_usd'])+D('8739.43')
    assert D(after['cash']['unsettled_usd'])==0
    assert {key:after['cash'][key] for key in protected}==protected
    assert h.ledger.rebuild(h.account_id)==after


def test_prior_session_next_morning_does_not_skip_a_settlement_day(h):
    state=deepcopy(h.state)
    monday=Session(label=date(2026,9,14),opens_at=datetime(2026,9,14,13,30,tzinfo=timezone.utc),
        closes_at=datetime(2026,9,14,20,0,tzinfo=timezone.utc))
    state['session']=monday.model_dump(mode='json')
    tuesday_premarket=datetime(2026,9,15,13,0,tzinfo=timezone.utc)
    assert settlement_observation_date(state,tuesday_premarket)==date(2026,9,15)


def test_session_rollover_reanchors_next_settlement_day(h):
    state=deepcopy(h.state)
    tuesday=Session(label=date(2026,9,15),opens_at=datetime(2026,9,15,13,30,tzinfo=timezone.utc),
        closes_at=datetime(2026,9,15,20,0,tzinfo=timezone.utc))
    state['session']=tuesday.model_dump(mode='json')
    assert settlement_observation_date(state,tuesday.closes_at+timedelta(seconds=1))==date(2026,9,16)


def test_multiple_fill_fifo_is_independent_of_json_key_order(h, order_factory):
    buy = order_factory(h)
    nonce = h.admit(buy)
    h.dispatch(buy, nonce)
    h.fill(buy, quantity="40", price="98", execution_id="z-first")
    h.fill(buy, quantity="60", price="99", execution_id="a-second")
    h.finalize(buy)
    h.send(Protection(instrument_id="stock-0", quantity="100", stop_usd="95", evidence_id="protected"))
    h.reconcile()
    sale = order_factory(h, quantity="50", side="SELL_OWNED")
    h.trade(sale)
    s = h.state
    assert D(s["lots"]["z-first"]["quantity"]) == 0
    assert D(s["lots"]["a-second"]["quantity"]) == 50
    assert D(s["cash"]["realized_gross_usd"]) == 90
    assert h.ledger.rebuild(h.account_id) == s


def test_atomic_competing_admission_and_retry(h, order_factory):
    a = order_factory(h, quantity="2500")
    b = order_factory(h, quantity="2500")
    version = h.state["version"]
    def attempt(order):
        cmd = Admit(intent=order, fee_allowance_usd="0", exit_cost_usd="0", stress_allowance_usd="0",
                    latest_exit_at=h.state["session"]["closes_at"].replace("20:00", "19:55"), max_holding_seconds=600)
        try:
            h.send(cmd, expected_version=version)
            return True
        except Rejected:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(attempt, [a, b])) == 1
    missing = a if a.intent_id not in h.state["orders"] else b
    with pytest.raises(Rejected, match="STOCK_EXPOSURE"):
        h.admit(missing)
    assert len(h.state["orders"]) == 1
    assert D(h.state["cash"]["reserved_usd"]) == D("250000")


def test_concurrent_cash_admission_after_settled_budget_consumption(h, order_factory):
    for i in range(4):
        if i==2:_advance_session(h,3)
        h.trade(_order_for_session(h,order_factory,symbol=f"stock-{i}",quantity="2000",stop="99"))
        h.trade(_order_for_session(h,order_factory,symbol=f"stock-{i}",side="SELL_OWNED",quantity="2000"))
    assert D(h.state["cash"]["settled_usd"]) == 200000
    assert D(h.state["cash"]["unsettled_usd"]) == 800000
    version = h.state["version"]
    orders = [_order_for_session(h,order_factory,symbol=f"stock-{i}",quantity="1500") for i in (4,5)]
    def attempt(order):
        from autonomous_trader.reference import Session
        try:
            h.send(Admit(intent=order, fee_allowance_usd="0", exit_cost_usd="0", stress_allowance_usd="0",
                latest_exit_at=Session.model_validate(h.state["session"]).liquidation_at, max_holding_seconds=600), expected_version=version)
            return True
        except Rejected:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(attempt, orders)) == 1
    remaining = next(o for o in orders if o.intent_id not in h.state["orders"])
    with pytest.raises(Rejected, match="INSUFFICIENT_SETTLED_CASH"):
        h.admit(remaining)


def test_protection_cannot_carry_over_to_reopened_position(h, order_factory):
    h.trade(order_factory(h))
    h.trade(order_factory(h, side="SELL_OWNED"))
    again = order_factory(h)
    nonce = h.admit(again)
    h.dispatch(again, nonce)
    h.fill(again)
    assert D(h.state["positions"]["stock-0"]["protected_quantity"]) == 0
    h.reconcile()
    with pytest.raises(Rejected, match="UNPROTECTED"):
        h.admit(order_factory(h, symbol="stock-1"))


def test_closed_historical_episode_does_not_block_new_episode(h,order_factory):
    first=order_factory(h,symbol='stock-0')
    h.trade(first)
    h.trade(order_factory(h,symbol='stock-0',side='SELL_OWNED'))
    again=order_factory(h,symbol='stock-0')
    again=OrderIntent(**{**again.model_dump(),'episode_id':'episode-stock-0-next'})
    nonce=h.admit(again)
    assert h.state['outbox'][again.intent_id]['nonce']==nonce
    assert first.episode_id in h.state['episodes'] and again.episode_id in h.state['episodes']


def test_active_same_instrument_episode_still_rejects_fragmentation(h,order_factory):
    h.trade(order_factory(h,symbol='stock-0'))
    other=order_factory(h,symbol='stock-0')
    other=OrderIntent(**{**other.model_dump(),'episode_id':'episode-stock-0-next'})
    with pytest.raises(Rejected,match='EPISODE_FRAGMENTATION'):
        h.admit(other)


def test_correcting_already_settled_sale_records_adjustment_without_money_route(h, order_factory):
    buy = order_factory(h)
    h.trade(buy)
    sell = order_factory(h, side="SELL_OWNED")
    sale_id = h.trade(sell)
    h.at += timedelta(days=3)
    h.send(ConfirmSettlement(execution_id=sale_id, amount_usd="10000", effective_on=date(2026, 9, 14), evidence_id="settled"))
    h.send(CorrectExecutions(changes=(CorrectedExecution(execution_id=sale_id, expected_revision=0,
        quantity="100", price_usd="99", fee_usd="0"),), evidence_id="price-correction"))
    assert D(h.state["cash"]["settled_usd"]) == 1000000
    assert D(h.state["cash"]["unsettled_usd"]) == -100  # explicit reconciliation liability
    assert h.state["latches"]["incident"]
    h.send(ConfirmSettlement(execution_id=sale_id, amount_usd="9900", effective_on=date(2026, 9, 14), evidence_id="adjustment-confirmed"))
    assert D(h.state["cash"]["settled_usd"]) == 999900
    assert D(h.state["cash"]["unsettled_usd"]) == 0
    assert h.ledger.rebuild(h.account_id) == h.state


def test_soar_rounded_fill_cash_proof_corrects_exact_eighteen_dollars_append_only(h,order_factory):
    buy=order_factory(h,quantity='10000',price='0.41',stop='0.40')
    h.trade(buy,price='0.41',fee='0.03')
    sell=order_factory(h,side='SELL_OWNED',quantity='10000',price='0.40',stop='0.40')
    sale_id=h.trade(sell,price='0.41',fee='2.07')
    before=h.ledger.journal(h.account_id)
    evidence=SaleCashEvidence(execution_id=sale_id,before_evidence_id='broker-before-sale',
        after_evidence_id='broker-after-sale',settled_before_usd=D('995899.97'),
        settled_after_usd=D('995899.97'),unsettled_before_usd=D('0'),
        unsettled_after_usd=D('4079.93'))
    command=correction_from_sale_cash(h.state,evidence)
    assert command.changes[0].price_usd==D('.4082')
    h.send(command)
    state=h.state
    assert D(state['cash']['unsettled_usd'])==D('4079.93')
    assert D(state['cash']['realized_gross_usd'])==D('-18')
    assert D(state['cash']['fees_usd'])==D('2.10')
    assert state['executions'][sale_id]['revision']==1
    assert h.ledger.journal(h.account_id)[:len(before)]==before
    assert h.ledger.rebuild(h.account_id)==state


@pytest.mark.parametrize('mutation,reason',[
    ('missing','EXECUTION_EVIDENCE_MISSING'),
    ('settled','BROKER_CASH_EVIDENCE_INCONSISTENT'),
    ('proceeds','BROKER_CASH_EVIDENCE_INCONSISTENT'),
])
def test_sale_cash_correction_missing_or_inconsistent_evidence_fails_closed(h,order_factory,mutation,reason):
    buy=order_factory(h,quantity='10000',price='0.41',stop='0.40');h.trade(buy,price='0.41',fee='0.03')
    sell=order_factory(h,side='SELL_OWNED',quantity='10000',price='0.40',stop='0.40')
    sale_id=h.trade(sell,price='0.41',fee='2.07');state=deepcopy(h.state)
    values=dict(execution_id=sale_id,before_evidence_id='before',after_evidence_id='after',
        settled_before_usd=D('995899.97'),settled_after_usd=D('995899.97'),
        unsettled_before_usd=D('0'),unsettled_after_usd=D('4079.93'))
    if mutation=='missing':values['execution_id']='missing'
    elif mutation=='settled':values['settled_after_usd']=D('995900.97')
    else:values['unsettled_after_usd']=D('3900')
    journal=h.ledger.journal(h.account_id)
    with pytest.raises(ValueError,match=reason):correction_from_sale_cash(state,SaleCashEvidence(**values))
    assert h.ledger.journal(h.account_id)==journal


def test_exact_five_slots_and_hundred_percent(h, order_factory):
    for i in range(5):
        h.admit(order_factory(h, symbol=f"stock-{i}", quantity="2000", stop="99"))
    assert D(h.state["cash"]["reserved_usd"]) == D("1000000")
    assert not risk_violations(h.state)
    with pytest.raises(Rejected, match="POSITION_LIMIT"):
        h.admit(order_factory(h, symbol="stock-5", quantity="1", stop="99"))


def test_aggregate_open_risk_and_episode_risk(h, order_factory):
    # Owner limits 2026-10-16: 10% per position, 20% across positions.
    for i in range(2):
        h.admit(order_factory(h, symbol=f"stock-{i}", quantity="2000", stop="50"))
    with pytest.raises(Rejected, match="OPEN_RISK"):
        h.admit(order_factory(h, symbol="stock-3", quantity="2000", stop="50"))
    with pytest.raises(Rejected, match="EPISODE_RISK"):
        h.admit(order_factory(h, symbol="stock-4", quantity="2000", stop="50"), fee="0.01")


def test_permits_once_stale_and_no_resubmission(h, order_factory):
    order = order_factory(h)
    nonce = h.admit(order)
    h.send(Mark(instrument_id="stock-0", bid_usd="100", observed_at=h.at, source_id="new-event"))
    with pytest.raises(Rejected, match="STALE_PERMIT"):
        h.send(ConsumePermit(nonce=nonce))
    s = h.send(RefreshPermit(intent_id=order.intent_id))
    fresh = s["outbox"][order.intent_id]["nonce"]
    assert fresh != nonce and s["permits"][nonce]["revoked"]
    eid = uuid4().hex
    h.send(ConsumePermit(nonce=fresh), event_id=eid)
    original = h.state
    h.send(ConsumePermit(nonce=fresh), event_id=eid, expected_version=0)
    assert h.state == original
    with pytest.raises(Rejected, match="PERMIT_UNAVAILABLE"):
        h.send(ConsumePermit(nonce=fresh))
    with pytest.raises(Rejected, match="NO_RETRY"):
        h.send(RefreshPermit(intent_id=order.intent_id))
    assert h.state["outbox"][order.intent_id]["status"] == "CLAIMED_UNCERTAIN"
    assert D(h.state["reservations"][order.intent_id]["cash_usd"]) == 10000
    assert not h.state["reconciliation"]["ready"]


@pytest.mark.parametrize("action", ["consume", "refresh"])
def test_pending_entry_quote_must_remain_fresh_at_authority_boundary(h, order_factory, action):
    # Reconciliation is fresh and no stock is held. Only the pending entry's
    # market quote expires between admission and attempted authority use.
    h.at += timedelta(seconds=3599)
    h.reconcile()
    order = order_factory(h)
    nonce = h.admit(order)
    h.at += timedelta(seconds=2)
    command = ConsumePermit(nonce=nonce) if action == "consume" else RefreshPermit(intent_id=order.intent_id)
    with pytest.raises(Rejected, match="STALE_MARKET"):
        h.send(command)
    assert not h.state["permits"][nonce]["consumed"]
    assert h.state["outbox"][order.intent_id]["status"] == "READY"
    assert D(h.state["reservations"][order.intent_id]["cash_usd"]) == 10000


def test_refresh_cannot_reauthorize_buy_after_market_crosses_stop(h, order_factory):
    order = order_factory(h)
    nonce = h.admit(order)
    h.send(Mark(instrument_id="stock-0", bid_usd="94", observed_at=h.at, source_id="crossed-stop"))
    with pytest.raises(Rejected, match="STOP_GEOMETRY"):
        h.send(RefreshPermit(intent_id=order.intent_id))
    assert h.state["outbox"][order.intent_id]["nonce"] == nonce
    assert not h.state["permits"][nonce]["consumed"]


def test_initial_protection_price_respects_instrument_increment(h, order_factory):
    with pytest.raises(Rejected, match="PRICE_INCREMENT"):
        h.admit(order_factory(h, stop="95.001"))
    assert h.state["reservations"] == {}


def test_quarantined_execution_cannot_be_cleared_by_matching_old_balances(h, order_factory):
    order = order_factory(h)
    nonce = h.admit(order)
    h.dispatch(order, nonce)
    event_id = uuid4().hex
    with pytest.raises(Rejected, match="OVERFILL"):
        h.send(Execution(execution_id="impossible-fill", intent_id=order.intent_id,
            quantity="101", price_usd="100", fee_usd="0", executed_at=h.at,
            settlement_date=None, evidence_id="inconsistent-observation"), event_id=event_id)
    assert h.state["incidents"][event_id]["pending_correction"]
    assert h.state["orders"][order.intent_id]["phase"] == "UNKNOWN"
    h.reconcile(resolved_incident_ids=(event_id,))
    assert not h.state["reconciliation"]["ready"]
    assert h.state["incidents"][event_id]["pending_correction"]
    assert D(h.state["reservations"][order.intent_id]["cash_usd"]) == 10000
    assert h.ledger.rebuild(h.account_id) == h.state


@pytest.mark.parametrize("pending", ["CANCEL_PENDING", "REPLACE_PENDING"])
def test_pending_sell_fill_never_releases_overlapping_shares(h, order_factory, pending):
    h.trade(order_factory(h))
    sell = order_factory(h, side="SELL_OWNED")
    nonce = h.admit(sell)
    h.dispatch(sell, nonce)
    h.send(OrderObservation(intent_id=sell.intent_id, status=pending, evidence_id="pending"))
    h.fill(sell, quantity="40")
    assert D(h.state["reservations"][sell.intent_id]["shares"]) == 60
    assert D(h.state["positions"]["stock-0"]["quantity"]) == 60
    h.reconcile()
    with pytest.raises(Rejected, match="SHARES_ALREADY_RESERVED"):
        h.admit(order_factory(h, side="SELL_OWNED", quantity="1"))
    h.finalize(sell, "CANCELED")
    assert sell.intent_id not in h.state["reservations"]


def test_correction_then_bust_preserves_original_history(h, order_factory):
    order = order_factory(h)
    xid = h.trade(order, fee="2")
    original = h.ledger.journal(h.account_id)
    correction = CorrectExecutions(changes=(CorrectedExecution(execution_id=xid, expected_revision=0,
        quantity="80", price_usd="99", fee_usd="1"),), evidence_id="corrected")
    h.send(correction)
    assert D(h.state["cash"]["settled_usd"]) == D("992079")
    assert D(h.state["positions"]["stock-0"]["quantity"]) == 80
    assert not h.state["reconciliation"]["ready"]
    assert h.ledger.journal(h.account_id)[:len(original)] == original
    h.send(CorrectExecutions(changes=(CorrectedExecution(execution_id=xid, expected_revision=1,
        quantity="0", price_usd="99", fee_usd="0"),), evidence_id="bust"))
    assert h.state["positions"] == {}
    assert D(h.state["cash"]["settled_usd"]) == 1000000
    assert D(h.state["episodes"][order.episode_id]["consumed_loss_usd"]) >= 2
    assert h.ledger.rebuild(h.account_id) == h.state


def test_cascade_bust_quarantines_then_batch_resolves(h, order_factory):
    buy = order_factory(h)
    xid = h.trade(buy)
    sell = order_factory(h, side="SELL_OWNED")
    sid = h.trade(sell)
    before = h.state["cash"].copy()
    pending_id = uuid4().hex
    h.send(CorrectExecutions(changes=(CorrectedExecution(execution_id=xid, expected_revision=0,
        quantity="0", price_usd="100", fee_usd="0"),), evidence_id="bust-buy"), event_id=pending_id)
    assert h.state["cash"] == before
    assert h.state["incidents"][pending_id]["pending_correction"]
    h.send(CorrectExecutions(changes=tuple(CorrectedExecution(execution_id=k, expected_revision=0,
        quantity="0", price_usd="100", fee_usd="0") for k in (xid, sid)), evidence_id="paired-bust",
        resolves_correction_ids=(pending_id,), terminal_details=tuple(FinalizeOrder(intent_id=o.intent_id,
            status="CANCELED", execution_ids=(eid,), total_quantity="0", total_fees_usd="0", evidence_id="complete-bust-detail")
            for o, eid in [(buy, xid), (sell, sid)])))
    assert D(h.state["cash"]["settled_usd"]) == 1000000
    assert D(h.state["cash"]["unsettled_usd"]) == 0
    assert not h.state["incidents"][pending_id]["pending_correction"]
    assert h.ledger.rebuild(h.account_id) == h.state


def test_journal_permissions_and_trigger(db_config, h):
    admin, writer = db_config
    for query in ["UPDATE journal SET kind='bad'", "DELETE FROM journal", "TRUNCATE journal"]:
        with pytest.raises(errors.InsufficientPrivilege):
            with writer.connect() as conn:
                conn.execute(query)
        with pytest.raises(errors.RaiseException, match="append-only"):
            with admin.connect() as conn:
                conn.execute(query)


def test_projection_corruption_repaired_from_journal(db_config, h, order_factory):
    h.trade(order_factory(h))
    before = h.state
    with db_config[0].connect() as conn:
        conn.execute("UPDATE accounts SET state='{}' WHERE account_id=%s", (h.account_id,))
        conn.execute("DELETE FROM position_lots WHERE account_id=%s", (h.account_id,))
    with pytest.raises((Rejected, KeyError)):
        h.ledger.snapshot(h.account_id)
    assert h.ledger.rebuild(h.account_id, repair=True) == before
    assert h.state == before
    with db_config[1].connect() as conn:
        assert conn.execute("SELECT count(*) FROM position_lots WHERE account_id=%s", (h.account_id,)).fetchone()[0] == 1


def test_reconciliation_mismatch_cannot_authorize(h, order_factory):
    h.reconcile(settled_usd="999999")
    assert not h.state["reconciliation"]["ready"]
    with pytest.raises(Rejected, match="RECONCILING"):
        h.admit(order_factory(h))
    incident_ids = tuple(h.state["incidents"])
    h.reconcile(resolved_incident_ids=incident_ids)
    assert not h.state["latches"]["incident"]


def test_exact_reconciliation_incident_resolution_preserves_append_only_history(h):
    incident_id='real-account-reconciliation-failed-fixture'
    h.send(Incident(reason='REAL_ACCOUNT_RECONCILIATION_FAILED',evidence_id='failure-evidence'),
        event_id=incident_id)
    before=h.ledger.journal(h.account_id)
    assert incident_id in h.state['incidents'] and h.state['latches']['incident']
    h.reconcile(resolved_incident_ids=(incident_id,),evidence_id='second-flat-success')
    after=h.ledger.journal(h.account_id)
    assert after[:len(before)]==before
    assert any(row['event_id']==incident_id and row['command']['kind']=='INCIDENT' for row in after)
    assert after[-1]['command']['kind']=='RECONCILE'
    assert after[-1]['command']['resolved_incident_ids']==[incident_id]
    assert incident_id not in h.state['incidents'] and not h.state['latches']['incident']
    rebuilt=h.ledger.rebuild(h.account_id)
    assert incident_id not in rebuilt['incidents'] and not rebuilt['latches']['incident']


def _consume_and_cancel(h,order_factory,*,symbol='stock-0'):
    order=_order_for_session(h,order_factory,symbol=symbol);nonce=h.admit(order);h.dispatch(order,nonce)
    h.finalize(order,'CANCELED');h.reconcile();return order


def _order_for_session(h,order_factory,**kwargs):
    order=order_factory(h,**kwargs);session=Session.model_validate(h.state['session'])
    return order.model_copy(update={'deadline':session.liquidation_at})


def _advance_session(h,days):
    old=Session.model_validate(h.state['session']);h.at+=timedelta(days=days)
    h.reconcile()
    session=old.model_copy(update={'label':old.label+timedelta(days=days),
        'opens_at':old.opens_at+timedelta(days=days),'closes_at':old.closes_at+timedelta(days=days)})
    h.send(NewSession(session=session))
    for instrument_id in h.state['instruments']:
        h.send(Mark(instrument_id=instrument_id,bid_usd='100',observed_at=h.at,source_id='session-rollover'))
    h.reconcile()


def test_entry_submission_session_limit_is_durable_across_restart(h,order_factory):
    _consume_and_cancel(h,order_factory,symbol='stock-0');_consume_and_cancel(h,order_factory,symbol='stock-1')
    assert entry_submission_counts(h.state,h.at)==(2,2)
    restarted=type(h)(Ledger(h.ledger.config),h.account_id,h.at,h.token,h.holder)
    third=order_factory(restarted,symbol='stock-2');nonce=restarted.admit(third)
    with pytest.raises(Rejected,match='SESSION_ENTRY_SUBMISSION_LIMIT'):
        restarted.send(ConsumePermit(nonce=nonce))
    assert not restarted.state['permits'][nonce]['consumed']


def test_pre_ceiling_envelope_replays_without_retroactively_rejecting_old_authority(h,order_factory):
    _consume_and_cancel(h,order_factory,symbol='stock-0');_consume_and_cancel(h,order_factory,symbol='stock-1')
    third=_order_for_session(h,order_factory,symbol='stock-2');nonce=h.admit(third);state=h.state
    body=ConsumePermit(nonce=nonce).model_dump(mode='json');server_at=datetime.fromisoformat(state['leader']['last_clock_at'])
    event=h.ledger._event(body,'legacy-pre-ceiling-consume',state['version']+1,h.at,server_at,
        state['leader']['epoch'],h.holder,digest(body))
    event.pop('entry_submission_limit_version')
    replayed=reduce_event(state,event)
    assert replayed['permits'][nonce]['consumed']
    assert entry_submission_counts(replayed,h.at)==(3,3)


def test_concurrent_new_attempts_cannot_cross_session_ceiling(h,order_factory):
    _consume_and_cancel(h,order_factory,symbol='stock-0');_consume_and_cancel(h,order_factory,symbol='stock-1')
    third=_order_for_session(h,order_factory,symbol='stock-2');nonce=h.admit(third);version=h.state['version']
    def attempt(_):
        try:
            h.ledger.execute(h.account_id,ConsumePermit(nonce=nonce),event_id=uuid4().hex,
                expected_version=version,holder=h.holder,token=h.token,at=h.at)
            return 'ACCEPTED'
        except Rejected as exc:return str(exc)
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(attempt,range(2)))
    assert 'SESSION_ENTRY_SUBMISSION_LIMIT' in results
    assert all(result in ('SESSION_ENTRY_SUBMISSION_LIMIT','STALE_ACCOUNT_VERSION') for result in results)
    assert not h.state['permits'][nonce]['consumed']
    assert entry_submission_counts(h.state,h.at)==(2,2)


def test_entry_submission_week_limit_and_rollovers(h,order_factory):
    # Friday belongs to the prior exchange week; Monday starts a fresh count.
    _consume_and_cancel(h,order_factory,symbol='stock-0');_consume_and_cancel(h,order_factory,symbol='stock-1')
    _advance_session(h,3)
    assert entry_submission_counts(h.state,h.at)==(0,0)
    for day in range(5):
        _consume_and_cancel(h,order_factory,symbol='stock-0');_consume_and_cancel(h,order_factory,symbol='stock-1')
        if day<4:_advance_session(h,1)
    assert entry_submission_counts(h.state,h.at)==(2,10)
    extra=_order_for_session(h,order_factory,symbol='stock-2');nonce=h.admit(extra)
    with pytest.raises(Rejected,match='WEEKLY_ENTRY_SUBMISSION_LIMIT'):
        h.send(ConsumePermit(nonce=nonce))
    _advance_session(h,3)
    assert entry_submission_counts(h.state,h.at)==(0,0)
    allowed=_order_for_session(h,order_factory,symbol='stock-2');h.dispatch(allowed,h.admit(allowed))


def test_rejected_and_uncertain_entry_authority_never_resets_count(h,order_factory):
    admitted=order_factory(h,symbol='stock-0');nonce=h.admit(admitted)
    h.finalize(admitted,'REJECTED');h.reconcile()
    assert entry_submission_counts(h.state,h.at)==(0,0)
    first=_consume_and_cancel(h,order_factory,symbol='stock-0')
    uncertain=order_factory(h,symbol='stock-1');nonce=h.admit(uncertain);h.send(ConsumePermit(nonce=nonce))
    assert h.state['orders'][uncertain.intent_id]['phase']=='UNKNOWN'
    h.send(OrderObservation(intent_id=uncertain.intent_id,status='ACKNOWLEDGED',evidence_id='uncertain-recovered'))
    h.finalize(uncertain,'CANCELED');h.reconcile()
    assert entry_submission_counts(h.state,h.at)==(2,2)
    extra=order_factory(h,symbol='stock-2');extra_nonce=h.admit(extra)
    with pytest.raises(Rejected,match='SESSION_ENTRY_SUBMISSION_LIMIT'):
        h.send(ConsumePermit(nonce=extra_nonce))


def test_entry_submission_limit_never_restricts_owned_share_exit(h,order_factory):
    buy=order_factory(h,symbol='stock-0',quantity='1');h.trade(buy)
    _consume_and_cancel(h,order_factory,symbol='stock-1')
    assert entry_submission_counts(h.state,h.at)==(2,2)
    sell=order_factory(h,symbol='stock-0',quantity='1',side='SELL_OWNED')
    nonce=h.admit(sell);h.dispatch(sell,nonce)
    assert h.state['permits'][nonce]['consumed'] and h.state['orders'][sell.intent_id]['phase']=='ACKNOWLEDGED'


def test_entry_submission_count_fails_closed_when_history_is_incomplete(h,order_factory):
    order=_consume_and_cancel(h,order_factory)
    state=h.state;permit=next(value for value in state['permits'].values() if value['intent_id']==order.intent_id)
    del permit['consumed_at']
    with pytest.raises(Rejected,match='ENTRY_SUBMISSION_COUNT_UNAVAILABLE'):
        entry_submission_counts(state,h.at)


def test_cancel_pending_outstanding_mismatch_is_scoped_and_not_multiplied(h,order_factory):
    order=order_factory(h);nonce=h.admit(order);h.dispatch(order,nonce)
    h.send(OrderObservation(intent_id=order.intent_id,status="CANCEL_PENDING",evidence_id="cancel-requested"))
    h.reconcile(outstanding_intent_ids=(),reconciliation_protocol='CANCEL_RECOVERY_V1')
    incident_ids=tuple(h.state['incidents'])
    assert len(incident_ids)==1
    assert h.state['incidents'][incident_ids[0]]['scope']=='CANCEL_PENDING_OUTSTANDING_ONLY'
    assert h.state['incidents'][incident_ids[0]]['intent_ids']==[order.intent_id]
    h.reconcile(outstanding_intent_ids=(),reconciliation_protocol='CANCEL_RECOVERY_V1')
    assert tuple(h.state['incidents'])==incident_ids
    h.finalize(order,'CANCELED')
    h.reconcile(outstanding_intent_ids=(),resolved_incident_ids=incident_ids)
    assert h.state['reconciliation']['ready'] and not h.state['latches']['incident']


def test_cancel_pending_resolution_does_not_clear_unrelated_incident(h,order_factory):
    order=order_factory(h);nonce=h.admit(order);h.dispatch(order,nonce)
    h.send(OrderObservation(intent_id=order.intent_id,status="CANCEL_PENDING",evidence_id="cancel-requested"))
    h.reconcile(outstanding_intent_ids=(),reconciliation_protocol='CANCEL_RECOVERY_V1')
    scoped=next(key for key,value in h.state['incidents'].items() if value.get('scope'))
    unrelated=uuid4().hex;h.send(Incident(reason='PROTECTION_FAILURE',evidence_id='unrelated'),event_id=unrelated)
    h.finalize(order,'CANCELED')
    h.reconcile(outstanding_intent_ids=(),resolved_incident_ids=(scoped,))
    assert set(h.state['incidents'])=={unrelated} and h.state['latches']['incident']


def test_fencing_and_lost_lease_are_durable(h):
    with pytest.raises(Rejected, match="OTHER_WRITER"):
        h.ledger.acquire_leader(h.account_id, "second-worker", event_id=uuid4().hex, at=h.at)
    with pytest.raises(Rejected, match="FENCED"):
        h.send(Incident(reason="test", evidence_id="test"), token=h.token - 1)
    independent = Ledger(h.ledger.config)
    assert independent.snapshot(h.account_id)["leader"]["token"] == h.token


@settings(max_examples=15, derandomize=True, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.lists(st.integers(1, 25), min_size=1, max_size=8))
def test_generated_partial_fill_and_duplicate_sequences(harness_factory, order_factory, steps):
    h = harness_factory(count=1)
    order = order_factory(h, quantity=str(sum(steps)))
    nonce = h.admit(order)
    h.dispatch(order, nonce)
    total = 0
    for step in steps:
        eid, xid = uuid4().hex, uuid4().hex
        command = Execution(execution_id=xid, intent_id=order.intent_id, quantity=str(step), price_usd="99",
            fee_usd="0", executed_at=h.at, settlement_date=None, evidence_id="generated")
        h.send(command, event_id=eid)
        after = h.state
        h.send(command, event_id=eid, expected_version=0)
        assert h.state == after
        total += step
        assert D(after["positions"]["stock-0"]["quantity"]) == total
        assert D(after["cash"]["settled_usd"]) == D(1000000) - D(total) * 99
        assert D(after["cash"]["reserved_usd"]) == D(sum(steps) - total) * 100
    assert h.ledger.rebuild(h.account_id) == h.state


def test_remote_database_configuration_is_not_representable():
    with pytest.raises(ValueError, match="loopback"):
        LocalDatabase(database="x", user="x", host="example.com")
