"""2026-10-30: one symbol Webull refused made the whole 20-symbol quote batch fail and blocked the daily start.
A refused (4xx) candidate is now set aside after a one-by-one re-read; held symbols and other failures still fail."""
from datetime import datetime, timezone
import pytest
from autonomous_trader.contracts import Instrument
from autonomous_trader.real_state import binding
from autonomous_trader import real_market as market


def make(monkeypatch, held=(), error='REAL_HTTP_400_INVALID_SYMBOL'):
    now = datetime(2026, 10, 6, 14, tzinfo=timezone.utc); account = 'fixture-account'
    cash = dict(currency='USD', settled_cash='500', unsettled_cash='0', cash_balance='500', net_liquidation_value='500')
    positions = [dict(symbol=s, quantity='1', instrument_type='EQUITY') for s in held]
    profiles = {s: dict(symbol=s, instrument_id=s, status='OC', lot_size='1') for s in ('GOOD', 'BAD')}
    source = object.__new__(market.RealMarketSource); source.clock = lambda: now
    source.account_binding = binding(account); source.market_authority = None; source.symbols = ('GOOD', 'BAD')
    source.bar_cache = {}; source.focused = False
    calls = []

    class Http:
        last_received_at = now
        def read(self, request):
            path = request.get_url().split('?')[0]
            if path.endswith('/accounts/list'):return {}
            if path.endswith('/positions/list'):return positions
            if path.endswith('/open-orders/list'):return {'data': []}
            if path.endswith('/balances/get'):return {'account_currency_assets': [cash]}
            raise AssertionError(path)
    http = Http()
    original = market.snapshot_request
    def snap(group, urgent=False):
        calls.append(tuple(group))
        req = original(group, urgent=urgent); req._group = tuple(group); return req
    def read(request):
        path = request.get_url().split('?')[0]
        if path.endswith('/snapshots/list'):
            if 'BAD' in request._group:raise ValueError(error)
            return [dict(symbol=s, instrument_id=s, bid='10', ask='10.01', list_status='LISTED',
                         quote_time=int(now.timestamp() * 1000)) for s in request._group]
        return Http.read(http, request)
    http.read = read
    monkeypatch.setattr(market, 'snapshot_request', snap)
    source.http = http; source._profiles = lambda *a: profiles; source._refresh_batch_bars = lambda *a: None
    source.account_observation = lambda: dict(positions=positions, cash=cash, open_client_ids=(), observed_at=now, received_at=now)
    monkeypatch.setattr(market, 'select_single_individual_cash', lambda *a: {'account_id': account})
    monkeypatch.setattr(market, 'listed_equity', lambda *a: True)
    monkeypatch.setattr(market, 'instrument', lambda p: Instrument(instrument_id=p['symbol'], ticker=p['symbol'], country='US',
        asset_type='STOCK', listing_exchange='NAS', liquid=True, tradable=True, quantity_increment='1',
        price_increment_usd='0.01', minimum_quantity='1'))
    return source, calls


def test_refused_candidate_is_set_aside_and_good_symbol_still_quoted(monkeypatch):
    source, calls = make(monkeypatch)
    packet = source.refresh()
    assert [i.symbol for i in packet.context.instruments] == ['GOOD']
    assert source.listing_blocked == {'BAD'} and calls[0] == ('GOOD', 'BAD') and ('GOOD',) in calls


def test_refused_held_symbol_still_fails_loudly(monkeypatch):
    source, calls = make(monkeypatch, held=('BAD',))
    with pytest.raises(ValueError, match='REAL_HTTP_400'):source.refresh()


@pytest.mark.parametrize('error', ['REAL_HTTP_500_INTERNAL', 'REAL_TRANSPORT_UNCERTAIN_NO_RETRY', 'REAL_READ_RATE_LIMITED'])
def test_other_failures_are_not_swallowed(monkeypatch, error):
    source, calls = make(monkeypatch, error=error)
    with pytest.raises(ValueError, match=error):source.refresh()
