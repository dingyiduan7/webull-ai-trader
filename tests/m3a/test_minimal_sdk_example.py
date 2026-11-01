"""Exercise the actual TradeClient initializer, with only the network mocked."""
import importlib.util
import json
from pathlib import Path
import pytest
import requests
from autonomous_trader.sandbox.secrets import Credentials


@pytest.mark.parametrize("config_status", [401, 200])
def test_minimal_sdk_does_not_skip_initializer_or_leak_credentials(config_status, monkeypatch, capsys, tmp_path):
    script = Path(__file__).resolve().parents[2] / "scripts/webull_minimal_account_list.py"
    spec = importlib.util.spec_from_file_location("minimal_sdk_diagnostic", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    credentials = Credentials("unit-key-SENTINEL", "unit-secret-SENTINEL")
    paths = []
    def send(session, request, **kwargs):
        paths.append(request.path_url)
        assert "x-signature" in request.headers
        response = requests.Response()
        response.status_code = config_status if request.path_url == "/openapi/config" else 401
        response._content = json.dumps({"token_check_enabled": False} if response.status_code == 200 else
            {"error_code": "UNAUTHORIZED", "message": credentials.app_secret}).encode()
        response.headers["Content-Type"] = "application/json"
        return response
    monkeypatch.setattr(requests.Session, "send", send)
    monkeypatch.chdir(tmp_path)
    result = module.run(credentials)
    assert result["account_list_called"] == (config_status == 200)
    assert result["stage"] == ("ACCOUNT_LIST" if config_status == 200 else "TRADE_CLIENT_INITIALIZATION")
    assert paths == (["/openapi/config", "/trading/accounts/list"] if config_status == 200 else ["/openapi/config"])
    assert result["exception_http_status"] == 401
    assert not list(tmp_path.iterdir())
    assert capsys.readouterr().out == ""
    for value in credentials.sensitive_values():
        assert value not in json.dumps(result)


def test_documented_sha1_diagnostic_matches_independent_get_calculation():
    import base64
    import hashlib
    import hmac
    from urllib.parse import quote
    script = Path(__file__).resolve().parents[2] / "scripts/webull_documented_signature_check.py"
    spec = importlib.util.spec_from_file_location("documented_signature_diagnostic", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    credentials = Credentials("synthetic-key", "synthetic-secret")
    headers = module.signed_headers(credentials, "2026-09-11T17:55:00Z", "synthetic-nonce")
    params = {k: v for k, v in headers.items() if k not in ("x-signature", "x-version")}
    canonical = module.PATH + "&" + "&".join(k + "=" + params[k] for k in sorted(params))
    expected = base64.b64encode(hmac.new(b"synthetic-secret&", quote(canonical, safe="").encode(), hashlib.sha1).digest()).decode()
    assert headers["x-signature"] == expected
    assert headers["x-version"] == "v2" and headers["x-signature-algorithm"] == "HMAC-SHA1"
    assert headers["host"] == "api.sandbox.webull.com"
