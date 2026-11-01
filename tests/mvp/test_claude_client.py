"""Claude transport: offline, real request builders and validators, fake HTTP session."""
from datetime import timedelta
from decimal import Decimal as D
import json

import pytest

from test_cycle import NOW, setup_cycle  # noqa: F401  (fixture)
from test_alpha_reset import observation, universe, open_raw
from autonomous_trader.contracts import DeepAlphaPlan
from autonomous_trader.decision_api import brain as brains
from autonomous_trader.decision_api.astra_probe import AstraClient, entry_alpha_schema, fast_alpha_request
from autonomous_trader.decision_api.claude_client import (ClaudeClient, IDENTITY, THINKING_HEADROOM,
    claude_schema, load_anthropic_key, messages_request, normalize_response)
from autonomous_trader.decision_api.pricing import estimate
from autonomous_trader.decision_api.claude_prompts import (DEEP_SYSTEM, ENTRY_SYSTEM, POSITION_SYSTEM,
    PROMPT_VERSION, SHADOW_SYSTEM)
from autonomous_trader.mvp.alpha import build_alpha_context
from autonomous_trader.sandbox.secrets import PreflightError

KEY = 'sk-ant-synthetic-test-key-0123456789abcdef'


class Budget:
    def __init__(self):
        self.claims, self.finished = [], []

    def claim(self, body, *, category=None):
        self.claims.append(body)
        return 'work-%d' % len(self.claims)

    def finish(self, claim, last_call):
        self.finished.append((claim, dict(last_call)))


class Response:
    def __init__(self, status, payload):
        self.status_code, self._raw = status, json.dumps(payload).encode()

    def iter_content(self, size):
        yield self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def session_factory(replies, seen):
    class Session:
        trust_env = True

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def request(self, method, url, **kwargs):
            seen.append(dict(method=method, url=url, **kwargs))
            status, payload = replies.pop(0)
            return Response(status, payload)
    return Session


def message(value, *, stop='end_turn', model='claude-opus-5-5', text=None):
    return {'type': 'message', 'model': model, 'stop_reason': stop,
            'content': [{'type': 'thinking', 'thinking': 'private'},
                        {'type': 'text', 'text': text if text is not None else json.dumps(value)}],
            'usage': {'input_tokens': 1000, 'cache_read_input_tokens': 2000,
                      'cache_creation_input_tokens': 500, 'output_tokens': 300}}


def client(replies, seen, **kw):
    value = ClaudeClient(KEY, session_factory=session_factory(replies, seen), **kw)
    value.budget = Budget()
    return value


def context_for(setup_cycle, n=2):
    args, _, _ = setup_cycle
    u = universe(tuple(observation('stock-' + c, c.upper() * 3) for c in 'bcdefghij'[:n]))
    args['controller'].refresh(u)
    return build_alpha_context(u, args['controller'].state, NOW)


def test_schema_subset_strips_unsupported_keywords_without_mutating_original():
    original = DeepAlphaPlan.model_json_schema()
    before = json.dumps(original, sort_keys=True)
    schema, native = claude_schema(original)
    text = json.dumps(schema)
    assert native and json.dumps(original, sort_keys=True) == before
    for keyword in ('maxLength', 'minLength', 'maximum', 'minimum', 'maxItems'):
        assert '"%s"' % keyword not in text


def test_entry_schema_exceeds_native_union_limit_and_is_embedded_in_prompt(setup_cycle):
    _, native = claude_schema(entry_alpha_schema())
    assert native is False
    body = fast_alpha_request(context_for(setup_cycle), mode='ENTRY', hard_policy={}, model='claude-sonnet-5-5')
    request, mode = messages_request(body, 'claude-sonnet-5-5')
    assert mode == 'PROMPT' and 'format' not in request['output_config']
    assert 'JSON Schema named AlphaDecision' in request['system'][0]['text']


