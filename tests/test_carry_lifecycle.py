"""Multi-day holding (owner decision 2026-10-14) through the real controller, gateway, runner and reducer.

Synthetic broker/gate (lifecycle_harness). Carry is switched on by the charter scope only; with the switch
off the original same-day behaviour must be unchanged."""
from concurrent.futures import Future
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
import json

import pytest

from autonomous_trader import real_daily
from autonomous_trader.carry import exit_horizon, future_sessions, MAX_HOLD_SESSIONS
from autonomous_trader.contracts import AlphaManagedPlan
from autonomous_trader.decisions import parse_alpha_decision, validate_alpha_semantics, DecisionError
from autonomous_trader.durable.projection import Rejected
from autonomous_trader.mvp.config import reviewed_calendar
from autonomous_trader.real_configuration import desired_configuration
from autonomous_trader.real_daily import verify_flat_account
from autonomous_trader.real_runner import RealRunner
from autonomous_trader.real_strategy_gateway import RealExecutionGateway

from lifecycle_harness import ACCOUNT, Broker, Clock, events_store, fixture_gate, instrument, open_account

MON = datetime(2026, 10, 19, 15, 0, tzinfo=timezone.utc)


@pytest.fixture
def carry_on(monkeypatch):
    monkeypatch.setattr(real_daily, 'OVERNIGHT_HOLDING', True)


def authority(state, overnight=True):
    return {'authority_id': 'charter-test', 'account_binding': ACCOUNT,
            'scope': {'risk_policy_sha256': state['policy_hash'], 'overnight': overnight}}


def world(tmp_path, *, configure=True, overnight=True):
    clock = Clock(MON)
    inst = instrument('abc', 'ABC')
    journal, controller = open_account(tmp_path, clock, '500', inst)
    broker = Broker(inst, clock, '500')
    broker.bid, broker.ask = D('10.00'), D('10.01')
    broker.fee = {'BUY': D('0'), 'SELL_OWNED': D('0.01')}
    if configure:
        controller.lease()
        controller.send(desired_configuration(authority(controller.state, overnight), controller.state, clock()))
    controller.refresh(broker.refresh(controller.state))
    gate = fixture_gate()
    events, store = events_store()
    gateway = RealExecutionGateway(gate, controller, broker, broker, store, clock)
    runner = RealRunner(controller, broker, gateway, gate, store, lambda: None, clock)
    plans = {}

    def plan(plan_id):
        found = [p['plan'] for k, p in events if k == 'REAL_PLAN' and p['plan_id'] == plan_id]
        return AlphaManagedPlan.model_validate(found[-1])
    runner.plan = plan
    runner.context = lambda packet: packet.context
    return clock, controller, broker, runner, gateway, events


def open_position(clock, controller, broker, runner, gateway, latest_exit, stop='9.60', qty='5'):
    session = reviewed_calendar().session(clock().date())
    ctx = broker.refresh(horizon=exit_horizon(session)).context
    decision = parse_alpha_decision(json.dumps({'schema_version': '1.0', 'action': 'OPEN', 'reason': 'multi-day',
        'open': {'instrument_id': 'abc', 'symbol': 'ABC', 'strategy_label': 'oversold_bounce_in_uptrend',
                 'thesis': 'oversold in uptrend; hold for the bounce', 'desired_quantity': qty,
                 'desired_notional_usd': None, 'entry_limit_usd': '10.01', 'hard_stop_usd': stop, 'target_usd': '12.00',
                 'entry_expires_at': (clock() + timedelta(minutes=2)).isoformat(),
                 'next_review_at': (clock() + timedelta(minutes=30)).isoformat(),
                 'latest_exit_at': latest_exit.isoformat()}, 'hold': None, 'modify': None, 'close': None}))
    validate_alpha_semantics(decision, ctx, clock(), maximum_quote_age_ms=10000)
    future = Future()
    future.set_result({'decision': decision.model_dump(mode='json'), 'model': 't', 'reasoning': 'r',
                       'usage': {'input_tokens': 0, 'output_tokens': 0}, 'estimated_cost_usd': '0',
                       'model_work_id': 't', 'latency_ms': 0})
    runner.pending = (future, ctx, 'ENTRY', None)
    runner.context = lambda packet: packet.context.model_copy(update={'exit_horizon_at': exit_horizon(session)})
    runner._apply(broker.refresh(horizon=exit_horizon(session)))
    buy = broker.sent[-1]
    gateway.reconcile_order(buy, 'ABC', broker.details[buy.client_order_id]['orders'][0]['order_id'])
    return buy


