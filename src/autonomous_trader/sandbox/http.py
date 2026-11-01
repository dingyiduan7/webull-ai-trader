"""Official SDK signing with a sandbox-only read transport and no SDK logs/cache.

Deliberately avoids TradeClient/DataClient initializers and their token/file-log
side effects. No durable ledger, permit or execution import belongs here.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
import json
import time
from collections import defaultdict,deque
from threading import Lock
from urllib.parse import urlsplit
import requests
from webull.core.client import ApiClient
from webull.core.http.initializer.config.bean.query_config_request import GetConfigRequest
from webull.trade.request.v2.get_account_list_request import GetAccountListRequest
from webull.trade.request.v2.get_account_balance_request import AccountBalanceRequest
from webull.trade.request.v2.get_account_positions_request import AccountPositionsRequest
from webull.trade.request.v3.get_order_open_request_v2 import OrderOpenRequestV2
from webull.data.request.get_instruments_request_v2 import GetInstrumentsRequestV2
from webull.data.request.get_snapshot_request import GetSnapshotRequest
from webull.data.request.get_historical_bars_request import GetHistoricalBarsRequest
from webull.data.request.get_batch_historical_bars_request import BatchHistoricalBarsRequest
from webull.data.request.screener.get_most_active_request_v2 import GetMostActiveRequestV2
from webull.data.request.screener.get_gainers_losers_request_v2 import GetGainersLosersRequestV2
from .secrets import Credentials, PreflightError
from .quiet import quiet_sdk

SYMBOL_RE = __import__('re').compile(r'[A-Z][A-Z0-9.]{0,9}')

HTTP_HOST = "api.sandbox.webull.com"
MQTT_HOST = "data-api.sandbox.webull.com"
GRPC_HOST = "events-api.sandbox.webull.com"
SDK_VERSION = "3.0.0"
GET_PATHS = frozenset({"/openapi/config", "/openapi/account/list", "/trading/accounts/list", "/trading/assets/balances/get",
    "/trading/assets/positions/list", "/trading/orders/open-orders/list",
    "/trading/instruments/stocks/profiles/list", "/market-data/stocks/snapshots/list",
    "/market-data/stocks/bars/get", "/market-data/screeners/top-actives/list",
    "/market-data/screeners/gainers-losers/list"})
SUBSCRIBE_PATH = "/market-data/streaming/subscribe"
UNSUBSCRIBE_PATH = "/market-data/streaming/unsubscribe"
HISTORY_BATCH_PATH = "/market-data/stocks/bars/list"
SNAPSHOT_PATH = "/market-data/stocks/snapshots/list"
MAX_STREAM_SYMBOLS = 35  # Current 30-name shortlist plus up to five owned names.
SAFE_HEADERS = frozenset({"date", "content-type", "retry-after", "ratelimit-limit", "ratelimit-remaining",
    "ratelimit-reset", "x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset"})
TOP_ACTIVE_RANK_TYPES = ('VOLUME','RELATIVE_VOLUME_10D','TURNOVER','TURNOVER_RATE','AMPLITUDE')
GAINERS_RANK_TYPES = ('MIN_3','MIN_5','DAY_1')


class Operation(str, Enum):
    AUTH_CONFIG = "auth_config"
    ACCOUNTS = "accounts"
    BALANCE = "balance"
    POSITIONS = "positions"
    OPEN_ORDERS = "open_orders"
    INSTRUMENTS = "instruments"
    SNAPSHOT = "snapshot"
    HISTORY = "history"
    HISTORY_BATCH = "history_batch"
    HISTORY_DAILY_BATCH = "history_daily_batch"
    TOP_ACTIVE = "top_active"
    GAINERS_LOSERS = "gainers_losers"


def build_request(operation: Operation, account_id="", cursor=None, symbols=("AAPL",), rank_type=None, direction='DESC'):
    op = Operation(operation)
    rank_type=('DAY_1' if op==Operation.GAINERS_LOSERS else 'VOLUME') if rank_type is None else rank_type
    classes = {Operation.AUTH_CONFIG: GetConfigRequest, Operation.ACCOUNTS: GetAccountListRequest, Operation.BALANCE: AccountBalanceRequest,
        Operation.POSITIONS: AccountPositionsRequest, Operation.OPEN_ORDERS: OrderOpenRequestV2,
        Operation.INSTRUMENTS: GetInstrumentsRequestV2, Operation.SNAPSHOT: GetSnapshotRequest,
        Operation.HISTORY: GetHistoricalBarsRequest, Operation.HISTORY_BATCH: BatchHistoricalBarsRequest,
        Operation.HISTORY_DAILY_BATCH: BatchHistoricalBarsRequest,
        Operation.TOP_ACTIVE: GetMostActiveRequestV2, Operation.GAINERS_LOSERS: GetGainersLosersRequestV2}
    req = classes[op]()
    if op in (Operation.BALANCE, Operation.POSITIONS, Operation.OPEN_ORDERS):
        if not account_id:
            raise PreflightError("DESIGNATED_ACCOUNT_REQUIRED")
        req.set_account_id(account_id)
    if op == Operation.OPEN_ORDERS:
        req.set_pagination_key(cursor)
    symbols=tuple(symbols)
    if not 1 <= len(symbols) <= 10 or len(set(symbols)) != len(symbols) or not all(SYMBOL_RE.fullmatch(x) for x in symbols):
        raise PreflightError("INVALID_SYMBOL_UNIVERSE")
    if op in (Operation.SNAPSHOT, Operation.INSTRUMENTS):
        req.set_symbols(list(symbols))
        req.set_category("US_STOCK")
    if op == Operation.HISTORY:
        if len(symbols)!=1:raise PreflightError("SINGLE_HISTORY_SYMBOL_REQUIRED")
        req.set_symbol(symbols[0])
        req.set_category("US_STOCK")
        req.set_timespan("M1")
        req.set_count(5)
    if op in (Operation.HISTORY_BATCH,Operation.HISTORY_DAILY_BATCH):
        req.set_symbols(list(symbols))
        req.set_category("US_STOCK")
        req.set_timespan("D" if op==Operation.HISTORY_DAILY_BATCH else "M1")
        req.set_count(250 if op==Operation.HISTORY_DAILY_BATCH else 5)
    if op == Operation.TOP_ACTIVE:
        if rank_type not in TOP_ACTIVE_RANK_TYPES:raise PreflightError("INVALID_TOP_ACTIVE_RANK")
        req.set_category("US_STOCK");req.set_rank_type(rank_type);req.set_direction("DESC")
        if rank_type!='TURNOVER_RATE':req.set_sort_by(rank_type)
    if op == Operation.GAINERS_LOSERS:
        if rank_type not in GAINERS_RANK_TYPES or direction not in ('ASC','DESC'):raise PreflightError("INVALID_GAINERS_LOSERS_RANK")
        req.set_category("US_STOCK");req.set_rank_type(rank_type);req.set_sort_by("CHANGE_RATIO");req.set_direction(direction)
    return req


def validate_request(request):
    target = urlsplit(request.get_url())
    if request.get_endpoint() not in (None, HTTP_HOST) or target.scheme or target.netloc or target.fragment:
        raise PreflightError("NON_SANDBOX_ENDPOINT_REJECTED")
    if request.get_method() == "GET" and target.path in GET_PATHS and not request.get_body_params():
        return target.path
    if request.get_method() == "POST" and target.path in (SUBSCRIBE_PATH, UNSUBSCRIBE_PATH):
        body = request.get_body_params() or {}
        symbols=body.get("symbols")
        required={"session_id","symbols","category","sub_types"}
        allowed=required|({"unsubscribe_all"} if target.path==UNSUBSCRIBE_PATH else set())
        if (set(body)<=allowed and required<=set(body)
            and isinstance(symbols,list) and 1<=len(symbols)<=MAX_STREAM_SYMBOLS and len(set(symbols))==len(symbols)
            and all(isinstance(x,str) and SYMBOL_RE.fullmatch(x) for x in symbols)
            and body["category"] == "US_STOCK"
            and body["sub_types"] in (["QUOTE", "SNAPSHOT", "TICK"],["QUOTE", "SNAPSHOT"])
            and (body["sub_types"]==["QUOTE", "SNAPSHOT", "TICK"] or symbols==["AAPL"])
            and body["session_id"]
            and (target.path!=UNSUBSCRIBE_PATH or body.get("unsubscribe_all") is False)):
            return target.path
    if request.get_method() == "POST" and target.path == HISTORY_BATCH_PATH:
        body=request.get_body_params() or {};symbols=body.get('symbols')
        if (set(body)=={"symbols","category","timespan","count"} and isinstance(symbols,list)
                and 1<=len(symbols)<=10 and len(set(symbols))==len(symbols)
                and all(isinstance(x,str) and SYMBOL_RE.fullmatch(x) for x in symbols)
                and body["category"]=="US_STOCK"
                and (body["timespan"],body["count"]) in (("M1",5),("D",250))):
            return target.path
    raise PreflightError("READ_ONLY_ENDPOINT_REJECTED")


@dataclass(frozen=True)
class HttpObservation:
    path: str
    status_code: int
    received_at: str
    headers: dict = field(repr=False)
    body: object = field(repr=False)
    header_names: tuple = ()


class EndpointRateLimiter:
    """Shared conservative rolling limit for official per-endpoint quotas."""
    def __init__(self,limit=24,window_seconds=60,*,clock=time.monotonic,sleep=time.sleep):
        if not 1<=limit<=30 or window_seconds<=0:raise ValueError("INVALID_RATE_LIMIT")
        self.limit,self.window_seconds=limit,window_seconds;self.clock,self.sleep=clock,sleep
        self.lock=Lock();self.calls=defaultdict(deque);self.stopped=set()

    def acquire(self,path,priority='normal'):
        if priority not in ('background','normal','safety'):raise ValueError('INVALID_RATE_PRIORITY')
        # Snapshot is shared by broad discovery, exact entry refreshes and
        # held-position protection. Preserve capacity in that order of
        # authority while keeping every request in one rolling counter.
        cap=self.limit
        if path==SNAPSHOT_PATH:
            cap=min(cap,{'background':16,'normal':21,'safety':24}[priority])
        while True:
            with self.lock:
                if path in self.stopped:raise PreflightError("RATE_LIMIT_STOP_NO_RETRY")
                now=self.clock();rows=self.calls[path]
                while rows and now-rows[0]>=self.window_seconds:rows.popleft()
                if len(rows)<cap:rows.append(now);return
                delay=max(.01,self.window_seconds-(now-rows[0])+.01)
            self.sleep(delay)

    def stop(self,path):
        with self.lock:self.stopped.add(path)

    def health(self):
        with self.lock:
            now=self.clock()
            return {path:sum(now-at<self.window_seconds for at in rows) for path,rows in self.calls.items()}


class SandboxHttp(ApiClient):
    def __init__(self, credentials: Credentials, *, session_factory=requests.Session, clock=time.monotonic, sleep=time.sleep,
            limiter=None,priority='normal'):
        if priority not in ('background','normal','safety'):raise ValueError('INVALID_RATE_PRIORITY')
        super().__init__(credentials.app_key, credentials.app_secret, "us", verify=True,
                         auto_retry=False, connect_timeout=5, timeout=10)
        self.set_token(credentials.access_token or None)
        self.add_endpoint("us", HTTP_HOST)
        self._session_factory, self._clock, self._sleep = session_factory, clock, sleep
        self._limiter,self._priority=limiter,priority
        self._last_request = None
        self._throttled = False
        self.contacted = []
        self.observations = []

    @quiet_sdk()
    def get_response(self, request):
        path = validate_request(request)  # Before signing or network access.
        if self._throttled:
            raise PreflightError("RATE_LIMIT_STOP_NO_RETRY")
        if self._limiter is not None:self._limiter.acquire(path,self._priority)
        elif self._last_request is not None:
            # Standalone certification clients retain their older conservative
            # pacing. Runtime readers share the official per-endpoint limiter.
            self._sleep(max(0, 3.2 - (self._clock() - self._last_request)))
        self._last_request = self._clock()
        # SDK constructs/signs the exact official request. Its stock Response
        # transport is intentionally not invoked (debug logs can contain secrets).
        wire = self._make_http_response(HTTP_HOST, request, 10, 5)
        url = "https://" + HTTP_HOST + wire.get_url()
        if urlsplit(url).hostname != HTTP_HOST:
            raise PreflightError("NON_SANDBOX_ENDPOINT_REJECTED")
        try:
            with self._session_factory() as session:
                session.trust_env = False  # no proxy/netrc/ambient credential inheritance
                prepared = session.prepare_request(requests.Request(request.get_method(), url,
                    headers=wire.get_headers(), data=wire.get_body()))
                self.contacted.append({"host": HTTP_HOST, "method": request.get_method(), "path": path})
                with session.send(prepared, allow_redirects=False, verify=True, timeout=(5, 10), stream=True) as response:
                    chunks, size = [], 0
                    for chunk in response.iter_content(65536):
                        size += len(chunk)
                        if size > 2_000_000:
                            raise PreflightError("RESPONSE_SIZE_LIMIT")
                        chunks.append(chunk)
                    raw = b"".join(chunks)
                    try:
                        body = json.loads(raw, parse_float=Decimal,
                            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                    except (ValueError, UnicodeError):
                        body = {"non_json_response": True, "byte_count": len(raw)}
                    headers = {k.lower(): v for k, v in response.headers.items() if k.lower() in SAFE_HEADERS}
                    self._throttled = response.status_code == 429
                    if self._throttled and self._limiter is not None:self._limiter.stop(path)
                    result = HttpObservation(path, response.status_code, datetime.now(timezone.utc).isoformat(),
                        headers, body, tuple(k.lower() for k in response.headers))
                    self.observations.append(result)
                    return result
        except PreflightError:
            raise
        except Exception:
            raise PreflightError("HTTP_TRANSPORT_FAILED") from None


class ReadOnlySandbox:
    """Observation API only. No order-write, funding, ledger or permit methods."""
    def __init__(self, credentials, **transport_options):
        self._http = SandboxHttp(credentials, **transport_options)
        self._designated = credentials.account_id

    def read(self, operation, cursor=None, symbols=("AAPL",), rank_type=None, direction='DESC'):
        return self._http.get_response(build_request(operation, self._designated, cursor, symbols, rank_type, direction))
