"""Replay of the live 2026-10-15 day and the 2026-10-16 start (CRWV 3 @66.34 -> 67.12, sale fee $0.01 that the
broker's cash had not deducted). Real controller, gateway, runner and reducer; synthetic broker and gate."""
from concurrent.futures import Future
from datetime import datetime, timezone
from decimal import Decimal as D
import json

import pytest

from autonomous_trader.cash_precision import make_adjuster
from autonomous_trader.contracts import AlphaManagedPlan, AlphaPositionContext
from autonomous_trader.decisions import parse_alpha_decision
from autonomous_trader.real_daily import verify_flat_account
from autonomous_trader.real_runner import RealRunner
from autonomous_trader.real_strategy_gateway import RealExecutionGateway

from lifecycle_harness import ACCOUNT, Broker, Clock, events_store, fixture_gate, instrument, open_account

CHECKPOINT = {'cash': {'settled_usd': '512.41', 'unsettled_usd': '0.00', 'equity_usd': '512.41'}}


def thursday(tmp_path):
    clock = Clock(datetime(2026, 10, 15, 17, 10, tzinfo=timezone.utc))
    inst = instrument()
    journal, controller = open_account(tmp_path, clock, '512.41', inst)
    broker = Broker(inst, clock, '512.41')
    broker.cash_fee_lag = True
    controller.refresh(broker.refresh(controller.state))
    gate = fixture_gate()
    events, store = events_store()
    gateway = RealExecutionGateway(gate, controller, broker, broker, store, clock)
    runner = RealRunner(controller, broker, gateway, gate, store, lambda: None, clock)
    runner.context = lambda packet: packet.context
    ctx = broker.refresh().context
    decision = parse_alpha_decision(json.dumps({'schema_version': '1.0', 'action': 'OPEN', 'reason': 'replay',
        'open': {'instrument_id': 'crwv', 'symbol': 'CRWV', 'strategy_label': 'replay', 'thesis': 'replay of 10-15',
                 'desired_quantity': '3', 'desired_notional_usd': None, 'entry_limit_usd': '66.34',
                 'hard_stop_usd': '65.00', 'target_usd': None,
                 'entry_expires_at': '2026-10-15T17:12:00+00:00', 'next_review_at': '2026-10-15T17:30:00+00:00',
                 'latest_exit_at': '2026-10-15T19:00:00+00:00'}, 'hold': None, 'modify': None, 'close': None}))
    future = Future()
    future.set_result({'decision': decision.model_dump(mode='json'), 'model': 'replay', 'reasoning': 'r',
                       'usage': {'input_tokens': 0, 'output_tokens': 0}, 'estimated_cost_usd': '0',
                       'model_work_id': 'replay', 'latency_ms': 0})
    runner.pending = (future, ctx, 'ENTRY', None)
    runner._apply(broker.refresh())
    buy = broker.sent[0]
    gateway.reconcile_order(buy, 'CRWV', broker.details[buy.client_order_id]['orders'][0]['order_id'])
    plan = AlphaManagedPlan.model_validate(next(p['plan'] for k, p in events if k == 'REAL_PLAN'))
    clock.advance(minutes=61)
    broker.bid, broker.ask = D('67.12'), D('67.13')
    controller.refresh(broker.refresh())
    pos = AlphaPositionContext(position_id='position-crwv', instrument_id='crwv', symbol='CRWV', quantity='3',
                               average_entry_usd='66.34', mark_usd='67.12', active_stop_usd='65.00',
                               latest_exit_at=plan.latest_exit_at, unrealized_pnl_usd='2.34', target_usd=None)
    current = broker.refresh().context.model_copy(update={'positions': (pos,)})
    future = Future()
    future.set_result({'decision': {'schema_version': '1.0', 'action': 'CLOSE', 'reason': 'replay sale',
                                    'open': None, 'hold': None, 'modify': None,
                                    'close': {'position_id': 'position-crwv', 'full_close': True}}})
    runner.pending = (future, current, 'POSITION', plan.plan_id)
    runner.plan = lambda _: plan
    runner.context = lambda _: current
    # r7v behaviour: the trader explains the unposted sale fee instead of failing reconciliation.
    broker.cash_adjuster = make_adjuster(controller, broker, clock)
    runner._apply(broker.refresh())
    sell = broker.sent[-1]
    gateway.reconcile_order(sell, 'CRWV', broker.details[sell.client_order_id]['orders'][0]['order_id'])
    runner.worker.shutdown(wait=True)
    s = controller.state
    assert (s['cash']['settled_usd'], s['cash']['unsettled_usd']) == ('313.39', '201.35')
    # Post-close: exact cash check fails on the unposted fee -> the live incident.
    controller.incident('REAL_POST_CLOSE_CASH_SETTLEMENT_MISMATCH')
    broker.cash_adjuster = None
    return clock, controller, broker, runner