def test_request_translation_carries_effort_thinking_cache_and_identity(setup_cycle):
    body = fast_alpha_request(context_for(setup_cycle), mode='POSITION', hard_policy={}, model='claude-sonnet-5-5',
                              reasoning='high')
    request, _ = messages_request(body, 'claude-sonnet-5-5')
    assert request['model'] == 'claude-sonnet-5-5' and request['thinking'] == {'type': 'adaptive'}
    assert request['output_config']['effort'] == 'high'
    assert request['system'][0]['cache_control'] == {'type': 'ephemeral', 'ttl': '1h'}
    # Claude-native prompts: no legacy identity preamble needed.
    assert request['system'][0]['text'] == POSITION_SYSTEM
    assert request['messages'] == [{'role': 'user', 'content': body['input'][1]['content']}]
    assert 'input' not in request and 'store' not in request and 'reasoning' not in request


def test_response_normalization_usage_status_and_fences():
    value = normalize_response(message({'a': 1}, model='claude-opus-5-5-20261001'), 'claude-opus-5-5')
    assert value['model'] == 'claude-opus-5-5' and value['status'] == 'completed'
    assert value['usage']['input_tokens'] == 3500
    assert value['usage']['input_tokens_details'] == {'cached_tokens': 2000, 'cache_write_tokens': 500}
    assert value['output'][0]['content'] == [{'type': 'output_text', 'text': '{"a": 1}'}]
    fenced = normalize_response(message(None, text='```json\n{"a": 2}\n```'), 'claude-opus-5-5')
    assert fenced['output'][0]['content'][0]['text'] == '{"a": 2}'
    cut = normalize_response(message({}, stop='max_tokens'), 'claude-opus-5-5')
    assert cut['status'] == 'incomplete' and cut['incomplete_details']['reason'] == 'max_output_tokens'
    refused = normalize_response(message({}, stop='refusal'), 'claude-opus-5-5')
    assert refused['status'] == 'incomplete' and refused['output'] == []
    assert normalize_response(message({}), 'claude-opus-5-5')['model'] == 'claude-opus-5-5'


def test_cost_uses_cache_aware_claude_prices():
    usage = normalize_response(message({}), 'claude-opus-5-5')['usage']
    # 1000 fresh*$4 + 2000 read*$0.20 + 500 1h-write*$8 + 300 out*$20, per million
    assert D(estimate('claude-opus-5-5', usage)) == D('0.0144')


def test_fast_entry_end_to_end_reserves_thinking_headroom_and_prices_the_call(setup_cycle):
    context = context_for(setup_cycle)
    seen = []
    brain = client([(200, message({'decision': {**open_raw(NOW), 'defer_to': None}}))], seen)
    result = brain.fast_decide(context, mode='ENTRY', hard_policy={}, clock=lambda: NOW)
    assert result['decision']['action'] == 'OPEN' and result['model'] == 'claude-opus-5-5'
    assert D(result['estimated_cost_usd']) == D('0.0144')
    call = seen[0]
    assert call['url'] == 'https://api.anthropic.com/v1/messages' and call['method'] == 'POST'
    assert call['headers']['x-api-key'] == KEY and call['headers']['anthropic-version'] == '2023-06-01'
    assert 'Authorization' not in call['headers'] and call['allow_redirects'] is False
    claimed = brain.budget.claims[0]['max_output_tokens']
    assert claimed == 1536 + THINKING_HEADROOM['medium'] == call['json']['max_tokens']
    work, last = brain.budget.finished[0]
    assert last['estimated_cost_usd'] == result['estimated_cost_usd'] and last['structured_mode'] == 'PROMPT'


def test_deep_plan_uses_native_structured_output(setup_cycle):
    context = context_for(setup_cycle)
    plan = {'regime_label': 'MIXED', 'regime_view': 'Mixed tape.', 'focus': [{'instrument_id': 'stock-b',
            'symbol': 'BBB', 'rank': 1, 'strategy_label': 'rs', 'thesis': 'Leads.', 'horizon': 'intraday'}]}
    seen = []
    brain = client([(200, message(plan))], seen)
    assert brain.deep_plan(context, {})['plan']['focus'][0]['symbol'] == 'BBB'
    fmt = seen[0]['json']['output_config']['format']
    assert fmt['type'] == 'json_schema' and 'maxLength' not in json.dumps(fmt)


