"""Offline safety tests. These never load local credentials or contact Webull."""
import ast
from datetime import datetime, timezone
from decimal import Decimal
from importlib.metadata import version
import json
from pathlib import Path
from urllib.parse import urlsplit
import logging
import pytest
import requests
from webull.core.request import ApiRequest
from webull.data.request.subscribe_request import SubscribeRequest
from autonomous_trader.sandbox.http import *
from autonomous_trader.sandbox.secrets import Credentials, PreflightError, load_credentials
from autonomous_trader.sandbox.sanitize import Sanitizer
from autonomous_trader.sandbox.evidence import fixture, select_account, reconciliation_assessment
from autonomous_trader.sandbox.streams import signed_order_subscription


@pytest.fixture
def credentials():
    return Credentials("unit-key-SENTINEL", "unit-secret-SENTINEL", "unit-account-SENTINEL", "unit-token-SENTINEL")


class FakeResponse:
    status_code = 200
    headers = {"Content-Type": "application/json", "Date": "Fri, 11 Sep 2026 14:00:00 GMT",
               "X-RateLimit-Remaining": "29", "X-Access-Token": "should-never-escape"}
    payload = b'{"cash_balance":"1000000.00"}'
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def iter_content(self, size):
        yield self.payload


class FakeSession(requests.Session):
    seen = []
    response = FakeResponse
    def send(self, prepared, **kwargs):
        assert self.trust_env is False
        assert kwargs["allow_redirects"] is False and kwargs["verify"] is True
        assert kwargs["timeout"] == (5, 10)
        assert urlsplit(prepared.url).hostname == HTTP_HOST
        assert "x-signature" in prepared.headers
        self.seen.append((prepared.method, urlsplit(prepared.url).path))
        return self.response()


@pytest.mark.parametrize("operation", list(Operation))
def test_official_request_signed_to_exact_read_endpoint(credentials, operation):
    adapter = ReadOnlySandbox(credentials, session_factory=FakeSession, sleep=lambda _: None)
    observation = adapter.read(operation)
    assert observation.status_code == 200
    assert observation.path in GET_PATHS or observation.path == HISTORY_BATCH_PATH
    assert "x-access-token" not in observation.headers
    assert observation.body["cash_balance"] == "1000000.00"


@pytest.mark.parametrize("path,method", [
    ("/trading/orders/place", "POST"), ("/trading/orders/replace", "POST"),
    ("/trading/orders/cancel", "POST"), ("/trading/orders/preview", "POST"),
    ("/broker/assets/balances/get", "GET"), ("/oauth-openapi/account/list", "GET"),
    ("/trading/accounts/list", "POST"), ("/auth/tokens/create", "POST"),
    ("/trading/transfers/create", "POST"), ("/market-data/watchlists/create", "POST"),
    ("/trading/orders/../orders/place", "GET"), ("https://api.webull.com/trading/accounts/list", "GET")])
def test_forbidden_route_rejected_before_signing_or_connection(credentials, path, method, monkeypatch):
    client = SandboxHttp(credentials)
    monkeypatch.setattr(client, "_make_http_response", lambda *a: pytest.fail("must not sign forbidden request"))
    with pytest.raises(PreflightError):
        client.get_response(ApiRequest(path, method=method))
    assert client.contacted == []


@pytest.mark.parametrize("host", ["api.webull.com", "broker-api.sandbox.webull.com", "api.sandbox.webull.com.evil.invalid"])
def test_host_override_rejected(credentials, host):
    req = build_request(Operation.ACCOUNTS)
    req.set_endpoint(host)
    with pytest.raises(PreflightError, match="NON_SANDBOX"):
        SandboxHttp(credentials).get_response(req)


def test_only_exact_market_subscription_allowed():
    req = SubscribeRequest()
    req.set_session_id("test-session")
    req.set_symbols(["AAPL"])
    req.set_category("US_STOCK")
    req.set_sub_types(["QUOTE", "SNAPSHOT"])
    assert validate_request(req) == SUBSCRIBE_PATH
    req.set_category("CRYPTO")
    with pytest.raises(PreflightError):
        validate_request(req)