def test_horizon_is_charter_bound(monkeypatch):
    monkeypatch.setattr(real_daily, 'OVERNIGHT_HOLDING', False)
    session = reviewed_calendar().session(MON.date())
    assert exit_horizon(session) == session.liquidation_at          # switch off: same day only
    monkeypatch.setattr(real_daily, 'OVERNIGHT_HOLDING', True)
    later = future_sessions(session.label, MAX_HOLD_SESSIONS)
    assert len(later) == MAX_HOLD_SESSIONS and exit_horizon(session) == later[-1].liquidation_at


def test_model_cannot_exceed_horizon_or_hold_overnight_without_switch(tmp_path):
    clock, controller, broker, runner, gateway, events = world(tmp_path, configure=False)
    session = reviewed_calendar().session(clock().date())
    tomorrow = future_sessions(session.label, 1)[0].liquidation_at
    with pytest.raises(Exception):
        open_position(clock, controller, broker, runner, gateway, tomorrow)
    assert broker.sent == []


def test_ledger_rejects_a_deadline_beyond_its_configured_horizon(tmp_path, carry_on):
    clock, controller, broker, runner, gateway, events = world(tmp_path, configure=False)
    session = reviewed_calendar().session(clock().date())
    with pytest.raises(Exception):   # validator horizon is open (switch on) but the ledger has carry_sessions 0
        open_position(clock, controller, broker, runner, gateway, future_sessions(session.label, 3)[-1].liquidation_at)
    assert all(o['phase'] != 'FILLED' for o in controller.state['orders'].values())


def test_position_is_carried_overnight_reconciled_and_managed_next_day(tmp_path, carry_on):
    clock, controller, broker, runner, gateway, events = world(tmp_path)
    session = reviewed_calendar().session(clock().date())
    deadline = future_sessions(session.label, 3)[-1].liquidation_at
    open_position(clock, controller, broker, runner, gateway, deadline)
    assert D(controller.state['positions']['abc']['quantity']) == 5
    # Liquidation time today: no same-day sale for a multi-day plan.
    clock.at = session.liquidation_at + timedelta(seconds=30)
    broker.bid, broker.ask = D('10.10'), D('10.11')
    controller.refresh(broker.refresh())
    assert runner._monitor_owned_safety(session) == []
    assert len(broker.sent) == 1
    # After the close: exact carried reconciliation lets the day close cleanly.
    clock.at = session.closes_at + timedelta(minutes=2)
    assert runner.step() == 'CLOSED_RECONCILED'
    assert controller.state['positions'] and not controller.state['incidents']
    # Next morning: preparation accepts the carried position, and the first step reviews it.
    nxt = future_sessions(session.label, 1)[0]
    clock.at = nxt.opens_at + timedelta(minutes=1)
    packet = broker.refresh(horizon=exit_horizon(nxt))
    proof = verify_flat_account(controller.state, packet, authority(controller.state), clock())
    assert proof['positions'] == 1
    controller.refresh(packet)
    assert controller.state['session']['label'] == nxt.label.isoformat()
    plan = runner._position_plan('abc')
    assert plan.next_review_at < nxt.opens_at       # => review due at the open
    runner.worker.shutdown(wait=True)


def test_overnight_gap_below_stop_exits_at_first_quote(tmp_path, carry_on):
    clock, controller, broker, runner, gateway, events = world(tmp_path)
    session = reviewed_calendar().session(clock().date())
    open_position(clock, controller, broker, runner, gateway, future_sessions(session.label, 3)[-1].liquidation_at)
    nxt = future_sessions(session.label, 1)[0]
    clock.at = nxt.opens_at + timedelta(seconds=5)
    broker.bid, broker.ask = D('9.20'), D('9.22')     # gapped below the 9.60 stop
    controller.refresh(broker.refresh(horizon=exit_horizon(nxt)))
    assert runner._monitor_owned_safety(nxt) == ['PROTECTIVE_STOP']
    assert broker.sent[-1].side == 'SELL_OWNED' and broker.sent[-1].order_type == 'MARKET'
    runner.worker.shutdown(wait=True)


