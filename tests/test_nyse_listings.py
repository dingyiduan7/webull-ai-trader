"""Owner approval 2026-10-21: NYSE listings are allowed once proven real-time. Every candidate still needs a
fresh (<= 10 s) quote and fresh session bars; a refused NYSE bar batch sets those symbols aside, never the cycle."""
from datetime import timedelta
from types import SimpleNamespace

import pytest

from autonomous_trader.real_market import RealMarketSource, TRADABLE_LISTINGS, NYSE_LISTINGS, NASDAQ_LISTINGS
from autonomous_trader.mvp.config import reviewed_calendar
from autonomous_trader.reference import NEW_YORK


def test_listing_sets():
    assert set(NASDAQ_LISTINGS) == {'NAS', 'NMS', 'NSQ'} and set(NYSE_LISTINGS) == {'NYSE', 'ASE', 'PSE'}
    assert set(TRADABLE_LISTINGS) == set(NASDAQ_LISTINGS) | set(NYSE_LISTINGS) and 'OTC' not in TRADABLE_LISTINGS


def source(read, held=()):
    from test_real_daily import NOW
    s = object.__new__(RealMarketSource)
    s.clock = lambda: NOW + timedelta(hours=1);s.bar_cache = {};s.held_symbols = held
    s.http = SimpleNamespace(read=read)
    session = reviewed_calendar().session(s.clock().astimezone(NEW_YORK).date())
    return s, session


def ref(t, ex):return SimpleNamespace(ticker=t, instrument_id=t.lower(), listing_exchange=ex)


def test_refused_nyse_batch_is_set_aside_not_fatal():
    calls = []
    def read(request):
        symbols = request.get_body_params()['symbols'];calls.append(symbols)
        if 'XOM' in symbols:raise ValueError('REAL_HTTP_403_NOT_ENTITLED')
        return {'result': [{'symbol': s, 'instrument_id': s.lower(), 'result': []} for s in symbols]}
    s, session = source(read)
    s._refresh_batch_bars([ref('AAPL', 'NAS'), ref('XOM', 'NYSE')], session)
    assert ['AAPL'] in calls and ['XOM'] in calls             # batched apart
    assert s.listing_blocked == {'XOM'}


def test_refused_nasdaq_batch_still_fails_loudly():
    def read(request):raise ValueError('REAL_HTTP_500_X')
    s, session = source(read)
    with pytest.raises(ValueError):
        s._refresh_batch_bars([ref('AAPL', 'NAS')], session)


def test_refused_batch_with_a_held_nyse_position_fails_loudly():
    def read(request):raise ValueError('REAL_HTTP_403_NOT_ENTITLED')
    s, session = source(read, held=('XOM',))
    with pytest.raises(ValueError):
        s._refresh_batch_bars([ref('XOM', 'NYSE')], session)


def test_rate_limit_is_never_swallowed():
    from autonomous_trader.real_read_budget import ReadRateLimited
    def read(request):raise ReadRateLimited('/market-data/stocks/bars/list', 30)
    s, session = source(read)
    with pytest.raises(ReadRateLimited):
        s._refresh_batch_bars([ref('XOM', 'NYSE')], session)
