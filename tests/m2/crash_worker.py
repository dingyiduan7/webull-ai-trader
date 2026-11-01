"""Disposable child process for actual client-death tests, never a service."""
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from autonomous_trader.durable.ledger import Ledger, LocalDatabase
from autonomous_trader.durable.models import COMMAND
from autonomous_trader.contracts import utc


def main():
    request = json.loads(Path(sys.argv[1]).read_text())
    def crash(boundary, connection):
        if boundary == request["boundary"]:
            os._exit(71)
    ledger = Ledger(LocalDatabase(**request["database"]), fault_hook=crash)
    ledger.execute(request["account_id"], COMMAND.validate_python(request["command"]),
        event_id=request["event_id"], expected_version=request["expected_version"],
        holder=request["holder"], token=request["token"], at=utc(request["at"]))


if __name__ == "__main__":
    main()
