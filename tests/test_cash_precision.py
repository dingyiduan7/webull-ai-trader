from datetime import datetime, timezone
from decimal import Decimal as D

from autonomous_trader.cash_precision import adjust_cash, fee_allowance

NOW = datetime(2026, 10, 16, 2, 40, tzinfo=timezone.utc)


def state(settled='313.39', unsettled='201.35', fees='0.01', lots=(('201.35', '2026-10-16'),)):
    return {'cash': {'settled_usd': settled, 'unsettled_usd': unsettled},
            'orders': {'buy': {'fees_usd': '0'}, 'sell': {'fees_usd': fees}},
            'settlements': {f'l{i}': {'outstanding_usd': a, 'settlement_date': d} for i, (a, d) in enumerate(lots)},
            'config': {'settlement_days': ['2026-10-15', '2026-10-16', '2026-10-19']},
            'session': {'label': '2026-10-15', 'opens_at': '2026-10-15T13:30:00+00:00',
                        'closes_at': '2026-10-15T20:00:00+00:00'}}


def broker(settled, unsettled, equity=None):
    total = D(settled) + D(unsettled)
    return {'settled_cash': settled, 'unsettled_cash': unsettled, 'cash_balance': str(total),
            'net_liquidation_value': equity or str(total)}


def test_fee_allowance_is_sum_of_recorded_order_fees():
    assert fee_allowance(state()) == D('0.01')


def test_thursday_incident_numbers_are_explained():
    cash, ev = adjust_cash(broker('514.75', '0.00'), state(), NOW)
    assert ev and ev['kind'] == 'BROKER_CASH_SURPLUS_EXPLAINED_BY_LEDGER_FEES' and ev['variance_usd'] == '0.01'
    assert (D(cash['settled_cash']), D(cash['unsettled_cash'])) == (D('514.74'), D('0.00')) or \
        (D(cash['settled_cash']) + D(cash['unsettled_cash']) == D('514.74'))
    assert D(cash['cash_balance']) == D('514.74')


def test_pre_overnight_variant_reduces_unsettled_bucket():
    cash, ev = adjust_cash(broker('313.39', '201.36'), state(), NOW)
    assert ev and (cash['settled_cash'], cash['unsettled_cash']) == ('313.39', '201.35')


def test_surplus_above_fee_allowance_is_unchanged():
    b = broker('514.77', '0.00')
    assert adjust_cash(b, state(), NOW) == (b, None)


def test_broker_below_ledger_is_never_adjusted():
    b = broker('514.00', '0.00')
    assert adjust_cash(b, state(), NOW) == (b, None)


def test_exact_match_is_untouched():
    b = broker('313.39', '201.35')
    assert adjust_cash(b, state(), NOW) == (b, None)


def test_unexplained_bucket_layout_is_unchanged():
    b = broker('400.00', '114.75')
    assert adjust_cash(b, state(), NOW) == (b, None)


def test_no_fees_recorded_means_no_adjustment():
    b = broker('514.75', '0.00')
    assert adjust_cash(b, state(fees='0'), NOW) == (b, None)


def test_input_not_mutated_and_garbage_fails_closed():
    b = broker('514.75', '0.00'); before = dict(b)
    adjust_cash(b, state(), NOW)
    assert b == before
    assert adjust_cash({'settled_cash': 'x'}, state(), NOW)[1] is None
