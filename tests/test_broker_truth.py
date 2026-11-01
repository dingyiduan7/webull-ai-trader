"""Owner 2026-10-18 broker-truth: the broker's cash is the true balance (journaled CASH_ADOPT), positions are
never adopted, nothing is adopted while an order is in flight, and the journal replays to the same state."""
from datetime import datetime, timezone
from decimal import Decimal as D

import pytest

from autonomous_trader import broker_truth
from autonomous_trader.durable.models import CashAdoption
from lifecycle_harness import Journal
from test_replay_2026_10_15 import friday


def test_deposit_or_withdrawal_sized_differences_are_adopted(tmp_path):
    clock, controller, broker, runner = friday(tmp_path)
    broker.settled -= D('120.00')            # e.g. a withdrawal the ledger never saw
    controller.refresh(broker.refresh())
    s = controller.state
    assert D(s['cash']['settled_usd']) == broker.settled and s['reconciliation']['ready']
    assert s['incidents'] == {} or all(i['reason'] != 'RECONCILIATION_MISMATCH' for i in s['incidents'].values())


def test_journal_replays_to_the_identical_state(tmp_path):
    clock, controller, broker, runner = friday(tmp_path)
    broker.settled += D('3.21')
    controller.refresh(broker.refresh())
    replayed = Journal(tmp_path / 'journal.jsonl').value
    assert replayed['cash'] == controller.state['cash'] and replayed['cash_adoptions'] == controller.state['cash_adoptions']


def test_nothing_is_adopted_while_an_order_is_in_flight():
    state = {'reservations': {'x': {}}, 'orders': {}, 'cash': {'settled_usd': '1', 'unsettled_usd': '0'}}
    assert broker_truth.plan(state, D('2'), D('0'), datetime(2026, 10, 19, 15, tzinfo=timezone.utc)) is None
    state = {'reservations': {}, 'orders': {'o': {'final': False}}, 'cash': state['cash']}
    assert broker_truth.plan(state, D('2'), D('0'), datetime(2026, 10, 19, 15, tzinfo=timezone.utc)) is None


def test_position_mismatch_is_never_folded_into_cash(tmp_path):
    clock, controller, broker, runner = friday(tmp_path)
    broker.quantity = D('1')                 # broker shows a share the ledger does not own
    with pytest.raises(ValueError):
        controller.refresh(broker.refresh())
    s = controller.state
    assert not s.get('cash_adoptions') and not s['reconciliation']['ready']


def test_adoption_record_requires_a_real_change():
    with pytest.raises(ValueError):
        CashAdoption(settled_delta_usd='0', unsettled_delta_usd='0', broker_settled_usd='1',
                     broker_unsettled_usd='0', evidence_id='e')


def test_post_close_cash_difference_is_adopted_and_the_day_closes(tmp_path):
    from datetime import timedelta
    from autonomous_trader.mvp.config import reviewed_calendar
    from test_carry_lifecycle import world
    clock, controller, broker, runner, gateway, events = world(tmp_path, configure=False)
    session = reviewed_calendar().session(clock().date())
    clock.at = session.closes_at + timedelta(minutes=1)
    broker.settled += D('5')
    result = runner.step()
    runner.worker.shutdown(wait=True)
    assert result == 'CLOSED_RECONCILED'
    assert D(controller.state['cash']['settled_usd']) == broker.settled
    assert any(k == 'REAL_BROKER_TRUTH_CASH' for k, _ in events)


# --- Regressions from the independent review (2026-10-18) ---

def test_adoption_never_trips_daily_drawdown_or_termination_latches(tmp_path):
    clock, controller, broker, runner = friday(tmp_path)
    broker.settled -= D('120.00')            # withdrawal-sized change adopted before the new session
    controller.refresh(broker.refresh())
    s = controller.state
    assert not any(s['latches'][k] for k in ('daily', 'drawdown', 'terminated'))
    broker.settled -= D('30.00')             # and another mid-session
    controller.refresh(broker.refresh())
    s = controller.state
    assert not any(s['latches'][k] for k in ('daily', 'drawdown', 'terminated'))
    assert D(s['cash']['settled_usd']) == broker.settled


def test_fee_lag_residual_never_leaves_a_lot_stuck(tmp_path):
    clock, controller, broker, runner = friday(tmp_path)
    broker.settled, broker.unsettled = D('514.74'), D('0.01')
    controller.refresh(broker.refresh())
    s = controller.state
    assert all(D(v['outstanding_usd']) == 0 for v in s['settlements'].values())   # the lot was confirmed
    assert (D(s['cash']['settled_usd']), D(s['cash']['unsettled_usd'])) == (D('514.74'), D('0.01'))
    broker.settled, broker.unsettled = D('514.75'), D('0')                          # next read: residual settled
    controller.refresh(broker.refresh())
    s = controller.state
    assert (D(s['cash']['settled_usd']), D(s['cash']['unsettled_usd'])) == (D('514.75'), D('0'))


def test_unknown_broker_shares_are_not_folded_into_cash(tmp_path):
    clock, controller, broker, runner = friday(tmp_path)
    broker.quantity = D('3'); broker.settled -= D('200.99')
    with pytest.raises(ValueError):
        controller.refresh(broker.refresh())
    s = controller.state
    assert not s.get('cash_adoptions') and not s['latches']['drawdown']


def test_stale_or_misbound_observation_is_not_adopted():
    from datetime import timedelta
    now = datetime(2026, 10, 19, 15, tzinfo=timezone.utc)
    good = {'account_binding': 'a', 'observed_at': now, 'cash': {'settled_cash': '1', 'unsettled_cash': '0', 'cash_balance': '1'}}
    assert broker_truth.valid_observation(good, 'a', now)
    assert not broker_truth.valid_observation({**good, 'account_binding': 'b'}, 'a', now)
    assert not broker_truth.valid_observation({**good, 'observed_at': now - timedelta(seconds=11)}, 'a', now)
    assert not broker_truth.valid_observation({k: v for k, v in good.items() if k != 'account_binding'}, 'a', now)
    assert not broker_truth.valid_observation({k: v for k, v in good.items() if k != 'observed_at'}, 'a', now)
    assert not broker_truth.valid_observation({**good, 'cash': {'settled_cash': '-1', 'unsettled_cash': '0', 'cash_balance': '-1'}}, 'a', now)
    assert not broker_truth.valid_observation({**good, 'cash': {'settled_cash': '1', 'unsettled_cash': '0', 'cash_balance': '2'}}, 'a', now)


def test_daily_preparation_account_accepts_a_broker_cash_difference(tmp_path):
    """2026-10-19 07:31 MT live block: preparation (read-only ledger) refused the broker's cash."""
    from scripts.run_real_daily import current_preparation_account
    from test_replay_2026_10_15 import authority
    clock, controller, broker, runner = friday(tmp_path)
    broker.settled += D('0.37')
    packet = broker.refresh()
    out = current_preparation_account(packet, broker.account_observation(), controller.state,
                                      authority(controller.state), clock())
    assert out.settled == broker.settled and out.unsettled == broker.unsettled


def test_daily_preparation_still_refuses_unknown_shares(tmp_path):
    from scripts.run_real_daily import current_preparation_account
    from test_replay_2026_10_15 import authority
    clock, controller, broker, runner = friday(tmp_path)
    broker.quantity = D('2'); broker.settled -= D('100')
    with pytest.raises(ValueError):
        current_preparation_account(broker.refresh(), broker.account_observation(), controller.state,
                                    authority(controller.state), clock())
