"""Owner decisions register: each owner decision is pinned to the code that must obey it.
Items still pending in v2 are strict xfails: when the work lands the test flips and
forces the xfail marker to be removed, so a decision can never silently regress."""
import pytest

from autonomous_trader import real_runner
from autonomous_trader.decision_api import claude_prompts as P
from autonomous_trader.decision_api.brain import DECISION_MODEL

PENDING = 'v2 pending'


def test_D_2026_10_15_incident_latch_never_sells_a_holding():
    assert 'incident' not in real_runner.LIQUIDATING_LATCHES


def test_D_2026_10_15_decision_model_is_opus_5_5():
    assert DECISION_MODEL == 'claude-opus-5-5'


def test_D_2026_10_14_not_fixed_to_day_trading_prompt_allows_multi_day_holds():
    assert 'No overnight holds' not in P.ENTRY_SYSTEM and 'Same-day only' not in P.POSITION_SYSTEM
    entry, position, _ = P.systems(True)
    assert 'minutes to several sessions' in entry and 'exit_horizon_at' in entry
    same_day, _, _ = P.systems(False)      # truthful while the charter switch is off
    assert 'not yet enabled overnight holding' in same_day


def test_D_2026_10_14_average_down_with_thesis_is_allowed():
    assert 'Never add shares' not in P.POSITION_SYSTEM and 'average down' in P.POSITION_SYSTEM
    assert 'never add to a broken thesis' in P.POSITION_SYSTEM.lower()


def test_D_2026_10_15_oversold_long_hold_guidance_present():
    assert 'oversold' in P.ENTRY_SYSTEM.lower()


def test_D_2026_10_14_no_scalping_bias_in_prompt():
    assert 'cents matter' not in P.ENTRY_SYSTEM.lower()


def test_D_2026_10_15_exits_are_limit_not_below_market():
    from autonomous_trader.real_strategy_gateway import PLANNED_EXIT_TTL
    assert PLANNED_EXIT_TTL.total_seconds() <= 120
    assert 'LIMIT sells at the current bid' in P.ENTRY_SYSTEM


def test_D_2026_10_15_target_reached_is_reviewed_not_auto_sold():
    assert 'reviewed again, not sold automatically' in P.POSITION_SYSTEM


def test_D_2026_10_15_strategy_cards_and_lessons_reach_the_model():
    assert 'STRATEGY REFERENCE CARDS' in P.ENTRY_SYSTEM and 'oversold_bounce_in_uptrend' in P.ENTRY_SYSTEM
    from autonomous_trader.decision_api.lessons import render
    assert 'L-001' in render()


def test_D_2026_10_14_overnight_requires_owner_signature():
    from autonomous_trader.real_daily import scope, OVERNIGHT_HOLDING
    assert scope()['overnight'] is OVERNIGHT_HOLDING   # charter-scoped: flipping it needs a re-sign


def test_D_2026_10_16_risk_limits_and_policy_revision():
    from decimal import Decimal
    from autonomous_trader.risk import POLICY
    from autonomous_trader.contracts import payload_hash
    from autonomous_trader.real_daily import policy_authorized, PRIOR_REAL_POLICY_SHA256
    assert POLICY.trade_risk == Decimal('0.10') and POLICY.open_risk == Decimal('0.20')
    signed = {'scope': {'risk_policy_sha256': payload_hash(POLICY)}}
    assert policy_authorized({'policy_hash': payload_hash(POLICY)}, signed)
    assert policy_authorized({'policy_hash': next(iter(PRIOR_REAL_POLICY_SHA256))}, signed)   # pending revision
    assert not policy_authorized({'policy_hash': 'unknown'}, signed)


def test_D_2026_10_16_never_force_sell_on_losses():
    assert real_runner.LIQUIDATING_LATCHES == ()
    assert 'Never panic' in P.POSITION_SYSTEM


def test_D_2026_10_16_overnight_only_for_high_upside():
    from decimal import Decimal as D
    from autonomous_trader.carry import qualifies_for_carry
    assert qualifies_for_carry(D('10'), D('9.50'), D('11'))          # +10%, 2:1
    assert not qualifies_for_carry(D('10'), D('9.50'), D('10.40'))   # +4%
    assert not qualifies_for_carry(D('10'), D('9'), D('10.80'))      # +8% but < 2:1
    assert not qualifies_for_carry(D('10'), D('9.50'), None)


def test_D_2026_10_16_dual_brains_trade_by_consensus():
    from autonomous_trader.decision_api import second_brain
    assert second_brain.MODE == 'CONSENSUS'
