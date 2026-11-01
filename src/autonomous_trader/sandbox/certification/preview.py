"""Preflight order estimate only: place/replace/cancel remain denied."""
from datetime import datetime, timezone
from hashlib import sha256
from urllib.parse import urlsplit
from webull.trade.request.v3.preview_order_request import PreviewOrderRequest
from .controls import MODE, validate_single_order
from .discovery import DiscoveryHttp
from ..http import HTTP_HOST
from ..quiet import quiet_sdk
from ..secrets import PreflightError


class PreviewHttp(DiscoveryHttp):
    def __init__(self, credentials, *, mode, journal, **kwargs):
        if mode != MODE or journal.account_hash != sha256(credentials.account_id.encode()).hexdigest():
            raise PreflightError("CERTIFICATION_MODE_OR_ACCOUNT_MISMATCH")
        super().__init__(credentials, **kwargs)
        self._account_id = credentials.account_id
        self._journal = journal

    @quiet_sdk()
    def preview(self, intent_id, order, prerequisites):
        now = datetime.now(timezone.utc)
        if prerequisites.account_id != self._account_id:
            raise PreflightError("PREREQUISITE_ACCOUNT_MISMATCH")
        validate_single_order(order, prerequisites, now)
        request = PreviewOrderRequest()
        request.set_account_id(self._account_id)
        request.set_new_orders([order])
        target = urlsplit(request.get_url())
        if request.get_method() != "POST" or target.path != "/trading/orders/preview" or target.netloc:
            raise PreflightError("PREVIEW_REQUEST_MISMATCH")
        # Precommit precedes signing and transmission. Failure never rearms ID.
        self._journal.claim(intent_id, "PREVIEW", order)
        return self._send_bounded(request, target.path)
