"""Per-model token prices and cost arithmetic. Decimal only; USD per million tokens.

Prices verified against the provider pricing pages on 2026-10-12. Unknown
models have no price, so a REAL decision with an unknown model fails closed
(REAL_MODEL_COST_UNRESOLVED) and the budget refuses to reserve for it.
"""
from decimal import Decimal

M = Decimal(1000000)

# input, cache_write, cache_read, output. Claude cache writes are priced at the
# 1-hour TTL rate (2x input) because the system prompt is cached for 1 hour.
PRICES = {
    'gpt-6-astra': dict(input=Decimal('10'), cache_write=Decimal('12.50'), cache_read=Decimal('1'), output=Decimal('50')),
    'claude-fable-5-1': dict(input=Decimal('10'), cache_write=Decimal('20'), cache_read=Decimal('0.25'), output=Decimal('50')),
    'claude-opus-5-5': dict(input=Decimal('4'), cache_write=Decimal('8'), cache_read=Decimal('0.20'), output=Decimal('20')),
    'claude-sonnet-5-5': dict(input=Decimal('2'), cache_write=Decimal('4'), cache_read=Decimal('0.10'), output=Decimal('10')),
    # Haiku 5.5 doubles-to-5x above 100k prompt tokens; the long-context rate is used
    # for reservations and applied to actual usage when the prompt exceeds 100k.
    'claude-haiku-5-5': dict(input=Decimal('0.10'), cache_write=Decimal('0.20'), cache_read=Decimal('0.01'), output=Decimal('0.50')),
}
LONG_CONTEXT = {
    'claude-haiku-5-5': (100000, dict(input=Decimal('0.50'), cache_write=Decimal('1'), cache_read=Decimal('0.05'), output=Decimal('2.50'))),
}


def priced(model) -> bool:
    return model in PRICES


def _rates(model, prompt_tokens):
    rates = PRICES[model]
    if model in LONG_CONTEXT and prompt_tokens > LONG_CONTEXT[model][0]:
        rates = LONG_CONTEXT[model][1]
    return rates


def worst_rates(model):
    rates = PRICES[model]
    if model in LONG_CONTEXT:
        long = LONG_CONTEXT[model][1]
        rates = {k: max(rates[k], long[k]) for k in rates}
    return rates


def estimate(model, usage):
    """Cost of one completed call from normalized usage, or None if unknown.

    usage.input_tokens is the TOTAL prompt (fresh + cache read + cache write);
    input_tokens_details carries cached_tokens (reads) and cache_write_tokens.
    """
    if model not in PRICES or not isinstance(usage, dict):
        return None
    details = usage.get('input_tokens_details') or {}
    inputs, outputs = usage.get('input_tokens'), usage.get('output_tokens')
    cached, writes = details.get('cached_tokens', 0), details.get('cache_write_tokens', 0)
    if any(type(v) is not int or v < 0 for v in (inputs, outputs, cached, writes)) or cached + writes > inputs:
        return None
    r = _rates(model, inputs)
    total = (Decimal(inputs - cached - writes) * r['input'] + Decimal(cached) * r['cache_read']
             + Decimal(writes) * r['cache_write'] + Decimal(outputs) * r['output']) / M
    return str(total)


def reserve(model, encoded_bytes, max_output_tokens):
    """Pessimistic pre-call reservation: every UTF-8 byte (plus 1,000 tokens of
    protocol overhead) billed as a fresh cache write, and the full output cap."""
    r = worst_rates(model)
    return (Decimal(encoded_bytes + 1000) * max(r['input'], r['cache_write'])
            + Decimal(max_output_tokens) * r['output']) / M
