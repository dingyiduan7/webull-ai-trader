"""An exit must succeed even when the last reconciliation lapsed minutes ago (live Oct 15 defect)."""
from concurrent.futures import Future
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from types import SimpleNamespace
import json
import pytest

from autonomous_trader.contracts import AlphaManagedPlan, CapabilityManifest, Instrument
from autonomous_trader.decisions import parse_alpha_decision, validate_alpha_semantics
from autonomous_trader.durable.models import CreateAccount
from autonomous_trader.mvp.config import reviewed_calendar
from autonomous_trader.real_runner import RealRunner
from autonomous_trader.real_strategy_gateway import RealExecutionGateway
from autonomous_trader.real_trading import RealController, RealExitPacket, CAPABILITY_VERSION
from tests.test_real_offline_round_trip import ACCOUNT, NOW, LocalProjectionJournal, SyntheticBroker


def held_position(tmp_path):
    clock = [NOW]
    session = reviewed_calendar().session(NOW.date())
    instrument = Instrument(instrument_id='synthetic', ticker='TEST', country='US', asset_type='STOCK',
        listing_exchange='NAS', liquid=True, tradable=True, quantity_increment='1', price_increment_usd='0.01', minimum_quantity='1')
    journal = LocalProjectionJournal(tmp_path/'j.jsonl')
    journal.append(CreateAccount(account_id=ACCOUNT, experiment_id='x', owner_id='t', initial_settled_usd='500',
        session=session, instruments=(instrument,), capabilities=CapabilityManifest(version=CAPABILITY_VERSION,
        environment='REAL', entry_order_types=('LIMIT',), protection_mechanisms=('LOCAL_MONITORED_STOP',)),
        settlement_days=(NOW.date(), (NOW+timedelta(days=1)).date()), mark_max_age_seconds=2,
        reconciliation_max_age_seconds=10).model_dump(mode='json'), NOW)
    broker = SyntheticBroker(instrument, session)
    controller = RealController(journal, ACCOUNT, lambda: clock[0])
    controller.refresh(broker.refresh(controller.state))
    gate = SimpleNamespace(check=lambda *a, **k: None, check_exit=lambda *a, **k: None,
                           read_checked=lambda fetch, clock, **k: (fetch(), None))
    events = []
    emit = lambda kind, payload, **kw: events.append((kind, payload))
    gateway = RealExecutionGateway(gate, controller, broker, broker, SimpleNamespace(emit_idempotent=emit, emit=emit),
        lambda: clock[0])
    context = broker.refresh(controller.state).context
    decision = parse_alpha_decision(json.dumps({'schema_version': '1.0', 'action': 'OPEN', 'reason': 'synthetic',
        'open': {'instrument_id': 'synthetic', 'symbol': 'TEST', 'strategy_label': 'synthetic-only',
            'thesis': 'synthetic', 'desired_quantity': '1', 'desired_notional_usd': None, 'entry_limit_usd': '10.01',
            'hard_stop_usd': '9.90', 'target_usd': None, 'entry_expires_at': (NOW+timedelta(minutes=2)).isoformat(),
            'next_review_at': (NOW+timedelta(minutes=5)).isoformat(), 'latest_exit_at': (NOW+timedelta(hours=1)).isoformat()},
        'hold': None, 'modify': None, 'close': None}))
    validate_alpha_semantics(decision, context, NOW, maximum_quote_age_ms=10000)
    runner = RealRunner(controller, broker, gateway, gate, SimpleNamespace(emit_idempotent=emit, emit=emit),
        lambda: None, lambda: clock[0])
    runner.context = lambda packet: packet.context
    future = Future(); future.set_result({'decision': decision.model_dump(mode='json'), 'model': 'synthetic',
        'reasoning': 'medium', 'usage': {'input_tokens': 0, 'output_tokens': 0}, 'estimated_cost_usd': '0',
        'model_work_id': 'synthetic', 'latency_ms': 0})
    runner.pending = (future, context, 'ENTRY', None)
    try: runner._apply(broker.refresh(controller.state))
    finally: runner.worker.shutdown(wait=True)
    intent = broker.sent[0]
    oid = broker.details[intent.client_order_id]['orders'][0]['order_id']
    assert gateway.reconcile_order(intent, 'TEST', oid)['status'] == 'FILLED'
    plan = AlphaManagedPlan.model_validate(next(p['plan'] for kind, p in events if kind == 'REAL_PLAN'))
    return clock, controller, gateway, broker, plan


@pytest.mark.parametrize('lapse_seconds', [30, 300])
def test_exit_after_reconciliation_lapse_is_admitted_with_fresh_evidence(tmp_path, lapse_seconds):
    clock, controller, gateway, broker, plan = held_position(tmp_path)
    clock[0] = NOW+timedelta(seconds=lapse_seconds)
    broker.exit_packet = lambda state, iid: RealExitPacket(ACCOUNT, clock[0], broker.settled, broker.unsettled,
        broker.settled+broker.unsettled+broker.quantity*broker.bid, {'synthetic': broker.quantity}, (), 'synthetic', 'TEST',
        broker.bid, broker.ask, clock[0], clock[0], 'fresh-exit-evidence-'+str(lapse_seconds))
    # The lapsed ledger evidence is the live-day condition.
    assert clock[0] > datetime.fromisoformat(controller.state['reconciliation']['valid_until'])
    gateway.protective_exit(plan, 'SAFETY_LATCH', quantity=D(1))
    assert broker.sent[-1].side == 'SELL_OWNED'


def test_exit_still_blocks_on_a_real_position_mismatch(tmp_path):
    clock, controller, gateway, broker, plan = held_position(tmp_path)
    clock[0] = NOW+timedelta(seconds=30)
    broker.exit_packet = lambda state, iid: RealExitPacket(ACCOUNT, clock[0], broker.settled, broker.unsettled,
        broker.settled+broker.unsettled, {'synthetic': D(2)}, (), 'synthetic', 'TEST',
        broker.bid, broker.ask, clock[0], clock[0], 'mismatch-evidence')
    n = len(broker.sent)
    with pytest.raises(Exception):
        gateway.protective_exit(plan, 'SAFETY_LATCH', quantity=D(1))
    assert len(broker.sent) == n
