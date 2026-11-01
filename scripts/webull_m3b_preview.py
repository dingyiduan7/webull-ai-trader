"""One bounded estimate after fresh flat-account checks; no order submission."""
from pathlib import Path
import sys
import os
import json
import argparse
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
for name in ("SSLKEYLOGFILE", "GRPC_TRACE", "GRPC_VERBOSITY", "WEBULL_LOG_LEVEL", "DEBUG"):
    os.environ.pop(name, None)

from autonomous_trader.sandbox.secrets import load_credentials, PreflightError
from autonomous_trader.sandbox.http import Operation, build_request
from autonomous_trader.sandbox.evidence import fixture, save_json
from autonomous_trader.sandbox.certification.sanitize import OrderSanitizer as Sanitizer
from autonomous_trader.sandbox.certification.controls import MODE, verify_flat, exact_decimal, regular_session
from autonomous_trader.sandbox.certification.journal import CertificationJournal
from autonomous_trader.sandbox.certification.preview import PreviewHttp


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", required=True, choices=[MODE])
    options = parser.parse_args()
    regular_session(datetime.now(timezone.utc))
    credentials = load_credentials(ROOT)
    sanitizer = Sanitizer(credentials.sensitive_values())
    journal = CertificationJournal(ROOT / ".local/m3b/certification.sqlite3", credentials.account_id)
    http = PreviewHttp(credentials, mode=options.mode, journal=journal)
    run = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    folder = ROOT / "tests/fixtures/webull_m3b" / run
    report = {"run_id": run, "scope": MODE, "order_writes": 0, "preview_requests": 0,
              "results": {}, "write_authority": False}
    observations = {}
    try:
        for name, op in (("accounts", Operation.ACCOUNTS), ("balance", Operation.BALANCE),
                         ("positions", Operation.POSITIONS), ("orders", Operation.OPEN_ORDERS),
                         ("instruments", Operation.INSTRUMENTS), ("snapshot", Operation.SNAPSHOT)):
            observation = http.get_response(build_request(op, credentials.account_id))
            observations[name] = observation
            path = folder / (name + ".json")
            save_json(path, fixture(observation, sanitizer), sanitizer)
            report["results"][name] = {"status": observation.status_code, "fixture": path.relative_to(ROOT).as_posix()}
            if observation.status_code != 200:
                raise PreflightError("PREREQUISITE_HTTP_FAILURE")
        prereq = verify_flat(options.mode, credentials.account_id, observations, datetime.now(timezone.utc))
        instruments = observations["instruments"].body.get("data")
        if (not isinstance(instruments, list) or len(instruments) != 1
                or instruments[0].get("symbol") != "AAPL" or instruments[0].get("category") != "US_STOCK"
                or instruments[0].get("sub_category") != "COMMON_STOCK"
                or exact_decimal(instruments[0].get("lot_size")) != 1):
            raise PreflightError("INSTRUMENT_NOT_CONFIRMED")
        snapshots = observations["snapshot"].body
        if not isinstance(snapshots, list) or len(snapshots) != 1 or snapshots[0].get("symbol") != "AAPL":
            raise PreflightError("SNAPSHOT_NOT_CONFIRMED")
        snap = snapshots[0]
        age = Decimal(str(datetime.now(timezone.utc).timestamp())) - Decimal(str(snap["quote_time"])) / 1000
        if not 0 <= age <= 2:
            raise PreflightError("QUOTE_STALE_OR_FUTURE")
        bid, ask = exact_decimal(snap["bid"]), exact_decimal(snap["ask"])
        if not 0 < bid <= ask or (ask - bid) / bid > Decimal("0.002"):
            raise PreflightError("QUOTE_SPREAD_OR_PRICE_INVALID")
        price = (bid * Decimal("0.90")).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        order = dict(client_order_id=uuid4().hex, combo_type="NORMAL", symbol="AAPL", instrument_type="EQUITY",
            market="US", order_type="LIMIT", limit_price=str(price), quantity="1", side="BUY",
            time_in_force="DAY", support_trading_session="CORE", entrust_type="QTY")
        intent_id = journal.prepare("PREVIEW", order)
        report["requested_order"] = sanitizer.clean(order)
        response = http.preview(intent_id, order, prereq)
        report["preview_requests"] = 1
        journal.record("PREVIEW_RESPONSE", fixture(response, sanitizer), intent_id)
        path = folder / "preview.json"
        save_json(path, fixture(response, sanitizer), sanitizer)
        report["results"]["preview"] = {"status": response.status_code, "fixture": path.relative_to(ROOT).as_posix()}
        report["result"] = "PREVIEW_RECORDED_NOT_ORDER_ACCEPTANCE"
    except PreflightError as exc:
        report["stop_reason"] = str(exc)
    finally:
        report["http_contact_attempts"] = http.contacted
        save_json(folder / "manifest.json", report, sanitizer)
        save_json(ROOT / "docs/m3b-preview.json", report, sanitizer)
        journal.close()
    print(json.dumps({"result": report.get("result", "STOPPED"), "stop_reason": report.get("stop_reason"),
                      "order_writes": 0, "preview_requests": report["preview_requests"]}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print('{"result":"STOPPED_DETAILS_SUPPRESSED","order_writes":0}')
        sys.exit(1)
