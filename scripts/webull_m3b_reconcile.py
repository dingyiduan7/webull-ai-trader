"""Read-only reconciliation by the already-persisted first client intent ID."""
from pathlib import Path
import sys
import os
import json
import time
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
for name in ("SSLKEYLOGFILE", "GRPC_TRACE", "GRPC_VERBOSITY", "WEBULL_LOG_LEVEL", "DEBUG"):
    os.environ.pop(name, None)

from webull.trade.request.v3.get_order_history_request_v2 import OrderHistoryRequestV2
from autonomous_trader.sandbox.secrets import load_credentials, PreflightError
from autonomous_trader.sandbox.http import Operation, build_request
from autonomous_trader.sandbox.evidence import fixture, save_json, select_account
from autonomous_trader.sandbox.quiet import quiet_sdk
from autonomous_trader.sandbox.certification.controls import MODE, exact_decimal
from autonomous_trader.sandbox.certification.journal import CertificationJournal
from autonomous_trader.sandbox.certification.first_order import FirstOrderHttp, exact_order_detail
from autonomous_trader.sandbox.certification.events import OrderEvents
from autonomous_trader.sandbox.certification.sanitize import OrderSanitizer


def main():
    credentials = load_credentials(ROOT)
    sanitizer = OrderSanitizer(credentials.sensitive_values())
    journal = CertificationJournal(ROOT / ".local/m3b/certification.sqlite3", credentials.account_id)
    intents = journal.db.execute("SELECT intent_id,payload FROM intents WHERE kind='PLACE'").fetchall()
    if len(intents) != 1:
        raise PreflightError("EXACTLY_ONE_PERSISTED_INTENT_REQUIRED")
    intent_id, payload = intents[0]
    client_id = json.loads(payload)["client_order_id"]
    http = FirstOrderHttp(credentials, mode=MODE, journal=journal)
    stream = OrderEvents(credentials)
    run = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    folder = ROOT / "tests/fixtures/webull_m3b" / run
    report = {"run_id":run,"scope":"M3B_RECONCILIATION_READ_ONLY","order_writes":0,
              "intent_reference":"PERSISTED_FIRST_ORDER","results":{},"events":[],"reservations_released":False}
    def record(name, response):
        path=folder/(name+".json")
        data=fixture(response,sanitizer)
        save_json(path,data,sanitizer)
        journal.record("RECOVERY_OBSERVATION",data,intent_id)
        report["results"][name]={"status":response.status_code,"fixture":path.relative_to(ROOT).as_posix()}
        if response.status_code != 200:
            raise PreflightError("RECOVERY_HTTP_FAILURE")
        return response.body
    try:
        with quiet_sdk():
            report["stream_subscribed"]=stream.start()
            accounts=record("accounts",http.get_response(build_request(Operation.ACCOUNTS)))
            if select_account(accounts,credentials.account_id).get("account_class") != "INDIVIDUAL_CASH":
                raise PreflightError("ACCOUNT_CLASS_MISMATCH")
            detail=record("detail",http.detail(client_id))
            row=exact_order_detail(detail,client_id)
            report["terminal_status"]=row["status"]
            report["filled_quantity"]=row["filled_quantity"]
            positions=record("positions",http.get_response(build_request(Operation.POSITIONS,credentials.account_id)))
            record("orders",http.get_response(build_request(Operation.OPEN_ORDERS,credentials.account_id)))
            record("balance",http.get_response(build_request(Operation.BALANCE,credentials.account_id)))
            report["flat_and_cancelled_by_direct_detail"]=positions==[] and row["status"]=="CANCELLED" and exact_decimal(row["filled_quantity"])==0
            history=OrderHistoryRequestV2()
            history.set_account_id(credentials.account_id)
            # Explicit diagnostic choice: use documented default last seven
            # days. Both tested time-filter spellings returned 417; retain that
            # mismatch rather than pretending a format has been certified.
            report["history_query_scope"] = "DOCUMENTED_DEFAULT_LAST_7_DAYS_ONE_PAGE"
            history_body=record("history",http._send_bounded(history,"/trading/orders/historical-orders/list"))
            report["history_pagination_key_present"]=bool(history_body.get("pagination_key")) if isinstance(history_body,dict) else None
            time.sleep(12)
    except PreflightError as exc:
        report["stop_reason"]=str(exc)
    finally:
        with quiet_sdk(): stream.close()
        for event in stream.drain():
            safe=dict(event,payload=sanitizer.clean(event["payload"]))
            report["events"].append(safe)
            journal.record("RECOVERY_GRPC_OBSERVATION",safe,intent_id)
        report["stream_result"]=stream.result
        report["http_contact_attempts"]=http.contacted
        save_json(folder/"manifest.json",report,sanitizer)
        save_json(ROOT/"docs/m3b-reconciliation.json",report,sanitizer)
        journal.close()
    print(json.dumps({k:report.get(k) for k in ("scope","order_writes","terminal_status","filled_quantity", "flat_and_cancelled_by_direct_detail","stop_reason")}))


if __name__ == "__main__":
    try: main()
    except Exception:
        print('{"result":"RECONCILIATION_STOPPED_DETAILS_SUPPRESSED"}')
        sys.exit(1)