def test_local_validation_still_enforces_constraints_dropped_from_native_schema(setup_cycle):
    context = context_for(setup_cycle)
    plan = {'regime_label': 'MIXED', 'regime_view': 'x' * 5000, 'focus': []}
    brain = client([(200, message(plan))], [])
    with pytest.raises(PreflightError, match='INVALID_DEEP_ALPHA_PLAN'):
        brain.deep_plan(context, {})


@pytest.mark.parametrize('status,code', [(429, 'ANTHROPIC_HTTP_429'), (529, 'ANTHROPIC_HTTP_529'),
                                         (400, 'ANTHROPIC_HTTP_400')])
def test_http_errors_fail_closed_with_fixed_codes_and_no_retry(setup_cycle, status, code):
    seen = []
    brain = client([(status, {'error': {'message': 'echo ' + KEY}})], seen)
    with pytest.raises(PreflightError, match=code):
        brain.fast_decide(context_for(setup_cycle), mode='ENTRY', hard_policy={}, clock=lambda: NOW)
    assert len(seen) == 1
    _, last = brain.budget.finished[0]
    assert last['error_code'] == code and KEY not in json.dumps(last)


def test_secret_echo_and_refusal_are_rejected(setup_cycle):
    brain = client([(200, message(None, text='{"leak":"%s"}' % KEY))], [])
    with pytest.raises(PreflightError, match='SECRET_IN_OUTPUT'):
        brain.deep_plan(context_for(setup_cycle), {})
    brain = client([(200, message({}, stop='refusal'))], [])
    with pytest.raises(PreflightError, match='MODEL_RESPONSE_NOT_COMPLETED'):
        brain.deep_plan(context_for(setup_cycle), {})


def test_post_without_budget_is_never_dispatched(setup_cycle):
    seen = []
    brain = ClaudeClient(KEY, session_factory=session_factory([], seen))
    with pytest.raises(PreflightError, match='MODEL_BUDGET_UNAVAILABLE'):
        brain.deep_plan(context_for(setup_cycle), {})
    assert seen == []


def test_verify_model_reads_models_list_and_never_substitutes():
    seen = []
    brain = client([(200, {'data': [{'id': 'claude-opus-5-5', 'type': 'model'}], 'has_more': False})], seen)
    assert brain.verify_model() == 'claude-opus-5-5'
    assert seen[0]['method'] == 'GET' and seen[0]['url'].startswith('https://api.anthropic.com/v1/models')
    brain = client([(200, {'data': [{'id': 'claude-haiku-5-5'}]})], [])
    with pytest.raises(PreflightError, match='CLAUDE_MODEL_UNAVAILABLE_NO_SUBSTITUTE'):
        brain.verify_model()


def test_unpriced_or_malformed_models_are_refused():
    for model in ('claude-unknown-1', 'gpt-6-astra', 'claude sonnet'):
        with pytest.raises(PreflightError, match='INVALID_MODEL_CONFIGURATION'):
            ClaudeClient(KEY, model=model)


def test_key_loading_from_env_and_file(tmp_path, monkeypatch):
    monkeypatch.delenv('ANTHROPIC_API_KEY', raising=False)
    monkeypatch.delenv('ANTHROPIC_API_KEY_FILE', raising=False)
    # Explicit root: the real encrypted key on the owner's PC must never leak into this test.
    with pytest.raises(PreflightError, match='ANTHROPIC_KEY_LOCATION_REQUIRED'):
        load_anthropic_key(tmp_path)
    path = tmp_path / 'key.txt'
    path.write_text('note\n' + KEY + '\n', encoding='utf-8')
    monkeypatch.setenv('ANTHROPIC_API_KEY_FILE', str(path))
    assert load_anthropic_key() == KEY
    path.write_text(KEY + '\n' + KEY.replace('0123', '9999'), encoding='utf-8')
    with pytest.raises(PreflightError, match='ANTHROPIC_KEY_FILE_AMBIGUOUS'):
        load_anthropic_key()


def test_brain_factory_defaults_to_claude():
    assert isinstance(brains.make_brain(KEY), ClaudeClient)
    assert brains.make_brain(KEY).model == brains.DECISION_MODEL == 'claude-opus-5-5'
    assert type(brains.make_brain('sk-legacy', model='gpt-6-astra')) is AstraClient
    assert brains.shadow_model('claude-sonnet-5-5') == 'claude-haiku-5-5'


