"""One-time live check of the Claude decision transport. No broker access, no orders.

Makes 3 small paid calls (well under $0.05 total) with the decision model:
  1. model list: the decision and shadow models exist for this key;
  2. native structured output with the real deep-plan schema;
  3. prompt-embedded schema mode with the real entry-decision schema.
Prints fixed result codes only (never the key) and writes
.local/claude-check/latest.json for the engineer to read.
"""
from datetime import datetime, timezone
from pathlib import Path
import json
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))

import jsonschema  # noqa: E402

from autonomous_trader.contracts import DeepAlphaPlan  # noqa: E402
from autonomous_trader.decision_api.astra_probe import entry_alpha_schema  # noqa: E402
from autonomous_trader.decision_api.brain import DECISION_MODEL, SHADOW_MODEL  # noqa: E402
from autonomous_trader.decision_api.claude_client import (ClaudeClient, load_anthropic_key,  # noqa: E402
    messages_request, normalize_response)
from autonomous_trader.decision_api.pricing import estimate  # noqa: E402
from autonomous_trader.runtime_storage import data_root  # noqa: E402
from autonomous_trader.sandbox.secrets import PreflightError  # noqa: E402


def call(client, name, schema, instruction, max_tokens=6000):
    body = {'model': client.model, 'reasoning': {'effort': 'low'}, 'max_output_tokens': max_tokens,
            'input': [{'role': 'developer', 'content': instruction},
                      {'role': 'user', 'content': json.dumps({'purpose': 'transport connection check'})}],
            'text': {'format': {'type': 'json_schema', 'name': name, 'strict': True, 'schema': schema}}}
    _, mode = messages_request(body, client.model)
    start = time.monotonic()
    response = client._transport('POST', '/responses', body)
    row = {'check': name, 'structured_mode': mode, 'latency_ms': round((time.monotonic() - start) * 1000),
           'status': response['status'], 'model': response['model'], 'usage': response['usage'],
           'estimated_cost_usd': estimate(client.model, response['usage'])}
    text = response['output'][0]['content'][0]['text'] if response['output'] else None
    return row, (json.loads(text) if text else None)


def main():
    report = {'at': datetime.now(timezone.utc).isoformat(), 'decision_model': DECISION_MODEL,
              'shadow_model': SHADOW_MODEL, 'checks': [], 'result': 'FAILED'}
    try:
        key = load_anthropic_key(ROOT)
        client = ClaudeClient(key, model=DECISION_MODEL, reasoning='low')
        listed = [x['id'] for x in client._transport('GET', '/models')['data']]
        report['checks'].append({'check': 'models', 'decision_model_listed': DECISION_MODEL in listed,
                                 'shadow_model_listed': SHADOW_MODEL in listed})
        if DECISION_MODEL not in listed or SHADOW_MODEL not in listed:
            raise PreflightError('CLAUDE_MODEL_UNAVAILABLE_NO_SUBSTITUTE')
        row, value = call(client, 'DeepAlphaPlan', DeepAlphaPlan.model_json_schema(),
                          'Connection check. Return regime_label "CHECK", regime_view "connection check", '
                          'and an empty focus list.')
        DeepAlphaPlan.model_validate(value)
        row['valid'] = True; report['checks'].append(row)
        row, value = call(client, 'AlphaDecision', entry_alpha_schema(),
                          'Connection check. No market evidence is supplied. Return decision.action ABSTAIN '
                          'with a short reason; every other action payload and defer_to must be null; '
                          'schema_version "1.0".')
        jsonschema.validate(value, entry_alpha_schema())
        row['valid'] = True; report['checks'].append(row)
        report['total_estimated_cost_usd'] = str(sum(
            __import__('decimal').Decimal(r['estimated_cost_usd']) for r in report['checks'] if r.get('estimated_cost_usd')))
        report['result'] = 'OK'
    except PreflightError as exc:
        report['error'] = str(exc)
        report['provider_error'] = getattr(locals().get('client'), 'last_error', None)
    except jsonschema.ValidationError as exc:
        report['error'] = 'SCHEMA_INVALID:' + exc.validator
    except Exception as exc:
        report['error'] = type(exc).__name__
    out = data_root(ROOT) / '.local' / 'claude-check' / 'latest.json'
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print('CLAUDE CHECK:', report['result'], report.get('error', ''), '| details: .local\\claude-check\\latest.json')
    return 0 if report['result'] == 'OK' else 1


if __name__ == '__main__':
    raise SystemExit(main())
