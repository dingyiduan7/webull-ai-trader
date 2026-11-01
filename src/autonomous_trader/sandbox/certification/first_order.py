"""Exactly one initial non-marketable buy/cancel certification sequence.

No replacement, executable buy, sale, bracket, generic POST or automatic retry.
Those stages require successful evidence from this stage first.
"""
from datetime import datetime, timezone
from hashlib import sha256
from urllib.parse import urlsplit
from webull.trade.request.v3.place_order_request import PlaceOrderRequest
from webull.trade.request.v3.cancel_order_request import CancelOrderRequest
from webull.trade.request.v3.get_order_detail_request import OrderDetailRequest
from .controls import MODE, validate_single_order, regular_session, exact_decimal
from .discovery import DiscoveryHttp
from ..http import HTTP_HOST
from ..quiet import quiet_sdk
from ..secrets import PreflightError


def exact_order_detail(body, client_id):
    if not isinstance(body, dict) or not isinstance(body.get("orders"), list) or len(body["orders"]) != 1:
        raise PreflightError("UNRECOGNIZED_ORDER_DETAIL")
    row = body["orders"][0]
    if (not isinstance(row, dict) or row.get("client_order_id") != client_id
            or row.get("symbol") != "AAPL" or row.get("side") != "BUY"
            or row.get("order_type") != "LIMIT" or not row.get("order_id")):
        raise PreflightError("ORDER_DETAIL_BINDING_MISMATCH")
    # The official detail schema calls these fields total_quantity and
    # filled_quantity; quantities are not inferred from absent fields.
    if exact_decimal(row.get("total_quantity")) != 1:
        raise PreflightError("ORDER_DETAIL_QUANTITY_MISMATCH")
    filled = exact_decimal(row.get("filled_quantity"))
    if not 0 <= filled <= 1:
        raise PreflightError("ORDER_DETAIL_FILLED_QUANTITY_INVALID")
    if row.get("status") not in ("PENDING", "SUBMITTED", "CANCELLED", "FILLED", "FAILED", "PARTIAL_FILLED"):
        raise PreflightError("UNKNOWN_ORDER_STATUS")
    return row


class FirstOrderHttp(DiscoveryHttp):
    def __init__(self, credentials, *, mode, journal, **kwargs):
        if mode != MODE or journal.account_hash != sha256(credentials.account_id.encode()).hexdigest():
            raise PreflightError("CERTIFICATION_MODE_OR_ACCOUNT_MISMATCH")
        super().__init__(credentials, **kwargs)
        self._account_id, self._journal = credentials.account_id, journal
        self._endpoint_times = {}

    def _send_bounded(self, request, path):
        # Each official limit is per endpoint. Independent endpoints do not
        # delay an already checked cancellation or age a just-read quote.
        self._last_request = self._endpoint_times.get(path)
        try:
            return super()._send_bounded(request, path)
        finally:
            self._endpoint_times[path] = self._last_request

    @quiet_sdk()
    def detail(self, client_id):
        request = OrderDetailRequest()
        request.set_account_id(self._account_id)
        request.set_client_order_id(client_id)
        return self._send_bounded(request, "/trading/orders/get")

    @quiet_sdk()
    def submit_first(self, intent_id, order, prerequisites, bid, quote_at, stream_ready):
        now = datetime.now(timezone.utc)
        validate_single_order(order, prerequisites, now)
        if prerequisites.account_id != self._account_id or not stream_ready:
            raise PreflightError("ACCOUNT_OR_EVENT_STREAM_NOT_READY")
        if not 0 <= (now - quote_at).total_seconds() <= 2:
            raise PreflightError("QUOTE_STALE_OR_FUTURE")
        if exact_decimal(order["limit_price"]) > bid * exact_decimal("0.90"):
            raise PreflightError("NON_MARKETABLE_COLLAR_REQUIRED")
        request = PlaceOrderRequest()
        request.set_account_id(self._account_id)
        request.set_new_orders([order])
        request.add_custom_headers_from_order([order])
        self._journal.claim(intent_id, "PLACE", order)
        return self._send_bounded(request, "/trading/orders/place")

    @quiet_sdk()
    def cancel_known_unfilled(self, intent_id, client_id, detail, prerequisites, stream_ready):
        now = datetime.now(timezone.utc)
        regular_session(now)
        if (prerequisites.account_id != self._account_id or not stream_ready
                or not 0 <= (now - prerequisites.received_at).total_seconds() <= 5):
            raise PreflightError("CANCEL_PREREQUISITES_NOT_READY")
        row = exact_order_detail(detail.body, client_id)
        if (detail.status_code != 200 or not 0 <= (now - datetime.fromisoformat(detail.received_at)).total_seconds() <= 5
                or row["status"] not in ("PENDING", "SUBMITTED") or exact_decimal(row["filled_quantity"]) != 0):
            raise PreflightError("CANCEL_TARGET_NOT_KNOWN_UNFILLED")
        recorded = self._journal.db.execute("SELECT payload FROM intents WHERE kind='PLACE'").fetchall()
        import json
        if len(recorded) != 1 or json.loads(recorded[0][0]).get("client_order_id") != client_id:
            raise PreflightError("CANCEL_NOT_OWN_CERTIFICATION_ORDER")
        request = CancelOrderRequest()
        request.set_account_id(self._account_id)
        request.set_client_order_id(client_id)
        self._journal.claim(intent_id, "CANCEL", {"client_order_id": client_id})
        return self._send_bounded(request, "/trading/orders/cancel")
