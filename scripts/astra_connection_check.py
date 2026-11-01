"""One-time live check of the second brain (Astra, OpenAI). No broker access, no orders.

GET /models only (free): the key works and the second-brain model is available to it. The first real
consensus call in trading then proves the decision path (event REAL_DUAL_BRAIN_DECISION).
Uses the key file named in .local/mvp/owner-config.json ("openai_key_file") or OPENAI_API_KEY(_FILE).
Prints fixed result codes only (never the key) and writes .local/astra-check/latest.json.
"""
from datetime import datetime, timezone
from pathlib import Path
import json
import os
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))

from autonomous_trader.decision_api.second_brain import SECOND_MODEL  # noqa: E402
from autonomous_trader.runtime_storage import data_root  # noqa: E402


def main():
    out = {'at': datetime.now(timezone.utc).isoformat(), 'model': SECOND_MODEL, 'checks': []}
    config = data_root(ROOT) / '.local/mvp/owner-config.json'
    if not os.environ.get('OPENAI_API_KEY') and not os.environ.get('OPENAI_API_KEY_FILE') and config.is_file():
        path = json.loads(config.read_text(encoding='utf-8-sig')).get('openai_key_file')
        if path:
            os.environ['OPENAI_API_KEY_FILE'] = path
    try:
        from autonomous_trader.decision_api.astra_probe import AstraClient, load_openai_key
        client = AstraClient(load_openai_key(), os.environ.get('OPENAI_PROJECT_ID'), model=SECOND_MODEL)
        client.verify_model()
        out['checks'].append({'check': 'MODEL_LISTED', 'ok': True})
    except Exception as exc:
        out['checks'].append({'check': 'KEY_OR_MODEL', 'ok': False, 'code': str(exc) if str(exc).isupper() else type(exc).__name__})
    folder = data_root(ROOT) / '.local/astra-check'
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'latest.json').write_text(json.dumps(out, indent=2), encoding='utf-8')
    ok = bool(out['checks']) and all(c['ok'] for c in out['checks'])
    print('ASTRA_AVAILABLE' if ok else 'ASTRA_UNAVAILABLE ' + json.dumps(out['checks']))
    return 0 if ok else 2


if __name__ == '__main__':
    raise SystemExit(main())
