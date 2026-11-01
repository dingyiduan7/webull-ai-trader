"""Synthetic REAL lifecycle harness (test-only): real controller, gateway and M2 reducer over a
JSONL journal, plus a programmable broker. No live authority, entitlement or broker proof.

Used by the Thursday 2026-10-15 replay and the multi-day carry simulations."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
import json
from types import SimpleNamespace
from uuid import uuid4

from autonomous_trader.contracts import AlphaContext, AlphaInstrumentContext, CapabilityManifest, Instrument
from autonomous_trader.durable.ledger import Ledger
from autonomous_trader.durable.models import CreateAccount
from autonomous_trader.durable.projection import reduce_event
from autonomous_trader.mvp.config import reviewed_calendar
from autonomous_trader.real_trading import RealController, RealPacket, RealExitPacket, CAPABILITY_VERSION

ACCOUNT = 'real-' + 'a' * 64


class Journal:
    execution_scope = 'REAL'

    def __init__(self, path):
        self.path, self.value = path, None
        for line in path.read_text().splitlines() if path.exists() else ():
            self.value = reduce_event(self.value, json.loads(line))

    def append(self, command, at, event_id=None):
        event = Ledger._event(self, command, event_id or uuid4().hex,
                              1 if self.value is None else self.value['version'] + 1, at, at, 'synthetic-epoch',
                              'test-only', 'test-only')
        updated = reduce_event(self.value, event)
        with self.path.open('a') as stream:
            stream.write(json.dumps(event) + '\n')
        self.value = updated
        return deepcopy(updated)

    def snapshot(self, account):
        assert account == ACCOUNT
        return deepcopy(self.value)

    def acquire_leader(self, account, holder, *, event_id, at, ttl_seconds):
        return self.append({'kind': 'LEASE', 'new_term': self.value['leader']['holder'] is None,
                            'recovery': False, 'leader': {'holder': holder, 'token': 1,
                                                          'expires_at': (at + timedelta(seconds=ttl_seconds)).isoformat(),
                                                          'epoch': 'synthetic-epoch', 'last_clock_at': at.isoformat()}},
                           at, event_id)

    def execute(self, account, command, *, event_id, expected_version, holder, token, at):
        assert account == ACCOUNT and expected_version == self.value['version']
        return self.append(command.model_dump(mode='json'), at, event_id)


class Clock:
    def __init__(self, at):
        self.at = at

    def __call__(self):
        return self.at

    def advance(self, **kw):
        self.at = self.at + timedelta(**kw)
        return self.at


def instrument(iid='crwv', ticker='CRWV'):
    return Instrument(instrument_id=iid, ticker=ticker, country='US', asset_type='STOCK', listing_exchange='NAS',
                      liquid=True, tradable=True, quantity_increment='1', price_increment_usd='0.01',
                      minimum_quantity='1')


class Broker:
    """Programmable cash-account broker. `cash_fee_lag` reproduces the 2026-10-15 behaviour: the order
    record reports a fee the account cash has not yet deducted."""

    def __init__(self, inst, clock, settled):
        self.inst, self.clock = inst, clock
        self.settled, self.unsettled, self.quantity = D(settled), D(0), D(0)
        self.bid, self.ask = D('66.33'), D('66.34')
        self.sent, self.details, self.lots = [], {}, []
        self.fee = {'BUY': D('0'), 'SELL_OWNED': D('0.01')}
        self.cash_fee_lag = False
        self.fill_price = None
        self.cash_adjuster = None
        self.last_cash_variance = None

    def open_ids(self):
        return tuple(cid for cid, d in self.details.items() if d['orders'][0]['status'] in ('SUBMITTED', 'PENDING'))

    def _cash(self):
        total = self.settled + self.unsettled
        cash = {'settled_cash': str(self.settled), 'unsettled_cash': str(self.unsettled), 'cash_balance': str(total),
                'net_liquidation_value': str(total + self.quantity * self.bid), 'currency': 'USD'}
        return self.cash_adjuster(dict(cash)) if self.cash_adjuster else cash

    def session(self):
        return reviewed_calendar().session(self.clock().date())

    def context(self, extra_horizon=None):
        now, session = self.clock(), self.session()
        q = AlphaInstrumentContext(instrument_id=self.inst.instrument_id, symbol=self.inst.ticker, quote_at=now,
                                   bid_usd=self.bid, ask_usd=self.ask, price_increment_usd='0.01',
                                   quantity_increment='1')
        kw = {} if extra_horizon is None else {'exit_horizon_at': extra_horizon}
        return AlphaContext(as_of=now, expires_at=now + timedelta(seconds=30), session_open=session.opens_at,
                            session_close=session.closes_at, liquidation_at=session.liquidation_at,
                            instruments=(q,), settled_cash_usd=self.settled, remaining_risk_usd='25',
                            reconciliation_ready=True, settlement_known=True, **kw)

    def settle_due(self):
        today = self.clock().astimezone(timezone.utc).date()
        for lot in list(self.lots):
            if lot[1] <= today:
                self.settled += lot[0]
                self.unsettled -= lot[0]
                self.lots.remove(lot)

    def refresh(self, state=None, horizon=None):
        now = self.clock()
        positions = {self.inst.instrument_id: self.quantity} if self.quantity else {}
        cash = self._cash()
        return RealPacket(ACCOUNT, now, D(cash['settled_cash']), D(cash['unsettled_cash']),
                          D(cash['net_liquidation_value']), positions, self.open_ids(),
                          (self.inst,), self.context(horizon), 'obs-' + uuid4().hex,
                          {self.inst.instrument_id: now})

    def account_observation(self):
        now = self.clock()
        return dict(account_binding=ACCOUNT, positions=tuple(
            [{'symbol': self.inst.ticker, 'instrument_id': self.inst.instrument_id, 'quantity': str(self.quantity)}]
            if self.quantity else []), open_client_ids=self.open_ids(), cash=self._cash(),
            observed_at=now, received_at=now)

    def exit_packet(self, state, iid):
        now = self.clock()
        cash = self._cash()
        return RealExitPacket(ACCOUNT, now, D(cash['settled_cash']), D(cash['unsettled_cash']),
                              D(cash['net_liquidation_value']),
                              {iid: self.quantity} if self.quantity else {}, self.open_ids(), iid, self.inst.ticker,
                              self.bid, self.ask, now, now, 'exit-' + uuid4().hex)

    ownership_packet = exit_packet

    def account_order_packet(self, state, iid):
        return self.exit_packet(state, iid)

    def submit(self, intent, symbol, controller, nonce, packet, gate, **kwargs):
        controller.consume(intent, nonce)
        self.sent.append(intent)
        if intent.side == 'SELL_OWNED' and intent.order_type == 'LIMIT' and getattr(self, 'rest_limit_sells', False):
            broker_id = 'b-' + str(len(self.sent))
            self.details[intent.client_order_id] = {'client_order_id': intent.client_order_id, 'orders': [{
                'client_order_id': intent.client_order_id, 'order_id': broker_id, 'symbol': symbol, 'side': 'SELL',
                'order_type': 'LIMIT', 'time_in_force': 'DAY', 'support_trading_session': 'CORE',
                'total_quantity': str(intent.quantity), 'filled_quantity': '0', 'status': 'SUBMITTED',
                'fees': [], 'commission': {'actual_commission': '0', 'receivable_commission': '0'}}]}
            return {'client_order_id': intent.client_order_id, 'order_id': broker_id}
        price = self.fill_price or (self.ask if intent.side == 'BUY' else self.bid)
        fee = self.fee[intent.side]
        posted_fee = D(0) if self.cash_fee_lag else fee
        if intent.side == 'BUY':
            self.settled -= intent.quantity * price + posted_fee
            self.quantity += intent.quantity
        else:
            proceeds = intent.quantity * price - posted_fee
            self.unsettled += proceeds
            self.quantity -= intent.quantity
            due = next(d for d in controller.state['config']['settlement_days']
                       if d > self.clock().date().isoformat())
            self.lots.append((proceeds, datetime.fromisoformat(due).date()))
        broker_id = 'b-' + str(len(self.sent))
        self.details[intent.client_order_id] = {'client_order_id': intent.client_order_id, 'orders': [{
            'client_order_id': intent.client_order_id, 'order_id': broker_id, 'symbol': symbol,
            'side': 'BUY' if intent.side == 'BUY' else 'SELL', 'order_type': intent.order_type,
            'time_in_force': 'DAY', 'support_trading_session': 'CORE', 'total_quantity': str(intent.quantity),
            'filled_quantity': str(intent.quantity), 'status': 'FILLED', 'filled_price': str(price),
            'filled_time_at': self.clock().isoformat(),
            'fees': [{'actual_value': str(fee), 'receivable_value': str(fee)}],
            'commission': {'actual_commission': '0', 'receivable_commission': '0'}}]}
        return {'client_order_id': intent.client_order_id, 'order_id': broker_id}

    def detail(self, client_id):
        return self.details[client_id]

    def cancel(self, client_id):
        self.details[client_id]['orders'][0]['status'] = 'CANCELLED'


def open_account(tmp_path, clock, settled, inst):
    session = reviewed_calendar().session(clock().date())
    cal = reviewed_calendar()
    days = tuple(session.label + timedelta(days=i) for i in range(30) if cal.settlement_day(session.label + timedelta(days=i)))
    journal = Journal(tmp_path / 'journal.jsonl')
    journal.append(CreateAccount(account_id=ACCOUNT, experiment_id='synthetic', owner_id='test',
                                 initial_settled_usd=str(settled), session=session, instruments=(inst,),
                                 capabilities=CapabilityManifest(version=CAPABILITY_VERSION, environment='REAL',
                                                                 entry_order_types=('LIMIT',),
                                                                 protection_mechanisms=('LOCAL_MONITORED_STOP',)),
                                 settlement_days=days, mark_max_age_seconds=2,
                                 reconciliation_max_age_seconds=10).model_dump(mode='json'), clock())
    return journal, RealController(journal, ACCOUNT, clock)


def fixture_gate():
    return SimpleNamespace(check=lambda *a, **k: None, check_exit=lambda *a, **k: None,
                           check_exit_observation=lambda *a, **k: None, check_exit_ownership=lambda *a, **k: None,
                           check_recovery=lambda *a, **k: None, check_buy_cancel=lambda *a, **k: None,
                           read_checked=lambda fetch, clock, **k: (fetch(), None))


def events_store():
    events = []
    emit = lambda kind, payload, **kw: events.append((kind, payload))
    return events, SimpleNamespace(emit_idempotent=emit, emit=emit)
