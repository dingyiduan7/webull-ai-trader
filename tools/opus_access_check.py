"""Read-only: does this Anthropic key list claude-opus-5-5? No model call, no cost, no broker access."""
from datetime import datetime, timezone
from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))

from autonomous_trader.decision_api.claude_client import ClaudeClient, load_anthropic_key  # noqa: E402

TARGET = 'claude-opus-5-5'


def main():
    report = {'at': datetime.now(timezone.utc).isoformat(), 'target': TARGET, 'result': 'FAILED'}
    try:
        client = ClaudeClient(load_anthropic_key(ROOT), model='claude-haiku-5-5', reasoning='low')
        listed = [x['id'] for x in client._transport('GET', '/models')['data']]
        report['opus_listed'] = TARGET in listed
        report['claude_models_listed'] = sorted(x for x in listed if x.startswith('claude-'))[:20]
        report['result'] = 'OPUS_AVAILABLE' if TARGET in listed else 'OPUS_NOT_AVAILABLE'
    except Exception as exc:
        report['error_class'] = type(exc).__name__
        report['error_code'] = str(exc)[:80]
    out = ROOT / '.local' / 'claude-check'
    out.mkdir(parents=True, exist_ok=True)
    (out / 'opus-access.json').write_text(json.dumps(report, indent=1), encoding='utf-8')
    print(report['result'])
    return 0 if report['result'] == 'OPUS_AVAILABLE' else 1


if __name__ == '__main__':
    raise SystemExit(main())