def test_encrypted_key_file_is_the_default_location(tmp_path, monkeypatch):
    from autonomous_trader.decision_api import claude_client as cc
    monkeypatch.delenv('ANTHROPIC_API_KEY', raising=False)
    monkeypatch.delenv('ANTHROPIC_API_KEY_FILE', raising=False)
    path = cc.dpapi_path(tmp_path)
    assert path == tmp_path / '.secrets' / 'anthropic.clixml'
    with pytest.raises(PreflightError, match='ANTHROPIC_KEY_LOCATION_REQUIRED'):
        load_anthropic_key(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text('<encrypted/>', encoding='utf-8')
    monkeypatch.setattr(cc, '_load_dpapi_key', lambda p: KEY if p == path else None)
    assert load_anthropic_key(tmp_path) == KEY


def test_claude_models_get_claude_native_prompts_and_legacy_models_do_not(setup_cycle):
    from autonomous_trader.decision_api.astra_probe import (FAST_ENTRY_PROMPT, deep_alpha_request,
        shadow_audit_request)
    context = context_for(setup_cycle)
    entry = fast_alpha_request(context, mode='ENTRY', hard_policy={}, model='claude-sonnet-5-5')
    assert entry['input'][0]['content'] == ENTRY_SYSTEM
    assert deep_alpha_request(context, {}, model='claude-sonnet-5-5')['input'][0]['content'] == DEEP_SYSTEM
    assert shadow_audit_request(context, {}, model='claude-haiku-5-5')['input'][0]['content'] == SHADOW_SYSTEM
    # Dual brain (2026-10-15): the second model answers the same v3 question as the primary.
    second = fast_alpha_request(context, mode='ENTRY', hard_policy={}, model='gpt-6-astra')
    assert second['input'][0]['content'] == ENTRY_SYSTEM
    legacy = fast_alpha_request(context, mode='ENTRY', hard_policy={}, model='gpt-5-legacy')
    assert legacy['input'][0]['content'].startswith(FAST_ENTRY_PROMPT)
    for text in (ENTRY_SYSTEM, POSITION_SYSTEM, DEEP_SYSTEM, SHADOW_SYSTEM):
        assert 'Astra' not in text   # (10% is now the owner's per-position planned-loss limit, 2026-10-16)
    for phrase in ('T+1', 'ABSTAIN', 'VWAP', 'opening-range', 'hard_stop_usd', 'must equal the position'):
        assert phrase in ENTRY_SYSTEM + POSITION_SYSTEM


def test_decision_evidence_records_the_prompt_actually_sent():
    from autonomous_trader.monitoring.decision_evidence import _prompt, PROMPT_SHA256
    from hashlib import sha256
    assert _prompt('claude-sonnet-5-5') == (PROMPT_VERSION, sha256(ENTRY_SYSTEM.encode()).hexdigest())
    assert _prompt('gpt-6-astra')[1] == PROMPT_SHA256


def test_models_list_paginates_without_body_headers_and_errors_are_diagnosable():
    seen = []
    brain = client([(200, {'data': [{'id': 'claude-haiku-5-5'}], 'has_more': True, 'last_id': 'claude-haiku-5-5'}),
                    (200, {'data': [{'id': 'claude-opus-5-5'}], 'has_more': False, 'last_id': 'claude-opus-5-5'})],
                   seen)
    assert brain.verify_model() == 'claude-opus-5-5'
    assert seen[0]['url'] == 'https://api.anthropic.com/v1/models'
    assert seen[1]['url'] == 'https://api.anthropic.com/v1/models?after_id=claude-haiku-5-5'
    assert 'content-type' not in seen[0]['headers'] and seen[0]['json'] is None
    brain = client([(400, {'type': 'error', 'error': {'type': 'invalid_request_error',
                                                      'message': 'bad thing near ' + KEY}})], [])
    with pytest.raises(PreflightError, match='ANTHROPIC_HTTP_400'):
        brain.verify_model()
    assert brain.last_error == {'status': 400, 'type': 'invalid_request_error', 'message': 'bad thing near [key]'}