def test_multi_day_plan_exits_at_its_own_deadline(tmp_path, carry_on):
    clock, controller, broker, runner, gateway, events = world(tmp_path)
    session = reviewed_calendar().session(clock().date())
    later = future_sessions(session.label, 2)
    deadline = later[0].opens_at + timedelta(hours=2)
    open_position(clock, controller, broker, runner, gateway, deadline)
    clock.at = later[0].opens_at + timedelta(hours=1)
    controller.refresh(broker.refresh(horizon=exit_horizon(later[0])))
    assert runner._monitor_owned_safety(later[0]) == []
    clock.at = deadline + timedelta(seconds=1)
    controller.refresh(broker.refresh(horizon=exit_horizon(later[0])))
    assert runner._monitor_owned_safety(later[0]) == ['MANDATORY_SAME_DAY_EXIT']
    runner.worker.shutdown(wait=True)


def test_without_the_switch_a_position_at_the_close_is_still_an_incident(tmp_path):
    clock, controller, broker, runner, gateway, events = world(tmp_path, configure=False)
    session = reviewed_calendar().session(clock().date())
    open_position(clock, controller, broker, runner, gateway, session.liquidation_at - timedelta(minutes=10))
    clock.at = session.closes_at + timedelta(minutes=2)
    assert runner.step() == 'REAL_EXPOSURE_OUTSIDE_SESSION'
    runner.worker.shutdown(wait=True)


def test_carried_position_mismatch_blocks_next_open(tmp_path, carry_on):
    clock, controller, broker, runner, gateway, events = world(tmp_path)
    session = reviewed_calendar().session(clock().date())
    open_position(clock, controller, broker, runner, gateway, future_sessions(session.label, 3)[-1].liquidation_at)
    nxt = future_sessions(session.label, 1)[0]
    clock.at = nxt.opens_at + timedelta(minutes=1)
    broker.quantity = D('4')                         # broker shows one share fewer
    with pytest.raises(ValueError, match='CARRY_POSITION_MISMATCH'):
        verify_flat_account(controller.state, broker.refresh(), authority(controller.state), clock())
    runner.worker.shutdown(wait=True)


def close_decision(runner, plan, current):
    future = Future()
    future.set_result({'decision': {'schema_version': '1.0', 'action': 'CLOSE', 'reason': 'thesis done',
                                    'open': None, 'hold': None, 'modify': None,
                                    'close': {'position_id': 'position-abc', 'full_close': True}}})
    runner.pending = (future, current, 'POSITION', plan.plan_id)


def position_context(broker, controller, plan):
    from autonomous_trader.contracts import AlphaPositionContext
    p = controller.state['positions']['abc']
    pos = AlphaPositionContext(position_id='position-abc', instrument_id='abc', symbol='ABC', quantity=p['quantity'],
                               average_entry_usd=D(p['basis_usd']) / D(p['quantity']), mark_usd=broker.bid,
                               active_stop_usd=plan.active_stop_usd, latest_exit_at=plan.latest_exit_at,
                               unrealized_pnl_usd='0', target_usd=plan.target_usd)
    return broker.refresh().context.model_copy(update={'positions': (pos,)})


def test_model_close_is_a_limit_at_the_bid_repeated_then_market(tmp_path):
    clock, controller, broker, runner, gateway, events = world(tmp_path, configure=False)
    session = reviewed_calendar().session(clock().date())
    open_position(clock, controller, broker, runner, gateway, session.liquidation_at - timedelta(minutes=30))
    broker.rest_limit_sells = True
    broker.bid, broker.ask = D('10.30'), D('10.31')
    plan = runner._position_plan('abc')
    current = position_context(broker, controller, plan)
    close_decision(runner, plan, current)
    runner.context = lambda _: current
    runner._apply(broker.refresh())
    for attempt in range(1, 4):
        sell = broker.sent[-1]
        assert sell.order_type == 'LIMIT' and sell.limit_price_usd == broker.bid
        assert runner._planned_exit(runner._position_plan('abc'), 'x') is None      # one attempt in flight
        broker.cancel(sell.client_order_id)
        gateway.reconcile_order(sell, 'ABC', broker.details[sell.client_order_id]['orders'][0]['order_id'])
        plan = runner._position_plan('abc')
        assert plan.exit_attempts == attempt and plan.exit_request.startswith('MODEL_CLOSE')
        clock.advance(seconds=61)
        controller.refresh(broker.refresh())
        assert runner._monitor_owned_safety(session) == ['PLANNED_EXIT']
    assert broker.sent[-1].order_type == 'MARKET'
    runner.worker.shutdown(wait=True)


