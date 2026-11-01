"""Real PostgreSQL backup/restore and immediate-restart WAL recovery tests.

These operate ONLY on this repository's disposable .local/pgdata-m2 cluster.
They never target an existing personal database installation or broker account.
"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from uuid import uuid4
from decimal import Decimal as D
import json
import os
import subprocess
import tempfile
import pytest
import psycopg
from psycopg import sql

from autonomous_trader.durable.ledger import Ledger, LocalDatabase
from autonomous_trader.durable.models import ConsumePermit, Incident
from autonomous_trader.durable.projection import Rejected, digest

ROOT = Path(__file__).resolve().parents[2]
PG_BIN = ROOT / ".local/postgresql/pgsql/bin"
PG_DATA = ROOT / ".local/pgdata-m2"


def checked_cluster(admin):
    with admin.connect() as conn:
        actual = Path(conn.execute("SHOW data_directory").fetchone()[0]).resolve()
        assert actual == PG_DATA.resolve()
        assert PG_DATA.resolve().is_relative_to((ROOT / ".local").resolve())
        assert conn.execute("SHOW port").fetchone()[0] == "55433"
        assert conn.execute("SHOW listen_addresses").fetchone()[0] == "127.0.0.1"
        assert conn.execute("SHOW fsync").fetchone()[0] == "on"
        assert conn.execute("SHOW synchronous_commit").fetchone()[0] == "on"


def pg(tool, *args):
    suffix = ".exe" if os.name == "nt" else ""
    # A Windows server child can retain pipe handles after pg_ctl exits. Use a
    # file so the harness waits for pg_ctl, not EOF from the background server.
    with tempfile.TemporaryFile(mode="w+t") as output:
        result = subprocess.run([str(PG_BIN / (tool + suffix)), *map(str, args)], stdout=output,
            stderr=subprocess.STDOUT, text=True, timeout=40,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        output.seek(0)
        text = output.read()
    assert result.returncode == 0, text
    return text


def test_backup_restore_into_new_database_preserves_and_fences(db_config, h, order_factory):
    admin, writer = db_config
    checked_cluster(admin)
    h.trade(order_factory(h))
    pending = order_factory(h, symbol="stock-1")
    nonce = h.admit(pending)
    original = h.state
    backup_dir = ROOT / ".local/backups"
    backup_dir.mkdir(exist_ok=True)
    archive = backup_dir / (admin.schema + ".dump")
    pg("pg_dump", "-h", "127.0.0.1", "-p", "55433", "-U", "m2_admin", "-d", "postgres",
       "-n", admin.schema, "-Fc", "-f", archive)
    archive.with_suffix(".manifest.json").write_text(json.dumps(dict(state_hash=digest(original),
        account_id=h.account_id, schema=admin.schema, fencing_token=h.token,
        consumed_nonces=[n for n, p in original["permits"].items() if p["consumed"]]), indent=2))
    restored_database = "m2_restore_" + uuid4().hex[:16]
    with admin.connect() as conn:
        conn.autocommit = True
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(restored_database)))
    pg("pg_restore", "--exit-on-error", "--no-owner", "-h", "127.0.0.1", "-p", "55433", "-U", "m2_admin",
       "-d", restored_database, archive)
    restored = Ledger(LocalDatabase(database=restored_database, user="m2_writer", schema=admin.schema))
    assert restored.snapshot(h.account_id) == original
    assert restored.rebuild(h.account_id) == original
    assert not restored.ready_in_current_database(h.account_id)
    recovered = restored.acquire_leader(h.account_id, "restored-worker", event_id=uuid4().hex, at=h.at)
    assert recovered["leader"]["token"] > original["leader"]["token"]
    assert recovered["permits"][nonce]["revoked"]
    assert recovered["outbox"][pending.intent_id]["status"] == "RECOVERY_UNCERTAIN"
    assert recovered["reservations"] == original["reservations"]
    assert restored.rebuild(h.account_id) == recovered


def test_immediate_postgres_restart_recovers_commits_and_rolls_back_inflight(db_config, h, order_factory):
    admin, writer = db_config
    checked_cluster(admin)
    h.trade(order_factory(h))
    pending = order_factory(h, symbol="stock-1")
    nonce = h.admit(pending)
    before = h.state
    reached = Event()
    release = Event()
    def hold(point, connection):
        if point == "after_journal":
            reached.set()
            assert release.wait(40)
    crashing = Ledger(writer, fault_hook=hold)
    def uncommitted():
        crashing.execute(h.account_id, Incident(reason="UNCOMMITTED_TEST", evidence_id="test"),
            event_id="uncommitted-restart-event", expected_version=before["version"], holder=h.holder, token=h.token, at=h.at)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(uncommitted)
        assert reached.wait(10)
        try:
            # Immediate mode crashes server processes: restart must perform WAL recovery.
            pg("pg_ctl", "-D", PG_DATA, "-m", "immediate", "-w", "stop")
            pg("pg_ctl", "-D", PG_DATA, "-l", ROOT / ".local/pg-m2.log",
               "-o", "-h 127.0.0.1 -p 55433 -c fsync=on -c synchronous_commit=on", "-w", "start")
        finally:
            release.set()
        with pytest.raises(psycopg.Error):
            future.result(timeout=10)
    assert h.ledger.snapshot(h.account_id) == before
    assert all(e["event_id"] != "uncommitted-restart-event" for e in h.ledger.journal(h.account_id))
    assert not h.ledger.ready_in_current_database(h.account_id)
    with pytest.raises(Rejected, match="FENCED"):
        h.send(ConsumePermit(nonce=nonce))
    recovered = h.ledger.acquire_leader(h.account_id, "recovered-worker", event_id=uuid4().hex, at=h.at)
    assert recovered["leader"]["token"] > h.token
    assert recovered["permits"][nonce]["revoked"]
    assert recovered["reservations"] == before["reservations"]
    assert h.ledger.rebuild(h.account_id) == recovered
    evidence = dict(server_version="PostgreSQL 17.11", fsync="on", synchronous_commit="on", restart_mode="immediate",
        committed_state_preserved=True, inflight_transaction_rolled_back=True, stale_leader_fenced=True,
        old_unused_permits_revoked=True, reservations_preserved=True, journal_rebuild_matches=True)
    (ROOT / "docs/m2-recovery-results.json").write_text(json.dumps(evidence, indent=2) + "\n")
