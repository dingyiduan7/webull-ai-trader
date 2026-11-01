from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace

import pytest

from autonomous_trader.real_read_budget import ReadBudget, ReadRateLimited, shared_budget
from autonomous_trader.real_market import RealMarketSource, snapshot_request
from test_real_daily import NOW

SNAP='/market-data/stocks/snapshots/list'


def budget():
    now=[100.0];waits=[]
    def sleep(delay):
        waits.append(delay);now[0]+=delay
    return ReadBudget(lambda:now[0],sleep),now,waits


def test_discovery_cannot_consume_held_quote_reserve_or_other_endpoint_budget():
    b,now,waits=budget()
    for _ in range(50):b.acquire(SNAP)
    with pytest.raises(ReadRateLimited):b.acquire(SNAP)
    for _ in range(10):b.acquire(SNAP,urgent=True)
    with pytest.raises(ReadRateLimited):b.acquire(SNAP,urgent=True)
    b.acquire('/trading/assets/balances/get')
    assert not waits
    now[0]+=60.05;b.acquire(SNAP)


def test_concurrent_clients_share_quota_without_overshoot():
    b,_,_=budget()
    def acquire(_):
        try:b.acquire(SNAP,urgent=True);return True
        except ReadRateLimited:return False
    with ThreadPoolExecutor(max_workers=12) as pool:
        assert sum(pool.map(acquire,range(120)))==60
    assert shared_budget('fixture-app') is shared_budget('fixture-app')
    assert shared_budget('fixture-other') is not shared_budget('fixture-app')


def test_short_account_quota_waits_without_busy_loop():
    b,now,waits=budget();path='/trading/assets/positions/list'
    for _ in range(3):b.acquire(path)
    assert len(waits)==1 and waits[0]==pytest.approx(2.05)
    assert len(b.calls[path])==1


@pytest.mark.parametrize('header,expected', [('90',90),(None,60),('nonsense',60),('nan',60),('1',60)])
def test_429_respects_retry_after_and_does_not_automatically_retry(header,expected):
    b,now,waits=budget()
    exc=b.rejected(SNAP,header)
    assert exc.retry_after==expected
    with pytest.raises(ReadRateLimited):b.acquire(SNAP,urgent=True)
    assert not b.calls[SNAP] and not waits
    now[0]+=expected;b.acquire(SNAP,urgent=True)


def test_flat_rate_limit_defers_scan_without_latching_or_recursive_retry(tmp_path,monkeypatch):
    from test_real_market_waiting import make_runner
    r,_,_,_,_,_,_,failures=make_runner(tmp_path,monkeypatch)
    now=[NOW];r.clock=lambda:now[0];events=[]
    r.emit=lambda *a,**kw:events.append(a)
    assert r._market_failure_or_incident(ReadRateLimited(SNAP,40),'SOURCE_REFRESH')=='WAITING_FOR_MARKET_DATA'
    r.source.account_observation=lambda:pytest.fail('must wait for endpoint quota')
    r.prepare_waiting_market=lambda:pytest.fail('must not restart scan immediately')
    assert r._wait_for_market_data()=='WAITING_FOR_MARKET_DATA'
    assert not failures and events[-1][1]['retry_after_seconds']==40


def test_profile_cache_only_reuses_discovery_and_keeps_selected_or_held_direct():
    s=object.__new__(RealMarketSource);now=[NOW];s.clock=lambda:now[0]
    s.profile_cache={};s.focused=False;calls=[]
    def read(request):
        calls.append(request)
        return {'data':[dict(symbol='TEST',instrument_id='one',status='OC')]}
    s.http=SimpleNamespace(read=read)
    for _ in range(2):assert s._profiles(('TEST',),[])['TEST']['instrument_id']=='one'
    assert len(calls)==1
    s._profiles(('TEST',),['TEST']);assert len(calls)==2
    s.focused=True;s._profiles(('TEST',),[]);assert len(calls)==3
    s.focused=False;now[0]+=timedelta(seconds=61)
    s._profiles(('TEST',),[]);assert len(calls)==4


def test_repeated_484_symbol_scan_reuses_49_metadata_batches_without_narrowing():
    symbols=tuple('S'+chr(65+i//26)+chr(65+i%26) for i in range(484))
    s=object.__new__(RealMarketSource);s.clock=lambda:NOW;s.profile_cache={};s.focused=False
    calls=[]
    def read(request):
        group=request.get_query_params()['symbols']
        if isinstance(group,str):group=group.split(',')
        calls.append(tuple(group))
        return {'data':[dict(symbol=symbol,instrument_id=symbol,status='OC') for symbol in group]}
    s.http=SimpleNamespace(read=read)
    assert set(s._profiles(symbols,[]))==set(symbols)
    assert len(calls)==49
    assert set(s._profiles(symbols,[]))==set(symbols)
    assert len(calls)==49
    s.focused=True;s._profiles((symbols[0],),[])
    assert len(calls)==50


def test_discovery_preserves_cached_bars_without_reusing_quotes():
    s=object.__new__(RealMarketSource);s.clock=lambda:NOW
    bars=(object(),);s.bar_cache={'TEST':bars,'GONE':()}
    s.http=SimpleNamespace(read=lambda request:[dict(symbol='TEST',price='3')])
    assert s.discover()==('TEST',)
    assert s.bar_cache=={'TEST':bars}
    assert snapshot_request(('TEST',),urgent=True)._aitrader_urgent_read
    assert not snapshot_request(('TEST',))._aitrader_urgent_read


def test_transport_429_blocks_next_read_before_signing_or_sending(monkeypatch):
    from autonomous_trader import real_strategy_broker as module
    b,_,_=budget();sends=[];signs=[]
    transport=object.__new__(module.RealStrategyHttp)
    transport.read_budget=b
    wire=SimpleNamespace(get_url=lambda:SNAP,get_headers=lambda:{},get_body=lambda:None)
    def sign(*args):signs.append(True);return wire
    transport.client=SimpleNamespace(_make_http_response=sign)
    class Response:
        status_code=429
        headers={'Retry-After':'90'}
        def __enter__(self):return self
        def __exit__(self,*args):pass
    class Session:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def prepare_request(self,request):return request
        def send(self,*args,**kwargs):sends.append(True);return Response()
    monkeypatch.setattr(module.requests,'Session',Session)
    for _ in range(2):
        with pytest.raises(ReadRateLimited):transport.read(snapshot_request(('TEST',)))
    assert len(sends)==len(signs)==1


def test_rate_limited_held_account_does_not_get_flat_entry_retry(monkeypatch,tmp_path):
    from test_real_market_waiting import make_runner
    r,_,_,state,_,_,_,failures=make_runner(tmp_path,monkeypatch)
    state['positions']['owned']={'quantity':'1'}
    # 2026-10-30: a held account waits out the rate limit (no incident) but never enters the flat waiting path.
    before=getattr(r.controller,'waiting_for_market_data',None)
    assert r._market_failure_or_incident(ReadRateLimited(SNAP,40),'SOURCE_REFRESH')=='HELD_READ_DEFERRED'
    assert not failures and getattr(r.controller,'waiting_for_market_data',None)==before
    state['latches']['incident']=True       # an already-latched holding keeps the incident path
    assert r._market_failure_or_incident(ReadRateLimited(SNAP,40),'SOURCE_REFRESH')=='INCIDENT'