def test_rate_limit_stops_without_hammering(credentials):
    class Limited(FakeResponse):
        status_code = 429
    class Session(FakeSession):
        response = Limited
    client = SandboxHttp(credentials, session_factory=Session)
    assert client.get_response(build_request(Operation.ACCOUNTS)).status_code == 429
    with pytest.raises(PreflightError, match="RATE_LIMIT_STOP"):
        client.get_response(build_request(Operation.ACCOUNTS))
    assert len(client.contacted) == 1


def test_requests_are_spaced_and_redirect_not_followed(credentials):
    sleeps = []
    class Redirect(FakeResponse):
        status_code = 302
        headers = {"Location": "https://api.webull.com/"}
    class Session(FakeSession):
        response = Redirect
    client = SandboxHttp(credentials, session_factory=Session, clock=lambda: 10, sleep=sleeps.append)
    assert client.get_response(build_request(Operation.ACCOUNTS)).status_code == 302
    client.get_response(build_request(Operation.ACCOUNTS))
    assert sleeps == [3.2]


def test_shared_runtime_limiter_is_per_endpoint_and_fail_stops_after_429():
    current=[0.0];sleeps=[]
    def sleep(seconds):sleeps.append(seconds);current[0]+=seconds
    limiter=EndpointRateLimiter(limit=2,window_seconds=10,clock=lambda:current[0],sleep=sleep)
    limiter.acquire('/screen-a');limiter.acquire('/screen-a');limiter.acquire('/screen-b')
    assert sleeps==[] and limiter.health()=={'/screen-a':2,'/screen-b':1}
    limiter.acquire('/screen-a')
    assert sleeps and sleeps[0]>=10 and limiter.health()['/screen-a']==1
    limiter.stop('/screen-a')
    with pytest.raises(PreflightError,match='RATE_LIMIT_STOP_NO_RETRY'):limiter.acquire('/screen-a')


def test_complete_screener_passes_fit_at_fifteen_seconds_but_not_twelve_with_headroom():
    current=[0.0];sleeps=[]
    def sleep(seconds):sleeps.append(seconds);current[0]+=seconds
    limiter=EndpointRateLimiter(limit=28,window_seconds=60,clock=lambda:current[0],sleep=sleep)
    for started in (0,15,30,45):
        current[0]=started
        for _ in range(6):limiter.acquire('/market-data/screeners/gainers-losers/list')
    assert sleeps==[] and limiter.health()['/market-data/screeners/gainers-losers/list']==24

    current[:]=[0.0];sleeps.clear()
    limiter=EndpointRateLimiter(limit=28,window_seconds=60,clock=lambda:current[0],sleep=sleep)
    for started in (0,12,24,36):
        current[0]=started
        for _ in range(6):limiter.acquire('/market-data/screeners/gainers-losers/list')
    current[0]=48
    for _ in range(5):limiter.acquire('/market-data/screeners/gainers-losers/list')
    assert sleeps and current[0]>=60


def test_snapshot_quota_reserves_capacity_for_exact_and_safety_reads():
    current=[0.0];sleeps=[]
    def sleep(seconds):sleeps.append(seconds);current[0]+=seconds
    limiter=EndpointRateLimiter(clock=lambda:current[0],sleep=sleep)
    for _ in range(16):limiter.acquire(SNAPSHOT_PATH,'background')
    for _ in range(5):limiter.acquire(SNAPSHOT_PATH,'normal')
    for _ in range(3):limiter.acquire(SNAPSHOT_PATH,'safety')
    assert sleeps==[] and limiter.health()[SNAPSHOT_PATH]==24
    limiter.acquire(SNAPSHOT_PATH,'safety')
    assert sleeps and current[0]>=60


def test_runtime_source_assigns_background_normal_and_safety_priorities(credentials):
    from autonomous_trader.mvp.context import SandboxSource
    source=SandboxSource(credentials)
    assert all(reader._http._priority=='normal' for reader in source.readers.values())
    assert all(reader._http._priority=='safety' for reader in source._safety_readers.values())
    assert source._reader_factory()._http._priority=='background'


def test_transport_errors_and_sdk_debug_never_emit_secrets(credentials, capsys, caplog, monkeypatch):
    class Failing(FakeSession):
        def send(self, *args, **kwargs):
            logging.error(credentials.app_key)
            print(credentials.app_secret)
            raise requests.ConnectionError(credentials.access_token)
    monkeypatch.setenv("WEBULL_API_DEBUG", "sdk")
    with pytest.raises(PreflightError, match="^HTTP_TRANSPORT_FAILED$"):
        SandboxHttp(credentials, session_factory=Failing).get_response(build_request(Operation.ACCOUNTS))
    assert capsys.readouterr().out == ""
    assert caplog.text == ""


