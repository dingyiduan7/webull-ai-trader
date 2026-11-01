"""Isolated append-only REAL observation ledger. Never grants order authority.

Broker cash is mirrored with provenance, not imported as a deposit in M2.
External holdings have no managed lots, cost basis, reservations or sell permits.
"""
from datetime import datetime, timezone
from hashlib import sha256
import json
import re

from psycopg import sql
from psycopg.types.json import Jsonb

from .durable.ledger import LocalDatabase
from .real_readiness import RealReadiness

SCHEMA = 'real_readiness'


def binding(account_id):
    if not account_id: raise ValueError('REAL_ACCOUNT_REQUIRED')
    return 'real-' + sha256(account_id.encode()).hexdigest()


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def check_namespace(db):
    if not re.fullmatch(r'real_readiness(?:_test_[a-f0-9]+)?', db.schema):
        raise ValueError('REAL_NAMESPACE_REQUIRED')


def provision(db):
    check_namespace(db)
    with db.connect() as c:
        c.execute(sql.SQL('CREATE SCHEMA IF NOT EXISTS {}').format(sql.Identifier(db.schema)))
        c.execute('''CREATE TABLE IF NOT EXISTS observation_journal (
            version bigint PRIMARY KEY, account_binding text NOT NULL,
            received_at timestamptz NOT NULL, payload jsonb NOT NULL,
            previous_hash text NOT NULL, event_hash text NOT NULL);
            CREATE TABLE IF NOT EXISTS account_state (
            singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
            version bigint NOT NULL, payload jsonb NOT NULL, state_hash text NOT NULL);
            CREATE OR REPLACE FUNCTION immutable_observation() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'APPEND_ONLY_REAL_JOURNAL'; END $$;
            DROP TRIGGER IF EXISTS immutable_observation ON observation_journal;
            CREATE TRIGGER immutable_observation BEFORE UPDATE OR DELETE OR TRUNCATE
            ON observation_journal FOR EACH STATEMENT EXECUTE FUNCTION immutable_observation();''')


def reduce_observation(previous, account_binding, observation):
    value = RealReadiness.model_validate(observation)
    if not re.fullmatch('real-[0-9a-f]{64}', account_binding):
        raise ValueError('REAL_ACCOUNT_BINDING_REQUIRED')
    if previous and previous['account_binding'] != account_binding:
        raise ValueError('REAL_ACCOUNT_BINDING_MISMATCH')
    if previous and value.observed_at <= datetime.fromisoformat(previous['observed_at'].replace('Z', '+00:00')):
        raise ValueError('REAL_OBSERVATION_NOT_NEWER')
    holdings = {p.symbol: str(p.quantity) for p in value.external_holdings}
    old_latch = bool(previous and previous['incident_latched'])
    changed = bool(previous and previous['external_quantities'] != holdings)
    incident = old_latch or changed or not value.account_totals_match or value.open_order_count != 0
    return {'environment': 'REAL_READ_ONLY', 'account_binding': account_binding,
        'observed_at': value.observed_at.isoformat(), 'external_quantities': holdings,
        'managed_positions': {}, 'reservations': {}, 'cash': {
            'settled_usd': str(value.settled_usd), 'unsettled_usd': str(value.unsettled_usd),
            'equity_usd': str(value.equity_usd), 'source': 'BROKER_OBSERVATION'},
        'incident_latched': incident, 'observation_reconciled': not incident,
        'entry_authority': False, 'overnight_enabled': False,
        'version': 1 if previous is None else previous['version']+1}


class RealObservationLedger:
    def __init__(self, db):
        check_namespace(db)
        self.db = db

    def reconcile(self, account_binding, observation, *, now=None):
        value = RealReadiness.model_validate(vars(observation))
        now = now or datetime.now(timezone.utc)
        if not 0 <= (now-value.observed_at).total_seconds() <= 30:
            raise ValueError('REAL_ACCOUNT_OBSERVATION_STALE')
        with self.db.connect() as c:
            # A single transaction owns both journal and checkpoint, across processes.
            c.execute('SELECT pg_advisory_xact_lock(481516,901)')
            prior = self._read(c)
            state = reduce_observation(prior, account_binding, value.model_dump(mode='json'))
            payload = value.model_dump(mode='json')
            last = c.execute('SELECT event_hash FROM observation_journal ORDER BY version DESC LIMIT 1').fetchone()
            prev_hash = last[0] if last else ''
            event = {'version': state['version'], 'account_binding': account_binding,
                     'payload': payload, 'previous_hash': prev_hash}
            c.execute('INSERT INTO observation_journal VALUES(%s,%s,%s,%s,%s,%s)',
                (state['version'], account_binding, value.observed_at, Jsonb(payload), prev_hash, digest(event)))
            c.execute('INSERT INTO account_state VALUES(true,%s,%s,%s) ON CONFLICT(singleton) '
                      'DO UPDATE SET version=excluded.version,payload=excluded.payload,state_hash=excluded.state_hash',
                      (state['version'], Jsonb(state), digest(state)))
            return state

    @staticmethod
    def _read(c):
        row = c.execute('SELECT version,payload,state_hash FROM account_state WHERE singleton').fetchone()
        if not row: return None
        if row[0] != row[1]['version'] or digest(row[1]) != row[2]:
            raise ValueError('REAL_CHECKPOINT_INTEGRITY')
        return row[1]

    def snapshot(self):
        with self.db.connect() as c:
            c.execute('SET TRANSACTION READ ONLY')
            return self._read(c)

    def verify_replay(self):
        with self.db.connect() as c:
            c.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
            state, previous_hash = None, ''
            for version, account, payload, prev, hashed in c.execute(
                    'SELECT version,account_binding,payload,previous_hash,event_hash FROM observation_journal ORDER BY version'):
                event = {'version': version, 'account_binding': account, 'payload': payload, 'previous_hash': prev}
                if prev != previous_hash or digest(event) != hashed:
                    raise ValueError('REAL_JOURNAL_INTEGRITY')
                state = reduce_observation(state, account, payload)
                if state['version'] != version: raise ValueError('REAL_JOURNAL_SEQUENCE')
                previous_hash = hashed
            if state != self._read(c): raise ValueError('REAL_REPLAY_MISMATCH')
            return state


class DisabledRealExecution:
    """Binding is inspectable. No transport or write operation can be obtained."""
    environment = 'REAL'
    host = 'api.webull.com'
    production_enabled = False

    def __init__(self, ledger, account_binding):
        self.ledger, self.account_binding = ledger, account_binding
        state = ledger.snapshot()
        if not state or state['account_binding'] != account_binding:
            raise ValueError('REAL_ACCOUNT_BINDING_MISMATCH')

    def execution_adapter(self):
        # No configuration flag or injected transport can unlock this factory.
        raise ValueError('PRODUCTION_DISABLED')
