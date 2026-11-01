"""Wire-level lifecycle: REAL controller/gateway/runner + the REAL write gate and HTTP signing path
(RealStrategyTransport._send, RealWriteAuthority). Only requests.Session.send is simulated (Webull side).

Test-only JSONL storage is NOT PostgreSQL transaction/fencing proof. Fake gate
and transport are explicitly not live authority, entitlement, or broker proof.
"""
from copy import deepcopy
from concurrent.futures import Future
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
import json
from pathlib import Path
import shutil
from types import SimpleNamespace
from uuid import uuid4

from autonomous_trader.contracts import (AlphaContext, AlphaInstrumentContext, AlphaPositionContext, AlphaManagedPlan,
    CapabilityManifest, Instrument)
from autonomous_trader.decisions import parse_alpha_decision, validate_alpha_semantics
from autonomous_trader.durable.ledger import Ledger
from autonomous_trader.durable.models import CreateAccount
from autonomous_trader.durable.projection import reduce_event
from autonomous_trader.mvp.alpha import translate_open
from autonomous_trader.mvp.config import reviewed_calendar
from autonomous_trader.real_strategy_gateway import RealExecutionGateway
from autonomous_trader.real_runner import RealRunner
from autonomous_trader.real_trading import RealController, RealPacket, CAPABILITY_VERSION, SOURCE
import test_real_offline_round_trip as _old
from test_real_offline_round_trip import SyntheticBroker

NOW = datetime(2026, 9, 29, 15, tzinfo=timezone.utc)
from autonomous_trader.real_state import binding
import requests
from autonomous_trader.real_strategy_broker import RealStrategyTransport
WEBULL_ACCOUNT = 'SIM-ACCOUNT-1'
ACCOUNT = binding(WEBULL_ACCOUNT)


_old.ACCOUNT = ACCOUNT   # SyntheticBroker.refresh builds packets for this binding


class LocalProjectionJournal:
    execution_scope = 'REAL'
    def __init__(self, path):
        self.path = path
        self.config = SimpleNamespace(schema='real_trader')
        self.value = None
        for line in path.read_text().splitlines() if path.exists() else ():
            self.value = reduce_event(self.value, json.loads(line))

    def append(self, command, at, event_id=None):
        event = Ledger._event(self, command, event_id or uuid4().hex,
            1 if self.value is None else self.value['version']+1, at, at, 'synthetic-epoch', 'test-only', 'test-only')
        updated = reduce_event(self.value, event)
        with self.path.open('a') as stream: stream.write(json.dumps(event)+'\n')
        self.value = updated
        return deepcopy(updated)

    def snapshot(self, account):
        assert account == ACCOUNT
        return deepcopy(self.value)

    def acquire_leader(self, account, holder, *, event_id, at, ttl_seconds):
        assert account == ACCOUNT
        return self.append({'kind': 'LEASE', 'new_term': self.value['leader']['holder'] is None,
            'recovery': False, 'leader': {'holder': holder, 'token': 1,
                'expires_at': (at+timedelta(seconds=ttl_seconds)).isoformat(),
                'epoch': 'synthetic-epoch', 'last_clock_at': at.isoformat()}}, at, event_id)

    def execute(self, account, command, *, event_id, expected_version, holder, token, at):
        assert account == ACCOUNT and expected_version == self.value['version']
        assert holder == self.value['leader']['holder'] and token == 1
        return self.append(command.model_dump(mode='json'), at, event_id)