def test_recursive_redaction_preserves_only_reviewed_values(credentials):
    s = Sanitizer(credentials.sensitive_values())
    raw = {"account_id": credentials.account_id, "account_type": "CASH", "accountType": "CASH",
        "cash_balance": "123.45", "unsettled_cash": "1.00", "x-app-key": credentials.app_key,
        "access_token": credentials.access_token, "nested": [{"message": "echo " + credentials.app_secret,
        "email": "someone@example.invalid", "name": "Jane Doe", "arbitrary": "reusable-unknown-token",
        "last_client_order_id": "private-order"}], "positions": []}
    text = s.encode(raw)
    for value in (*credentials.sensitive_values(), "someone@example.invalid", "Jane Doe", "reusable-unknown-token", "private-order"):
        assert value not in text
    clean = json.loads(text)
    assert clean["cash_balance"] == "123.45" and clean["accountType"] == "CASH"
    assert clean["positions"] == []
    assert "access_token" not in clean


def test_fixture_headers_shapes_and_no_authority(credentials):
    s = Sanitizer(credentials.sensitive_values())
    result = fixture(HttpObservation("/trading/assets/balances/get", 200, "2026-09-11T14:00:00Z",
        {"x-ratelimit-remaining": "29"}, {"buying_power": "1000000.0", "positions": []}), s)
    assert result["headers"]["x-ratelimit-remaining"] == "29"
    assert result["original_value_types"]["positions"]["observed_items"] == []
    assessment = reconciliation_assessment([result])
    assert not assessment["can_admit_risk"] and not assessment["can_reconcile_automatically"]


@pytest.mark.parametrize("rows,selected", [([], "a"), ([{"account_id": "a", "account_type": "MARGIN"}], "a"),
    ([{"account_id": "a", "account_type": "CASH"}], ""), ([{"account_id": "a", "account_type": "1"}], "a")])
def test_account_binding_never_guesses_cash_or_designation(rows, selected):
    with pytest.raises(PreflightError):
        select_account(rows, selected)


def test_account_binding_requires_exact_returned_cash_identity():
    assert select_account([{"account_id": "a", "account_type": "CASH"}], "a")["account_type"] == "CASH"


def test_grpc_order_only_sdk_signing_silent(credentials, capsys):
    request, metadata = signed_order_subscription(credentials)
    assert request.subscribeType == 1
    assert list(request.accounts) == [credentials.account_id]
    assert dict(metadata)["x-app-key"] == credentials.app_key
    assert "x-signature" in dict(metadata)
    assert capsys.readouterr().out == ""


def test_sdk_runtime_compatibility_and_no_constructor_connections(credentials):
    from webull.data.data_streaming_client import DataStreamingClient
    from webull.trade.trade_events_client import TradeEventsClient
    assert version("webull-openapi-python-sdk") == SDK_VERSION
    mqtt = DataStreamingClient(credentials.app_key, credentials.app_secret, "us", "test",
        http_host=HTTP_HOST, mqtt_host=MQTT_HOST)
    assert mqtt._mqtt_host == MQTT_HOST
    grpc_client = TradeEventsClient(credentials.app_key, credentials.app_secret, host=GRPC_HOST)
    assert grpc_client._host == GRPC_HOST


def test_missing_secrets_fail_closed_without_searching_other_directories(tmp_path, monkeypatch):
    for name in ("APP_KEY", "APP_SECRET", "ACCOUNT_ID", "ACCESS_TOKEN"):
        monkeypatch.delenv("WEBULL_SANDBOX_" + name, raising=False)
    with pytest.raises(PreflightError, match="MISSING_SANDBOX"):
        load_credentials(tmp_path)


def test_boundary_has_no_ledger_writes_or_top_level_sdk_initialization():
    root = Path(__file__).resolve().parents[2] / "src/autonomous_trader"
    for path in (root / "sandbox").glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not any(x in node.module for x in ("durable", "portfolio", "invariants", "trade_client", "data_client"))
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                assert node.name not in {"place_order", "replace_order", "cancel_order", "transfer", "deposit", "withdraw", "borrow"}
    for path in root.rglob("*.py"):
        if "sandbox" not in path.parts:
            assert "autonomous_trader.sandbox" not in path.read_text()


