"""Unactivated recovery for the four untouched external ETF holdings.

This is a new one-time campaign.  It proves and references the permanently
blocked ICLN campaign; it never reopens or mutates that campaign.
"""
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from hashlib import sha256
from secrets import token_hex
import re
import time
from uuid import uuid4

from psycopg.types.json import Jsonb

from . import real_liquidation as prior
from .real_state import check_namespace, digest


ALLOWLIST = ('SMH', 'ESGU', 'PHO', 'LIT')
EXPECTED_QUANTITIES = dict(zip(ALLOWLIST, map(Decimal, ('0.00188', '0.00719', '0.01733', '0.01408'))))
CAMPAIGN = 'owner-external-liquidation-recovery-2026-09-29'
EXECUTION_APPROVED = False  # Supervised retry ended; source lock restored.
FRACTIONAL_SELL_RULES_VERIFIED = True
JOURNAL_TABLE = 'liquidation_recovery_journal'

Blocked = prior.Blocked
number = prior.number
regular = prior.regular


def cents(value):
    return number(value).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Snapshot:
    account_binding: str
    received_at: datetime
    positions: dict
    position_last_prices: dict
    settled: Decimal
    unsettled: Decimal
    equity: Decimal
    cash: Decimal
    open_orders: tuple
    quotes: dict
    broker_market_value: Decimal | None = None
    precision_cash_evidence: Decimal | None = None
    account_type: str = 'INDIVIDUAL_CASH'
    host: str = 'api.webull.com'
    totals_match: bool = True
    display_rounding_proven: bool = False

    def evidence(self):
        return dict(observed_at=self.received_at.isoformat(),
            positions={s: str(q) for s, q in self.positions.items()},
            position_last_prices={s: str(v) for s, v in self.position_last_prices.items()},
            settled=str(self.settled), unsettled=str(self.unsettled), cash=str(self.cash),
            equity=str(self.equity), open_order_count=len(self.open_orders),
            broker_market_value=None if self.broker_market_value is None else str(self.broker_market_value),
            precision_cash_evidence=None if self.precision_cash_evidence is None else str(self.precision_cash_evidence),
            account_totals_match=self.totals_match,
            display_rounding_proven=self.display_rounding_proven)


def _rounding_identity(snapshot):
    if (not snapshot.display_rounding_proven or snapshot.broker_market_value is None
            or snapshot.precision_cash_evidence is None
            or set(snapshot.position_last_prices) != set(snapshot.positions)):
        return False
    exact_market = sum((number(q) * number(snapshot.position_last_prices[s])
                        for s, q in snapshot.positions.items()), Decimal(0))
    precise_cash = number(snapshot.precision_cash_evidence)
    return (precise_cash >= 0 and cents(precise_cash) == snapshot.cash
            and cents(exact_market) == number(snapshot.broker_market_value)
            and cents(precise_cash + exact_market) == snapshot.equity)


