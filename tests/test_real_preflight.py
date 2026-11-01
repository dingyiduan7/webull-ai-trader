from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
import json

import pytest
import requests
from webull.core.request import ApiRequest

from autonomous_trader.real_preflight import (REAL_HTTP_HOST, REAL_READ_PATHS, RealPreflightError,
    RealReadOnly, RealReadOnlyHttp, Operation, high_level_summary, market_freshness,
    select_single_individual_cash)
from autonomous_trader.real_secrets import RealCredentials


@pytest.fixture
def credentials():
    return RealCredentials("real-key-SENTINEL", "real-secret-SENTINEL", "real-account-SENTINEL", "real-token-SENTINEL")


class FakeResponse:
    status_code = 200
    headers = {"Content-Type": "application/json", "X-Access-Token": "must-not-escape"}
    payload = b'{"ok":true}'
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def iter_content(self, size): yield self.payload


class FakeSession(requests.Session):
    seen = []
    def send(self, prepared, **kwargs):
        assert self.trust_env is False
        assert kwargs == {"allow_redirects": False, "verify": True, "timeout": (5, 10), "stream": True}
        assert urlsplit(prepared.url).hostname == REAL_HTTP_HOST
        assert prepared.method == "GET" and "x-signature" in prepared.headers
        self.seen.append(urlsplit(prepared.url).path)
        return FakeResponse()


@pytest.mark.parametrize("operation", [Operation.ACCOUNTS, Operation.BALANCE, Operation.POSITIONS,
    Operation.OPEN_ORDERS, Operation.SNAPSHOT])
def test_only_minimum_signed_real_read_routes_are_representable(credentials, operation):
    account = "real-account-SENTINEL" if operation in (Operation.BALANCE, Operation.POSITIONS, Operation.OPEN_ORDERS) else ""
    observation = RealReadOnly(credentials, session_factory=FakeSession).read(operation, account_id=account)
    assert observation.status_code == 200 and observation.path in REAL_READ_PATHS
    assert "x-access-token" not in observation.headers


@pytest.mark.parametrize("path,method", [("/trading/orders/place", "POST"),
    ("/trading/orders/cancel", "POST"), ("/trading/transfers/create", "POST"),
    ("/broker/assets/balances/get", "GET"), ("/trading/accounts/list", "POST"),
    ("https://api.sandbox.webull.com/trading/accounts/list", "GET")])
def test_write_wrong_product_and_wrong_host_routes_fail_before_signing(credentials, path, method, monkeypatch):
    client = RealReadOnlyHttp(credentials)
    monkeypatch.setattr(client, "_make_http_response", lambda *args: pytest.fail("forbidden request signed"))
    with pytest.raises(RealPreflightError):
        client.get_response(ApiRequest(path, method=method))
    assert client.contacted == []


def test_account_selection_requires_exactly_one_individual_cash_without_exposing_ids():
    one = [{"account_id": "private-one", "account_type": "CASH", "account_class": "INDIVIDUAL_CASH"},
        {"account_id": "private-margin", "account_type": "MARGIN", "account_class": "INDIVIDUAL_MARGIN"}]
    assert select_single_individual_cash(one)["account_id"] == "private-one"
    for rows in ([], one + [{"account_id": "private-two", "account_type": "CASH", "account_class": "INDIVIDUAL_CASH"}]):
        with pytest.raises(RealPreflightError) as exc:
            select_single_individual_cash(rows)
        assert str(exc.value) == "REAL_CASH_ACCOUNT_SELECTION_AMBIGUOUS"
        assert "private" not in str(exc.value)


def test_summary_contains_only_high_level_evidence_and_current_sample():
    now = datetime(2026, 9, 29, 14, 0, tzinfo=timezone.utc)
    quote_time = int((now - timedelta(seconds=1)).timestamp() * 1000)
    summary = high_level_summary(
        {"account_id": "private-account", "account_type": "CASH", "account_class": "INDIVIDUAL_CASH"},
        {"account_currency_assets": [{"currency": "USD", "settled_cash": "1234.567",
            "unsettled_cash": "1", "buying_power": "1200", "net_liquidation_value": "1300"}]},
        [{"symbol": "PRIVATE", "quantity": "2"}], [{"order_id": "private-order"}],
        [{"symbol": "AAPL", "quote_time": str(quote_time)}], now.isoformat(), now)
    text = json.dumps(summary)
    assert summary["cash"] == {"settled_usd": "1234.57", "unsettled_usd": "1.00",
        "buying_power_usd": "1200.00", "equity_usd": "1300.00"}
    assert summary["positions"]["count"] == summary["open_orders"]["count"] == 1
    assert summary["market_data"]["finding"] == "OBSERVED_CURRENT_SAMPLE"
    assert "private" not in text.lower() and summary["production_execution_enabled"] is False


def test_stale_market_sample_is_reported_not_upgraded():
    now = datetime(2026, 9, 29, 14, 0, tzinfo=timezone.utc)
    result = market_freshness([{"symbol": "AAPL", "quote_time": str(int((now-timedelta(minutes=15)).timestamp()*1000))}],
        now.isoformat(), now)
    assert result["finding"] == "OBSERVED_DELAYED_OR_STALE_SAMPLE"


def test_real_preflight_is_not_imported_by_runtime_and_production_stays_disabled():
    root = Path(__file__).resolve().parents[1]
    for path in (root / "scripts" / "run_ai_trader.py", root / "scripts" / "run_trading_cycle.py"):
        text = path.read_text(encoding="utf-8")
        assert "real_preflight" not in text and "load_real_credentials" not in text
    from autonomous_trader.mvp.config import RuntimeConfig, ExecutionMode
    with pytest.raises(ValueError, match="PRODUCTION_DISABLED"):
        RuntimeConfig(ExecutionMode.PRODUCTION).validate()