def test_recorded_error_fixtures_are_observations_not_successful_certification():
    root = Path(__file__).resolve().parents[1] / "fixtures/webull_sandbox"
    paths = list(root.glob("*/accounts.json"))
    assert paths, "Expected actual sanitized account probe evidence"
    for path in paths:
        event = json.loads(path.read_text())
        assert event["origin"] == "OBSERVED_SANDBOX_HTTP" and event["host"] == HTTP_HOST
        if event["status_code"] != 200:
            with pytest.raises(PreflightError):
                select_account(event["body"], "unconfirmed")
        assert not reconciliation_assessment([event])["can_admit_risk"]


def test_explicit_legacy_diagnostic_stays_read_only(credentials):
    from webull.trade.request.v2.get_account_list import GetAccountList
    request = GetAccountList()
    request.set_version("v2")
    assert request.get_version() == "v2"
    result = SandboxHttp(credentials, session_factory=FakeSession).get_response(request)
    assert result.path == "/openapi/account/list"
    assert FakeSession.seen[-1] == ("GET", result.path)


@pytest.mark.parametrize("accepted", [False, True])
def test_mqtt_auth_failure_stops_and_success_reconnect_is_bounded(credentials, monkeypatch, accepted):
    from autonomous_trader.sandbox import streams
    from webull.data.internal.quotes_client import ConnectException
    instances = []
    class Client:
        def __init__(self, *args, **kwargs):
            assert kwargs["mqtt_host"] == MQTT_HOST and kwargs["tls_enable"]
            instances.append(self)
            self.closed = False
        def disable_logger(self): pass
        def register_payload_decoder(self, *args): pass
        def _quotes_connect(self, host, port):
            assert (host, port) == (MQTT_HOST, 1883)
            if not accepted:
                raise ConnectException(104, credentials.app_key)
            self.on_connect_success(self, self._api_client, "private-session")
        def subscribe(self, symbols, category, types):
            assert (symbols, category, types) == (["AAPL"], "US_STOCK", ["QUOTE", "SNAPSHOT"])
            self.on_subscribe_success()
        def disconnect(self): pass
        def _sock_close(self): self.closed = True
    monkeypatch.setattr(streams, "DataStreamingClient", Client)
    monkeypatch.setattr(streams.time, "sleep", lambda _: None)
    result = streams.mqtt_probe(credentials, object(), Sanitizer(credentials.sensitive_values()), duration=0)
    assert len(instances) == (2 if accepted else 1)
    assert all(c.closed for c in instances)
    Sanitizer(credentials.sensitive_values()).assert_clean(json.dumps(result))
    if not accepted:
        assert result["attempts"][0]["error_code"] == 104


@pytest.mark.parametrize("accepted", [False, True, "expired_after_success"])
def test_grpc_auth_failure_stops_and_reconnect_closes_subscriptions(credentials, monkeypatch, accepted):
    from autonomous_trader.sandbox import streams
    calls, channels = [], []
    class Channel:
        def __enter__(self): return self
        def __exit__(self, *args): self.closed = True
    def channel(host, security, **kwargs):
        assert host == GRPC_HOST + ":443"
        c = Channel()
        channels.append(c)
        return c
    class Ready:
        def result(self, timeout): assert timeout == 5
    class Call:
        def __iter__(self):
            yield streams.pb.SubscribeResponse(eventType=streams.pb.SubscribeSuccess if accepted else streams.pb.AuthError,
                contentType="application/json", payload=json.dumps({"message": credentials.app_secret}))
            if accepted == "expired_after_success":
                yield streams.pb.SubscribeResponse(eventType=streams.pb.SubscribeExpired)
        def cancel(self): self.closed = True
    class Stub:
        def __init__(self, channel): pass
        def Subscribe(self, request, metadata, timeout):
            assert request.subscribeType == 1 and timeout == 8
            c = Call()
            calls.append(c)
            return c
    monkeypatch.setattr(streams.grpc, "secure_channel", channel)
    monkeypatch.setattr(streams.grpc, "channel_ready_future", lambda _: Ready())
    monkeypatch.setattr(streams.rpc, "EventServiceStub", Stub)
    monkeypatch.setattr(streams.time, "sleep", lambda _: None)
    result = streams.grpc_probe(credentials, Sanitizer(credentials.sensitive_values()))
    assert len(calls) == (2 if accepted is True else 1)
    assert all(x.closed for x in calls + channels)
    Sanitizer(credentials.sensitive_values()).assert_clean(json.dumps(result))


