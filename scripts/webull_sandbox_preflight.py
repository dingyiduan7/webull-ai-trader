"""Explicit read-only network runner. Never imported or run by offline pytest."""
import os
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
# Prevent ambient debug/SSL key logging before importing SDK/native transports.
for name in ("WEBULL_API_DEBUG", "GRPC_TRACE", "GRPC_VERBOSITY", "SSLKEYLOGFILE"):
    os.environ.pop(name, None)
from dataclasses import replace
from datetime import datetime, timezone
from importlib.metadata import version
import json
import platform
import argparse
from autonomous_trader.sandbox.secrets import load_credentials, PreflightError
from autonomous_trader.sandbox.sanitize import Sanitizer
from autonomous_trader.sandbox.http import ReadOnlySandbox, Operation, SDK_VERSION
from autonomous_trader.sandbox.evidence import fixture, select_account, save_json, reconciliation_assessment, market_timing_observation
from autonomous_trader.sandbox.streams import mqtt_probe, grpc_probe


def main():
    args = argparse.ArgumentParser(description=__doc__)
    modes = args.add_mutually_exclusive_group()
    modes.add_argument("--accounts-only", action="store_true", help="One read-only account-list diagnostic; no streams")
    modes.add_argument("--mqtt-only", action="store_true", help="Bounded read-only MQTT diagnostic")
    modes.add_argument("--auth-only", action="store_true", help="Read authentication configuration and account list; no token creation or streams")
    modes.add_argument("--legacy-accounts", action="store_true", help="One documented legacy account-list read to diagnose endpoint migration; no streams")
    modes.add_argument("--http-only", action="store_true", help="All read-only HTTP observations; no streams")
    options = args.parse_args()
    if version("webull-openapi-python-sdk") != SDK_VERSION:
        raise PreflightError("UNTESTED_SDK_VERSION")
    credentials = load_credentials(ROOT)
    sanitizer = Sanitizer(credentials.sensitive_values())
    adapter = ReadOnlySandbox(credentials)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output = ROOT / "tests/fixtures/webull_sandbox" / run_id
    report = {"run_id": run_id, "sdk_version": SDK_VERSION, "python_version": platform.python_version(),
        "scope": "M3A_READ_ONLY", "order_writes": 0, "account_confirmed": False, "results": {},
        "configuration_presence": {"account_id": bool(credentials.account_id), "access_token": bool(credentials.access_token)},
        "market_delay_finding": "UNDETERMINED_REQUIRES_ENTITLEMENT_AND_ACTIVE_SESSION_EVIDENCE"}
    for op in ([] if options.mqtt_only else [Operation.AUTH_CONFIG, Operation.ACCOUNTS] if options.auth_only else [Operation.ACCOUNTS] if options.accounts_only or options.legacy_accounts else Operation):
        if op in (Operation.BALANCE, Operation.POSITIONS, Operation.OPEN_ORDERS) and not report["account_confirmed"]:
            report["results"][op.value] = {"blocked": "CASH_ACCOUNT_NOT_CONFIRMED"}
            continue
        try:
            if options.legacy_accounts:
                # Official SDK retains this request; current signature guide and
                # SDK 2.0.19 specify v2. Explicit diagnostic, never automatic fallback.
                from webull.trade.request.v2.get_account_list import GetAccountList
                request = GetAccountList()
                request.set_version("v2")
                observation = adapter._http.get_response(request)
                report["diagnostic_protocol"] = "LEGACY_ACCOUNT_LIST_V2_WITH_SDK_3_SIGNER"
            else:
                observation = adapter.read(op)
            save_json(output / (op.value + ".json"), fixture(observation, sanitizer), sanitizer)
            report["results"][op.value] = {"status": observation.status_code, "fixture": (output / (op.value + ".json")).relative_to(ROOT).as_posix()}
            if op == Operation.AUTH_CONFIG and observation.status_code == 200 and isinstance(observation.body, dict):
                enabled = observation.body.get("token_check_enabled")
                report["authentication_requires_token"] = enabled if isinstance(enabled, bool) else "UNDETERMINED"
            if op == Operation.ACCOUNTS and observation.status_code == 200:
                try:
                    select_account(observation.body, credentials.account_id)
                    report["account_confirmed"] = True
                    report["account_type"] = "CASH"
                except PreflightError as exc:
                    report["account_binding_issue"] = str(exc)
            if op == Operation.OPEN_ORDERS and observation.status_code == 200:
                # v3 cursor protocol. A truncated or repeated cursor is explicit;
                # an empty/stale list never proves terminality or releases shares.
                cursors = set()
                for page in range(1, 4):
                    body = observation.body
                    if not isinstance(body, dict) or not isinstance(body.get("data"), list):
                        report["open_order_pagination"] = "UNRECOGNIZED_V3_SHAPE"
                        break
                    cursor = body.get("pagination_key")
                    if not cursor:
                        report["open_order_pagination"] = "EXHAUSTED_NOT_PROOF_OF_FINALITY"
                        break
                    if not isinstance(cursor, str) or cursor in cursors:
                        report["open_order_pagination"] = "INVALID_OR_REPEATED_CURSOR"
                        break
                    cursors.add(cursor)
                    observation = adapter.read(op, cursor=cursor)
                    save_json(output / f"open_orders_page_{page}.json", fixture(observation, sanitizer), sanitizer)
                    if observation.status_code != 200:
                        report["open_order_pagination"] = "PAGE_REQUEST_FAILED"
                        break
                else:
                    report["open_order_pagination"] = "PAGE_BOUND_REACHED_INCOMPLETE"
        except PreflightError as exc:
            report["results"][op.value] = {"error": str(exc)}
    http_only = options.accounts_only or options.auth_only or options.legacy_accounts or options.http_only
    report["mqtt"] = mqtt_probe(credentials, adapter._http, sanitizer) if not http_only else {"not_requested": True}
    report["grpc"] = grpc_probe(credentials, sanitizer) if report["account_confirmed"] and not http_only else {"blocked": "CASH_ACCOUNT_NOT_CONFIRMED_OR_HTTP_ONLY"}
    # Includes the SDK's guarded HTTP subscribe responses (no request credentials).
    for index, observation in enumerate(adapter._http.observations):
        save_json(output / f"http_{index:02d}.json", fixture(observation, sanitizer), sanitizer)
    report["http_contact_attempts"] = adapter._http.contacted
    report["market_timing"] = market_timing_observation(adapter._http.observations, report["mqtt"])
    report["market_delay_finding"] = report["market_timing"]["finding"]
    report["reconciliation"] = reconciliation_assessment(adapter._http.observations)
    report["sanitization_checks"] = "KNOWN_SECRET_SCAN_PASSED_AND_SCALARS_ALLOWLISTED"
    save_json(output / "manifest.json", report, sanitizer)
    report_name = "m3a-http-diagnostic.json" if options.http_only else "m3a-legacy-diagnostic.json" if options.legacy_accounts else "m3a-auth-diagnostic.json" if options.auth_only else "m3a-mqtt-diagnostic.json" if options.mqtt_only else "m3a-account-diagnostic.json" if options.accounts_only else "m3a-observations.json"
    save_json(ROOT / "docs" / report_name, report, sanitizer)
    print(json.dumps({"result": "OBSERVATIONS_RECORDED", "report": "docs/" + report_name,
                      "account_confirmed": report["account_confirmed"], "order_writes": 0}))


if __name__ == "__main__":
    try:
        main()
    except PreflightError as exc:
        print(json.dumps({"result": "BLOCKED", "reason": str(exc)}))
        sys.exit(2)
    except Exception:
        print(json.dumps({"result": "BLOCKED", "reason": "UNEXPECTED_ERROR_DETAILS_SUPPRESSED"}))
        sys.exit(3)
