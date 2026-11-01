"""Second decision model ("dual brain", owner decision 2026-10-14/15) — shadow stage.

Stage 1 (this module): the second model (Astra, OpenAI) receives the exact context and the same v3 system
prompt as the primary (Opus), and its decision is recorded next to the primary's, together with what the
deterministic consensus combiner (dual_brain) would have done. It has NO order authority: nothing it says
can open, add, close or modify. Stage 2 (consensus authority) needs the owner's re-signed charter.

Off unless switched on (MODE) and an OpenAI key is configured; spend is accounted through the same
ModelBudget as the primary, so the daily REAL spend ceiling bounds both models together.
"""
from datetime import datetime, timezone
from decimal import Decimal

SECOND_MODEL = 'gpt-6-astra'
MODES = ('OFF', 'SHADOW', 'CONSENSUS')
# Owner decision 2026-10-16 ("dual brains to trade"): CONSENSUS = an Opus entry is placed only when the second
# model agrees (conservative merge); the second model alone can never trade; without a key it is skipped.
MODE = 'CONSENSUS'
SHADOW_CALLS_PER_SESSION = 20


def enabled():
    return MODE == 'SHADOW'


def consensus():
    return MODE == 'CONSENSUS'


def make_second_brain(budget, *, key_loader=None, factory=None):
    """Build the second brain or return (None, reason). Never raises."""
    if not (enabled() or consensus()):
        return None, 'SECOND_BRAIN_OFF'
    try:
        from .astra_probe import load_openai_key
        from .brain import make_brain
        key = (key_loader or load_openai_key)()
        brain = (factory or make_brain)(key, model=SECOND_MODEL, reasoning='medium')
        brain.budget = budget
        return brain, None
    except Exception as exc:
        code = str(exc) if str(exc).isupper() else type(exc).__name__
        return None, 'SECOND_BRAIN_UNAVAILABLE:' + code[:80]


def shadow_record(mode, context, primary, second, error, *, model=SECOND_MODEL):
    """Evidence row comparing the two models and the consensus combiner's verdict. Pure."""
    from ..decisions import parse_alpha_decision
    import json
    from .dual_brain import combine_entry, combine_position

    def parsed(result):
        try:
            return parse_alpha_decision(json.dumps(result['decision'])) if isinstance(result, dict) else None
        except Exception:
            return None
    p, s = parsed(primary), parsed(second)
    combined = combine_entry(p, s) if mode == 'ENTRY' else combine_position(p, s)
    return dict(mode=mode, second_model=model, context_as_of=context.as_of.isoformat(),
                primary_action=p.action if p else None, second_action=s.action if s else None,
                primary_instrument=p.open.instrument_id if p and p.open else None,
                second_instrument=s.open.instrument_id if s and s.open else None,
                second_reason=(s.reason[:500] if s else None), second_error=error,
                second_cost_usd=(str(second.get('estimated_cost_usd')) if isinstance(second, dict) else None),
                consensus_mode=combined.mode, consensus_action=(combined.decision.action if combined.decision else None),
                consensus_challenge=combined.challenge, authority='NONE_SHADOW_ONLY',
                recorded_at=datetime.now(timezone.utc).isoformat())
