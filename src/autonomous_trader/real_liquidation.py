"""One owner-directed disposal, separate from AITrader's disabled REAL runner.

No strategy, BUY, cancel, replacement, retry, or automatic activation path.
Execution remains locked pending review of the dry run and this implementation.
"""
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re
import time
from uuid import uuid4
from secrets import token_hex
from hashlib import sha256

from psycopg.types.json import Jsonb
from .mvp.config import reviewed_calendar
from .reference import NEW_YORK
from .real_state import check_namespace, digest

ALLOWLIST = ('ICLN', 'SMH', 'ESGU', 'PHO', 'LIT')
CAMPAIGN = 'owner-external-liquidation-2026-09-29'
EXECUTION_APPROVED = False  # Campaign attempt is consumed and durably BLOCKED; never reopen it.
FRACTIONAL_SELL_RULES_VERIFIED = True  # Verified against current official Webull fractional-share rules.


class Blocked(ValueError):
    """Fixed codes only. Never include raw responses or credentials."""


def number(value):
    try:
        if isinstance(value, (bool, float)):
            raise ValueError()
        result = Decimal(value)
        if not result.is_finite(): raise ValueError()
        return result
    except (ValueError, TypeError, InvalidOperation):
        raise Blocked('INVALID_NUMERIC_EVIDENCE') from None


def regular(now):
    if now.tzinfo is None: raise Blocked('NAIVE_CLOCK')
    session = reviewed_calendar().session(now.astimezone(NEW_YORK).date())
    if session is None or not session.contains(now): raise Blocked('REGULAR_SESSION_REQUIRED')
    return session


@dataclass(frozen=True)
class Snapshot:
    account_binding: str
    received_at: datetime
    positions: dict
    settled: Decimal
    unsettled: Decimal
    equity: Decimal
    cash: Decimal
    open_orders: tuple
    quotes: dict
    account_type: str = 'INDIVIDUAL_CASH'
    host: str = 'api.webull.com'
    totals_match: bool = True

    def evidence(self):
        return dict(observed_at=self.received_at.isoformat(),
            positions={s: str(q) for s, q in self.positions.items()},
            settled=str(self.settled), unsettled=str(self.unsettled), cash=str(self.cash),
            equity=str(self.equity), open_order_count=len(self.open_orders))


def validate_snapshot(snapshot, expected, account, now, *, market=True):
    if snapshot.account_binding != account or not re.fullmatch(r'real-[0-9a-f]{64}', account):
        raise Blocked('ACCOUNT_BINDING_MISMATCH')
    if snapshot.host != 'api.webull.com' or snapshot.account_type != 'INDIVIDUAL_CASH':
        raise Blocked('REAL_CASH_ACCOUNT_REQUIRED')
    if not 0 <= (now-snapshot.received_at).total_seconds() <= 10:
        raise Blocked('STALE_ACCOUNT_OBSERVATION')
    if (not snapshot.totals_match or snapshot.cash != snapshot.settled+snapshot.unsettled
            or snapshot.settled < 0 or snapshot.unsettled < 0):
        raise Blocked('CASH_RECONCILIATION_REQUIRED')
    if snapshot.open_orders: raise Blocked('OPEN_OR_UNCERTAIN_ORDER')
    if snapshot.positions != expected or set(expected)-set(ALLOWLIST):
        raise Blocked('POSITION_ALLOWLIST_OR_QUANTITY_MISMATCH')
    for quantity in expected.values():
        if not Decimal('0.00001') < number(quantity) < 1:
            raise Blocked('FRACTIONAL_QUANTITY_UNSUPPORTED')
    if market:
        regular(now)
        for symbol, quantity in expected.items():
            quote = snapshot.quotes.get(symbol)
            if not quote: raise Blocked('MARKET_ENTITLEMENT_OR_QUOTE_UNVERIFIED')
            if quote.get('category') != 'US_ETF': raise Blocked('ETF_QUOTE_CATEGORY_REQUIRED')
            if (not 0 <= (now-quote['at']).total_seconds() <= 10
                    or not 0 <= (now-quote['received_at']).total_seconds() <= 10):
                raise Blocked('STALE_OR_FUTURE_QUOTE')
            bid, ask = number(quote['bid']), number(quote['ask'])
            if not 0 < bid <= ask: raise Blocked('INVALID_QUOTE')
            if quantity*bid < Decimal('0.01'): raise Blocked('FRACTIONAL_SELL_VALUE_UNSUPPORTED')


