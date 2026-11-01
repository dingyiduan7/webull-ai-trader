"""PostgreSQL transactional ledger and deterministic risk service.

Only loopback PostgreSQL connections are representable. This module never
dispatches an order. Consuming an outbox permit records uncertainty atomically
so a future adapter cannot treat process failure as fresh submission authority.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
import re

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from ..contracts import utc
from ..risk import DenyAllOwners, Mode
from ..reference import Session
from .models import CreateAccount, COMMAND, Restart
from .projection import reduce_event, digest, canonical, require, Rejected, stamp


@dataclass(frozen=True)
class LocalDatabase:
    database: str
    user: str
    port: int = 55433
    schema: str = "trader_m2"
    host: str = "127.0.0.1"

    def __post_init__(self):
        if self.host != "127.0.0.1" or not 1 <= self.port <= 65535:
            raise ValueError("M2 supports only explicit loopback PostgreSQL")
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", self.schema):
            raise ValueError("invalid database schema identifier")

    def connect(self):
        return psycopg.connect(host=self.host, hostaddr=self.host, port=self.port,
            dbname=self.database, user=self.user, password="", sslmode="disable", connect_timeout=3,
            application_name="autonomous_trader_M2_OFFLINE",
            options=f"-c search_path={self.schema},pg_catalog -c synchronous_commit=on -c lock_timeout=5000 -c statement_timeout=30000")


def migrate(config: LocalDatabase):
    """Run using a separately held migration/admin identity, never a model role."""
    with config.connect() as conn:
        conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(config.schema)))
        conn.execute((Path(__file__).parent / "schema.sql").read_text(encoding="utf-8"))


def grant_writer(admin: LocalDatabase, role: str):
    """Least-privilege local role provisioning. No password or API credential."""
    with admin.connect() as conn:
        if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone():
            conn.execute(sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE").format(sql.Identifier(role)))
        conn.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(sql.Identifier(admin.schema), sql.Identifier(role)))
        for table in PROJECTIONS + ("accounts",):
            conn.execute(sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON {} TO {}").format(sql.Identifier(table), sql.Identifier(role)))
        conn.execute(sql.SQL("GRANT SELECT, INSERT ON journal TO {}").format(sql.Identifier(role)))
        conn.execute(sql.SQL("GRANT SELECT ON schema_version TO {}").format(sql.Identifier(role)))


PROJECTIONS = ("cash_balances", "position_lots", "positions", "settlement_lots", "reservations", "safety_latches",
               "segments", "owner_restarts", "risk_permits", "outbox", "leadership", "reconciliation")


class Ledger:
    def __init__(self, config: LocalDatabase, *, owner_authenticator=None, fault_hook=None, execution_scope='OFFLINE'):
        if execution_scope not in ('OFFLINE','SANDBOX','REAL'):raise ValueError('PRODUCTION_DISABLED')
        real_namespace=bool(re.fullmatch(r'real_trader(?:_test_[a-f0-9]+)?',config.schema))
        if (execution_scope=='REAL') != real_namespace:raise ValueError('REAL_LEDGER_NAMESPACE_REQUIRED')
        self.execution_scope=execution_scope
        self.config = config
        self.owner_authenticator = owner_authenticator or DenyAllOwners()
        # A local test hook can raise/kill at named crash boundaries. Never journaled.
        self.fault_hook = fault_hook or (lambda boundary, connection: None)

    @staticmethod
    def _clock(conn):
        now, started, oid = conn.execute("SELECT clock_timestamp(), pg_postmaster_start_time(), oid FROM pg_database WHERE datname=current_database()").fetchone()
        return now, stamp(started) + "|database_oid=" + str(oid)

    def _load(self, conn, account_id):
        row = conn.execute("SELECT version,state,state_hash,head_hash FROM accounts WHERE account_id=%s FOR UPDATE", (account_id,)).fetchone()
        require(row is not None, "UNKNOWN_ACCOUNT")
        version, state, state_hash, head_hash = row
        require(version == state["version"] and digest(state) == state_hash, "CHECKPOINT_INTEGRITY_FAILURE")
        require((state.get('execution_scope')=='REAL') == (self.execution_scope=='REAL'), 'LEDGER_ENVIRONMENT_MISMATCH')
        return state, head_hash

    @staticmethod
    def _duplicate(conn, account_id, event_id, request_hash):
        old = conn.execute("SELECT request_hash,payload FROM journal WHERE account_id=%s AND event_id=%s", (account_id, event_id)).fetchone()
        if old:
            require(old[0] == request_hash, "IDEMPOTENCY_KEY_CONFLICT")
            if old[1]["command"]["kind"] == "REJECTED":
                raise Rejected(old[1]["command"]["reason"])
            return True
        return False

    def _event(self, command, event_id, sequence, at, server_at, epoch, actor, request_hash):
        return dict(schema_version=1, event_id=event_id, sequence=sequence, command=command,
            execution_scope=self.execution_scope,
            logical_at=stamp(at), server_at=stamp(server_at), epoch=epoch, source_id=actor,
            correlation_id=event_id, causation_id=None, request_hash=request_hash,
            permit_expires_at=stamp(server_at + timedelta(seconds=30)),
            # Existing schema-v1 journals predate the submission ceilings.
            # Version the rule in the immutable envelope so replay never
            # retroactively rejects authority that was valid when recorded.
            entry_submission_limit_version=3 if self.execution_scope=='REAL' else 1,
            # The original sandbox-economics authorization used a $100 model
            # default. New events bind the current constructor defaults while
            # old envelopes rebuild the values effective when they were made.
            economic_policy_default_version=1)

    def _persist(self, conn, account_id, previous_hash, event, state):
        event_hash = digest([previous_hash, event])
        conn.execute("""INSERT INTO journal(account_id,sequence,event_id,schema_version,kind,correlation_id,causation_id,
                     source_id,logical_at,recorded_at,request_hash,previous_hash,event_hash,payload)
                     VALUES(%s,%s,%s,1,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (account_id, event["sequence"], event["event_id"], event["command"]["kind"], event["correlation_id"], event["causation_id"],
             event["source_id"], event["logical_at"], event["server_at"], event["request_hash"], previous_hash, event_hash, Jsonb(event)))
        self.fault_hook("after_journal", conn)
        self._project(conn, state)
        self.fault_hook("after_projections", conn)
        conn.execute("UPDATE accounts SET version=%s,state=%s,state_hash=%s,head_hash=%s WHERE account_id=%s",
            (state["version"], Jsonb(state), digest(state), event_hash, account_id))
        self.fault_hook("before_commit", conn)

    @staticmethod
    def _project(conn, s):
        account = s["account_id"]
        for table in PROJECTIONS:
            conn.execute(sql.SQL("DELETE FROM {} WHERE account_id=%s").format(sql.Identifier(table)), (account,))
        cash_fields = ("settled_usd", "reserved_usd", "unsettled_usd", "realized_gross_usd", "fees_usd",
                       "realized_net_usd", "unrealized_usd", "liquidation_equity_usd")
        conn.execute("INSERT INTO cash_balances VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                     (account, *(Decimal(s["cash"][k]) for k in cash_fields)))
        for lid, lot in s["lots"].items():
            conn.execute("INSERT INTO position_lots VALUES(%s,%s,%s,%s,%s,%s,%s)",
                (account, lid, lot["instrument_id"], lot["episode_id"], Decimal(lot["quantity"]), Decimal(lot["entry_price_usd"]), Jsonb(lot)))
        for symbol, p in s["positions"].items():
            conn.execute("INSERT INTO positions VALUES(%s,%s,%s,%s)", (account, symbol, Decimal(p["quantity"]), Jsonb(p)))
        for xid, lot in s["settlements"].items():
            conn.execute("INSERT INTO settlement_lots VALUES(%s,%s,%s,%s,%s,%s)",
                (account, xid, Decimal(lot["amount_usd"]), Decimal(lot["confirmed_usd"]), lot["settlement_date"], Jsonb(lot)))
        for oid, r in s["reservations"].items():
            conn.execute("INSERT INTO reservations VALUES(%s,%s,%s,%s,%s,%s,%s)",
                (account, oid, *(Decimal(r[k]) for k in ("cash_usd", "shares", "exposure_usd", "risk_usd")), Jsonb(r)))
        for name, active in s["latches"].items():
            conn.execute("INSERT INTO safety_latches VALUES(%s,%s,%s)", (account, name, active))
        for segment in s["segments"]:
            conn.execute("INSERT INTO segments VALUES(%s,%s,%s,%s,%s)",
                (account, segment["segment_id"], Decimal(segment["reference_usd"]), Decimal(segment["high_water_usd"]), Jsonb(segment)))
        for action_id, record in s["restarts"].items():
            conn.execute("INSERT INTO owner_restarts VALUES(%s,%s,%s)", (account, action_id, Jsonb(record)))
        for nonce, p in s["permits"].items():
            conn.execute("INSERT INTO risk_permits VALUES(%s,%s,%s,%s,%s,%s)",
                (account, nonce, p["intent_id"], p["consumed"], p["revoked"], Jsonb(p)))
        for oid, out in s["outbox"].items():
            conn.execute("INSERT INTO outbox VALUES(%s,%s,%s,%s,%s)",
                (account, oid, out["client_order_id"], out["status"], Jsonb(out)))
        leader = s["leader"]
        conn.execute("INSERT INTO leadership VALUES(%s,%s,%s,%s,%s,%s)",
            (account, leader["holder"], leader["token"], leader["expires_at"], leader["epoch"], leader["last_clock_at"]))
        conn.execute("INSERT INTO reconciliation VALUES(%s,%s,%s)",
            (account, s["reconciliation"]["ready"], Jsonb(s["reconciliation"])))

    def create(self, command: CreateAccount, *, event_id: str, at: datetime):
        command = CreateAccount.model_validate(command.model_dump())
        require(command.capabilities.environment==('REAL' if self.execution_scope=='REAL' else 'OFFLINE'),
                'CAPABILITY_ENVIRONMENT_MISMATCH')
        if self.execution_scope=='REAL':require(command.account_id.startswith('real-'),'REAL_ACCOUNT_BINDING_REQUIRED')
        body = command.model_dump(mode="json")
        request_hash = digest(body)
        with self.config.connect() as conn:
            # Unique account insertion serializes competing initializations.
            conn.execute("INSERT INTO accounts(account_id) VALUES(%s) ON CONFLICT DO NOTHING", (command.account_id,))
            conn.execute("SELECT account_id FROM accounts WHERE account_id=%s FOR UPDATE", (command.account_id,))
            if self._duplicate(conn, command.account_id, event_id, request_hash):
                return self._load(conn, command.account_id)[0]
            require(conn.execute("SELECT version FROM accounts WHERE account_id=%s", (command.account_id,)).fetchone()[0] == 0, "ACCOUNT_ALREADY_EXISTS")
            server_at, epoch = self._clock(conn)
            event = self._event(body, event_id, 1, at, server_at, epoch, "offline-bootstrap", request_hash)
            state = reduce_event(None, event)
            self._persist(conn, command.account_id, "", event, state)
        self.fault_hook("after_commit", None)
        return state

    def snapshot(self, account_id):
        with self.config.connect() as conn:
            return self._load(conn, account_id)[0]

    def ready_in_current_database(self, account_id):
        with self.config.connect() as conn:
            state, _ = self._load(conn, account_id)
            now, epoch = self._clock(conn)
            leader = state["leader"]
            return (state["reconciliation"]["ready"] and state["reconciliation"]["epoch"] == epoch
                and leader["epoch"] == epoch and leader["expires_at"] is not None and utc(leader["expires_at"]) > now)

    def status(self, account_id, *, at):
        s = self.snapshot(account_id)
        flags = s["latches"]
        session = Session.model_validate(s["session"])
        if flags["terminated"]:
            mode = Mode.TERMINATED
        elif flags["drawdown"]:
            mode = Mode.DRAWDOWN_HALTED
        elif not session.contains(utc(at)):
            mode = Mode.CLOSED
        elif flags["daily"] or flags["incident"] or s["risk_breaches"] or not session.permits_entry(utc(at)):
            mode = Mode.REDUCE_ONLY
        elif not self.ready_in_current_database(account_id) or utc(at) > utc(s["reconciliation"]["valid_until"]):
            mode = Mode.RECONCILING
        elif any(Decimal(p["protected_quantity"]) < Decimal(p["quantity"]) for p in s["positions"].values()):
            mode = Mode.ENTRY_BLOCKED
        else:
            mode = Mode.READY
        return {"mode": mode.value, "version": s["version"], "latches": flags, "risk_breaches": s["risk_breaches"]}

    def acquire_leader(self, account_id, holder, *, event_id, at, ttl_seconds=30):
        require(0 < ttl_seconds <= 60, "INVALID_LEASE_TTL")
        request_hash = digest(dict(holder=holder, ttl_seconds=ttl_seconds, kind="LEASE"))
        with self.config.connect() as conn:
            state, head = self._load(conn, account_id)
            if self._duplicate(conn, account_id, event_id, request_hash):
                return state
            now, epoch = self._clock(conn)
            previous = state["leader"]
            require(now >= utc(previous["last_clock_at"]), "DATABASE_CLOCK_REGRESSION")
            active = previous["epoch"] == epoch and previous["expires_at"] and utc(previous["expires_at"]) > now
            require(not active or previous["holder"] == holder, "LEASE_HELD_BY_OTHER_WRITER")
            new_term = not active
            token = previous["token"] + int(new_term)
            leader = dict(holder=holder, token=token, expires_at=stamp(now + timedelta(seconds=ttl_seconds)),
                          epoch=epoch, last_clock_at=stamp(now))
            event = self._event(dict(kind="LEASE", leader=leader, new_term=new_term,
                recovery=previous["epoch"] is not None and previous["epoch"] != epoch), event_id, state["version"] + 1,
                                at, now, epoch, holder, request_hash)
            state = reduce_event(state, event)
            self._persist(conn, account_id, head, event, state)
        self.fault_hook("after_commit", None)
        return state

    def execute(self, account_id, command, *, event_id, expected_version, holder, token, at):
        command = COMMAND.validate_python(command.model_dump())
        body = command.model_dump(mode="json")
        request_hash = digest(body)
        rejected = None
        with self.config.connect() as conn:
            state, head = self._load(conn, account_id)
            if self._duplicate(conn, account_id, event_id, request_hash):
                return state  # Existing durable effect; this never reissues authority.
            require(expected_version == state["version"], "STALE_ACCOUNT_VERSION")
            now, epoch = self._clock(conn)
            leader = state["leader"]
            require(now >= utc(leader["last_clock_at"]), "DATABASE_CLOCK_REGRESSION")
            require(leader["holder"] == holder and leader["token"] == token and leader["epoch"] == epoch
                and leader["expires_at"] and now < utc(leader["expires_at"]), "FENCED_OR_EXPIRED_WRITER")
            event = self._event(body, event_id, state["version"] + 1, at, now, epoch, holder, request_hash)
            if command.kind in ('AUTHORIZE_SANDBOX_ECONOMICS','REVISE_SANDBOX_ECONOMICS','RELEASE_SANDBOX_PREFERENCES',
                    'AUTHORIZE_DAILY_DOWNSIDE_BUDGET','AUTHORIZE_MODEL_BUDGET_CAP'):
                require(self.execution_scope=='SANDBOX','SANDBOX_POLICY_SCOPE_REQUIRED')
                event['policy_scope']='SANDBOX'
            if isinstance(command, Restart):
                principal = self.owner_authenticator.verify(command.action)
                require(principal == state["owner_id"], "AUTHENTICATED_OWNER_REQUIRED")
                event["authenticated_owner"] = principal
            try:
                updated = reduce_event(state, event)
            except (Rejected, KeyError) as exc:
                rejected = Rejected(str(exc))
                if utc(at) < utc(state["last_logical_at"]):
                    raise rejected
                event["command"] = dict(kind="QUARANTINE" if command.kind == "EXECUTION" else "REJECTED",
                                        reason=str(exc), original_command=body)
                updated = reduce_event(state, event)
            self._persist(conn, account_id, head, event, updated)
        self.fault_hook("after_commit", None)
        if rejected:
            raise rejected
        return updated

    def journal(self, account_id):
        with self.config.connect() as conn:
            return [row[0] for row in conn.execute("SELECT payload FROM journal WHERE account_id=%s ORDER BY sequence", (account_id,))]

    def rebuild(self, account_id, *, repair=False):
        """Verify chain and rerun the versioned reducer; optionally replace projections.

        Rebuild does not clear latches, consumed nonces, fencing tokens or history.
        It requires a trusted recovery identity with projection write permissions.
        """
        with self.config.connect() as conn:
            row = conn.execute("SELECT state,head_hash FROM accounts WHERE account_id=%s FOR UPDATE", (account_id,)).fetchone()
            require(row is not None, "UNKNOWN_ACCOUNT")
            state, previous_hash, sequence = None, "", 0
            for seq, event_id, prev, hashed, event in conn.execute(
                "SELECT sequence,event_id,previous_hash,event_hash,payload FROM journal WHERE account_id=%s ORDER BY sequence", (account_id,)):
                sequence += 1
                require(seq == sequence == event["sequence"] and event_id == event["event_id"] and event["schema_version"] == 1, "JOURNAL_SEQUENCE_OR_VERSION")
                require(prev == previous_hash and digest([previous_hash, event]) == hashed, "JOURNAL_HASH_FAILURE")
                state = reduce_event(state, event)
                previous_hash = hashed
            require(state is not None and previous_hash == row[1], "JOURNAL_HEAD_MISMATCH")
            if repair:
                self._project(conn, state)
                conn.execute("UPDATE accounts SET state=%s,state_hash=%s,version=%s WHERE account_id=%s",
                             (Jsonb(state), digest(state), state["version"], account_id))
            else:
                require(digest(state) == digest(row[0]), "REBUILD_DIFFERS_FROM_CHECKPOINT")
            return state