def test_add_shares_keeps_the_active_stop_and_respects_the_episode_risk(tmp_path):
    clock, controller, broker, runner, gateway, events = world(tmp_path, configure=False)
    session = reviewed_calendar().session(clock().date())
    open_position(clock, controller, broker, runner, gateway, session.liquidation_at - timedelta(minutes=30),
                  qty='3')
    broker.bid, broker.ask = D('9.80'), D('9.81')
    controller.refresh(broker.refresh())
    plan = runner._position_plan('abc')
    current = position_context(broker, controller, plan)

    def add(stop, qty='2'):
        decision = {'schema_version': '1.0', 'action': 'OPEN', 'reason': 'average down, thesis intact',
            'open': {'instrument_id': 'abc', 'symbol': 'ABC', 'strategy_label': 'averaging_down',
                     'thesis': 'same thesis, better price', 'desired_quantity': qty, 'desired_notional_usd': None,
                     'entry_limit_usd': '9.81', 'hard_stop_usd': stop, 'target_usd': '12.00',
                     'entry_expires_at': (clock() + timedelta(minutes=2)).isoformat(),
                     'next_review_at': (clock() + timedelta(minutes=20)).isoformat(),
                     'latest_exit_at': (clock() + timedelta(minutes=40)).isoformat()},
            'hold': None, 'modify': None, 'close': None}
        future = Future()
        future.set_result({'decision': decision, 'estimated_cost_usd': '0', 'model_work_id': 'w'})
        runner.pending = (future, current, 'POSITION', plan.plan_id)
        runner.context = lambda _: current
        runner._apply(broker.refresh())
    sent = len(broker.sent)
    add('9.50')                                   # stop differs from the active 9.60 -> rejected, no order
    assert len(broker.sent) == sent
    add(str(plan.active_stop_usd))
    buy = broker.sent[-1]
    assert buy.side == 'BUY' and buy.protection.initial_stop_usd == plan.active_stop_usd
    gateway.reconcile_order(buy, 'ABC', broker.details[buy.client_order_id]['orders'][0]['order_id'])
    assert D(controller.state['positions']['abc']['quantity']) == 5
    assert runner._position_plan('abc').plan_id == plan.plan_id          # managed by the original plan
    runner.worker.shutdown(wait=True)


def test_reaching_the_target_asks_the_model_once_per_plan_version(tmp_path):
    clock, controller, broker, runner, gateway, events = world(tmp_path, configure=False)
    session = reviewed_calendar().session(clock().date())
    open_position(clock, controller, broker, runner, gateway, session.liquidation_at - timedelta(minutes=30))
    broker.bid, broker.ask = D('12.05'), D('12.06')
    started = []
    runner._start = lambda context, mode, plan_id=None: started.append((mode, plan_id))
    runner._entry_slot = lambda *a: (None, None)
    for _ in range(2):
        runner.pending = None
        runner._advance_packet(broker.refresh(), session, prepared=True)
    assert started == [('POSITION', runner._position_plan('abc').plan_id)]
    assert any(k == 'REAL_POSITION_EXCURSION' for k, _ in events)
    runner.worker.shutdown(wait=True)


def test_snap_exit_puts_every_deadline_inside_a_session(carry_on):
    from autonomous_trader.carry import snap_exit
    session = reviewed_calendar().session(MON.date())
    nxt = future_sessions(session.label, 1)[0]
    assert snap_exit(session.closes_at, session) == session.liquidation_at            # 16:00 today -> 15:55
    assert snap_exit(session.closes_at + timedelta(hours=8), session) == session.liquidation_at   # overnight
    inside = nxt.opens_at + timedelta(hours=1)
    assert snap_exit(inside, session) == inside
    assert snap_exit(nxt.closes_at, session) == nxt.liquidation_at


