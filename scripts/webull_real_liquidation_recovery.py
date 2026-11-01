"""Manual review entrypoint for the unactivated four-ETF recovery campaign."""
from pathlib import Path
import argparse
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))

from autonomous_trader import real_liquidation_recovery as workflow
from autonomous_trader.real_liquidation_recovery_broker import RecoveryBroker
from autonomous_trader.real_state import RealObservationLedger, provision
from autonomous_trader.durable.ledger import LocalDatabase
from autonomous_trader.real_secrets import load_real_credentials
from autonomous_trader.sandbox.sanitize import Sanitizer


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    if args.execute and args.dry_run:
        raise workflow.Blocked('CHOOSE_ONE_MODE')
    if args.execute and not workflow.EXECUTION_APPROVED:
        raise workflow.Blocked('JUDICATOR_APPROVAL_REQUIRED')
    database = LocalDatabase('postgres', 'm2_admin', schema='real_readiness')
    provision(database)
    state = RealObservationLedger(database).verify_replay()
    if not state:
        raise workflow.Blocked('REAL_ACCOUNT_BINDING_REQUIRED')
    credentials = load_real_credentials(ROOT)
    sanitizer = Sanitizer(tuple(v for v in (credentials.app_key, credentials.app_secret,
        credentials.account_id, credentials.access_token) if v))
    journal = workflow.Journal(database)
    broker = RecoveryBroker(credentials, state['account_binding'], journal=journal)
    if args.execute:
        if not sys.stdin.isatty():
            raise workflow.Blocked('SUPERVISED_TERMINAL_REQUIRED')
        result = workflow.execute_once(broker, journal, state['account_binding'])
    else:
        result = workflow.dry_run(broker, journal, state['account_binding'])
    encoded = json.dumps(result)
    sanitizer.assert_clean(encoded)
    print(encoded)


if __name__ == '__main__':
    try:
        main()
    except workflow.Blocked as exc:
        print(json.dumps({'status':'BLOCKED','blocker':str(exc)}))
        raise SystemExit(2)
    except Exception:
        print(json.dumps({'status':'BLOCKED','blocker':'READ_OR_LOCAL_FAILURE'}))
        raise SystemExit(2)