def validate_snapshot(snapshot, expected, account, now, *, market=True, fresh_symbols=None):
    if snapshot.account_binding != account or not re.fullmatch(r'real-[0-9a-f]{64}', account):
        raise Blocked('ACCOUNT_BINDING_MISMATCH')
    if snapshot.host != 'api.webull.com' or snapshot.account_type != 'INDIVIDUAL_CASH':
        raise Blocked('REAL_CASH_ACCOUNT_REQUIRED')
    if not 0 <= (now-snapshot.received_at).total_seconds() <= 10:
        raise Blocked('STALE_ACCOUNT_OBSERVATION')
    if snapshot.cash != snapshot.settled + snapshot.unsettled or snapshot.settled < 0 or snapshot.unsettled < 0:
        raise Blocked('CASH_RECONCILIATION_REQUIRED')
    if not snapshot.totals_match and not _rounding_identity(snapshot):
        raise Blocked('CASH_RECONCILIATION_REQUIRED')
    if snapshot.open_orders:
        raise Blocked('OPEN_OR_UNCERTAIN_ORDER')
    if snapshot.positions != expected or set(expected)-set(ALLOWLIST):
        raise Blocked('RECOVERY_POSITION_OR_QUANTITY_MISMATCH')
    for symbol, quantity in expected.items():
        if quantity != EXPECTED_QUANTITIES[symbol] or not Decimal('0.00001') < number(quantity) < 1:
            raise Blocked('EXACT_RECOVERY_QUANTITY_REQUIRED')
    if market:
        regular(now)
        required_fresh = set(expected) if fresh_symbols is None else set(fresh_symbols)
        if required_fresh-set(expected):
            raise Blocked('QUOTE_SYMBOL_SCOPE_INVALID')
        for symbol, quantity in expected.items():
            quote = snapshot.quotes.get(symbol)
            if not quote:
                raise Blocked('MARKET_ENTITLEMENT_OR_QUOTE_UNVERIFIED')
            if quote.get('category') != 'US_ETF':
                raise Blocked('ETF_QUOTE_CATEGORY_REQUIRED')
            if symbol in required_fresh and (not 0 <= (now-quote['at']).total_seconds() <= 10
                    or not 0 <= (now-quote['received_at']).total_seconds() <= 10):
                raise Blocked('STALE_OR_FUTURE_QUOTE')
            bid, ask = number(quote['bid']), number(quote['ask'])
            if not 0 < bid <= ask:
                raise Blocked('INVALID_QUOTE')
            if quantity*bid < Decimal('0.01'):
                raise Blocked('FRACTIONAL_SELL_VALUE_UNSUPPORTED')


def order_for(symbol, quantity, client_id):
    if symbol not in ALLOWLIST:
        raise Blocked('ONLY_RECOVERY_ALLOWLIST_SELL')
    order = prior.order_for(symbol, quantity, client_id)
    validate_order(order, {symbol: quantity})
    return order


def validate_order(order, owned):
    prior.validate_order(order, owned)
    if order.get('symbol') not in ALLOWLIST or number(order['quantity']) != EXPECTED_QUANTITIES[order['symbol']]:
        raise Blocked('ONLY_RECOVERY_ALLOWLIST_SELL')


def fill_detail(body, order, broker_id):
    validate_order(order, {order['symbol']: number(order['quantity'])})
    return prior.fill_detail(body, order, broker_id)


def prior_blocked_evidence(db, account):
    with db.connect() as c:
        if c.execute("SELECT to_regclass('liquidation_journal')").fetchone()[0] is None:
            raise Blocked('PRIOR_BLOCKED_CAMPAIGN_REQUIRED')
        rows = c.execute('SELECT event,payload FROM liquidation_journal ORDER BY seq').fetchall()
    starts = [p for e,p in rows if e == 'STARTED']
    fills = [p for e,p in rows if e == 'FILL']
    blocked = [p for e,p in rows if e == 'BLOCKED']
    if (len(starts) != 1 or starts[0].get('account_binding') != account
            or starts[0].get('campaign') != prior.CAMPAIGN
            or len(fills) != 1 or fills[0].get('symbol') != 'ICLN'
            or number(fills[0].get('quantity')) != Decimal('0.05675')
            or len(blocked) != 1 or blocked[0].get('blocker') != 'CASH_RECONCILIATION_REQUIRED'
            or any(e == 'COMPLETE' for e,_ in rows)
            or sum(e == 'CAPABILITY_CONSUMED' for e,_ in rows) != 1):
        raise Blocked('PRIOR_BLOCKED_CAMPAIGN_REQUIRED')
    evidence = dict(campaign=prior.CAMPAIGN, fill={k:fills[0].get(k) for k in
        ('symbol','quantity','price','fees_usd','expected_net_cash_usd','status')},
        blocker=blocked[0].get('blocker'))
    return digest(evidence)


