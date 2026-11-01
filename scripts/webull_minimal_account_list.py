"""User-requested official minimal SDK flow; no project HTTP adapter is used."""
import json
import os
from pathlib import Path
import sys
from datetime import datetime, timezone
from decimal import Decimal
from importlib.metadata import version
from unittest.mock import patch
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import requests
from webull.core.client import ApiClient
from webull.trade.trade_client import TradeClient
from webull.core.http.initializer.token.token_manager import TokenManager
from autonomous_trader.sandbox.secrets import load_credentials, PreflightError
from autonomous_trader.sandbox.quiet import quiet_sdk
from autonomous_trader.sandbox.sanitize import Sanitizer
from autonomous_trader.sandbox.http import HttpObservation, SAFE_HEADERS
from autonomous_trader.sandbox.evidence import fixture, save_json


def run(credentials):
    sanitizer = Sanitizer(credentials.sensitive_values())
    report = {"sdk_version": version("webull-openapi-python-sdk"), "stage": "API_CLIENT",
              "account_list_called": False, "http": [], "order_writes": 0,
              "flow": "OFFICIAL_API_CLIENT_AND_TRADE_CLIENT_NO_PROJECT_HTTP_ADAPTER"}
    original_send = requests.Session.send

    def observe_send(session, request, **kwargs):
        url = urlsplit(request.url)
        if (url.scheme != "https" or url.hostname != "api.sandbox.webull.com"
                or url.port not in (None, 443) or request.method != "GET"
                or url.path not in ("/openapi/config", "/trading/accounts/list")
                or len(report["http"]) >= 2):
            raise PreflightError("MINIMAL_READ_ONLY_SCOPE_STOP")
        if kwargs.get("verify") is not True or kwargs.get("allow_redirects") is not False:
            raise PreflightError("SDK_TRANSPORT_SECURITY_STOP")
        # Delegate unchanged to requests through the SDK's own Response transport.
        # No request/signature/client initialization is replaced or bypassed.
        response = original_send(session, request, **kwargs)
        try:
            body = json.loads(response.content, parse_float=Decimal)
        except (ValueError, UnicodeError):
            body = {"non_json_response": True}
        observation = HttpObservation(url.path, response.status_code,
            datetime.now(timezone.utc).isoformat(),
            {k.lower(): v for k, v in response.headers.items() if k.lower() in SAFE_HEADERS},
            body, tuple(k.lower() for k in response.headers))
        report["http"].append(fixture(observation, sanitizer))
        return response

    def stop_token_cache(*args, **kwargs):
        raise PreflightError("TOKEN_FLOW_REQUIRED_READ_ONLY_DIAGNOSTIC_STOP")

    # Contain only logging/cache and ambient proxy/netrc/debug configuration.
    # A token requirement is reported explicitly; never fabricate config success.
    clean_env = dict(os.environ)
    for key in list(clean_env):
        if key.lower() in ("http_proxy", "https_proxy", "all_proxy", "webull_api_debug",
                          "webull_api_ca_bundle", "sslkeylogfile"):
            clean_env.pop(key)
    clean_env["NETRC"] = os.devnull
    with quiet_sdk(), patch.dict(os.environ, clean_env, clear=True), \
            patch.object(ApiClient, "set_stream_logger", lambda *a, **k: None), \
            patch.object(ApiClient, "set_file_logger", lambda *a, **k: None), \
            patch.object(TokenManager, "__init__", stop_token_cache), \
            patch.object(requests.Session, "send", observe_send):
        try:
            APP_KEY, APP_SECRET = credentials.app_key, credentials.app_secret
            api_client = ApiClient(APP_KEY, APP_SECRET, "us")
            api_client.add_endpoint("us", "api.sandbox.webull.com")

            report["stage"] = "TRADE_CLIENT_INITIALIZATION"
            trade_client = TradeClient(api_client)
            report["stage"] = "ACCOUNT_LIST"
            report["account_list_called"] = True
            res = trade_client.account_v2.get_account_list()
            report["result_status"] = res.status_code
            report["stage"] = "RETURNED"
        except PreflightError as exc:
            report["stop"] = str(exc)
        except Exception as exc:
            report["exception_type"] = type(exc).__name__
            status = getattr(exc, "http_status", None)
            if isinstance(status, int):
                report["exception_http_status"] = status
    sanitizer.assert_clean(json.dumps(report))
    return report


def main():
    credentials = load_credentials(ROOT)
    report = run(credentials)
    report["run_id"] = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    sanitizer = Sanitizer(credentials.sensitive_values())
    save_json(ROOT / "docs/m3a-minimal-sdk-diagnostic.json", report, sanitizer)
    save_json(ROOT / "tests/fixtures/webull_sandbox" / report["run_id"] / "minimal_sdk.json", report, sanitizer)
    print(json.dumps({"stage": report["stage"], "account_list_called": report["account_list_called"],
                      "http_statuses": [r["status_code"] for r in report["http"]],
                      "report": "docs/m3a-minimal-sdk-diagnostic.json", "order_writes": 0}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print('{"result":"DIAGNOSTIC_FAILED_DETAILS_SUPPRESSED"}')
        sys.exit(1)
