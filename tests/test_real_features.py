"""Session feature engine: exact arithmetic on synthetic bars."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from types import SimpleNamespace

from autonomous_trader.real_features import daily_features, instrument_features, session_features

OPEN = datetime(2026, 10, 13, 13, 30, tzinfo=timezone.utc)


def bar(minute, o, h, l, c, v=1000):
    return SimpleNamespace(at=OPEN + timedelta(minutes=minute), open_usd=D(o), high_usd=D(h), low_usd=D(l),
                           close_usd=D(c), volume=D(v))


def trend(n=30, start=10.0, step=0.01):
    out = []
    for i in range(n):
        p = start + step * i
        out.append(bar(i, f'{p:.2f}', f'{p + 0.02:.2f}', f'{p - 0.01:.2f}', f'{p + 0.01:.2f}'))
    return out


def test_vwap_opening_range_and_momentum():
    bars = trend()
    f = session_features(bars, OPEN + timedelta(minutes=30), OPEN)
    assert f['bars'] == 30 and f['minutes_since_open'] == 30 and f['open'] == '10.0000'
    assert f['last'] == '10.3000' and f['high'] == '10.3100'
    # Equal volume: VWAP is the mean typical price ((h+l+c)/3 = p + 0.00667).
    assert abs(D(f['vwap']) - D('10.1517')) < D('0.0001')
    assert f['last_vs_vwap_bps'] > 0 and f['change_from_open_bps'] == 300
    assert f['opening_range_15m']['last_vs_range'] == 'ABOVE'
    assert f['change_5m_bps'] == 49 and f['minutes_since_day_high'] == 0
    assert f['bar_coverage'] == '1.00' and f['volume_pace_5m_vs_session'] == '1.00'


def test_no_bars_and_future_bars_are_not_evidence():
    assert session_features([], OPEN + timedelta(minutes=3), OPEN) == {'minutes_since_open': 3, 'bars': 0}
    f = session_features(trend(10), OPEN + timedelta(minutes=4, seconds=30), OPEN)
    assert f['bars'] == 5 and f['opening_range_15m'] is None and f['change_15m_bps'] is None


def test_thin_trading_shows_in_coverage():
    sparse = [b for i, b in enumerate(trend(30)) if i % 3 == 0]
    assert session_features(sparse, OPEN + timedelta(minutes=30), OPEN)['bar_coverage'] == '0.33'


def daily(n, close=20.0):
    return [{'time': (OPEN - timedelta(days=n - i)).date().isoformat() + 'T04:00:00Z', 'open': close,
             'high': close + 1, 'low': close - 1, 'close': close + (0.1 * i), 'volume': 1_000_000} for i in range(n)]


def test_daily_context_gap_atr_and_volume():
    rows = daily(25) + [{'time': OPEN.date().isoformat() + 'T04:00:00Z', 'open': 1, 'high': 1, 'low': 1,
                         'close': 1, 'volume': 1}]  # today's partial bar is excluded
    f = daily_features(rows, OPEN.date(), session_volume='250000', session_open_price='23.00', last='23.50')
    assert f['prior_sessions'] == 25 and f['prior_close'] == '22.4000'
    assert f['gap_bps'] == 268 and f['session_volume_vs_avg_daily'] == '0.25'
    assert f['atr14_bps'] is not None and f['last_vs_prior_high'] == 'ABOVE'
    assert daily_features([], OPEN.date()) == {'prior_sessions': 0}


def test_instrument_features_combine_both():
    value = instrument_features(trend(20), daily(25), OPEN + timedelta(minutes=20), OPEN)
    assert value['session']['bars'] == 20 and value['daily']['gap_bps'] is not None


class FakeHttp:
    def __init__(self, responses):
        self.responses, self.requests = list(responses), []

    def read(self, request):
        self.requests.append(request)
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def source_with(http, now):
    from autonomous_trader.real_market import RealMarketSource
    source = object.__new__(RealMarketSource)
    source.http, source.clock, source.bar_cache = http, (lambda: now), {}
    return source


def session_obj():
    from datetime import date
    return SimpleNamespace(opens_at=OPEN, label=date(2026, 10, 13))


def raw_bars(n):
    return [{'time': (OPEN + timedelta(minutes=i)).isoformat(), 'open': '400', 'high': '401', 'low': '399',
             'close': str(400 + i * 0.1), 'volume': '1000'} for i in range(n)]


def test_market_regime_reads_spy_once_per_refresh_window_and_never_blocks():
    import json
    now = OPEN + timedelta(minutes=30)
    http = FakeHttp([{'result': [{'symbol': 'SPY', 'result': raw_bars(30)}]},
                     {'result': [{'symbol': 'SPY', 'result': daily(25, close=400.0)}]}])
    source = source_with(http, now)
    regime = json.loads(source._market_regime(session_obj()))
    assert regime['symbol'] == 'SPY' and regime['features']['session']['bars'] == 30
    assert regime['features']['daily']['prior_sessions'] == 25
    # Within the refresh window and same session: no further reads.
    assert json.loads(source._market_regime(session_obj()))['features']['session']['bars'] == 30
    assert len(http.requests) == 2
    broken = source_with(FakeHttp([ValueError('REAL_READ_RATE_LIMITED')]), now)
    assert json.loads(broken._market_regime(session_obj())) == {'symbol': 'SPY', 'unavailable': True}


def test_instrument_context_is_compact_and_failed_daily_read_is_explicit():
    import json
    from autonomous_trader.contracts import AlphaInstrumentContext
    now = OPEN + timedelta(minutes=30)
    source = source_with(FakeHttp([ValueError('boom')]), now)
    source.bar_cache['ABC'] = tuple(trend(30))
    item = AlphaInstrumentContext(instrument_id='i-abc', symbol='ABC', quote_at=now, bid_usd='10.29',
        ask_usd='10.30', price_increment_usd='0.01', quantity_increment='1',
        market_context=json.dumps({'market_provenance': 'WEBULL_REAL_READ_ONLY', 'affordability_ceiling_usd': '100',
                                   'quality': 'x' * 900}))
    (enriched,) = source._with_features([item], [SimpleNamespace(ticker='ABC')], session_obj())
    value = json.loads(enriched.market_context)
    assert value['features']['session']['bars'] == 30 and value['features']['daily'] == {'prior_sessions': 0}
    assert 'quality' not in value and len(enriched.market_context) < 2000