class WireBroker:
    """Packets/state from the synthetic account; every order and detail goes through the real transport."""
    def __init__(self, instrument, session):
        self.instrument, self.session = instrument, session
        self.settled, self.unsettled, self.quantity = D(500), D(0), D(0)
        self.bid, self.ask = D('10'), D('10.01')
        self.sent, self.details, self.posts = [], {}, []
        creds = SimpleNamespace(app_key='k'*8, app_secret='s'*8, access_token='t'*8, account_id='')
        self.transport = RealStrategyTransport(creds, WEBULL_ACCOUNT)

    refresh = SyntheticBroker.refresh

    def fake_send(self, session, prepared, **kwargs):
        import urllib.parse
        url = urllib.parse.urlsplit(prepared.url); body = json.loads(prepared.body) if prepared.body else {}
        resp = requests.Response(); resp.status_code = 200
        if url.path == '/trading/orders/place':
            order = body['new_orders'][0]; self.posts.append(order)
            qty = D(order['quantity']); price = self.ask if order['side'] == 'BUY' else self.bid
            if order['side'] == 'BUY': self.settled -= qty*price+D('.02'); self.quantity += qty
            else:
                assert qty == self.quantity; self.unsettled += qty*price-D('.02'); self.quantity -= qty
            cid = order['client_order_id']; broker_id = 'wire-'+str(len(self.posts))
            self.details[cid] = {'client_order_id': cid, 'orders': [{'client_order_id': cid, 'order_id': broker_id,
                'symbol': order['symbol'], 'side': order['side'], 'order_type': order['order_type'], 'time_in_force': 'DAY',
                'support_trading_session': 'CORE', 'total_quantity': order['quantity'], 'filled_quantity': order['quantity'],
                'status': 'FILLED', 'filled_price': str(price), 'filled_time_at': NOW.isoformat(),
                'fees': [{'actual_value': '.02', 'receivable_value': '.02'}],
                'commission': {'actual_commission': '0', 'receivable_commission': '0'}}]}
            payload = {'client_order_id': cid, 'order_id': broker_id}
        elif url.path == '/trading/orders/get':
            cid = urllib.parse.parse_qs(url.query)['client_order_id'][0]; payload = self.details[cid]
        else: raise AssertionError('unexpected wire route '+url.path)
        resp._content = json.dumps(payload).encode(); resp.headers['Content-Type'] = 'application/json'
        resp.raw = SimpleNamespace(close=lambda: None, release_conn=lambda: None)
        resp.iter_content = lambda size: iter([resp._content])
        return resp

    def submit(self, intent, symbol, controller, nonce, packet, gate, **kwargs):
        self.sent.append(intent)
        return self.transport.submit(intent, symbol, controller, nonce, packet, gate, **kwargs)

    def detail(self, client_id): return self.transport.detail(client_id)