def order_for(symbol, quantity, client_id):
    order = dict(symbol=symbol, quantity=str(quantity), client_order_id=client_id,
        combo_type='NORMAL', instrument_type='EQUITY', market='US', side='SELL',
        order_type='MARKET', time_in_force='DAY', support_trading_session='CORE', entrust_type='QTY')
    validate_order(order, {symbol: quantity})
    return order


def validate_order(order, owned):
    fixed = dict(combo_type='NORMAL', instrument_type='EQUITY', market='US', side='SELL',
        order_type='MARKET', time_in_force='DAY', support_trading_session='CORE', entrust_type='QTY')
    if (set(order) != set(fixed)|{'symbol', 'quantity', 'client_order_id'}
            or any(order.get(k) != v for k, v in fixed.items())
            or order['symbol'] not in ALLOWLIST
            or not re.fullmatch('[a-f0-9]{32}', order['client_order_id'])):
        raise Blocked('ONLY_ALLOWLIST_CORE_FRACTIONAL_SELL')
    qty = number(order['quantity'])
    if not Decimal('0.00001') < qty < 1 or qty != owned.get(order['symbol']):
        raise Blocked('EXACT_OWNED_QUANTITY_REQUIRED')


def fill_detail(body, order, broker_id):
    if (not isinstance(body, dict) or body.get('client_order_id') != order['client_order_id']
            or not isinstance(body.get('orders'), list) or len(body['orders']) != 1):
        raise Blocked('DETAIL_IDENTITY_MISMATCH')
    row = body['orders'][0]
    for key in ('symbol', 'side', 'order_type', 'time_in_force', 'support_trading_session', 'client_order_id'):
        if row.get(key) != order[key]: raise Blocked('DETAIL_IDENTITY_MISMATCH')
    qty, filled = number(row.get('total_quantity')), number(row.get('filled_quantity'))
    if row.get('order_id') != broker_id or qty != number(order['quantity']):
        raise Blocked('DETAIL_IDENTITY_MISMATCH')
    if row.get('status') == 'PARTIAL_FILLED' or 0 < filled < qty:
        raise Blocked('PARTIAL_FILL_STOP_SEQUENCE')
    if row.get('status') in ('PENDING', 'SUBMITTED') and filled == 0: return None
    if row.get('status') != 'FILLED' or filled != qty: raise Blocked('REJECTED_OR_UNCERTAIN_ORDER')
    price = number(row.get('filled_price'))
    if price <= 0: raise Blocked('INVALID_FILL_PRICE')
    fees, commission = row.get('fees'), row.get('commission')
    if not isinstance(fees, list) or not isinstance(commission, dict): raise Blocked('FEES_UNRESOLVED')
    cost = Decimal(0)
    for fee in fees:
        actual = number(fee.get('actual_value'))
        if actual < 0 or actual != number(fee.get('receivable_value')): raise Blocked('FEES_UNRESOLVED')
        cost += actual
    if commission:
        actual = number(commission.get('actual_commission'))
        if actual < 0 or actual != number(commission.get('receivable_commission')): raise Blocked('FEES_UNRESOLVED')
        cost += actual
    # Do not pretend an average fill price is more precise than the broker supplies.
    net = (qty*price-cost).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    return dict(symbol=order['symbol'], quantity=str(qty), price=str(price),
                fees_usd=str(cost), expected_net_cash_usd=str(net), status='FILLED',
                client_order_id=order['client_order_id'])


@dataclass(frozen=True)
class SubmissionCapability:
    campaign: str
    namespace: str
    account_binding: str
    symbol: str
    quantity: str
    client_order_id: str
    order_hash: str
    token: str = field(repr=False)


