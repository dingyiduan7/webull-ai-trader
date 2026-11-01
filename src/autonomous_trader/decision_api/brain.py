"""Single construction point for the decision model ("brain").

Every runtime path builds its model client here, so the provider and model
choice live in one place. Claude is the default; the legacy OpenAI client is
kept only so old evidence and tests that name gpt-6-astra still resolve.
"""
import os

from .astra_probe import AstraClient, PROMPT, load_openai_key
from .claude_client import ClaudeClient, load_anthropic_key

# Decisions that can open or manage a position.
DECISION_MODEL = 'claude-opus-5-5'
# Non-authoritative counterfactual audits: cheapest capable model.
SHADOW_MODEL = 'claude-haiku-5-5'


def is_claude(model):
    return isinstance(model, str) and model.startswith('claude-')


def load_model_key(model=DECISION_MODEL):
    return load_anthropic_key() if is_claude(model) else load_openai_key()


def shadow_model(model):
    return SHADOW_MODEL if is_claude(model) else model


def make_brain(key, *, model=DECISION_MODEL, reasoning='medium', instructions=PROMPT,
               approved_sme_advisory=None):
    if is_claude(model):
        return ClaudeClient(key, model=model, reasoning=reasoning, instructions=instructions,
                            approved_sme_advisory=approved_sme_advisory)
    return AstraClient(key, os.getenv('OPENAI_PROJECT_ID'), model=model, reasoning=reasoning,
                       instructions=instructions, approved_sme_advisory=approved_sme_advisory)