def test_refused_add_never_crashes_and_consumes_the_review(tmp_path):
    clock, controller, broker, runner, gateway, events = world(tmp_path, configure=False)
    session = reviewed_calendar().session(clock().date())
    open_position(clock, controller, broker, runner, gateway, session.liquidation_at - timedelta(minutes=5), qty='3')
    plan = runner._position_plan('abc')
    clock.at = session.liquidation_at - timedelta(minutes=10)          # inside the close-lifecycle margin
    broker.bid, broker.ask = D('9.80'), D('9.81')
    controller.refresh(broker.refresh())
    current = position_context(broker, controller, plan)
    decision = {'schema_version': '1.0', 'action': 'OPEN', 'reason': 'late add',
        'open': {'instrument_id': 'abc', 'symbol': 'ABC', 'strategy_label': 'averaging_down', 'thesis': 't',
                 'desired_quantity': '2', 'desired_notional_usd': None, 'entry_limit_usd': '9.81',
                 'hard_stop_usd': str(plan.active_stop_usd), 'target_usd': None,
                 'entry_expires_at': (clock() + timedelta(minutes=1)).isoformat(),
                 'next_review_at': (clock() + timedelta(minutes=2)).isoformat(),
                 'latest_exit_at': (clock() + timedelta(minutes=4)).isoformat()},
        'hold': None, 'modify': None, 'close': None}
    future = Future()
    future.set_result({'decision': decision, 'estimated_cost_usd': '0', 'model_work_id': 'w'})
    runner.pending = (future, current, 'POSITION', plan.plan_id)
    runner.context = lambda _: current
    sent = len(broker.sent)
    runner._apply(broker.refresh())                                     # must not raise
    assert len(broker.sent) == sent
    assert any(k == 'REAL_PAID_DECISION_VALIDATION' and p.get('reason') == 'REAL_ADD_OUTSIDE_ENTRY_WINDOW'
               for k, p in events)
    assert runner._position_plan('abc').next_review_at > clock()
    runner.worker.shutdown(wait=True)


def test_protective_stop_cancels_a_resting_planned_limit_first(tmp_path):
    clock, controller, broker, runner, gateway, events = world(tmp_path, configure=False)
    session = reviewed_calendar().session(clock().date())
    open_position(clock, controller, broker, runner, gateway, session.liquidation_at - timedelta(minutes=30))
    broker.rest_limit_sells = True
    broker.transport_cancel = []

    def cancel_for_safety(intent, controller_, gate, packet, reason):
        broker.transport_cancel.append(reason)
        broker.cancel(intent.client_order_id)
    broker.cancel_planned_sell_for_safety = cancel_for_safety
    broker.bid, broker.ask = D('10.30'), D('10.31')
    plan = runner._position_plan('abc')
    current = position_context(broker, controller, plan)
    close_decision(runner, plan, current)
    runner.context = lambda _: current
    runner._apply(broker.refresh())
    assert broker.sent[-1].order_type == 'LIMIT'
    broker.rest_limit_sells = False
    broker.bid, broker.ask = D('9.55'), D('9.56')                    # through the 9.60 stop
    controller.refresh(broker.refresh())
    assert runner._monitor_owned_safety(session) == ['PROTECTIVE_STOP']
    assert broker.transport_cancel == ['PROTECTIVE_STOP'] and broker.sent[-1].order_type == 'MARKET'
    runner.worker.shutdown(wait=True)


def test_target_review_is_not_repeated_after_a_hold_that_keeps_the_target(tmp_path):
    clock, controller, broker, runner, gateway, events = world(tmp_path, configure=False)
    session = reviewed_calendar().session(clock().date())
    open_position(clock, controller, broker, runner, gateway, session.liquidation_at - timedelta(minutes=30))
    broker.bid, broker.ask = D('12.05'), D('12.06')
    started = []
    runner._start = lambda context, mode, plan_id=None: started.append(mode)
    runner._entry_slot = lambda *a: (None, None)
    runner.pending = None
    runner._advance_packet(broker.refresh(), session, prepared=True)
    plan = runner._position_plan('abc')                     # simulate a HOLD: version bump, same target
    events.append(('REAL_PLAN', dict(plan_id=plan.plan_id, original=False, plan=AlphaManagedPlan.model_validate(
        {**plan.model_dump(), 'version': plan.version + 1}).model_dump(mode='json'))))
    runner.pending = None
    runner._advance_packet(broker.refresh(), session, prepared=True)
    assert started == ['POSITION']
    runner.worker.shutdown(wait=True)


def test_flat_recovery_opens_the_new_session_so_yesterdays_daily_latch_resets(tmp_path):
    clock, controller, broker, runner, gateway, events = world(tmp_path, configure=False)
    controller.incident('X') if False else None
    state = controller.state
    nxt = future_sessions(reviewed_calendar().session(clock().date()).label, 1)[0]
    clock.at = nxt.opens_at + timedelta(minutes=1)
    controller.refresh_flat_recovery({**broker.account_observation(), 'evidence_id': 'e1'})
    assert controller.state['session']['label'] == nxt.label.isoformat()
    runner.worker.shutdown(wait=True)