@dataclass(frozen=True)
class SubmissionCapability:
    campaign: str
    namespace: str
    account_binding: str
    prior_evidence_hash: str
    sequence_index: int
    previous_event_head_hash: str
    market_evidence_hash: str
    symbol: str
    quantity: str
    client_order_id: str
    order_hash: str
    token: str = field(repr=False)


class Journal:
    def __init__(self, db):
        check_namespace(db)
        self.db, self._owner = db, None
        self.namespace = digest(dict(host=db.host, port=db.port, database=db.database,
            schema=db.schema, journal=JOURNAL_TABLE))
        with db.connect() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS liquidation_recovery_journal (
                seq bigserial PRIMARY KEY, event text NOT NULL, payload jsonb NOT NULL,
                at timestamptz NOT NULL DEFAULT now());
                CREATE UNIQUE INDEX IF NOT EXISTS liquidation_recovery_once
                ON liquidation_recovery_journal(event) WHERE event='STARTED';
                CREATE UNIQUE INDEX IF NOT EXISTS liquidation_recovery_capability_once
                ON liquidation_recovery_journal ((payload->>'token_hash')) WHERE event='CAPABILITY_CONSUMED';
                DROP TRIGGER IF EXISTS immutable_liquidation_recovery ON liquidation_recovery_journal;
                CREATE TRIGGER immutable_liquidation_recovery BEFORE UPDATE OR DELETE OR TRUNCATE
                ON liquidation_recovery_journal FOR EACH STATEMENT EXECUTE FUNCTION immutable_observation();''')

    def append(self, event, payload):
        with self.db.connect() as c:
            c.execute('INSERT INTO liquidation_recovery_journal(event,payload) VALUES(%s,%s)',
                      (event, Jsonb(payload)))

    def events(self):
        with self.db.connect() as c:
            return c.execute('SELECT event,payload FROM liquidation_recovery_journal ORDER BY seq').fetchall()

    def _require_owner(self):
        if self._owner is None or self._owner.closed:
            raise Blocked('RECOVERY_WRITER_REQUIRED')
        try:
            held = self._owner.execute("SELECT EXISTS(SELECT 1 FROM pg_locks WHERE locktype='advisory' "
                "AND pid=pg_backend_pid() AND classid=481516 AND objid=904 AND granted)").fetchone()[0]
            if not held:
                raise Blocked('RECOVERY_WRITER_LOST')
        except Exception:
            raise Blocked('RECOVERY_WRITER_LOST') from None

    @staticmethod
    def _cap_evidence(cap):
        return dict(campaign=cap.campaign, namespace=cap.namespace, account_binding=cap.account_binding,
            prior_evidence_hash=cap.prior_evidence_hash, sequence_index=cap.sequence_index,
            previous_event_head_hash=cap.previous_event_head_hash,
            market_evidence_hash=cap.market_evidence_hash,
            symbol=cap.symbol, quantity=cap.quantity,
            client_order_id=cap.client_order_id, order_hash=cap.order_hash,
            token_hash=sha256(cap.token.encode()).hexdigest())

    @staticmethod
    def _event_head(rows):
        head = ''
        for index, (event, payload) in enumerate(rows):
            head = digest(dict(index=index, event=event, payload=payload, previous_hash=head))
        return head

    @staticmethod
    def _post_fill_proves(payload, remaining, initial, cumulative_net):
        try:
            if payload.get('positions') != {s:str(q) for s,q in remaining.items()}:
                return False
            settled, unsettled = number(payload.get('settled')), number(payload.get('unsettled'))
            cash, equity = number(payload.get('cash')), number(payload.get('equity'))
            if (settled != number(initial.get('settled'))
                    or unsettled != number(initial.get('unsettled')) + cumulative_net
                    or cash != settled+unsettled or payload.get('open_order_count') != 0):
                return False
            if payload.get('account_totals_match') is True:
                return True
            if payload.get('display_rounding_proven') is not True:
                return False
            prices = payload.get('position_last_prices')
            if not isinstance(prices, dict) or set(prices) != set(remaining):
                return False
            exact_market = sum((number(q)*number(prices[s]) for s,q in remaining.items()), Decimal(0))
            precise_cash = number(payload.get('precision_cash_evidence'))
            broker_market = number(payload.get('broker_market_value'))
            return (precise_cash >= 0 and cents(precise_cash) == cash
                    and cents(exact_market) == broker_market
                    and cents(precise_cash+exact_market) == equity)
        except Blocked:
            return False

    def _sequence_state(self, rows, account):
        starts = [i for i,(event,_) in enumerate(rows) if event == 'STARTED']
        if len(starts) != 1 or any(event != 'DRY_RUN' for event,_ in rows[:starts[0]]):
            raise Blocked('DURABLE_RECOVERY_CLAIM_REQUIRED')
        start_index = starts[0]
        start = rows[start_index][1]
        prior_hash = prior_blocked_evidence(self.db, account)
        if (start.get('campaign') != CAMPAIGN or start.get('account_binding') != account
                or start.get('prior_evidence_hash') != prior_hash
                or start.get('observation', {}).get('positions') != {s:str(q) for s,q in EXPECTED_QUANTITIES.items()}):
            raise Blocked('DURABLE_RECOVERY_CLAIM_REQUIRED')
        cursor = start_index+1
        remaining = dict(EXPECTED_QUANTITIES)
        cumulative = Decimal(0)
        initial = start['observation']
        for sequence_index, symbol in enumerate(ALLOWLIST):
            if cursor == len(rows):
                return sequence_index, 'READY', None, remaining, cumulative
            event, order = rows[cursor]
            if event != 'SUBMISSION_INTENT':
                raise Blocked('RECOVERY_EVENT_SEQUENCE_INVALID')
            if order.get('symbol') != symbol:
                raise Blocked('RECOVERY_SYMBOL_SEQUENCE_INVALID')
            validate_order(order, {symbol:EXPECTED_QUANTITIES[symbol]})
            if sum(p.get('client_order_id') == order.get('client_order_id')
                   for e,p in rows if e == 'SUBMISSION_INTENT') != 1:
                raise Blocked('EXACT_DURABLE_INTENT_REQUIRED')
            cursor += 1
            if cursor == len(rows):
                return sequence_index, 'INTENT', order, remaining, cumulative
            event, issued = rows[cursor]
            expected_head = self._event_head(rows[:cursor])
            if (event != 'CAPABILITY_ISSUED' or issued.get('sequence_index') != sequence_index
                    or issued.get('previous_event_head_hash') != expected_head
                    or not re.fullmatch(r'[a-f0-9]{64}', str(issued.get('market_evidence_hash','')))
                    or issued.get('campaign') != CAMPAIGN or issued.get('namespace') != self.namespace
                    or issued.get('account_binding') != account
                    or issued.get('prior_evidence_hash') != prior_hash
                    or not re.fullmatch(r'[a-f0-9]{64}', str(issued.get('token_hash','')))
                    or issued.get('symbol') != symbol or issued.get('quantity') != str(EXPECTED_QUANTITIES[symbol])
                    or issued.get('client_order_id') != order.get('client_order_id')
                    or issued.get('order_hash') != digest(order)):
                raise Blocked('RECOVERY_EVENT_SEQUENCE_INVALID')
            cursor += 1
            if cursor == len(rows):
                return sequence_index, 'ISSUED', order, remaining, cumulative
            event, consumed = rows[cursor]
            if event != 'CAPABILITY_CONSUMED' or consumed != issued:
                raise Blocked('RECOVERY_EVENT_SEQUENCE_INVALID')
            cursor += 1
            if cursor == len(rows):
                return sequence_index, 'CONSUMED', order, remaining, cumulative
            event, acknowledged = rows[cursor]
            if event != 'ACKNOWLEDGED' or acknowledged != {'client_order_id':order['client_order_id']}:
                raise Blocked('RECOVERY_EVENT_SEQUENCE_INVALID')
            cursor += 1
            if cursor == len(rows):
                return sequence_index, 'ACKNOWLEDGED', order, remaining, cumulative
            event, fill = rows[cursor]
            try:
                quantity, price, fees = number(fill.get('quantity')), number(fill.get('price')), number(fill.get('fees_usd'))
                net = number(fill.get('expected_net_cash_usd'))
            except (AttributeError, Blocked):
                raise Blocked('RECOVERY_FILL_EVIDENCE_INVALID') from None
            if (event != 'FILL' or fill.get('status') != 'FILLED' or fill.get('symbol') != symbol
                    or fill.get('client_order_id') != order['client_order_id']
                    or quantity != EXPECTED_QUANTITIES[symbol] or price <= 0 or fees < 0
                    or net != (quantity*price-fees).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)):
                raise Blocked('RECOVERY_FILL_EVIDENCE_INVALID')
            cumulative += net
            del remaining[symbol]
            cursor += 1
            if cursor == len(rows):
                return sequence_index, 'FILLED', order, remaining, cumulative
            event, observation = rows[cursor]
            if (event != 'POST_FILL_OBSERVATION'
                    or not self._post_fill_proves(observation, remaining, initial, cumulative)):
                raise Blocked('RECOVERY_POST_FILL_RECONCILIATION_REQUIRED')
            cursor += 1
        if cursor != len(rows):
            raise Blocked('RECOVERY_EVENT_SEQUENCE_INVALID')
        return len(ALLOWLIST), 'COMPLETE_SEQUENCE', None, remaining, cumulative

    def _authority(self, rows, order, account, phase):
        validate_order(order, {order['symbol']: number(order['quantity'])})
        if any(event in ('BLOCKED','COMPLETE') for event,_ in rows):
            raise Blocked('RECOVERY_NO_LONGER_ACTIVE')
        sequence_index, current_phase, current_order, _, _ = self._sequence_state(rows, account)
        if current_phase != phase or current_order != order:
            raise Blocked('RECOVERY_EVENT_SEQUENCE_INVALID')
        return sequence_index

    @staticmethod
    def _market_evidence(snapshot, symbol):
        quote = snapshot.quotes[symbol]
        return digest(dict(symbol=symbol, quantity=str(snapshot.positions[symbol]),
            category=quote['category'], bid=str(quote['bid']), ask=str(quote['ask']),
            source_at=quote['at'].isoformat(), received_at=quote['received_at'].isoformat()))

    def issue(self, order, account, snapshot, now):
        self._require_owner()
        validate_snapshot(snapshot, snapshot.positions, account, now,
                          fresh_symbols=(order.get('symbol'),))
        if snapshot.positions.get(order.get('symbol')) != number(order.get('quantity')):
            raise Blocked('EXACT_RECOVERY_QUANTITY_REQUIRED')
        with self.db.connect() as c:
            c.execute('SELECT pg_advisory_xact_lock(481516,905)')
            rows = c.execute('SELECT event,payload FROM liquidation_recovery_journal ORDER BY seq').fetchall()
            sequence_index = self._authority(rows, order, account, 'INTENT')
            if any(e == 'CAPABILITY_ISSUED' and (p.get('client_order_id') == order['client_order_id']
                    or p.get('symbol') == order['symbol']) for e,p in rows):
                raise Blocked('CAPABILITY_ALREADY_ISSUED')
            cap = SubmissionCapability(CAMPAIGN, self.namespace, account,
                prior_blocked_evidence(self.db, account), sequence_index, self._event_head(rows),
                self._market_evidence(snapshot, order['symbol']),
                order['symbol'], order['quantity'],
                order['client_order_id'], digest(order), token_hex(32))
            c.execute('INSERT INTO liquidation_recovery_journal(event,payload) VALUES(%s,%s)',
                      ('CAPABILITY_ISSUED', Jsonb(self._cap_evidence(cap))))
        return cap

    def consume(self, cap, order, account, snapshot, now):
        self._require_owner()
        validate_snapshot(snapshot, snapshot.positions, account, now,
                          fresh_symbols=(order.get('symbol'),))
        if (type(cap) is not SubmissionCapability or cap.campaign != CAMPAIGN
                or cap.namespace != self.namespace or cap.account_binding != account
                or cap.symbol != order.get('symbol') or cap.quantity != order.get('quantity')
                or cap.client_order_id != order.get('client_order_id') or cap.order_hash != digest(order)
                or cap.market_evidence_hash != self._market_evidence(snapshot, order['symbol'])
                or cap.prior_evidence_hash != prior_blocked_evidence(self.db, account)):
            raise Blocked('RECOVERY_JOURNAL_CAPABILITY_REQUIRED')
        expected = self._cap_evidence(cap)
        with self.db.connect() as c:
            c.execute('SELECT pg_advisory_xact_lock(481516,905)')
            rows = c.execute('SELECT event,payload FROM liquidation_recovery_journal ORDER BY seq').fetchall()
            if any(e == 'CAPABILITY_CONSUMED' and p.get('client_order_id') == cap.client_order_id for e,p in rows):
                raise Blocked('JOURNAL_CAPABILITY_ALREADY_CONSUMED')
            sequence_index = self._authority(rows, order, account, 'ISSUED')
            if cap.sequence_index != sequence_index:
                raise Blocked('RECOVERY_EVENT_SEQUENCE_INVALID')
            if not any(e == 'CAPABILITY_ISSUED' and p == expected for e,p in rows):
                raise Blocked('JOURNAL_CAPABILITY_NOT_ISSUED')
            c.execute('INSERT INTO liquidation_recovery_journal(event,payload) VALUES(%s,%s)',
                      ('CAPABILITY_CONSUMED', Jsonb(expected)))

    @contextmanager
    def once(self):
        with self.db.connect() as c:
            if not c.execute('SELECT pg_try_advisory_lock(481516,904)').fetchone()[0]:
                raise Blocked('RECOVERY_WRITER_BUSY')
            try:
                if any(e == 'STARTED' for e,_ in self.events()):
                    raise Blocked('ONE_TIME_RECOVERY_ALREADY_CONSUMED')
                self._owner = c
                yield
            finally:
                self._owner = None
                c.execute('SELECT pg_advisory_unlock(481516,904)')


def dry_run(broker, journal, account, clock=lambda: datetime.now(timezone.utc)):
    prior_hash = prior_blocked_evidence(journal.db, account)
    snapshot = broker.refresh()
    if snapshot.positions != EXPECTED_QUANTITIES:
        raise Blocked('EXACT_FOUR_UNTOUCHED_HOLDINGS_REQUIRED')
    validate_snapshot(snapshot, EXPECTED_QUANTITIES, account, clock(),
                      fresh_symbols=(ALLOWLIST[0],))
    blockers = []
    if not EXECUTION_APPROVED:
        blockers.append('JUDICATOR_APPROVAL_REQUIRED')
    if not FRACTIONAL_SELL_RULES_VERIFIED:
        blockers.append('FRACTIONAL_SELL_MINIMA_SOURCE_REVIEW')
    result = dict(campaign=CAMPAIGN, status='PENDING_REVIEW' if blockers else 'READY',
        activated=EXECUTION_APPROVED, prior_evidence_hash=prior_hash,
        observation=snapshot.evidence(), blockers=blockers,
        market_evidence={s:dict(category=q['category'], source_at=q['at'].isoformat(),
            received_at=q['received_at'].isoformat(), bid=str(q['bid']), ask=str(q['ask']))
            for s,q in snapshot.quotes.items()},
        planned_actions=[dict(symbol=s, side='SELL', quantity=str(EXPECTED_QUANTITIES[s]),
            order_type='MARKET', session='CORE', entrust_type='QTY') for s in ALLOWLIST])
    journal.append('DRY_RUN', result)
    return result


def execute_once(broker, journal, account, *, clock=lambda: datetime.now(timezone.utc), sleep=time.sleep):
    with journal.once():
        journal._require_owner()
        reviewed = journal.events()
        if len(reviewed) != 2 or any(event != 'DRY_RUN' for event,_ in reviewed):
            raise Blocked('EXACT_REVIEWED_RECOVERY_HISTORY_REQUIRED')
        prior_hash = prior_blocked_evidence(journal.db, account)
        initial = broker.refresh()
        if initial.positions != EXPECTED_QUANTITIES:
            raise Blocked('EXACT_FOUR_UNTOUCHED_HOLDINGS_REQUIRED')
        validate_snapshot(initial, EXPECTED_QUANTITIES, account, clock(),
                          fresh_symbols=(ALLOWLIST[0],))
        journal._require_owner()
        if journal.events() != reviewed:
            raise Blocked('RECOVERY_HISTORY_CHANGED_BEFORE_START')
        journal.append('STARTED', dict(campaign=CAMPAIGN, account_binding=account,
            prior_evidence_hash=prior_hash, observation=initial.evidence()))
        expected, net_total, current = dict(EXPECTED_QUANTITIES), Decimal(0), initial
        try:
            for sequence_index, symbol in enumerate(ALLOWLIST):
                validate_snapshot(current, expected, account, clock(), fresh_symbols=(symbol,))
                if current.settled != initial.settled or current.unsettled != initial.unsettled+net_total:
                    raise Blocked('CASH_MOVEMENT_MISMATCH')
                order = order_for(symbol, expected[symbol], uuid4().hex)
                journal.append('SUBMISSION_INTENT', order)
                capability = journal.issue(order, account, current, clock())
                validate_snapshot(current, expected, account, clock(), fresh_symbols=(symbol,))
                response = broker.sell(order, current, capability=capability)
                if (not isinstance(response, dict) or response.get('client_order_id') != order['client_order_id']
                        or not isinstance(response.get('order_id'), str) or not response['order_id']):
                    raise Blocked('SUBMISSION_UNCERTAIN')
                journal.append('ACKNOWLEDGED', dict(client_order_id=order['client_order_id']))
                fill = None
                for attempt in range(3):
                    fill = fill_detail(broker.detail(order['client_order_id']), order, response['order_id'])
                    if fill is not None:
                        break
                    if attempt < 2:
                        sleep(2)
                if fill is None:
                    raise Blocked('ORDER_FINALITY_UNCERTAIN')
                journal.append('FILL', fill)
                del expected[symbol]
                net_total += number(fill['expected_net_cash_usd'])
                after = broker.refresh()
                validate_snapshot(after, expected, account, clock(), market=False)
                journal.append('POST_FILL_OBSERVATION', after.evidence())
                if after.settled != initial.settled or after.unsettled != initial.unsettled+net_total:
                    raise Blocked('CASH_MOVEMENT_MISMATCH')
                if expected:
                    next_symbol = ALLOWLIST[sequence_index+1]
                    validate_snapshot(after, expected, account, clock(), fresh_symbols=(next_symbol,))
                    current = after
            journal.append('COMPLETE', dict(status='COMPLETE', account_binding=account,
                prior_evidence_hash=prior_hash, expected_net_cash_usd=str(net_total),
                observation=after.evidence()))
            return dict(status='COMPLETE', positions={}, net_cash_usd=str(net_total))
        except Exception as exc:
            code = str(exc) if isinstance(exc, Blocked) else 'BROKER_OR_LOCAL_FAILURE_UNCERTAIN'
            journal.append('BLOCKED', dict(status='BLOCKED', blocker=code))
            raise Blocked(code) from None
