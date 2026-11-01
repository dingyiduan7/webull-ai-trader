"""Dashboard must never go blank: snapshot failures become a readable degraded page + a log line, and the
standalone launcher restarts itself."""
import json, importlib, sys
from pathlib import Path
from types import SimpleNamespace


def test_failing_reader_gives_degraded_json_and_error_log(tmp_path):
    from autonomous_trader.monitoring.server import DashboardServer
    calls = []
    def boom():
        calls.append(1); raise RuntimeError('db down')
    server = DashboardServer(SimpleNamespace(root=tmp_path, snapshot=boom), port=0)   # binds only; no connects
    try:
        data = json.loads(server.snapshot_bytes())
        assert data['system']['status'] == 'ERROR' and 'RuntimeError' in data['system']['last_error']
        assert 'RuntimeError' in (tmp_path / '.local' / 'dashboard' / 'dashboard-errors.log').read_text()
        server.snapshot_bytes(); assert len(calls) == 1      # 1.5 s cache: a refresh storm cannot hammer the database
    finally:
        server.server_close()


def test_standalone_launcher_restarts_after_failure_and_logs(tmp_path, monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    launcher = importlib.import_module('scripts.run_readonly_dashboard')
    monkeypatch.setattr(launcher, 'ROOT', tmp_path)
    attempts = []
    class Boom:
        def __init__(self, *a, **k): attempts.append(1)
        def serve_forever(self): raise OSError('port busy')
    monkeypatch.setattr(launcher, 'DashboardServer', Boom)
    monkeypatch.setattr(launcher, 'DashboardReader', lambda root: None)
    assert launcher.main([], sleep=lambda s: None, attempts=3) == 1 and len(attempts) == 3
    assert 'port busy' in (tmp_path / '.local' / 'dashboard' / 'dashboard-errors.log').read_text()