def test_wire_open_fill_protect_exit_through_real_write_gate(tmp_path, monkeypatch):
    session = reviewed_calendar().session(NOW.date())
    instrument = Instrument(instrument_id='synthetic', ticker='TEST', country='US', asset_type='STOCK',
        listing_exchange='NAS', liquid=True, tradable=True, quantity_increment='1', price_increment_usd='0.01', minimum_quantity='1')
    journal = LocalProjectionJournal(tmp_path/'synthetic.jsonl')
    journal.append(CreateAccount(account_id=ACCOUNT, experiment_id='synthetic', owner_id='test',
        initial_settled_usd='500', session=session, instruments=(instrument,),
        capabilities=CapabilityManifest(version=CAPABILITY_VERSION, environment='REAL', entry_order_types=('LIMIT',),
            protection_mechanisms=('LOCAL_MONITORED_STOP',)), settlement_days=(NOW.date(),(NOW+timedelta(days=1)).date()),
        mark_max_age_seconds=2, reconciliation_max_age_seconds=10).model_dump(mode='json'), NOW)
    broker = WireBroker(instrument, session)
    monkeypatch.setattr(requests.Session, "send", lambda s, p, **k: broker.fake_send(s, p, **k))
    controller = RealController(journal, ACCOUNT, lambda: NOW)
    controller.refresh(broker.refresh(controller.state))
    # The external authority gate is a fixture; this test grants no actual authority.
    # The authority gate's own policy checks are covered elsewhere; the write path insists on the exact RealGate type.
    from autonomous_trader.real_trading import RealGate
    noop = lambda self, *a, **k: None
    for name in ('check', 'check_exit', 'check_buy_cancel', 'check_recovery', 'check_exit_ownership', 'check_exit_observation'):
        monkeypatch.setattr(RealGate, name, noop)
    monkeypatch.setattr(RealGate, 'read_checked', lambda self, fetch, clock, **k: (fetch(), None))
    gate = object.__new__(RealGate); gate.account = ACCOUNT
    events = []
    emit = lambda kind, payload, **kw: events.append((kind,payload))
    store = SimpleNamespace(emit_idempotent=emit, emit=emit)
    gateway = RealExecutionGateway(gate, controller, broker, broker, store, lambda: NOW)
    context = broker.refresh(controller.state).context
    # Explicit synthetic Astra output, not a paid model call or market claim.
    decision = parse_alpha_decision(json.dumps({'schema_version': '1.0', 'action': 'OPEN', 'reason': 'Synthetic lifecycle',
        'open': {'instrument_id': 'synthetic', 'symbol': 'TEST', 'strategy_label': 'synthetic-only',
            'thesis': 'Synthetic round trip, no alpha claim', 'desired_quantity': '1', 'desired_notional_usd': None,
            'entry_limit_usd': '10.01', 'hard_stop_usd': '9.90', 'target_usd': None,
            'entry_expires_at': (NOW+timedelta(minutes=2)).isoformat(),
            'next_review_at': (NOW+timedelta(minutes=5)).isoformat(),
            'latest_exit_at': (NOW+timedelta(hours=1)).isoformat()}, 'hold': None, 'modify': None, 'close': None}))
    validate_alpha_semantics(decision, context, NOW, maximum_quote_age_ms=10000)
    runner = RealRunner(controller, broker, gateway, gate, store,
        lambda: (_ for _ in ()).throw(AssertionError('No model transport')), lambda: NOW)
    # Context acquisition/feedback DB are fixtures; use the real result handler.
    runner.context = lambda packet: packet.context
    result = {'decision': decision.model_dump(mode='json'), 'model': 'synthetic-no-call', 'reasoning': 'medium',
        'usage': {'input_tokens': 0, 'output_tokens': 0}, 'estimated_cost_usd': '0',
        'model_work_id': 'synthetic-only', 'latency_ms': 0}
    future = Future(); future.set_result(result)
    runner.pending = (future, context, 'ENTRY', None)
    try: runner._apply(broker.refresh(controller.state))
    finally: runner.worker.shutdown(wait=True)
    assert len(broker.sent) == 1 and len(broker.posts) == 1 and broker.posts[0]['side'] == 'BUY'
    assert set(broker.posts[0]) >= {'client_order_id','symbol','order_type','quantity','side','time_in_force','limit_price'}
    intent = broker.sent[0]
    plan = AlphaManagedPlan.model_validate(next(p['plan'] for kind,p in events if kind == 'REAL_PLAN'))
    bid = broker.details[intent.client_order_id]['orders'][0]['order_id']
    assert gateway.reconcile_order(intent, 'TEST', bid)['status'] == 'FILLED'
    assert D(controller.state['positions']['synthetic']['protected_quantity']) == 1
    assert next(iter(controller.state['permits'].values()))['consumed']
    assert LocalProjectionJournal(journal.path).value == controller.state
    broker.bid, broker.ask = D('10.20'), D('10.21')
    controller.refresh(broker.refresh(controller.state))
    class _ExitPacket:                       # real RealPacket (real validate) plus the ticker the exit path reads
        def __init__(self, packet):
            self._p = packet; self.symbol = 'TEST'
            q = packet.context.instruments[0]                      # planned exits are LIMIT at the bid (v3)
            self.bid_usd, self.ask_usd, self.instrument_id = q.bid_usd, q.ask_usd, q.instrument_id
            self.quote_at, self.quote_received_at = q.quote_at, packet.quote_received_at[q.instrument_id]
        def __getattr__(self, name): return getattr(self._p, name)
    broker.exit_packet = lambda *args: _ExitPacket(broker.refresh(controller.state))
    position = AlphaPositionContext(position_id='position-synthetic', instrument_id='synthetic', symbol='TEST',
        quantity='1', average_entry_usd='10.01', mark_usd='10.20', active_stop_usd='9.90',
        latest_exit_at=plan.latest_exit_at, unrealized_pnl_usd='0.19', target_usd=None)
    current = broker.refresh(controller.state).context.model_copy(update={'positions': (position,)})
    close = {'schema_version': '1.0', 'action': 'CLOSE', 'reason': 'Synthetic thesis reassessment',
        'open': None, 'hold': None, 'modify': None, 'close': {'position_id': 'position-synthetic', 'full_close': True}}
    future = Future(); future.set_result({'decision': close})
    runner.pending = (future, current, 'POSITION', plan.plan_id)
    runner.plan = lambda _: plan
    runner.context = lambda _: current
    runner._apply(broker.refresh(controller.state))
    assert len(broker.sent) == 2 and broker.posts[1]['side'] == 'SELL'
    sell = broker.sent[-1]
    sid = broker.details[sell.client_order_id]['orders'][0]['order_id']
    assert sell.quantity == 1 and sell.side == 'SELL_OWNED'
    assert sell.order_type == 'LIMIT' and sell.limit_price_usd == D('10.20')   # never below the market
    assert gateway.reconcile_order(sell, 'TEST', sid)['status'] == 'FILLED'
    final = controller.state
    assert final['positions'] == {} and all(o['final'] for o in final['orders'].values())
    assert final['reconciliation']['ready'] and final['reconciliation']['settlement_known']
    assert D(final['cash']['settled_usd']) == D('489.97')
    assert D(final['cash']['unsettled_usd']) == D('10.18')
    assert D(final['cash']['realized_net_usd']) == D('.15')
    assert D(final['cash']['fees_usd']) == D('.04')
    assert len(broker.sent) == 2 and all(p['consumed'] for p in final['permits'].values())
    assert LocalProjectionJournal(journal.path).value == final
    # (Retired 2026-10-12: tail that exercised the old multi-agent advisory layer
    # via a staging-only path. Every trading assertion above is kept.)