def test_overnight_gap_does_not_trip_the_daily_loss_latch(tmp_path, carry_on):
    clock, controller, broker, runner, gateway, events = world(tmp_path)
    session = reviewed_calendar().session(clock().date())
    open_position(clock, controller, broker, runner, gateway, future_sessions(session.label, 3)[-1].liquidation_at,
                  stop='9.60', qty='9')
    nxt = future_sessions(session.label, 1)[0]
    clock.at = nxt.opens_at + timedelta(seconds=5)
    broker.bid, broker.ask = D('7.10'), D('7.11')      # 29% gap on the position = ~5.2% of equity
    controller.refresh(broker.refresh(horizon=exit_horizon(nxt)))
    s = controller.state
    assert D(s['day_start_equity']) < D('480')          # the gap is in the starting equity, not today's loss
    assert s['latches']['daily'] is False
    assert D(s['day_start_equity']) == D(s['cash']['liquidation_equity_usd'])
    assert runner._monitor_owned_safety(nxt) == ['PROTECTIVE_STOP']     # the stop still fires on the gap
    runner.worker.shutdown(wait=True)


def test_exit_authority_survives_a_missing_session_certificate_but_only_in_session(monkeypatch):
    from autonomous_trader import real_trading, real_daily
    gate = real_trading.RealGate.__new__(real_trading.RealGate)
    gate.root, gate.account = '/nonexistent', ACCOUNT
    def no_cert(*a, **k):
        raise ValueError('REAL_DAILY_CERTIFICATE_INVALID')
    monkeypatch.setattr(real_trading.RealGate, 'offline_receipt', staticmethod(no_cert))
    monkeypatch.setattr(real_daily, 'load_authority', lambda root, now: {'account_binding': ACCOUNT,
        'build_sha256': 'b', 'flat_checkpoint_sha256': 'c', 'scope': {}})
    session = reviewed_calendar().session(MON.date())
    receipt = gate._protective_receipt(session.opens_at + timedelta(hours=1))
    assert receipt['scope'] == 'REAL_PROTECTIVE_EXIT_V1' and receipt['verdict'] == 'GO'
    with pytest.raises(ValueError):
        gate._protective_receipt(session.closes_at + timedelta(hours=1))
    def revoked(root, now):
        raise ValueError('REAL_PERSISTENT_AUTHORITY_INVALID')
    monkeypatch.setattr(real_daily, 'load_authority', revoked)
    with pytest.raises(ValueError):                      # a revoked charter stops even exits from this path
        gate._protective_receipt(session.opens_at + timedelta(hours=1))


def test_unreconcilable_close_stops_after_bounded_retries(tmp_path):
    clock, controller, broker, runner, gateway, events = world(tmp_path, configure=False)
    session = reviewed_calendar().session(clock().date())
    clock.at = session.closes_at + timedelta(minutes=1)
    # Broker-truth (2026-10-18) adopts cash differences; a position the ledger does not own still cannot reconcile.
    broker.quantity = D('1')
    results = [runner.step() for _ in range(10)]
    assert results[0] not in ('CLOSED_RECONCILED', 'CLOSED_UNRECONCILED') and 'CLOSED_UNRECONCILED' not in results
    clock.advance(minutes=91)                                  # broker records get a 90-minute window
    assert runner.step() == 'CLOSED_UNRECONCILED'
    runner.worker.shutdown(wait=True)


def test_yesterdays_daily_latch_never_sells_today(tmp_path, carry_on):
    clock, controller, broker, runner, gateway, events = world(tmp_path)
    session = reviewed_calendar().session(clock().date())
    open_position(clock, controller, broker, runner, gateway, future_sessions(session.label, 3)[-1].liquidation_at)
    state = controller.state
    state['latches']['daily'] = True                           # journaled yesterday
    runner.controller = type('C', (), {'state': state, 'clock': controller.clock})()
    nxt = future_sessions(session.label, 1)[0]
    clock.at = nxt.opens_at + timedelta(minutes=1)
    assert runner._monitor_owned_safety(nxt) == []
    assert len(broker.sent) == 1
    runner.worker.shutdown(wait=True)