class Journal:
    """Append-only local evidence, persistent one-use claim, session writer lock."""
    def __init__(self, db):
        check_namespace(db)
        self.db = db
        self._owner = None
        self.namespace = digest(dict(host=db.host, port=db.port, database=db.database, schema=db.schema))
        with db.connect() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS liquidation_journal (
                seq bigserial PRIMARY KEY, event text NOT NULL, payload jsonb NOT NULL,
                at timestamptz NOT NULL DEFAULT now());
                CREATE UNIQUE INDEX IF NOT EXISTS liquidation_once ON liquidation_journal(event)
                WHERE event='STARTED';
                CREATE UNIQUE INDEX IF NOT EXISTS liquidation_capability_once
                ON liquidation_journal ((payload->>'token_hash')) WHERE event='CAPABILITY_CONSUMED';
                DROP TRIGGER IF EXISTS immutable_liquidation ON liquidation_journal;
                CREATE TRIGGER immutable_liquidation BEFORE UPDATE OR DELETE OR TRUNCATE
                ON liquidation_journal FOR EACH STATEMENT EXECUTE FUNCTION immutable_observation();''')

    def append(self, event, payload):
        with self.db.connect() as c:
            c.execute('INSERT INTO liquidation_journal(event,payload) VALUES(%s,%s)', (event, Jsonb(payload)))

    def events(self):
        with self.db.connect() as c:
            return c.execute('SELECT event,payload FROM liquidation_journal ORDER BY seq').fetchall()

    def _require_owner(self):
        if self._owner is None or self._owner.closed: raise Blocked('CAMPAIGN_WRITER_REQUIRED')
        # A lost PostgreSQL session loses the writer lock and all capability authority.
        try:
            held = self._owner.execute("SELECT EXISTS(SELECT 1 FROM pg_locks WHERE locktype='advisory' "
                "AND pid=pg_backend_pid() AND classid=481516 AND objid=902 AND granted)").fetchone()[0]
            if not held: raise Blocked('CAMPAIGN_WRITER_LOST')
        except Exception: raise Blocked('CAMPAIGN_WRITER_LOST') from None

    def _authority(self, rows, order, account):
        validate_order(order, {order['symbol']: number(order['quantity'])})
        starts = [p for event, p in rows if event == 'STARTED']
        if (len(starts) != 1 or starts[0].get('campaign') != CAMPAIGN
                or starts[0].get('account_binding') != account):
            raise Blocked('DURABLE_CAMPAIGN_CLAIM_REQUIRED')
        if any(event in ('BLOCKED', 'COMPLETE') for event, _ in rows):
            raise Blocked('CAMPAIGN_NO_LONGER_ACTIVE')
        owned = starts[0].get('observation', {}).get('positions', {})
        if set(owned) != set(ALLOWLIST) or number(owned.get(order['symbol'])) != number(order['quantity']):
            raise Blocked('CAMPAIGN_QUANTITY_MISMATCH')
        intents = [p for event, p in rows if event == 'SUBMISSION_INTENT']
        if not intents or intents[-1] != order or sum(p.get('client_order_id') == order['client_order_id'] for p in intents) != 1:
            raise Blocked('EXACT_DURABLE_INTENT_REQUIRED')

    @staticmethod
    def _capability_evidence(cap):
        return dict(campaign=cap.campaign, namespace=cap.namespace, account_binding=cap.account_binding,
            symbol=cap.symbol, quantity=cap.quantity, client_order_id=cap.client_order_id,
            order_hash=cap.order_hash, token_hash=sha256(cap.token.encode()).hexdigest())

    def issue(self, order, account):
        self._require_owner()
        with self.db.connect() as c:
            c.execute('SELECT pg_advisory_xact_lock(481516,903)')
            rows = c.execute('SELECT event,payload FROM liquidation_journal ORDER BY seq').fetchall()
            self._authority(rows, order, account)
            if any(event == 'CAPABILITY_ISSUED' and (p.get('client_order_id') == order['client_order_id']
                    or p.get('symbol') == order['symbol']) for event, p in rows):
                raise Blocked('CAPABILITY_ALREADY_ISSUED')
            cap = SubmissionCapability(CAMPAIGN, self.namespace, account, order['symbol'],
                order['quantity'], order['client_order_id'], digest(order), token_hex(32))
            c.execute('INSERT INTO liquidation_journal(event,payload) VALUES(%s,%s)',
                      ('CAPABILITY_ISSUED', Jsonb(self._capability_evidence(cap))))
        return cap

    def consume(self, cap, order, account):
        self._require_owner()
        if (type(cap) is not SubmissionCapability or cap.campaign != CAMPAIGN or cap.namespace != self.namespace
                or cap.account_binding != account or cap.symbol != order.get('symbol')
                or cap.quantity != order.get('quantity') or cap.client_order_id != order.get('client_order_id')
                or cap.order_hash != digest(order)):
            raise Blocked('JOURNAL_CAPABILITY_REQUIRED')
        expected = self._capability_evidence(cap)
        with self.db.connect() as c:
            c.execute('SELECT pg_advisory_xact_lock(481516,903)')
            rows = c.execute('SELECT event,payload FROM liquidation_journal ORDER BY seq').fetchall()
            self._authority(rows, order, account)  # recheck durable claim + exact intent at POST boundary
            if not any(event == 'CAPABILITY_ISSUED' and p == expected for event, p in rows):
                raise Blocked('JOURNAL_CAPABILITY_NOT_ISSUED')
            if any(event == 'CAPABILITY_CONSUMED' and p.get('client_order_id') == cap.client_order_id for event, p in rows):
                raise Blocked('JOURNAL_CAPABILITY_ALREADY_CONSUMED')
            c.execute('INSERT INTO liquidation_journal(event,payload) VALUES(%s,%s)',
                      ('CAPABILITY_CONSUMED', Jsonb(expected)))
        # The durable commit precedes POST: any subsequent failure burns authority.

    @contextmanager
    def once(self):
        with self.db.connect() as c:
            if not c.execute('SELECT pg_try_advisory_lock(481516,902)').fetchone()[0]:
                raise Blocked('LIQUIDATION_WRITER_BUSY')
            try:
                if any(event == 'STARTED' for event, _ in self.events()):
                    raise Blocked('ONE_TIME_ATTEMPT_ALREADY_CONSUMED')
                self._owner = c
                yield
            finally:
                self._owner = None
                c.execute('SELECT pg_advisory_unlock(481516,902)')


def dry_run(broker, journal, account, clock=lambda: datetime.now(timezone.utc)):
    snapshot = broker.refresh()
    if set(snapshot.positions) != set(ALLOWLIST): raise Blocked('EXACT_FIVE_HOLDINGS_REQUIRED')
    validate_snapshot(snapshot, snapshot.positions, account, clock(), market=False)
    blockers = ['JUDICATOR_APPROVAL_REQUIRED']
    if not FRACTIONAL_SELL_RULES_VERIFIED: blockers.append('FRACTIONAL_SELL_MINIMA_SOURCE_REVIEW')
    try: validate_snapshot(snapshot, snapshot.positions, account, clock())
    except Blocked as exc: blockers.append(str(exc))
    result = dict(campaign=CAMPAIGN, status='PENDING_REVIEW', activated=False,
        observation=snapshot.evidence(), blockers=blockers,
        market_evidence={s: dict(category=q['category'], source_at=q['at'].isoformat(),
            received_at=q['received_at'].isoformat(), bid=str(q['bid']), ask=str(q['ask']))
            for s, q in snapshot.quotes.items()},
        planned_actions=[dict(symbol=s, side='SELL', quantity=str(snapshot.positions[s]),
            order_type='MARKET', session='CORE', entrust_type='QTY') for s in ALLOWLIST])
    journal.append('DRY_RUN', result)
    return result


def execute_once(broker, journal, account, *, clock=lambda: datetime.now(timezone.utc), sleep=time.sleep):
    """Testable sequence. Real transport separately enforces the approval lock.

Only read retries are allowed, bounded to three checks. A crash after STARTED
requires read-only investigation, never re-entry into this sequence.
"""
    with journal.once():
        initial = broker.refresh()
        if set(initial.positions) != set(ALLOWLIST): raise Blocked('EXACT_FIVE_HOLDINGS_REQUIRED')
        validate_snapshot(initial, initial.positions, account, clock())
        journal.append('STARTED', dict(campaign=CAMPAIGN, account_binding=account, observation=initial.evidence()))
        expected, net_total = dict(initial.positions), Decimal(0)
        try:
            for symbol in ALLOWLIST:
                current = broker.refresh()
                validate_snapshot(current, expected, account, clock())
                if current.settled != initial.settled or current.unsettled != initial.unsettled+net_total:
                    raise Blocked('CASH_MOVEMENT_MISMATCH')
                order = order_for(symbol, expected[symbol], uuid4().hex)
                journal.append('SUBMISSION_INTENT', order)  # committed before network
                capability = journal.issue(order, account)
                # Recheck wall clock after durable I/O and before the write.
                validate_snapshot(current, expected, account, clock())
                response = broker.sell(order, current, capability=capability)
                if (not isinstance(response, dict) or response.get('client_order_id') != order['client_order_id']
                        or not isinstance(response.get('order_id'), str) or not response['order_id']):
                    raise Blocked('SUBMISSION_UNCERTAIN')
                journal.append('ACKNOWLEDGED', dict(client_order_id=order['client_order_id']))
                fill = None
                for attempt in range(3):
                    fill = fill_detail(broker.detail(order['client_order_id']), order, response['order_id'])
                    if fill is not None: break
                    if attempt < 2: sleep(2)
                if fill is None: raise Blocked('ORDER_FINALITY_UNCERTAIN')
                journal.append('FILL', fill)
                del expected[symbol]
                net_total += number(fill['expected_net_cash_usd'])
                after = broker.refresh()
                validate_snapshot(after, expected, account, clock(), market=False)
                journal.append('POST_FILL_OBSERVATION', after.evidence())
                if after.settled != initial.settled or after.unsettled != initial.unsettled+net_total:
                    raise Blocked('CASH_MOVEMENT_MISMATCH')
            journal.append('COMPLETE', dict(status='COMPLETE', account_binding=account,
                expected_net_cash_usd=str(net_total), observation=after.evidence()))
            return dict(status='COMPLETE', positions={}, net_cash_usd=str(net_total))
        except Exception as exc:
            code = str(exc) if isinstance(exc, Blocked) else 'BROKER_OR_LOCAL_FAILURE_UNCERTAIN'
            journal.append('BLOCKED', dict(status='BLOCKED', blocker=code))
            raise Blocked(code) from None


def dashboard_status(db):
    """Read-only projection; never provisions tables or triggers a broker read."""
    try:
        check_namespace(db)
        with db.connect() as c:
            c.execute('SET TRANSACTION READ ONLY')
            if c.execute("SELECT to_regclass('liquidation_journal')").fetchone()[0] is None:
                return {'status': 'PENDING_REVIEW', 'activated': False}
            rows = c.execute('SELECT event,payload,at FROM liquidation_journal ORDER BY seq').fetchall()
        started = any(event == 'STARTED' for event, _, _ in rows)
        completed = [payload for event, payload, _ in rows if event == 'COMPLETE']
        blocked = [payload for event, payload, _ in rows if event == 'BLOCKED']
        status = 'COMPLETE' if completed else 'BLOCKED' if blocked else 'INCOMPLETE_UNCERTAIN' if started else 'PENDING_REVIEW'
        dry = [payload for event, payload, _ in rows if event == 'DRY_RUN']
        return dict(status=status, activated=EXECUTION_APPROVED, attempt_consumed=started,
            filled_count=sum(event == 'FILL' for event, _, _ in rows),
            last_event_at=rows[-1][2].isoformat() if rows else None,
            blocker=blocked[-1]['blocker'] if blocked else None,
            dry_run_blockers=dry[-1]['blockers'] if dry else [],
            planned_actions=dry[-1]['planned_actions'] if dry else [],
            final_observation=completed[-1]['observation'] if completed else None)
    except Exception:
        return {'status': 'EVIDENCE_UNAVAILABLE', 'activated': False}