def test_market_protobuf_observation_preserves_fields_without_wrapper_synthesis():
    from autonomous_trader.sandbox.streams import ProtobufObservationDecoder, market_pb
    from google.protobuf.json_format import MessageToDict
    packet = market_pb.Snapshot(basic=market_pb.Basic(symbol="AAPL", instrument_id="public-instrument",
        timestamp="1789149600000", trading_session="RTH"), price="123.4567", open="122.00", trade_time="1789149600000")
    decoded = ProtobufObservationDecoder(market_pb.Snapshot).parse(packet.SerializeToString())
    clean = Sanitizer().clean(MessageToDict(decoded, preserving_proto_field_name=True))
    assert clean["price"] == "123.4567" and clean["basic"]["timestamp"] == "1789149600000"
    assert clean["basic"]["trading_session"] == "RTH"
    assert "close" not in clean  # SDK convenience wrapper incorrectly derives close from open.


@pytest.mark.parametrize("timestamp,finding", [
    ("1789149600000", "OBSERVED_NEAR_REAL_TIME_FOR_AAPL_SAMPLE"),
    ("1789148700000", "OBSERVED_APPROXIMATELY_15_MINUTE_DELAY_FOR_AAPL_SAMPLE"),
    ("<redacted:string>", "UNDETERMINED"),
])
def test_market_age_finding_requires_timestamps_and_does_not_grant_authority(timestamp, finding):
    from autonomous_trader.sandbox.evidence import market_timing_observation
    observation = HttpObservation("/market-data/stocks/snapshots/list", 200, "2026-09-11T18:00:00.100000+00:00", {},
        [{"symbol": "AAPL", "quote_time": timestamp}])
    assert market_timing_observation([observation], {})["finding"] == finding
    assert not reconciliation_assessment([observation])["can_admit_risk"]


def test_account_class_and_observed_cash_fields_survive_without_identifiers(credentials):
    clean = Sanitizer(credentials.sensitive_values()).clean({"account_class": "INDIVIDUAL_CASH",
        "account_id": credentials.account_id, "total_cash_balance": "1000000.00",
        "account_currency_assets": [{"settled_cash": "1000000.00", "unsettled_cash": "0.00",
                                     "night_trading_buying_power": "1000000"}]})
    assert clean["account_class"] == "INDIVIDUAL_CASH"
    assert clean["total_cash_balance"] == "1000000.00"
    assert clean["account_id"] != credentials.account_id
    assert "reserved_cash" not in clean["account_currency_assets"][0]


def test_history_post_only_accepts_bounded_read_payload():
    request = build_request(Operation.HISTORY_BATCH)
    assert validate_request(request) == HISTORY_BATCH_PATH
    request.set_count(1000000)
    with pytest.raises(PreflightError):
        validate_request(request)


def test_bounded_multi_symbol_read_payload_is_allowed():
    symbols=('AAPL','MSFT','NVDA')
    assert validate_request(build_request(Operation.HISTORY_BATCH,symbols=symbols))==HISTORY_BATCH_PATH
    assert build_request(Operation.SNAPSHOT,symbols=symbols).get_query_params()['symbols']==','.join(symbols)
    with pytest.raises(PreflightError,match='INVALID_SYMBOL_UNIVERSE'):
        build_request(Operation.SNAPSHOT,symbols=('AAPL','bad symbol'))


def test_gainers_losers_read_is_bounded_to_official_long_only_discovery_surfaces():
    for direction in ('ASC','DESC'):
        request=build_request(Operation.GAINERS_LOSERS,rank_type='DAY_1',direction=direction)
        assert validate_request(request)=='/market-data/screeners/gainers-losers/list'
        assert request.get_query_params()=={'rank_type':'DAY_1','category':'US_STOCK',
            'sort_by':'CHANGE_RATIO','direction':direction}
    with pytest.raises(PreflightError,match='INVALID_GAINERS_LOSERS_RANK'):
        build_request(Operation.GAINERS_LOSERS,rank_type='MONTH_1',direction='DESC')
