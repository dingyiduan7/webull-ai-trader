"""Owner-authorized essential M3B round trip; never a general execution gateway."""
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from hashlib import sha256
import json
from uuid import uuid4
from webull.trade.request.v3.place_order_request import PlaceOrderRequest
from .controls import MODE, exact_decimal, regular_session, validate_single_order, FlatPrerequisites
from .first_order import FirstOrderHttp, exact_order_detail
from .journal import CertificationJournal, canonical
from ..evidence import select_account
from ..secrets import PreflightError
from ..quiet import quiet_sdk

AUTHORIZATION = 'OWNER_2026_09_11_ESSENTIAL_ROUND_TRIP'


def checked_detail(body, order, broker_id):
    if not isinstance(body, dict) or body.get('client_order_id') != order['client_order_id']:
        raise PreflightError('DETAIL_GROUP_ID_MISMATCH')
    # Reuse strict quantity/status parsing without expanding the old BUY-only API.
    rows = body.get('orders')
    if not isinstance(rows, list) or len(rows) != 1 or rows[0].get('side') != order['side']:
        raise PreflightError('DETAIL_SIDE_MISMATCH')
    row = exact_order_detail(dict(body, orders=[dict(rows[0], side='BUY')]), order['client_order_id'])
    if (row['order_id'] != broker_id or body.get('combo_order_id') != broker_id
            or exact_decimal(row.get('limit_price')) != exact_decimal(order['limit_price'])
            or row.get('time_in_force') != 'DAY' or row.get('support_trading_session') != 'CORE'):
        raise PreflightError('DETAIL_ORDER_BINDING_MISMATCH')
    if row['status'] == 'FILLED':
        price = exact_decimal(row.get('filled_price'))
        limit = exact_decimal(order['limit_price'])
        if (exact_decimal(row['filled_quantity']) != 1 or price <= 0
                or (order['side'] == 'BUY' and price > limit)
                or (order['side'] == 'SELL' and price < limit)):
            raise PreflightError('FILL_QUANTITY_OR_PRICE_CONFLICT')
    return rows[0]


def owned_share(body):
    if not isinstance(body, list) or len(body) != 1 or not isinstance(body[0], dict):
        raise PreflightError('EXACT_ONE_POSITION_REQUIRED')
    row = body[0]
    if row.get('symbol') != 'AAPL':
        raise PreflightError('POSITION_SYMBOL_MISMATCH')
    # Explicit conservative candidates, never infer holdings from buying power.
    quantities = [row[k] for k in ('quantity', 'qty', 'total_quantity') if k in row]
    if not quantities or any(exact_decimal(q) != 1 for q in quantities):
        raise PreflightError('POSITION_QUANTITY_NOT_ONE')
    for key in ('available_quantity', 'sellable_quantity'):
        if key in row and exact_decimal(row[key]) != 1:
            raise PreflightError('POSITION_NOT_FULLY_AVAILABLE')
    for key, allowed in (('side', ('BUY', 'LONG')), ('direction', ('LONG', 'BUY')),
            ('instrument_type', ('EQUITY',)), ('category', ('US_STOCK',))):
        if key in row and row[key] not in allowed:
            raise PreflightError('POSITION_CLASS_OR_DIRECTION_CONFLICT')
    return row


def account_state(observations, account_id, now, owned=False):
    regular_session(now)
    if any(o.status_code != 200 or not 0 <= (now-datetime.fromisoformat(o.received_at)).total_seconds() <= 30
            for o in observations.values()):
        raise PreflightError('STALE_OR_FAILED_REST_STATE')
    if select_account(observations['accounts'].body, account_id).get('account_class') != 'INDIVIDUAL_CASH':
        raise PreflightError('WRONG_ACCOUNT_CLASS')
    if observations['orders'].body != {'data': []}:
        raise PreflightError('OPEN_OR_UNRECOGNIZED_ORDERS')
    if owned:
        owned_share(observations['positions'].body)
    elif observations['positions'].body != []:
        raise PreflightError('ACCOUNT_NOT_FLAT')
    body = observations['balance'].body
    assets = body.get('account_currency_assets') if isinstance(body, dict) else None
    if not isinstance(assets, list) or len(assets) != 1 or assets[0].get('currency') != 'USD':
        raise PreflightError('BALANCE_SHAPE_CHANGED')
    cash = assets[0]
    nums = {k: exact_decimal(cash.get(k)) for k in ('net_liquidation_value', 'cash_balance',
        'settled_cash', 'unsettled_cash', 'market_value', 'day_profit_loss', 'buying_power')}
    if (nums['net_liquidation_value'] < Decimal('999000') or nums['settled_cash'] < 0
            or nums['cash_balance'] < 0 or abs(nums['day_profit_loss']) > 1000
            or (owned and not 0 < nums['market_value'] <= 1000)
            or (not owned and nums['market_value'] != 0)):
        raise PreflightError('ACCOUNT_STATE_OUTSIDE_TINY_TEST_BOUNDS')
    return FlatPrerequisites(account_id, now, nums['net_liquidation_value'], nums['settled_cash'])


