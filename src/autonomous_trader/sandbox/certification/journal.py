"""Local certification write-ahead journal, separate from the M2 account ledger.

No broker credentials or raw account identifiers are stored. SQL transactions
claim each request once before transmission; response loss never rearms it.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from uuid import uuid4
from ..secrets import PreflightError


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class CertificationJournal:
    def __init__(self, path: Path, account_id: str):
        if not account_id:
            raise PreflightError("DESIGNATED_ACCOUNT_REQUIRED")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.account_hash = sha256(account_id.encode()).hexdigest()
        self.db = sqlite3.connect(path, timeout=0, isolation_level=None)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS binding (account_hash TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS intents (
                intent_id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL,
                payload_hash TEXT NOT NULL, claimed INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY, at TEXT NOT NULL, kind TEXT NOT NULL,
                intent_id TEXT, payload TEXT NOT NULL);
            CREATE TRIGGER IF NOT EXISTS no_event_update BEFORE UPDATE ON events
                BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
            CREATE TRIGGER IF NOT EXISTS no_event_delete BEFORE DELETE ON events
                BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
            CREATE TRIGGER IF NOT EXISTS no_intent_rearm BEFORE UPDATE ON intents
                WHEN NEW.claimed != 1 OR OLD.claimed != 0
                OR NEW.payload != OLD.payload OR NEW.payload_hash != OLD.payload_hash
                OR NEW.kind != OLD.kind OR NEW.intent_id != OLD.intent_id
                BEGIN SELECT RAISE(ABORT, 'NO_REARM'); END;
            CREATE TRIGGER IF NOT EXISTS no_intent_delete BEFORE DELETE ON intents
                BEGIN SELECT RAISE(ABORT, 'NO_DELETE'); END;
        """)
        with self.transaction():
            rows = self.db.execute("SELECT account_hash FROM binding").fetchall()
            if rows and rows != [(self.account_hash,)]:
                raise PreflightError("JOURNAL_ACCOUNT_MISMATCH")
            if not rows:
                self.db.execute("INSERT INTO binding VALUES (?)", (self.account_hash,))

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def _event(self, kind, intent_id, payload):
        self.db.execute("INSERT INTO events(at,kind,intent_id,payload) VALUES (?,?,?,?)",
            (datetime.now(timezone.utc).isoformat(), kind, intent_id, canonical(payload)))

    def record(self, kind, payload, intent_id=None):
        with self.transaction():
            self._event(kind, intent_id, payload)

    def prepare(self, kind, payload, intent_id=None):
        if kind not in ("PREVIEW", "PLACE", "REPLACE", "CANCEL"):
            raise PreflightError("UNKNOWN_CERTIFICATION_ACTION")
        intent_id = intent_id or uuid4().hex
        text = canonical(payload)
        digest = sha256(text.encode()).hexdigest()
        with self.transaction():
            if kind == "PLACE" and self.db.execute("SELECT 1 FROM intents WHERE kind='PLACE'").fetchone():
                raise PreflightError("FIRST_SEQUENCE_ALREADY_RECORDED_RECONCILE_ONLY")
            if self.db.execute("SELECT 1 FROM intents WHERE intent_id=?", (intent_id,)).fetchone():
                raise PreflightError("INTENT_ALREADY_EXISTS_NO_NEW_ID_RETRY")
            self.db.execute("INSERT INTO intents VALUES (?,?,?,?,0)", (intent_id, kind, text, digest))
            self._event("PREPARED", intent_id, {"kind": kind, "payload": payload, "payload_hash": digest})
        return intent_id

    def claim(self, intent_id, kind, payload):
        with self.transaction():
            row = self.db.execute("SELECT kind,payload,claimed FROM intents WHERE intent_id=?", (intent_id,)).fetchone()
            if row != (kind, canonical(payload), 0):
                raise PreflightError("INTENT_MISMATCH_OR_ALREADY_CLAIMED")
            self.db.execute("UPDATE intents SET claimed=1 WHERE intent_id=?", (intent_id,))
            self._event("TRANSMISSION_UNCERTAIN", intent_id, {"kind": kind})

    def close(self):
        self.db.close()
