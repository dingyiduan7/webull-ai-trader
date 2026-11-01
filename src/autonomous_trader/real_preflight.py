"""Bounded read-only Webull production preflight; never execution authority."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
import json
from urllib.parse import urlsplit

import requests
from webull.core.client import ApiClient

from .real_secrets import RealCredentials
from .sandbox.evidence import account_rows
from .sandbox.http import Operation, build_request, SAFE_HEADERS
from .sandbox.quiet import quiet_sdk


REAL_HTTP_HOST = "api.webull.com"
REAL_READ_PATHS = frozenset({
    "/trading/accounts/list",
    "/trading/assets/balances/get",
    "/trading/assets/positions/list",
    "/trading/orders/open-orders/list",
    "/market-data/stocks/snapshots/list",
})
REAL_OPERATIONS = frozenset({Operation.ACCOUNTS, Operation.BALANCE, Operation.POSITIONS,
    Operation.OPEN_ORDERS, Operation.SNAPSHOT})


class RealPreflightError(ValueError):
    """Fixed codes only; broker payloads and identifiers never enter errors."""


@dataclass(frozen=True)
class RealObservation:
    path: str
    status_code: int
    received_at: str
    headers: dict = field(repr=False)
    body: object = field(repr=False)


def validate_real_read_request(request):
    target = urlsplit(request.get_url())
    if request.get_endpoint() not in (None, REAL_HTTP_HOST):
        raise RealPreflightError("NON_REAL_ENDPOINT_REJECTED")
    if target.scheme or target.netloc or target.fragment or request.get_method() != "GET":
        raise RealPreflightError("REAL_READ_ONLY_ENDPOINT_REJECTED")
    if target.path not in REAL_READ_PATHS or request.get_body_params():
        raise RealPreflightError("REAL_READ_ONLY_ENDPOINT_REJECTED")
    return target.path


class RealReadOnlyHttp(ApiClient):
    def __init__(self, credentials: RealCredentials, *, session_factory=requests.Session):
        super().__init__(credentials.app_key, credentials.app_secret, "us", verify=True,
            auto_retry=False, connect_timeout=5, timeout=10)
        self.set_token(credentials.access_token or None)
        self.add_endpoint("us", REAL_HTTP_HOST)
        self._session_factory = session_factory
        self.contacted = []

    @quiet_sdk()
    def get_response(self, request):
        path = validate_real_read_request(request)
        wire = self._make_http_response(REAL_HTTP_HOST, request, 10, 5)
        url = "https://" + REAL_HTTP_HOST + wire.get_url()
        if urlsplit(url).hostname != REAL_HTTP_HOST:
            raise RealPreflightError("NON_REAL_ENDPOINT_REJECTED")
        try:
            with self._session_factory() as session:
                session.trust_env = False
                prepared = session.prepare_request(requests.Request(request.get_method(), url,
                    headers=wire.get_headers(), data=wire.get_body()))
                self.contacted.append((request.get_method(), path))
                with session.send(prepared, allow_redirects=False, verify=True,
                        timeout=(5, 10), stream=True) as response:
                    chunks, size = [], 0
                    for chunk in response.iter_content(65536):
                        size += len(chunk)
                        if size > 2_000_000:
                            raise RealPreflightError("REAL_RESPONSE_SIZE_LIMIT")
                        chunks.append(chunk)
                    try:
                        body = json.loads(b"".join(chunks), parse_float=Decimal,
                            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                    except (ValueError, UnicodeError):
                        body = {"non_json_response": True}
                    headers = {k.lower(): v for k, v in response.headers.items() if k.lower() in SAFE_HEADERS}
                    return RealObservation(path, response.status_code,
                        datetime.now(timezone.utc).isoformat(), headers, body)
        except RealPreflightError:
            raise
        except Exception:
            raise RealPreflightError("REAL_HTTP_TRANSPORT_FAILED") from None


class RealReadOnly:
    def __init__(self, credentials, **transport_options):
        self._http = RealReadOnlyHttp(credentials, **transport_options)

    def read(self, operation, *, account_id="", cursor=None, symbols=("AAPL",)):
        operation = Operation(operation)
        if operation not in REAL_OPERATIONS:
            raise RealPreflightError("REAL_READ_ONLY_OPERATION_REJECTED")
        return self._http.get_response(build_request(operation, account_id, cursor, symbols))


def select_single_individual_cash(body):
    try:
        rows = account_rows(body)
    except Exception:
        raise RealPreflightError("REAL_ACCOUNT_LIST_SHAPE") from None
    eligible = [row for row in rows if row.get("account_type") == "CASH"
        and row.get("account_class") == "INDIVIDUAL_CASH" and row.get("account_id")]
    if len(eligible) != 1:
        raise RealPreflightError("REAL_CASH_ACCOUNT_SELECTION_AMBIGUOUS")
    return eligible[0]


def status_error(operation, status_code):
    if status_code in (401, 407):
        return "REAL_AUTHENTICATION_FAILED"
    if status_code == 403:
        return "REAL_MARKET_DATA_PERMISSION_DENIED" if operation == Operation.SNAPSHOT else "REAL_ACCOUNT_PERMISSION_DENIED"
    if status_code == 429:
        return "REAL_RATE_LIMITED"
    return "REAL_READ_ENDPOINT_FAILED"


def require_success(operation, observation):
    if observation.status_code != 200:
        raise RealPreflightError(status_error(operation, observation.status_code))
    return observation


def _money(value):
    try:
        number = Decimal(str(value))
        if not number.is_finite():
            raise InvalidOperation
        return format(number.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN), "f")
    except (InvalidOperation, TypeError, ValueError):
        raise RealPreflightError("REAL_MONEY_FIELD_INVALID") from None


def market_freshness(snapshot, received_at, now):
    if not isinstance(snapshot, list) or len(snapshot) != 1 or snapshot[0].get("symbol") != "AAPL":
        raise RealPreflightError("REAL_MARKET_DATA_SHAPE")
    row = snapshot[0]
    try:
        source = datetime.fromtimestamp(int(row["quote_time"]) / 1000, timezone.utc)
        received = datetime.fromisoformat(received_at)
        age = received - source
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        raise RealPreflightError("REAL_MARKET_TIMESTAMP_INVALID") from None
    if received.tzinfo is None or not timedelta_zero() <= now - received <= __import__("datetime").timedelta(seconds=30):
        raise RealPreflightError("REAL_MARKET_OBSERVATION_STALE")
    seconds = Decimal(str(age.total_seconds())).quantize(Decimal("0.001"))
    if seconds < Decimal("-2"):
        finding = "FUTURE_TIMESTAMP_INVALID"
    elif seconds <= Decimal("10"):
        finding = "OBSERVED_CURRENT_SAMPLE"
    else:
        finding = "OBSERVED_DELAYED_OR_STALE_SAMPLE"
    metadata = any(key in row for key in ("entitlement", "entitlement_status", "quote_delay", "data_source"))
    return {"symbol": "AAPL", "finding": finding, "quote_age_seconds": str(seconds),
        "explicit_entitlement_metadata": metadata}


def timedelta_zero():
    return __import__("datetime").timedelta(0)


def high_level_summary(account, balance, positions, orders, snapshot, snapshot_received_at, now):
    assets = balance.get("account_currency_assets") if isinstance(balance, dict) else None
    if not isinstance(assets, list) or len(assets) != 1 or assets[0].get("currency") != "USD":
        raise RealPreflightError("REAL_USD_CASH_SHAPE")
    cash = assets[0]
    if not isinstance(positions, list) or any(not isinstance(row, dict) for row in positions):
        raise RealPreflightError("REAL_POSITION_LIST_SHAPE")
    if not isinstance(orders, list) or any(not isinstance(row, dict) for row in orders):
        raise RealPreflightError("REAL_OPEN_ORDER_LIST_SHAPE")
    try:
        position_count = sum(Decimal(str(row.get("quantity", "0"))) != 0 for row in positions)
    except (InvalidOperation, TypeError, ValueError):
        raise RealPreflightError("REAL_POSITION_QUANTITY_INVALID") from None
    return {
        "result": "READ_ONLY_REAL_PREFLIGHT_COMPLETE",
        "environment": "REAL_ACCOUNT_READ_ONLY_PREFLIGHT",
        "production_execution_enabled": False,
        "account": {"account_type": account["account_type"], "account_class": account["account_class"],
            "currency": "USD"},
        "cash": {"settled_usd": _money(cash.get("settled_cash")),
            "unsettled_usd": _money(cash.get("unsettled_cash")),
            "buying_power_usd": _money(cash.get("buying_power")),
            "equity_usd": _money(cash.get("net_liquidation_value"))},
        "positions": {"count": position_count},
        "open_orders": {"count": len(orders)},
        "market_data": market_freshness(snapshot, snapshot_received_at, now),
    }
