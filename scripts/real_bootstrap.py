"""Read-only live-trading bootstrap for this Auto Trader copy. Never places, changes or cancels an order.

Run once (during regular market hours for the full result):
  1. Confirms the single Webull INDIVIDUAL_CASH account is flat (no positions, no open orders)
     and fully settled, with consistent cash totals.
  2. Reconciles the local read-only observation journal and writes a fresh
     REAL_FLAT_CHECKPOINT_V3 (.local/real-readiness/flat-checkpoint-v3-<time>.json).
  3. Reports whether the local live ledger already exists and whether it matches the account.
  4. During market hours: measures real-time quote freshness (AAPL, QQQ) as entitlement evidence.
  5. Writes .local/real-readiness/deployment-evidence.json when both pieces exist.
Prints one JSON summary line (no secrets) and writes the same to .local/real-readiness/bootstrap-latest.json.
"""
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))

from autonomous_trader import owner_authorization as owner  # noqa: E402
from autonomous_trader.durable.ledger import LocalDatabase  # noqa: E402
from autonomous_trader.mvp.config import reviewed_calendar  # noqa: E402
from autonomous_trader.real_flat_checkpoint import CHECKPOINT_SCHEMA_V3, checkpoint_digest, load_checkpoint  # noqa: E402
from autonomous_trader.real_preflight import (Operation, RealReadOnly, require_success,  # noqa: E402
    select_single_individual_cash)
from autonomous_trader.real_readiness import account_readiness  # noqa: E402
from autonomous_trader.real_secrets import load_real_credentials  # noqa: E402
from autonomous_trader.real_state import RealObservationLedger, binding, provision  # noqa: E402
from autonomous_trader.real_trading import LIVE_ENTITLEMENT_PRODUCT, LIVE_QUOTE_MAX_AGE_MS, build_digest  # noqa: E402
from autonomous_trader.reference import NEW_YORK  # noqa: E402
from autonomous_trader.runtime_storage import data_root  # noqa: E402

FRESHNESS_SYMBOLS = ('AAPL', 'QQQ')


def quote_time(value):
    number = int(str(value))
    return datetime.fromtimestamp(number / 1000 if number > 10**11 else number, timezone.utc)


def delay_evidence(quote):
    delay = str(quote.get('delay_type') or quote.get('quote_type') or '').upper().replace('-', '_')
    if quote.get('is_delayed') is True or 'DELAY' in delay:
        raise ValueError('REAL_QUOTES_EXPLICITLY_DELAYED')
    return ('EXPLICIT_RESPONSE' if quote.get('is_delayed') is False or delay in ('REAL_TIME', 'REALTIME')
            else 'DOCUMENTED_PRODUCTION_OPENAPI_SUBSCRIPTION_PLUS_CURRENT_EFFECTIVENESS')


def account_snapshot(client):
    accounts = require_success(Operation.ACCOUNTS, client.read(Operation.ACCOUNTS))
    account_id = select_single_individual_cash(accounts.body)['account_id']
    balance = require_success(Operation.BALANCE, client.read(Operation.BALANCE, account_id=account_id))
    positions = require_success(Operation.POSITIONS, client.read(Operation.POSITIONS, account_id=account_id))
    orders = require_success(Operation.OPEN_ORDERS, client.read(Operation.OPEN_ORDERS, account_id=account_id))
    observation = account_readiness(balance.body, positions.body, orders.body, datetime.now(timezone.utc))
    return account_id, observation


def flat_problems(observation):
    problems = []
    if observation.external_holdings:
        problems.append('POSITIONS_HELD')
    if observation.open_order_count:
        problems.append('OPEN_ORDERS')
    if not observation.account_totals_match:
        problems.append('CASH_TOTALS_INCONSISTENT')
    if observation.unsettled_usd != 0:
        problems.append('UNSETTLED_CASH_WAIT_FOR_SETTLEMENT')
    if observation.equity_usd != observation.settled_usd + observation.unsettled_usd:
        problems.append('EQUITY_NOT_EQUAL_CASH')
    return problems


def write_checkpoint(folder, account_binding, observation, version):
    stamp = observation.observed_at.strftime('%Y%m%dT%H%M%SZ')
    value = dict(schema=CHECKPOINT_SCHEMA_V3, environment='REAL_READ_ONLY', account_binding=account_binding,
                 observed_at=observation.observed_at.isoformat(),
                 account={'type': 'INDIVIDUAL_CASH', 'sole_account': True},
                 positions_count=0, open_order_count=0,
                 cash=dict(settled_usd=str(observation.settled_usd), unsettled_usd=str(observation.unsettled_usd),
                           cash_balance_usd=str(observation.settled_usd + observation.unsettled_usd),
                           equity_usd=str(observation.equity_usd), totals_reconciled=True),
                 local_integrity={'observation_replay_verified': True, 'observation_ledger_version': version},
                 authority=False, created_by='scripts/real_bootstrap.py')
    path = folder / ('flat-checkpoint-v3-' + stamp + '.json')
    with path.open('x', encoding='utf-8') as out:
        json.dump(value, out, sort_keys=True, separators=(',', ':'))
    load_checkpoint(path, account_binding=account_binding)  # self-check through the runtime validator
    return path, checkpoint_digest(path)


