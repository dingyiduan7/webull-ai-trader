from uuid import uuid4
from decimal import Decimal as D
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
import json
import os
import subprocess
import sys
import pytest
from autonomous_trader.durable.ledger import Ledger
from autonomous_trader.durable.models import Admit, ConsumePermit
from autonomous_trader.reference import Session


@pytest.mark.parametrize("boundary", ["after_journal", "after_projections", "before_commit"])
def test_transaction_crash_rolls_back_all_obligations(db_config, h, order_factory, boundary):
    order = order_factory(h)
    before = h.state
    def crash(point, connection):
        if point == boundary:
            raise RuntimeError("simulated process failure at " + point)
    h.ledger = Ledger(db_config[1], fault_hook=crash)
    with pytest.raises(RuntimeError):
        h.admit(order)
    assert Ledger(db_config[1]).snapshot(h.account_id) == before
    with db_config[1].connect() as conn:
        for table in ("reservations", "outbox", "risk_permits"):
            assert conn.execute(f"SELECT count(*) FROM {table} WHERE account_id=%s", (h.account_id,)).fetchone()[0] == 0


def test_commit_succeeded_response_lost_and_idempotent_recovery(db_config, h, order_factory):
    order = order_factory(h)
    command = Admit(intent=order, fee_allowance_usd="1", exit_cost_usd="1", stress_allowance_usd="0",
        latest_exit_at=Session.model_validate(h.state["session"]).liquidation_at, max_holding_seconds=600)
    event_id = uuid4().hex
    def lost_response(point, connection):
        if point == "after_commit":
            raise ConnectionError("response lost after successful commit")
    h.ledger = Ledger(db_config[1], fault_hook=lost_response)
    with pytest.raises(ConnectionError):
        h.send(command, event_id=event_id)
    h.ledger = Ledger(db_config[1])
    before = h.state
    h.send(command, event_id=event_id, expected_version=0)
    assert h.state == before
    assert len(h.state["outbox"]) == 1
    assert D(h.state["cash"]["reserved_usd"]) == 10001


def test_real_backend_termination_rolls_back(db_config, h, order_factory):
    admin = db_config[0]
    before = h.state
    def terminate(point, connection):
        if point == "after_journal":
            pid = connection.info.backend_pid
            with admin.connect() as control:
                assert control.execute("SELECT pg_terminate_backend(%s)", (pid,)).fetchone()[0]
    h.ledger = Ledger(db_config[1], fault_hook=terminate)
    with pytest.raises(Exception):
        h.admit(order_factory(h))
    assert Ledger(db_config[1]).snapshot(h.account_id) == before


def test_racing_permit_consumers_one_durable_claim(h, order_factory):
    order = order_factory(h)
    nonce = h.admit(order)
    version = h.state["version"]
    def consume(_):
        try:
            h.send(ConsumePermit(nonce=nonce), expected_version=version)
            return True
        except ValueError:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(consume, range(2))) == 1
    assert h.state["permits"][nonce]["consumed"]
    assert h.state["outbox"][order.intent_id]["status"] == "CLAIMED_UNCERTAIN"


@pytest.mark.parametrize("boundary", ["after_journal", "after_commit"])
def test_actual_client_process_death_at_permit_claim(db_config, h, order_factory, tmp_path, boundary):
    order = order_factory(h)
    nonce = h.admit(order)
    before = h.state
    request = dict(database=asdict(db_config[1]), boundary=boundary, account_id=h.account_id,
        command=ConsumePermit(nonce=nonce).model_dump(mode="json"), event_id=uuid4().hex,
        expected_version=before["version"], holder=h.holder, token=h.token, at=h.at.isoformat())
    path = tmp_path / "offline-crash-request.json"
    path.write_text(json.dumps(request))
    result = subprocess.run([sys.executable, str(Path(__file__).with_name("crash_worker.py")), str(path)],
        capture_output=True, text=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    assert result.returncode == 71, result.stdout + result.stderr
    after = Ledger(db_config[1]).snapshot(h.account_id)
    assert after["permits"][nonce]["consumed"] is (boundary == "after_commit")
    assert after["reservations"] == before["reservations"]
    if boundary == "after_journal":
        assert after == before
    assert h.ledger.rebuild(h.account_id) == after
