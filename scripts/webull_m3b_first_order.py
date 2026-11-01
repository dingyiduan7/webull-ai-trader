"""Bounded first buy/cancel sequence only; never advances to another order."""
from pathlib import Path
import sys
import os
import json
import argparse
import time
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
for name in ("SSLKEYLOGFILE", "GRPC_TRACE", "GRPC_VERBOSITY", "WEBULL_LOG_LEVEL", "DEBUG"):
    os.environ.pop(name, None)

from autonomous_trader.sandbox.secrets import load_credentials, PreflightError
from autonomous_trader.sandbox.http import Operation, build_request, SandboxHttp
from autonomous_trader.sandbox.evidence import fixture, save_json, select_account
from autonomous_trader.sandbox.quiet import quiet_sdk
from autonomous_trader.sandbox.certification.sanitize import OrderSanitizer
from autonomous_trader.sandbox.certification.controls import MODE, verify_flat, exact_decimal, regular_session, FlatPrerequisites
from autonomous_trader.sandbox.certification.journal import CertificationJournal
from autonomous_trader.sandbox.certification.first_order import FirstOrderHttp, exact_order_detail
from autonomous_trader.sandbox.certification.events import OrderEvents
from autonomous_trader.durable.ledger import LocalDatabase

WRITER_LOCK=17482026