def test_thursday_numbers_match_live_ledger(tmp_path):
    clock, controller, broker, runner = thursday(tmp_path)
    assert (broker.settled, broker.unsettled) == (D('313.39'), D('201.36'))
    assert controller.state['latches']['incident'] is True


def friday(tmp_path):
    clock, controller, broker, runner = thursday(tmp_path)
    clock.at = datetime(2026, 10, 16, 13, 45, tzinfo=timezone.utc)
    broker.settle_due()
    assert (broker.settled, broker.unsettled) == (D('514.75'), D('0'))
    return clock, controller, broker, runner


def authority(state):
    return {'account_binding': ACCOUNT, 'scope': {'risk_policy_sha256': state['policy_hash']}}


def test_friday_daily_preparation_accepts_broker_truth_without_the_fee_explanation(tmp_path):
    """Owner 2026-10-18 (broker-truth): the unexplained cent no longer refuses the start; it is adopted."""
    clock, controller, broker, runner = friday(tmp_path)
    proof = verify_flat_account(controller.state, broker.refresh(), authority(controller.state), clock())
    assert {k: D(v) for k, v in proof['broker_truth'].items()} == {'settled_delta': D('0.01'), 'unsettled_delta': D(0)}
    controller.refresh(broker.refresh())
    s = controller.state
    assert (D(s['cash']['settled_usd']), D(s['cash']['unsettled_usd'])) == (D('514.75'), D('0'))
    assert s['reconciliation']['ready']
    assert [(D(a['settled']), D(a['unsettled'])) for a in s['cash_adoptions'].values()] == [(D('0.01'), D(0))]


def test_friday_start_settles_and_clears_the_incident(tmp_path):
    clock, controller, broker, runner = friday(tmp_path)
    broker.cash_adjuster = make_adjuster(controller, broker, clock)
    proof = verify_flat_account(controller.state, broker.refresh(), authority(controller.state), clock())
    assert proof['settled_usd'] == '514.74' and proof['incident_ids']
    assert broker.last_cash_variance['variance_usd'] == '0.01'
    controller.refresh(broker.refresh())
    s = controller.state
    assert (D(s['cash']['settled_usd']), D(s['cash']['unsettled_usd'])) == (D('514.74'), D('0'))
    runner._single_writer = lambda: True
    runner.authority_receipt = {'account_binding': ACCOUNT, 'market_session_label': '2026-10-16'}
    assert runner._record_reconciliation_success(broker.refresh(), CHECKPOINT) is False
    clock.advance(seconds=2)
    assert runner._record_reconciliation_success(broker.refresh(), CHECKPOINT) is True
    assert controller.state['incidents'] == {} and controller.state['latches']['incident'] is False


def test_untraded_ledger_still_binds_to_the_checkpoint(tmp_path):
    from autonomous_trader.real_waiting import resolution_baseline
    state = {'orders': {}, 'cash': {'settled_usd': '1', 'unsettled_usd': '0', 'liquidation_equity_usd': '1'}}
    assert resolution_baseline(state, CHECKPOINT) == (D('512.41'), D('0.00'), D('512.41'))
    assert resolution_baseline(state, {}) is None


def test_an_unexplained_difference_is_adopted_and_logged(tmp_path):
    clock, controller, broker, runner = friday(tmp_path)
    broker.settled += D('0.01')
    broker.cash_adjuster = make_adjuster(controller, broker, clock)
    verify_flat_account(controller.state, broker.refresh(), authority(controller.state), clock())
    controller.refresh(broker.refresh())
    s = controller.state
    assert (D(s['cash']['settled_usd']), D(s['cash']['unsettled_usd'])) == (broker.settled, broker.unsettled)
    assert s['reconciliation']['ready']
