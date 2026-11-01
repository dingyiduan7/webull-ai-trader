"""Locked, sell-only broker boundary for the one-time external-holdings disposal."""
from datetime import datetime, timezone
from decimal import Decimal
import json
from urllib.parse import urlsplit

import requests
from webull.trade.request.v3.place_order_request import PlaceOrderRequest
from webull.trade.request.v3.get_order_detail_request import OrderDetailRequest
from . import real_liquidation as policy
from .real_preflight import (RealReadOnly, RealReadOnlyHttp, Operation, require_success,
                            select_single_individual_cash)
from .real_readiness import account_readiness
from .real_state import binding, digest
from .sandbox.quiet import quiet_sdk
from .sandbox.http import build_request

REAL_LIQUIDATION_NAMESPACE = digest(dict(host='127.0.0.1', port=55433, database='postgres', schema='real_readiness'))


class LiquidationBroker:
    def __init__(self, credentials, expected_account_binding, *, journal=None):
        self.reader = RealReadOnly(credentials)
        self.http = RealReadOnlyHttp(credentials)
        self.expected = expected_account_binding
        self._account_id = None
        self._latest = None
        self._journal = journal

    def refresh(self):
        # Authenticate again through a signed account-list call on every refresh.
        accounts = require_success(Operation.ACCOUNTS, self.reader.read(Operation.ACCOUNTS))
        account = select_single_individual_cash(accounts.body)
        if binding(account['account_id']) != self.expected: raise policy.Blocked('ACCOUNT_BINDING_MISMATCH')
        self._account_id = account['account_id']
        observations = [accounts]
        for operation in (Operation.POSITIONS, Operation.OPEN_ORDERS, Operation.BALANCE):
            observations.append(require_success(operation, self.reader.read(operation, account_id=self._account_id)))
        _, positions, orders, balance = observations
        at = min(datetime.fromisoformat(o.received_at) for o in observations)
        ready = account_readiness(balance.body, positions.body, orders.body, at)
        if any(h.symbol not in policy.ALLOWLIST for h in ready.external_holdings):
            raise policy.Blocked('POSITION_ALLOWLIST_OR_QUANTITY_MISMATCH')
        quotes = {}
        try: policy.regular(datetime.now(timezone.utc))
        except policy.Blocked: pass  # Closed-session dry run never claims current entitlement.
        else:
            if ready.external_holdings:
                symbols = tuple(h.symbol for h in ready.external_holdings)
                request = build_request(Operation.SNAPSHOT, symbols=symbols)
                request.set_category('US_ETF')
                observation = require_success(Operation.SNAPSHOT, self.http.get_response(request))
                if not isinstance(observation.body, list): raise policy.Blocked('MARKET_QUOTE_SHAPE')
                for row in observation.body:
                    symbol = row.get('symbol')
                    if (symbol not in symbols or symbol in quotes or row.get('category', 'US_ETF') != 'US_ETF'):
                        raise policy.Blocked('MARKET_QUOTE_IDENTITY')
                    quotes[symbol] = dict(category='US_ETF', bid=policy.number(row.get('bid')), ask=policy.number(row.get('ask')),
                        at=datetime.fromtimestamp(int(row['quote_time'])/1000, timezone.utc),
                        received_at=datetime.fromisoformat(observation.received_at))
                if set(quotes) != set(symbols): raise policy.Blocked('MARKET_QUOTE_IDENTITY')
        self._latest = policy.Snapshot(self.expected, at,
            {h.symbol: h.quantity for h in ready.external_holdings}, ready.settled_usd, ready.unsettled_usd,
            ready.equity_usd, ready.settled_usd+ready.unsettled_usd, tuple(orders.body['data']), quotes,
            totals_match=ready.account_totals_match)
        return self._latest

    def sell(self, order, snapshot, *, capability=None):
        if type(self._journal) is not policy.Journal or type(capability) is not policy.SubmissionCapability:
            raise policy.Blocked('JOURNAL_CAPABILITY_REQUIRED')
        if self._journal.namespace != REAL_LIQUIDATION_NAMESPACE:
            raise policy.Blocked('REAL_JOURNAL_NAMESPACE_REQUIRED')
        if not policy.EXECUTION_APPROVED: raise policy.Blocked('JUDICATOR_APPROVAL_REQUIRED')
        if not policy.FRACTIONAL_SELL_RULES_VERIFIED: raise policy.Blocked('FRACTIONAL_SELL_MINIMA_SOURCE_REVIEW')
        if self._account_id is None: raise policy.Blocked('AUTHENTICATED_ACCOUNT_REQUIRED')
        if snapshot is not self._latest: raise policy.Blocked('BROKER_OBSERVATION_REQUIRED')
        policy.validate_snapshot(snapshot, snapshot.positions, self.expected, datetime.now(timezone.utc))
        order = dict(order)
        policy.validate_order(order, snapshot.positions)
        request = PlaceOrderRequest()
        request.set_account_id(self._account_id)
        request.set_new_orders([order])
        request.add_custom_headers_from_order([order])
        return self._send(request, order=order, capability=capability)

    def detail(self, client_id):
        if self._account_id is None or not __import__('re').fullmatch('[a-f0-9]{32}', client_id):
            raise policy.Blocked('DETAIL_IDENTITY_MISMATCH')
        request = OrderDetailRequest()
        request.set_account_id(self._account_id)
        request.set_client_order_id(client_id)
        return self._send(request)

    @quiet_sdk()
    def _send(self, request, *, order=None, capability=None):
        # No SDK retries, proxy inheritance, redirects, generic POST or cancel route.
        target = urlsplit(request.get_url())
        if target.scheme or target.netloc or request.get_endpoint() not in (None, 'api.webull.com'):
            raise policy.Blocked('REAL_ENDPOINT_REQUIRED')
        if order is not None:
            if type(self._journal) is not policy.Journal or type(capability) is not policy.SubmissionCapability:
                raise policy.Blocked('JOURNAL_CAPABILITY_REQUIRED')
            if self._journal.namespace != REAL_LIQUIDATION_NAMESPACE:
                raise policy.Blocked('REAL_JOURNAL_NAMESPACE_REQUIRED')
            if not policy.EXECUTION_APPROVED or not policy.FRACTIONAL_SELL_RULES_VERIFIED:
                raise policy.Blocked('JUDICATOR_APPROVAL_REQUIRED')
            policy.regular(datetime.now(timezone.utc))
            if self._latest is None: raise policy.Blocked('BROKER_OBSERVATION_REQUIRED')
            policy.validate_order(order, self._latest.positions)
            policy.validate_snapshot(self._latest, self._latest.positions, self.expected, datetime.now(timezone.utc))
            if (request.get_method() != 'POST' or target.path != '/trading/orders/place'
                    or request.get_body_params() != {'account_id': self._account_id, 'new_orders': [order]}):
                raise policy.Blocked('ONLY_ALLOWLIST_CORE_FRACTIONAL_SELL')
        elif request.get_method() != 'GET' or target.path != '/trading/orders/get' or request.get_body_params():
            raise policy.Blocked('DETAIL_READ_ONLY')
        wire = self.http._make_http_response('api.webull.com', request, 10, 5)
        try:
            with requests.Session() as session:
                session.trust_env = False
                prepared = session.prepare_request(requests.Request(request.get_method(),
                    'https://api.webull.com'+wire.get_url(), headers=wire.get_headers(), data=wire.get_body()))
                if order is not None:
                    policy.validate_snapshot(self._latest, self._latest.positions, self.expected, datetime.now(timezone.utc))
                    self._journal.consume(capability, order, self.expected)
                    # No retry or fallible external operation between durable consumption and POST.
                with session.send(prepared, allow_redirects=False, verify=True, timeout=(5, 10), stream=True) as response:
                    if response.status_code != 200: raise policy.Blocked('BROKER_REJECTED_OR_UNCERTAIN')
                    chunks, size = [], 0
                    for chunk in response.iter_content(65536):
                        size += len(chunk)
                        if size > 2_000_000: raise policy.Blocked('RESPONSE_TOO_LARGE_UNCERTAIN')
                        chunks.append(chunk)
                    return json.loads(b''.join(chunks), parse_float=Decimal,
                        parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except policy.Blocked: raise
        except Exception: raise policy.Blocked('TRANSPORT_UNCERTAIN_NO_RETRY') from None