def acquire_writer_lock():
    connection=LocalDatabase('postgres','m2_admin',schema='mvp_sandbox').connect();connection.autocommit=True
    if not connection.execute('SELECT pg_try_advisory_lock(%s)',(WRITER_LOCK,)).fetchone()[0]:
        connection.close();raise PreflightError('RUNNER_ALREADY_ACTIVE')
    return connection


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", required=True, choices=[MODE])
    options = parser.parse_args()
    regular_session(datetime.now(timezone.utc))
    writer_lock=acquire_writer_lock()
    credentials = load_credentials(ROOT)
    sanitizer = OrderSanitizer(credentials.sensitive_values())
    journal = CertificationJournal(ROOT / ".local/m3b/certification.sqlite3", credentials.account_id)
    # Refuse a second sequence even if a prior list is empty. Recovery is read-only.
    if journal.db.execute("SELECT 1 FROM intents WHERE kind='PLACE'").fetchone():
        journal.close()
        raise PreflightError("FIRST_SEQUENCE_ALREADY_RECORDED_RECONCILE_ONLY")
    preview = json.loads((ROOT / "docs/m3b-preview.json").read_text())
    if preview.get("results", {}).get("preview", {}).get("status") != 200:
        raise PreflightError("PREVIEW_REQUIRED")
    # Preview fee evidence must be actual reviewed numeric values, not redacted.
    preview_body = json.loads((ROOT / preview["results"]["preview"]["fixture"]).read_text())["body"]
    fee = exact_decimal(preview_body.get("estimated_transaction_fee"))
    if not 0 <= fee <= Decimal("12.50") or preview_body.get("currency") != "USD":
        raise PreflightError("FEE_ESTIMATE_NOT_WITHIN_TEST_RESERVE")
    reads = SandboxHttp(credentials)
    writer = FirstOrderHttp(credentials, mode=options.mode, journal=journal)
    stream = OrderEvents(credentials)
    run = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    folder = ROOT / "tests/fixtures/webull_m3b" / run
    report = {"run_id": run, "scope": MODE, "phase": "FIRST_NONMARKETABLE_BUY_CANCEL",
              "order_submissions": 0, "cancel_transmissions": 0, "events": [], "results": {},
              "reservations_released": False, "advanced_to_next_test": False}
    def record(name, observation):
        path = folder / (name + ".json")
        data = fixture(observation, sanitizer)
        save_json(path, data, sanitizer)
        journal.record("HTTP_OBSERVATION", data)
        report["results"][name] = {"status": observation.status_code, "fixture": path.relative_to(ROOT).as_posix()}
        return observation
    def read_bundle(prefix):
        return {name: record(prefix + "_" + name, reads.get_response(build_request(op, credentials.account_id)))
            for name, op in (("accounts", Operation.ACCOUNTS), ("balance", Operation.BALANCE),
                ("positions", Operation.POSITIONS), ("orders", Operation.OPEN_ORDERS),
                ("instruments", Operation.INSTRUMENTS), ("snapshot", Operation.SNAPSHOT))}
    def collect():
        raw = stream.drain()
        for event in raw:
            safe = dict(event, payload=sanitizer.clean(event["payload"]))
            journal.record("GRPC_OBSERVATION", safe)
            report["events"].append(safe)
        return raw
    try:
        with quiet_sdk():
            if not stream.start():
                raise PreflightError("GRPC_SUBSCRIPTION_NOT_READY")
            observations = read_bundle("before")
            prereq = verify_flat(options.mode, credentials.account_id, observations, datetime.now(timezone.utc))
            profiles = observations["instruments"].body.get("data")
            if (not isinstance(profiles, list) or len(profiles) != 1 or profiles[0].get("symbol") != "AAPL"
                    or profiles[0].get("category") != "US_STOCK" or profiles[0].get("sub_category") != "COMMON_STOCK"
                    or profiles[0].get("status") != "OC" or exact_decimal(profiles[0].get("lot_size")) != 1):
                raise PreflightError("INSTRUMENT_CAPABILITY_CHANGED")
            snap = observations["snapshot"].body
            if not isinstance(snap, list) or len(snap) != 1 or snap[0].get("symbol") != "AAPL":
                raise PreflightError("SNAPSHOT_BINDING_MISMATCH")
            snap = snap[0]
            bid, ask = exact_decimal(snap.get("bid")), exact_decimal(snap.get("ask"))
            if not 0 < bid <= ask or (ask-bid)/bid > Decimal("0.002"):
                raise PreflightError("QUOTE_INVALID")
            price = (bid * Decimal("0.90")).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
            quote_at = datetime.fromtimestamp(int(snap["quote_time"])/1000, timezone.utc)
            order = dict(client_order_id=uuid4().hex, combo_type="NORMAL", symbol="AAPL", instrument_type="EQUITY",
                market="US", order_type="LIMIT", limit_price=str(price), quantity="1", side="BUY",
                time_in_force="DAY", support_trading_session="CORE", entrust_type="QTY")
            client_id = order["client_order_id"]
            intent_id = journal.prepare("PLACE", order)
            journal.record("RESERVED", {"quantity": "1", "cash": str(price+25), "exposure": str(price),
                "risk_full_principal_and_costs": str(price+25), "fee_reserve": "25"}, intent_id)
            report["order"] = sanitizer.clean(order)
            report["order_submissions"] = 1  # attempted; transport ambiguity retains this obligation
            submitted = record("submit", writer.submit_first(intent_id, order, prereq, bid, quote_at, stream.healthy()))
            if submitted.status_code != 200:
                raise PreflightError("SUBMIT_NOT_CONFIRMED_RECONCILE_ONLY")
            detail = record("working_detail", writer.detail(client_id))
            row = exact_order_detail(detail.body, client_id)
            report["working_status"] = row["status"]
            if row["status"] not in ("PENDING", "SUBMITTED") or exact_decimal(row["filled_quantity"]) != 0:
                raise PreflightError("UNEXPECTED_EXECUTION_OR_STATUS_RECONCILE_ONLY")
            record("working_orders", reads.get_response(build_request(Operation.OPEN_ORDERS, credentials.account_id)))
            time.sleep(8)
            events = collect()
            report["initial_order_event_observed"] = any(isinstance(e["payload"], dict)
                and e["payload"].get("client_order_id") == client_id for e in events)
            # Missing initial ack is a certification gap, but direct known
            # unfilled detail permits the already-authorized cancellation cleanup.
            before_cancel = read_bundle("cancel_before")
            now = datetime.now(timezone.utc)
            if any(o.status_code != 200 or not 0 <= (now-datetime.fromisoformat(o.received_at)).total_seconds() <= 30
                   for o in before_cancel.values()):
                raise PreflightError("CANCEL_ACCOUNT_READS_UNCERTAIN")
            selected = select_account(before_cancel["accounts"].body, credentials.account_id)
            if selected.get("account_class") != "INDIVIDUAL_CASH" or before_cancel["positions"].body != []:
                raise PreflightError("CANCEL_ACCOUNT_OR_POSITION_CHANGED")
            open_body = before_cancel["orders"].body
            if not isinstance(open_body, dict) or not isinstance(open_body.get("data"), list) or open_body.get("pagination_key"):
                raise PreflightError("CANCEL_OPEN_ORDERS_INCOMPLETE")
            def client_ids(value):
                if isinstance(value, dict):
                    return ({value["client_order_id"]} if value.get("client_order_id") else set()).union(
                        *(client_ids(v) for v in value.values()))
                if isinstance(value, list):
                    return set().union(*(client_ids(v) for v in value))
                return set()
            ids = client_ids(open_body["data"])
            if ids - {client_id} or (open_body["data"] and ids != {client_id}):
                raise PreflightError("OTHER_OR_UNRECOGNIZED_OPEN_ORDER")
            # Account is observed afresh; no buying-power based release occurs.
            assets = before_cancel["balance"].body["account_currency_assets"]
            if len(assets) != 1 or assets[0].get("currency") != "USD":
                raise PreflightError("CANCEL_BALANCE_UNKNOWN")
            prereq = FlatPrerequisites(credentials.account_id, now, exact_decimal(assets[0]["net_liquidation_value"]),
                                      exact_decimal(assets[0]["settled_cash"]))
            detail = record("cancel_target_detail", writer.detail(client_id))
            cancel_id = journal.prepare("CANCEL", {"client_order_id": client_id})
            report["cancel_transmissions"] = 1
            response = record("cancel", writer.cancel_known_unfilled(cancel_id, client_id, detail, prereq, stream.healthy()))
            if response.status_code != 200:
                raise PreflightError("CANCEL_NOT_CONFIRMED_RECONCILE_ONLY")
            time.sleep(4)
            terminal = record("terminal_detail", writer.detail(client_id))
            terminal_row = exact_order_detail(terminal.body, client_id)
            events = collect()
            report["terminal_status"] = terminal_row["status"]
            report["cancel_event_observed"] = any(isinstance(e["payload"], dict)
                and e["payload"].get("client_order_id") == client_id
                and e["payload"].get("scene_type") == "CANCEL_SUCCESS" for e in events)
            record("after_positions", reads.get_response(build_request(Operation.POSITIONS, credentials.account_id)))
            record("after_orders", reads.get_response(build_request(Operation.OPEN_ORDERS, credentials.account_id)))
            record("after_balance", reads.get_response(build_request(Operation.BALANCE, credentials.account_id)))
            report["result"] = "FIRST_SEQUENCE_OBSERVED_REQUIRES_REVIEW"
            if terminal_row["status"] != "CANCELLED" or exact_decimal(terminal_row["filled_quantity"]) != 0:
                raise PreflightError("TERMINAL_NOT_CONFIRMED")
            if not report["initial_order_event_observed"]:
                report["stop_reason"] = "INITIAL_ORDER_EVENT_NOT_OBSERVED"
            elif not report["cancel_event_observed"]:
                report["stop_reason"] = "CANCEL_EVENT_NOT_OBSERVED"
    except PreflightError as exc:
        report["stop_reason"] = str(exc)
    except Exception:
        report["stop_reason"] = "UNEXPECTED_ERROR_RECONCILE_ONLY_DETAILS_SUPPRESSED"
    finally:
        with quiet_sdk():
            stream.close()
        collect()
        report["http_contact_attempts"] = reads.contacted + writer.contacted
        report["order_submissions"] = sum(x["path"] == "/trading/orders/place" for x in writer.contacted)
        report["cancel_transmissions"] = sum(x["path"] == "/trading/orders/cancel" for x in writer.contacted)
        save_json(folder / "manifest.json", report, sanitizer)
        save_json(ROOT / "docs/m3b-first-order.json", report, sanitizer)
        journal.record("SEQUENCE_STOPPED", {"stop_reason": report.get("stop_reason"), "reservations_released": False})
        journal.close()
    print(json.dumps({k: report.get(k) for k in ("result", "stop_reason", "order_submissions", "cancel_transmissions", "terminal_status")}))
    writer_lock.close()


if __name__ == "__main__":
    try:
        main()
    except PreflightError as exc:
        print(json.dumps({"result": "STOPPED_BEFORE_SEQUENCE", "reason": str(exc)}))
        sys.exit(2)
    except Exception:
        print('{"result":"STOPPED_DETAILS_SUPPRESSED"}')
        sys.exit(1)