def test_close_decision_near_the_close_is_kept_for_the_next_open(tmp_path, carry_on):
    clock, controller, broker, runner, gateway, events = world(tmp_path)
    session = reviewed_calendar().session(clock().date())
    open_position(clock, controller, broker, runner, gateway, future_sessions(session.label, 3)[-1].liquidation_at)
    clock.at = session.closes_at - timedelta(minutes=2)
    controller.refresh(broker.refresh())
    plan = runner._position_plan('abc')
    current = position_context(broker, controller, plan)
    close_decision(runner, plan, current)
    runner.context = lambda _: current
    runner._apply(broker.refresh())
    assert len(broker.sent) == 1                               # nothing placed across the close
    assert runner._position_plan('abc').exit_request.startswith('MODEL_CLOSE')
    runner.worker.shutdown(wait=True)


def test_protection_only_opens_the_session_from_exit_packets_and_can_sell(tmp_path, carry_on):
    clock, controller, broker, runner, gateway, events = world(tmp_path)
    session = reviewed_calendar().session(clock().date())
    open_position(clock, controller, broker, runner, gateway, future_sessions(session.label, 3)[-1].liquidation_at)
    nxt = future_sessions(session.label, 1)[0]
    clock.at = nxt.opens_at + timedelta(minutes=2)
    def no_certificate(root, now):
        raise ValueError('REAL_DAILY_CERTIFICATE_INVALID')
    runner.gate.offline_receipt = no_certificate
    runner.gate.root = 'x'
    broker.bid, broker.ask = D('9.50'), D('9.51')                     # below the 9.60 stop
    assert runner.step() == 'SAFETY_EXITS_SUBMITTED'
    assert controller.state['session']['label'] == nxt.label.isoformat()
    assert controller.protection_only and broker.sent[-1].order_type == 'MARKET'
    runner.worker.shutdown(wait=True)


def test_validator_refuses_an_overnight_plan_without_high_upside(tmp_path, carry_on):
    clock, controller, broker, runner, gateway, events = world(tmp_path)
    session = reviewed_calendar().session(clock().date())
    ctx = broker.refresh(horizon=exit_horizon(session)).context
    def decision(target):
        return parse_alpha_decision(json.dumps({'schema_version': '1.0', 'action': 'OPEN', 'reason': 'r',
            'open': {'instrument_id': 'abc', 'symbol': 'ABC', 'strategy_label': 's', 'thesis': 't',
                     'desired_quantity': '5', 'desired_notional_usd': None, 'entry_limit_usd': '10.01',
                     'hard_stop_usd': '9.60', 'target_usd': target,
                     'entry_expires_at': (clock() + timedelta(minutes=2)).isoformat(),
                     'next_review_at': (clock() + timedelta(minutes=30)).isoformat(),
                     'latest_exit_at': future_sessions(session.label, 2)[-1].liquidation_at.isoformat()},
            'hold': None, 'modify': None, 'close': None}))
    with pytest.raises(DecisionError, match='high upside'):
        validate_alpha_semantics(decision('10.30'), ctx, clock(), maximum_quote_age_ms=10000)
    validate_alpha_semantics(decision('11.00'), ctx, clock(), maximum_quote_age_ms=10000)
    runner.worker.shutdown(wait=True)


def test_policy_revision_is_journal_bound_and_replays(tmp_path):
    from autonomous_trader.contracts import payload_hash
    from autonomous_trader.economic_policy import effective_policy, LEGACY_REAL_POLICY
    from autonomous_trader.real_configuration import ensure_configuration
    from autonomous_trader.risk import POLICY
    from lifecycle_harness import Journal
    clock, controller, broker, runner, gateway, events = world(tmp_path, configure=False)
    assert controller.state['policy_hash'] == payload_hash(LEGACY_REAL_POLICY)          # ledger opened pre-revision
    assert effective_policy(controller.state).trade_risk == D('0.015')
    authority = {'authority_id': 'charter-2026-10-16', 'account_binding': ACCOUNT,
                 'scope': {'risk_policy_sha256': payload_hash(POLICY), 'overnight': True}}
    ensure_configuration(controller, authority, clock())
    s = controller.state
    assert s['policy_hash'] == payload_hash(POLICY) and effective_policy(s).trade_risk == D('0.10')
    replayed = Journal(tmp_path / 'journal.jsonl').value
    from autonomous_trader.durable.ledger import digest
    assert digest(replayed) == digest(s)
    runner.worker.shutdown(wait=True)