def ledger_status(account_binding, observation):
    from autonomous_trader.durable.ledger import Ledger
    from autonomous_trader.durable.projection import Rejected
    try:
        state = Ledger(LocalDatabase('postgres', 'm2_admin', schema='real_trader'),
                       execution_scope='REAL').snapshot(account_binding)
    except Rejected as exc:
        return 'NOT_CREATED_WILL_OPEN_FROM_CHECKPOINT' if str(exc) == 'UNKNOWN_ACCOUNT' else 'UNREADABLE'
    except Exception:
        return 'NOT_CREATED_WILL_OPEN_FROM_CHECKPOINT_OR_SCHEMA_ABSENT'
    cash = state.get('cash', {})
    same = (Decimal(cash.get('settled_usd', '-1')) == observation.settled_usd
            and Decimal(cash.get('unsettled_usd', '-1')) == observation.unsettled_usd
            and not state.get('positions'))
    return 'EXISTS_MATCHES_ACCOUNT' if same else 'EXISTS_DIFFERS_FROM_ACCOUNT'


def freshness_evidence(client, account_binding, session, now):
    response = client.read(Operation.SNAPSHOT, symbols=FRESHNESS_SYMBOLS)
    body = require_success(Operation.SNAPSHOT, response).body
    received = datetime.fromisoformat(response.received_at)
    if not isinstance(body, list) or {q.get('symbol') for q in body} != set(FRESHNESS_SYMBOLS):
        raise ValueError('REAL_QUOTE_SHAPE')
    samples = []
    for quote in sorted(body, key=lambda q: q['symbol']):
        at = quote_time(quote['quote_time'])
        age = round((received - at).total_seconds() * 1000)
        samples.append(dict(symbol=quote['symbol'], quote_at=at.isoformat(), received_at=received.isoformat(),
                            age_ms=age, delay_evidence=delay_evidence(quote)))
    if any(not 0 <= s['age_ms'] <= LIVE_QUOTE_MAX_AGE_MS for s in samples):
        return None, samples
    return dict(account_binding=account_binding, build_sha256=build_digest(ROOT),
                session_label=session.label.isoformat(), status='VERIFIED', product=LIVE_ENTITLEMENT_PRODUCT,
                observed_at=now.isoformat(),
                source_evidence=dict(kind='LIVE_QUOTE_FRESHNESS', environment='PRODUCTION',
                                     http_host='api.webull.com', max_quote_age_ms=LIVE_QUOTE_MAX_AGE_MS,
                                     samples=samples)), samples


def main():
    from scripts.run_real_daily import ensure_database
    folder = data_root(ROOT) / '.local' / 'real-readiness'
    folder.mkdir(parents=True, exist_ok=True)
    report = {'at': datetime.now(timezone.utc).isoformat(), 'result': 'FAILED', 'orders_placed': 0}
    try:
        ensure_database(ROOT)
        credentials = load_real_credentials(ROOT)
        client = RealReadOnly(credentials)
        account_id, observation = account_snapshot(client)
        account_binding = binding(account_id)
        report.update(equity_usd=str(observation.equity_usd), settled_usd=str(observation.settled_usd),
                      unsettled_usd=str(observation.unsettled_usd), positions=len(observation.external_holdings),
                      open_orders=observation.open_order_count)
        problems = flat_problems(observation)
        if problems:
            report.update(result='ACCOUNT_NOT_READY', problems=problems)
            return report
        database = LocalDatabase('postgres', 'm2_admin', schema='real_readiness')
        provision(database)
        journal = RealObservationLedger(database)
        state = journal.reconcile(account_binding, observation, now=datetime.now(timezone.utc))
        journal.verify_replay()
        path, sha = write_checkpoint(folder, account_binding, observation, state['version'])
        report.update(checkpoint=path.name, checkpoint_sha256=sha,
                      live_ledger=ledger_status(account_binding, observation))
        now = datetime.now(timezone.utc)
        session = reviewed_calendar().session(now.astimezone(NEW_YORK).date())
        if session is None or not session.contains(now):
            report.update(result='CHECKPOINT_ONLY_RUN_AGAIN_DURING_MARKET_HOURS')
            return report
        entitlement, samples = freshness_evidence(client, account_binding, session, now)
        report['quote_age_ms'] = {s['symbol']: s['age_ms'] for s in samples}
        if entitlement is None:
            report.update(result='QUOTES_NOT_REALTIME')
            return report
        owner.write_deployment_evidence(data_root(ROOT), dict(schema=owner.EVIDENCE_SCHEMA,
            flat_checkpoint_sha256=sha, entitlement_evidence=entitlement))
        report.update(result='OK_DEPLOYMENT_EVIDENCE_WRITTEN')
        return report
    except Exception as exc:
        text = str(exc)
        report['error'] = text if isinstance(exc, ValueError) and text.isupper() and ' ' not in text else type(exc).__name__
        return report
    finally:
        line = json.dumps(report, separators=(',', ':'))
        (folder / 'bootstrap-latest.json').write_text(line, encoding='utf-8')
        print(line)


if __name__ == '__main__':
    raise SystemExit(0 if main()['result'].startswith(('OK', 'CHECKPOINT_ONLY')) else 1)
