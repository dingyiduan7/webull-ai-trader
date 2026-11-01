"""Discovery liquidity floor for new entries, and the runner/trailing playbook text."""
from decimal import Decimal as D

import pytest

from autonomous_trader.decision_api import claude_prompts as prompts
from autonomous_trader.real_market import MAX_ENTRY_SPREAD_BPS, MIN_ENTRY_PRICE, _liquid_enough


@pytest.mark.parametrize('bid,ask,ok', [
    (D('3.79'), D('3.80'), True),        # BMBL-like: $3.80, ~26 bps
    (D('17.27'), D('17.29'), True),      # WFF-like
    (D('0.64'), D('0.65'), False),       # sub-$2 micro-cap
    (D('1.99'), D('2.00'), False),       # mid just under the floor
    (D('2.00'), D('2.01'), True),        # at the floor, 50 bps spread
    (D('5.00'), D('5.06'), False),       # 120 bps spread
    (D('5.00'), D('5.05'), True)])       # exactly 100 bps spread
def test_liquid_enough(bid, ask, ok):
    assert _liquid_enough(bid, ask) is ok
    assert MIN_ENTRY_PRICE == D('2') and MAX_ENTRY_SPREAD_BPS == D('100')


def test_prompts_teach_trailing_and_version_moved():
    # v3 (owner 2026-10-14/15): multi-day investor-trader prompt replaced the intraday scalping playbook.
    assert prompts.PROMPT_VERSION == 'claude-decision-v3'
    assert 'about 100% on the day' in prompts.SETUPS
    assert 'ratchet the stop' in prompts.POSITION_OUTPUT and 'omit it only when you' in prompts.ENTRY_OUTPUT
    assert prompts.ENTRY_SYSTEM.count('SETUPS (reference') == 1
