"""One bounded production-host read-only preflight. No order/write capability."""
from datetime import datetime, timezone
from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from autonomous_trader.real_preflight import (RealPreflightError, RealReadOnly, Operation,
    high_level_summary, require_success, select_single_individual_cash)
from autonomous_trader.real_secrets import RealSecretError, load_real_credentials
from autonomous_trader.sandbox.sanitize import Sanitizer
from autonomous_trader.real_readiness import account_readiness, save_readiness
from autonomous_trader.real_state import provision, RealObservationLedger, DisabledRealExecution, binding
from autonomous_trader.durable.ledger import LocalDatabase


def main():
    credentials = load_real_credentials(ROOT)
    sanitizer = Sanitizer(tuple(v for v in (credentials.app_key, credentials.app_secret,
        credentials.account_id, credentials.access_token) if v))
    client = RealReadOnly(credentials)
    accounts = require_success(Operation.ACCOUNTS, client.read(Operation.ACCOUNTS))
    selected = select_single_individual_cash(accounts.body)
    account_id = selected["account_id"]
    balance = require_success(Operation.BALANCE, client.read(Operation.BALANCE, account_id=account_id))
    positions = require_success(Operation.POSITIONS, client.read(Operation.POSITIONS, account_id=account_id))
    open_orders = require_success(Operation.OPEN_ORDERS, client.read(Operation.OPEN_ORDERS, account_id=account_id))
    if not isinstance(open_orders.body, dict) or not isinstance(open_orders.body.get("data"), list):
        raise RealPreflightError("REAL_OPEN_ORDER_LIST_SHAPE")
    if open_orders.body.get("pagination_key"):
        raise RealPreflightError("REAL_OPEN_ORDER_PAGINATION_UNRESOLVED")
    observation = account_readiness(balance.body, positions.body, open_orders.body, datetime.now(timezone.utc))
    database = LocalDatabase('postgres', 'm2_admin', schema='real_readiness')
    provision(database)
    ledger = RealObservationLedger(database)
    state = ledger.reconcile(binding(account_id), observation)
    ledger.verify_replay()
    DisabledRealExecution(ledger, binding(account_id))  # binding only, no adapter available
    observation = observation.model_copy(update={'observation_ledger_version': state['version'],
        'observation_reconciled': state['observation_reconciled'],
        'observation_incident_latched': state['incident_latched']})
    save_readiness(ROOT, observation, sanitizer)
    snapshot = client.read(Operation.SNAPSHOT, symbols=("AAPL",))
    if snapshot.status_code == 403:
        save_readiness(ROOT, observation.model_copy(update={'market_data': 'ENTITLEMENT_BLOCKED'}), sanitizer)
    require_success(Operation.SNAPSHOT, snapshot)
    result = high_level_summary(selected, balance.body, positions.body, open_orders.body["data"],
        snapshot.body, snapshot.received_at, datetime.now(timezone.utc))
    text = json.dumps(result, separators=(",", ":"))
    sanitizer.assert_clean(text)
    save_readiness(ROOT, observation.model_copy(update={'market_data': result['market_data']['finding']}), sanitizer)
    print(text)


if __name__ == "__main__":
    try:
        main()
    except (RealSecretError, RealPreflightError) as exc:
        print(json.dumps({"result": "REAL_PREFLIGHT_UNVERIFIED", "reason": str(exc)}, separators=(",", ":")))
        raise SystemExit(2)
    except Exception:
        print(json.dumps({"result": "REAL_PREFLIGHT_UNVERIFIED", "reason": "REAL_PREFLIGHT_FAILED"}, separators=(",", ":")))
        raise SystemExit(2)
