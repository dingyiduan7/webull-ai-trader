"""One explicit read using the official signature guide; never an auto fallback.

Source: https://developer.webull.com/apis/docs/authentication/signature/
Uses the official SDK's SHA1 primitive and canonical string builder, not its
default composer which hardcodes SHA256. The pinned adapter remains unchanged.
"""
import os
import sys
import json
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
for name in ("WEBULL_API_DEBUG", "SSLKEYLOGFILE"):
    os.environ.pop(name, None)
import requests
from webull.core.auth.algorithm import sha_hmac1
from webull.core.auth.composer.default_signature_composer import _build_sign_string
from autonomous_trader.sandbox.secrets import load_credentials
from autonomous_trader.sandbox.quiet import quiet_sdk
from autonomous_trader.sandbox.http import HTTP_HOST, HttpObservation, SAFE_HEADERS
from autonomous_trader.sandbox.sanitize import Sanitizer
from autonomous_trader.sandbox.evidence import fixture, save_json

PATH = "/openapi/account/list"


def signed_headers(credentials, timestamp, nonce):
    signing = {"host": HTTP_HOST, "x-app-key": credentials.app_key,
        "x-timestamp": timestamp, "x-signature-nonce": nonce,
        "x-signature-version": "1.0", "x-signature-algorithm": "HMAC-SHA1"}
    signature = sha_hmac1.get_sign_string(_build_sign_string(signing, PATH, None), credentials.app_secret + "&")
    return {**signing, "x-signature": signature, "x-version": "v2"}


def main():
    credentials = load_credentials(ROOT)
    sanitizer = Sanitizer(credentials.sensitive_values())
    with quiet_sdk(), requests.Session() as session:
        session.trust_env = False
        headers = signed_headers(credentials, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), uuid4().hex)
        with session.get("https://" + HTTP_HOST + PATH, headers=headers,
                         verify=True, allow_redirects=False, timeout=(5, 10)) as response:
            body = json.loads(response.content, parse_float=Decimal)
            observation = HttpObservation(PATH, response.status_code,
                datetime.now(timezone.utc).isoformat(),
                {k.lower(): v for k, v in response.headers.items() if k.lower() in SAFE_HEADERS},
                body, tuple(k.lower() for k in response.headers))
    report = {"diagnostic": "EXPLICIT_OFFICIAL_GUIDE_SHA1_V2_ACCOUNT_LIST",
        "default_adapter_changed": False, "order_writes": 0,
        "http": fixture(observation, sanitizer),
        "run_id": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")}
    save_json(ROOT / "docs/m3a-documented-signature-diagnostic.json", report, sanitizer)
    save_json(ROOT / "tests/fixtures/webull_sandbox" / report["run_id"] / "documented_signature.json", report, sanitizer)
    print(json.dumps({"status": observation.status_code, "report": "docs/m3a-documented-signature-diagnostic.json", "order_writes": 0}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print('{"result":"SIGNATURE_DIAGNOSTIC_FAILED_DETAILS_SUPPRESSED"}')
        sys.exit(1)