def marketable_order(snapshot, side, now, client_id=None):
    if side not in ('BUY', 'SELL') or not isinstance(snapshot, list) or len(snapshot) != 1:
        raise PreflightError('QUOTE_SHAPE_OR_SIDE')
    row = snapshot[0]
    if row.get('symbol') != 'AAPL':
        raise PreflightError('QUOTE_SYMBOL')
    bid, ask = exact_decimal(row.get('bid')), exact_decimal(row.get('ask'))
    if not 0 < bid <= ask or (ask-bid)/bid > Decimal('.002'):
        raise PreflightError('QUOTE_SPREAD')
    quote_at = datetime.fromtimestamp(int(row['quote_time'])/1000, timezone.utc)
    if not 0 <= (now-quote_at).total_seconds() <= 2:
        raise PreflightError('QUOTE_STALE_OR_FUTURE')
    price = (ask*Decimal('1.002') if side == 'BUY' else bid*Decimal('.998')).quantize(
        Decimal('.01'), rounding=ROUND_CEILING if side == 'BUY' else ROUND_FLOOR)
    order = dict(client_order_id=client_id or uuid4().hex, combo_type='NORMAL', symbol='AAPL',
        instrument_type='EQUITY', market='US', order_type='LIMIT', limit_price=str(price), quantity='1',
        side=side, time_in_force='DAY', support_trading_session='CORE', entrust_type='QTY')
    return order


class RoundTripJournal(CertificationJournal):
    def released(self):
        return bool(self.db.execute("SELECT 1 FROM events WHERE kind='OWNER_PRIOR_RESERVATION_RELEASE'").fetchone())

    def release_prior(self, evidence):
        # Call only following fresh exact canceled detail and flat $1m cash checks.
        if evidence != {'authorization': AUTHORIZATION, 'cancelled_zero_fill': True,
                'matching_identifiers': True, 'flat_unchanged_cash': True, 'released_cash': '324.34'}:
            raise PreflightError('OWNER_RELEASE_EVIDENCE_REQUIRED')
        with self.transaction():
            if not self.released():
                rows = self.db.execute("SELECT intent_id FROM intents WHERE kind='PLACE' AND claimed=1").fetchall()
                if len(rows) != 1:
                    raise PreflightError('PRIOR_INTENT_NOT_UNIQUE')
                self._event('OWNER_PRIOR_RESERVATION_RELEASE', rows[0][0], evidence)

    def prepare_leg(self, order):
        kind = 'ROUND_TRIP_' + order['side']
        if kind not in ('ROUND_TRIP_BUY', 'ROUND_TRIP_SELL'):
            raise PreflightError('ROUND_TRIP_SIDE')
        text = canonical(order)
        intent_id = uuid4().hex
        with self.transaction():
            if not self.released() or self.db.execute('SELECT 1 FROM intents WHERE kind=?', (kind,)).fetchone():
                raise PreflightError('ROUND_TRIP_NOT_RELEASED_OR_ALREADY_PREPARED')
            if kind == 'ROUND_TRIP_SELL' and not self.db.execute(
                    "SELECT 1 FROM events WHERE kind='ROUND_TRIP_BUY_FILLED_RECONCILED'").fetchone():
                raise PreflightError('OWNED_BUY_NOT_RECONCILED')
            self.db.execute('INSERT INTO intents VALUES (?,?,?,?,0)',
                (intent_id, kind, text, sha256(text.encode()).hexdigest()))
            self._event('PREPARED', intent_id, {'kind': kind, 'payload': order})
        return intent_id


class RoundTripHttp(FirstOrderHttp):
    @quiet_sdk()
    def submit_leg(self, intent_id, order, observations, snapshot, buy_proof=None):
        now = datetime.now(timezone.utc)
        side = order.get('side')
        if side not in ('BUY', 'SELL'):
            raise PreflightError('ROUND_TRIP_SIDE')
        prerequisites = account_state(observations, self._account_id, now, owned=side == 'SELL')
        expected = marketable_order(snapshot.body, side, now, order.get('client_order_id'))
        if snapshot.status_code != 200 or order != expected:
            raise PreflightError('EXACT_BOUNDED_MARKETABLE_ORDER_REQUIRED')
        validate_single_order(dict(order, side='BUY'), prerequisites, now)
        if side == 'BUY':
            from zoneinfo import ZoneInfo
            local = now.astimezone(ZoneInfo('America/New_York'))
            if local.hour*60+local.minute >= 15*60+50:
                raise PreflightError('TOO_LATE_FOR_ROUND_TRIP_ENTRY')
        else:
            if buy_proof is None:
                raise PreflightError('FRESH_BUY_FILL_PROOF_REQUIRED')
            detail, buy_order, broker_id = buy_proof
            row = checked_detail(detail.body, buy_order, broker_id)
            if (buy_order['side'] != 'BUY' or detail.status_code != 200 or row['status'] != 'FILLED'
                    or not 0 <= (now-datetime.fromisoformat(detail.received_at)).total_seconds() <= 30):
                raise PreflightError('FRESH_BUY_FILL_PROOF_REQUIRED')
            stored = self._journal.db.execute("SELECT payload FROM intents WHERE kind='ROUND_TRIP_BUY' AND claimed=1").fetchall()
            if len(stored) != 1 or json.loads(stored[0][0]) != buy_order:
                raise PreflightError('BUY_PROOF_NOT_THIS_ROUND_TRIP')
        request = PlaceOrderRequest()
        request.set_account_id(self._account_id)
        request.set_new_orders([order])
        request.add_custom_headers_from_order([order])
        self._journal.claim(intent_id, 'ROUND_TRIP_'+side, order)
        return self._send_bounded(request, '/trading/orders/place')
