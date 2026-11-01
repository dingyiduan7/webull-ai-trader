"""Live-trading bootstrap: flat checkpoint V3, measured entitlement, deployment evidence.
Real PostgreSQL (isolated schema), fake read-only broker, no network."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from types import SimpleNamespace
from uuid import uuid4
import json

import pytest
from psycopg import sql

from autonomous_trader import owner_authorization as owner
from autonomous_trader.durable.ledger import LocalDatabase
from autonomous_trader.real_flat_checkpoint import load_checkpoint, validate_checkpoint
from autonomous_trader.real_trading import LIVE_ENTITLEMENT_PRODUCT, validate_realtime_entitlement

ACCOUNT = 'acct-123'
OPEN_NOW = datetime(2026, 10, 13, 15, 0, tzinfo=timezone.utc)      # Tue 11:00 ET, regular session
CLOSED_NOW = datetime(2026, 10, 13, 1, 0, tzinfo=timezone.utc)     # evening, closed


class Response:
    def __init__(self, body, received_at):
        self.body, self.status_code, self.received_at = body, 200, received_at.isoformat()


class Broker:
    """Fake RealReadOnly: answers only the five read operations the bootstrap uses."""

    def __init__(self, now, *, positions=(), orders=(), unsettled='0.00', quote_age_s=2):
        self.now, self.positions, self.orders, self.unsettled, self.age = now, list(positions), list(orders), unsettled, quote_age_s
        self.calls = []

    def read(self, operation, *, account_id='', cursor=None, symbols=('AAPL',)):
        name = getattr(operation, 'name', str(operation))
        self.calls.append(name)
        settled = D('505.44') - D(self.unsettled)
        bodies = {
            'ACCOUNTS': [{'account_id': ACCOUNT, 'account_type': 'CASH', 'account_class': 'INDIVIDUAL_CASH'}],
            'BALANCE': {'account_currency_assets': [{'currency': 'USD', 'settled_cash': str(settled),
                        'unsettled_cash': self.unsettled, 'cash_balance': '505.44', 'net_liquidation_value': '505.44'}]},
            'POSITIONS': self.positions,
            'OPEN_ORDERS': {'data': self.orders},
            'SNAPSHOT': [{'symbol': s, 'quote_time': int((self.now - timedelta(seconds=self.age)).timestamp() * 1000)}
                         for s in symbols]}
        return Response(bodies[name], self.now)


@pytest.fixture
def boot(tmp_path, monkeypatch):
    import scripts.real_bootstrap as boot
    import scripts.run_real_daily as daily
    schema = 'real_readiness_test_' + uuid4().hex[:12]
    admin = LocalDatabase('postgres', 'm2_admin', schema='public')
    real_db = LocalDatabase

    def database(name, role, schema='public'):
        return real_db(name, role, schema=schema.replace('real_readiness', schema_name) if schema == 'real_readiness' else schema)
    schema_name = schema
    monkeypatch.setattr(boot, 'LocalDatabase', database)
    monkeypatch.setattr(daily, 'ensure_database', lambda root: None)
    monkeypatch.setattr(boot, 'ROOT', tmp_path)
    monkeypatch.setattr(boot, 'build_digest', lambda root: 'b' * 64)
    monkeypatch.setattr(boot, 'load_real_credentials', lambda root: SimpleNamespace())
    monkeypatch.setattr(boot, 'ledger_status', lambda binding, observation: 'NOT_CREATED_WILL_OPEN_FROM_CHECKPOINT')
    yield boot, tmp_path, monkeypatch
    with admin.connect() as c:
        c.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(sql.Identifier(schema)))


def run(boot, monkeypatch, broker, now):
    monkeypatch.setattr(boot, 'RealReadOnly', lambda credentials: broker)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now
    monkeypatch.setattr(boot, 'datetime', Clock)
    return boot.main()


def test_market_hours_bootstrap_writes_valid_checkpoint_and_evidence(boot):
    boot, root, monkeypatch = boot
    report = run(boot, monkeypatch, Broker(OPEN_NOW), OPEN_NOW)
    assert report['result'] == 'OK_DEPLOYMENT_EVIDENCE_WRITTEN' and report['orders_placed'] == 0
    assert report['quote_age_ms'] == {'AAPL': 2000, 'QQQ': 2000}
    from autonomous_trader.real_state import binding
    path = root / '.local' / 'real-readiness' / report['checkpoint']
    value, sha = load_checkpoint(path, account_binding=binding(ACCOUNT))
    assert sha == report['checkpoint_sha256'] and value['cash']['equity_usd'] == '505.44'
    evidence = owner.load_deployment_evidence(root)
    assert evidence['flat_checkpoint_sha256'] == sha
    prepare = dict(account_binding=binding(ACCOUNT), build_sha256='b' * 64, market_session_label='2026-10-13')
    assert validate_realtime_entitlement(evidence['entitlement_evidence'], prepare, OPEN_NOW, root)
    assert json.loads((root / '.local/real-readiness/bootstrap-latest.json').read_text()) == report
    assert ACCOUNT not in json.dumps(report) and ACCOUNT not in path.read_text()


def test_closed_market_writes_checkpoint_only(boot):
    boot, root, monkeypatch = boot
    report = run(boot, monkeypatch, Broker(CLOSED_NOW), CLOSED_NOW)
    assert report['result'] == 'CHECKPOINT_ONLY_RUN_AGAIN_DURING_MARKET_HOURS'
    assert not owner.evidence_path(root).exists()


@pytest.mark.parametrize('kwargs,problem', [
    (dict(positions=[{'symbol': 'AAL', 'quantity': '1', 'market_value': '13', 'last_price': '13'}]), 'POSITIONS_HELD'),
    (dict(orders=[{'client_order_id': 'x'}]), 'OPEN_ORDERS'),
    (dict(unsettled='10.00'), 'UNSETTLED_CASH_WAIT_FOR_SETTLEMENT')])
def test_not_flat_or_unsettled_account_writes_nothing(boot, kwargs, problem):
    boot, root, monkeypatch = boot
    report = run(boot, monkeypatch, Broker(OPEN_NOW, **kwargs), OPEN_NOW)
    assert report['result'] == 'ACCOUNT_NOT_READY' and problem in report['problems']
    assert not list((root / '.local/real-readiness').glob('flat-checkpoint-*'))


def test_delayed_quotes_are_not_entitlement(boot):
    boot, root, monkeypatch = boot
    report = run(boot, monkeypatch, Broker(OPEN_NOW, quote_age_s=900), OPEN_NOW)
    assert report['result'] == 'QUOTES_NOT_REALTIME' and report['quote_age_ms']['AAPL'] == 900000
    assert not owner.evidence_path(root).exists()


def v3(**changes):
    value = dict(schema='REAL_FLAT_CHECKPOINT_V3', environment='REAL_READ_ONLY', account_binding='real-x',
                 observed_at=OPEN_NOW.isoformat(), account={'type': 'INDIVIDUAL_CASH', 'sole_account': True},
                 positions_count=0, open_order_count=0,
                 cash=dict(settled_usd='505.44', unsettled_usd='0', cash_balance_usd='505.44', equity_usd='505.44',
                           totals_reconciled=True),
                 local_integrity={'observation_replay_verified': True, 'observation_ledger_version': 3},
                 authority=False, created_by='scripts/real_bootstrap.py')
    value.update(changes)
    return value


@pytest.mark.parametrize('changes,code', [
    (dict(account_binding='real-y'), 'V3_SCHEMA'),
    (dict(positions_count=1), 'V3_NOT_FLAT'),
    (dict(cash=dict(settled_usd='495.44', unsettled_usd='10', cash_balance_usd='505.44', equity_usd='505.44',
                    totals_reconciled=True)), 'V3_CASH'),
    (dict(authority=True), 'V3_AUTHORITY'),
    (dict(local_integrity={'observation_replay_verified': True, 'observation_ledger_version': 0}), 'V3_LOCAL'),
    (dict(extra='x'), 'V3_SCHEMA')])
def test_v3_checkpoint_rejects_every_unsafe_variant(changes, code):
    assert validate_checkpoint(v3(), account_binding='real-x')
    with pytest.raises(ValueError, match=code):
        validate_checkpoint(v3(**changes), account_binding='real-x')


def test_live_entitlement_rejects_stale_tampered_or_short_evidence():
    sample = dict(symbol='AAPL', quote_at=(OPEN_NOW - timedelta(seconds=2)).isoformat(),
                  received_at=OPEN_NOW.isoformat(), age_ms=2000, delay_evidence='EXPLICIT_RESPONSE')
    good = dict(account_binding='real-x', build_sha256='b', session_label='2026-10-13', status='VERIFIED',
                product=LIVE_ENTITLEMENT_PRODUCT, observed_at=OPEN_NOW.isoformat(),
                source_evidence=dict(kind='LIVE_QUOTE_FRESHNESS', environment='PRODUCTION', http_host='api.webull.com',
                                     max_quote_age_ms=10000, samples=[sample, dict(sample, symbol='QQQ')]))
    prepare = dict(account_binding='real-x', build_sha256='b', market_session_label='2026-10-13')
    assert validate_realtime_entitlement(good, prepare, OPEN_NOW, None) == good
    stale = dict(sample, quote_at=(OPEN_NOW - timedelta(minutes=15)).isoformat(), age_ms=900000)
    for bad in (dict(good, source_evidence=dict(good['source_evidence'], samples=[sample])),
                dict(good, source_evidence=dict(good['source_evidence'], samples=[sample, stale])),
                dict(good, source_evidence=dict(good['source_evidence'], samples=[sample, dict(sample, age_ms=1)])),
                dict(good, status='PENDING'), dict(good, account_binding='real-y'),
                dict(good, observed_at=(OPEN_NOW + timedelta(minutes=1)).isoformat())):
        with pytest.raises(ValueError, match='REAL_ENTITLEMENT_UNVERIFIED'):
            validate_realtime_entitlement(bad, prepare, OPEN_NOW, None)


def test_first_ledger_opens_from_v3_checkpoint_with_exact_cash_and_no_receivables():
    from autonomous_trader.mvp.config import reviewed_calendar
    from autonomous_trader.real_trading import open_ledger_from_checkpoint
    session = reviewed_calendar().session(OPEN_NOW.date())
    created = []
    ledger = SimpleNamespace(create=lambda command, **kw: created.append((command, kw)))
    packet = SimpleNamespace(account_binding='real-x', instruments=(), validate=lambda now, flat: session,
                             settled=D('505.44'), unsettled=D('0'), equity=D('505.44'))
    open_ledger_from_checkpoint(ledger, v3(), packet, lambda: OPEN_NOW)
    command, kw = created[0]
    assert command.account_id == 'real-x' and command.initial_settled_usd == D('505.44')
    assert command.opening_receivables == () and kw['event_id'] == 'real-opening-real-x'
    for bad_packet, code in ((SimpleNamespace(**{**vars(packet), 'settled': D('500')}), 'CASH_CHANGED'),
                             (SimpleNamespace(**{**vars(packet), 'account_binding': 'real-y'}), 'BINDING')):
        with pytest.raises(ValueError, match=code):
            open_ledger_from_checkpoint(ledger, v3(), bad_packet, lambda: OPEN_NOW)
    with pytest.raises(ValueError, match='V3_REQUIRED'):
        open_ledger_from_checkpoint(ledger, dict(v3(), schema='REAL_FLAT_CHECKPOINT_V2'), packet, lambda: OPEN_NOW)
