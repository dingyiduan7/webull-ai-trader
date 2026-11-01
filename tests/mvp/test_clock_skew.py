"""Regression for the 2026-09-14 slow Windows clock incident."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from test_cycle import setup_cycle
from autonomous_trader.durable.projection import Rejected


@pytest.mark.parametrize("future_seconds", [1.809465, 0.000001])
def test_future_quote_rejected_then_reconciles_after_clock_sync(setup_cycle, future_seconds):
    args, broker, ledger = setup_cycle
    controller = args["controller"]
    # Actual Webull quote_time is Unix milliseconds, not Denver wall time.
    raw_quote_time = 1789392723871
    quote_at = datetime.fromtimestamp(raw_quote_time / 1000, timezone.utc)
    assert quote_at.isoformat() == "2026-09-14T13:32:03.871000+00:00"
    slow_now = quote_at - timedelta(seconds=future_seconds)
    observation = replace(args["source"].refresh(controller.state),
                          quote_at=quote_at, received_at=slow_now)
    controller.clock = lambda: slow_now
    with pytest.raises(ValueError, match="STALE_MARKET_OR_ACCOUNT"):
        observation.require_fresh(slow_now)
    with pytest.raises(Rejected, match="FUTURE_MARK"):
        controller.refresh(observation)

    # Correct the comparison clock only: never clamp or offset the source mark.
    corrected_now = quote_at + timedelta(milliseconds=100)
    controller.clock = lambda: corrected_now
    observation.require_fresh(corrected_now)
    state = controller.refresh(observation)
    assert state["reconciliation"]["ready"]
    assert ledger.rebuild(controller.account_id) == state
    assert not state["orders"] and not broker.orders
