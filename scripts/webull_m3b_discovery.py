"""Explicit M3B discovery only. This script has no order-writing route."""
from pathlib import Path
import sys
import os
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
for name in ("SSLKEYLOGFILE", "GRPC_TRACE", "GRPC_VERBOSITY", "WEBULL_LOG_LEVEL", "DEBUG"):
    os.environ.pop(name, None)

from autonomous_trader.sandbox.secrets import load_credentials, PreflightError
from autonomous_trader.sandbox.http import Operation, build_request
from autonomous_trader.sandbox.evidence import fixture, save_json, select_account
from autonomous_trader.sandbox.sanitize import Sanitizer
from autonomous_trader.sandbox.certification.discovery import DiscoveryHttp, calendar_request


def main():
    credentials = load_credentials(ROOT)
    if not credentials.account_id:
        raise PreflightError("DESIGNATED_ACCOUNT_REQUIRED")
    sanitizer = Sanitizer(credentials.sensitive_values())
    http = DiscoveryHttp(credentials)
    run = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    folder = ROOT / "tests/fixtures/webull_m3b" / run
    report = {"run_id": run, "scope": "M3B_DISCOVERY_READ_ONLY", "order_writes": 0,
              "results": {}, "account_confirmed": False, "write_authority": False}
    requests_to_make = [(op.value, build_request(op, credentials.account_id)) for op in
        (Operation.ACCOUNTS, Operation.BALANCE, Operation.POSITIONS, Operation.OPEN_ORDERS, Operation.INSTRUMENTS)]
    day = datetime.now(ZoneInfo("America/New_York")).date()
    requests_to_make.append(("calendar", calendar_request(day)))
    requests_to_make.append(("snapshot", build_request(Operation.SNAPSHOT)))
    for name, request in requests_to_make:
        try:
            observation = http.get_response(request)
        except PreflightError as exc:
            report["stop_reason"] = str(exc)
            report["failed_operation"] = name
            break
        target = folder / (name + ".json")
        save_json(target, fixture(observation, sanitizer), sanitizer)
        report["results"][name] = {"status": observation.status_code,
            "fixture": target.relative_to(ROOT).as_posix()}
        if observation.status_code != 200:
            report["stop_reason"] = name.upper() + "_READ_FAILED"
            break
        if name == "accounts":
            selected = select_account(observation.body, credentials.account_id)
            if selected.get("account_class") != "INDIVIDUAL_CASH":
                raise PreflightError("INDIVIDUAL_CASH_NOT_CONFIRMED")
            report["account_confirmed"] = True
        if name == "positions" and observation.body != []:
            report["stop_reason"] = "INITIAL_POSITIONS_NOT_EMPTY"
            break
        if name == "open_orders" and observation.body != {"data": []}:
            report["stop_reason"] = "INITIAL_ORDERS_NOT_EMPTY_OR_PAGINATED"
            break
    report["http_contact_attempts"] = http.contacted
    save_json(folder / "manifest.json", report, sanitizer)
    save_json(ROOT / "docs/m3b-discovery.json", report, sanitizer)
    print(json.dumps({"result": "DISCOVERY_RECORDED", "order_writes": 0,
                      "stop_reason": report.get("stop_reason")}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print('{"result":"STOPPED_DETAILS_SUPPRESSED","order_writes":0}')
        sys.exit(1)
