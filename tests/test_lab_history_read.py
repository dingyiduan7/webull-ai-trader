"""Owner approval 2026-10-19: a lab-only read-only Webull daily-bars request with a start date, for backtesting.
The trading read paths keep refusing the windowed shape; the lab path accepts only daily bars with a window."""
import sys
from pathlib import Path

import pytest

from autonomous_trader.real_strategy_broker import RealStrategyHttp, _lab_history_batch, _read_only_history_batch
from autonomous_trader.sandbox.http import build_request, Operation

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))


def daily(symbols=('SPY',), count='250', end=1_700_000_000_000, start=None, timespan=None):
    r = build_request(Operation.HISTORY_DAILY_BATCH, symbols=symbols)
    r.set_count(count)
    if end is not None:r.set_end_time(end)
    if start is not None:r.set_start_time(start)
    if timespan:r.add_body_params('timespan', timespan)
    return r


def test_lab_shape_accepted_only_with_a_window():
    assert _lab_history_batch(daily())
    assert _lab_history_batch(daily(end=None, start=1_600_000_000_000))
    assert not _lab_history_batch(daily(end=None))                 # no window: that is the trader's own shape


@pytest.mark.parametrize('request_', [
    lambda: daily(timespan='M1'),
    lambda: daily(count='251'),
    lambda: daily(end='1700000000000'),
    lambda: daily(end=True),
    lambda: daily(end=-5),
])
def test_lab_shape_rejects_anything_else(request_):
    assert not _lab_history_batch(request_())


def test_more_than_ten_symbols_rejected():
    r = daily();r.add_body_params('symbols', list('ABCDEFGHIJK'))
    assert not _lab_history_batch(r)


def test_extra_body_field_rejected():
    r = daily();r.add_body_params('account_id', 'x')
    assert not _lab_history_batch(r)


def test_trading_read_paths_still_refuse_windowed_bars():
    assert not _read_only_history_batch(daily())
    http = RealStrategyHttp.__new__(RealStrategyHttp)
    with pytest.raises(ValueError, match='REAL_READ_ROUTE_REQUIRED|REAL_WRITE_CAPABILITY_REQUIRED'):
        http.read(daily())


def test_lab_read_refuses_non_lab_shape_before_any_network():
    http = RealStrategyHttp.__new__(RealStrategyHttp)
    with pytest.raises(ValueError, match='REAL_LAB_HISTORY_ROUTE_REQUIRED'):
        http.read_lab_history(daily(end=None))


def bars(dates):
    return [dict(date=d, open=1.0, high=1.0, low=1.0, close=1.0, volume=1e6) for d in dates]


def test_backfill_pages_backwards_and_merges_without_duplicates():
    import strategy_lab as L
    series = {'AAA': bars(['2025-10-21', '2025-10-22'])}
    calls = []

    class Http:
        def read_lab_history(self, request):
            body = request.get_body_params();calls.append(body['end_time'])
            if len(calls) == 1:
                return {'result': [{'symbol': 'AAA', 'result': [
                    dict(time=d, open=1, high=1, low=1, close=1, volume=1) for d in ('2025-10-17', '2025-10-20', '2025-10-21')]}]}
            return {'result': [{'symbol': 'AAA', 'result': []}]}
    status = L.backfill(series, pages=3, http=Http(), sleep=lambda s: None)
    assert [b['date'] for b in series['AAA']] == ['2025-10-17', '2025-10-20', '2025-10-21', '2025-10-22']
    assert status['added_bars'] == 2 and not status['unsupported']
    assert calls[0] < 1_761_004_800_000                   # end_time strictly before the earliest saved bar


def test_backfill_reports_unsupported_when_webull_ignores_the_window():
    import strategy_lab as L
    series = {'AAA': bars(['2025-10-21'])}

    class Http:
        def read_lab_history(self, request):
            return {'result': [{'symbol': 'AAA', 'result': [dict(time='2025-10-21', open=1, high=1, low=1, close=1, volume=1)]}]}
    status = L.backfill(series, pages=3, http=Http(), sleep=lambda s: None)
    assert status['unsupported'] is True and len(series['AAA']) == 1
