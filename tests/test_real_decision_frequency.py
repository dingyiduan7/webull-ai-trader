from dataclasses import replace
from concurrent.futures import Future
from types import SimpleNamespace
from test_real_packet_handoff import setup
from autonomous_trader.mvp.config import reviewed_calendar
from autonomous_trader.reference import NEW_YORK
from autonomous_trader.real_daily import scope


def test_three_new_contexts_dispatch_but_unchanged_context_does_not(tmp_path,monkeypatch):
    r,p,starts,events,checks=setup(tmp_path,monkeypatch)
    r.controller.waiting_for_market_data=False
    used=set()
    class Connection:
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def execute(self,query,args):self.key=args[0];return self
        def fetchone(self):return (1,) if self.key in used else None
    r.store.db.connect=Connection
    r.emit=lambda kind,payload,**kw:used.add(kw['key'])
    session=reviewed_calendar().session(r.clock().astimezone(NEW_YORK).date())
    for evidence in ('new cost evidence A','new cost evidence B','new cost evidence C'):
        fresh=replace(p,context=p.context.model_copy(update={'execution_cost_evidence':evidence}))
        assert r._advance_packet(fresh,session,prepared=True)=='PORTFOLIO_EVALUATION'
        assert r._advance_packet(fresh,session,prepared=True)=='MONITORING'
    assert len(starts)==3 and len(used)==3


def test_inflight_decision_prevents_another_portfolio_dispatch(tmp_path,monkeypatch):
    r,p,starts,events,checks=setup(tmp_path,monkeypatch)
    r.controller.waiting_for_market_data=False
    r.pending=(Future(),p.context,'ENTRY',None)
    r._apply=lambda packet:None
    session=reviewed_calendar().session(r.clock().astimezone(NEW_YORK).date())
    assert r._advance_packet(p,session,prepared=True)=='ASTRA_PENDING_OR_APPLIED'
    assert not starts and not events


def test_trade_scope_does_not_set_decision_frequency():
    value=scope()
    assert value['astra_calls_per_session'] is None and value['astra_calls_per_week'] is None
    assert value['entries_per_session'] is None and value['round_trips_per_exchange_week'] is None
    from autonomous_trader.real_daily import OVERNIGHT_HOLDING
    assert value['overnight'] is OVERNIGHT_HOLDING and value['monthly_model_cost_cap_enabled'] is False


def test_native_discovery_bad_symbol_does_not_poison_valid_broad_universe():
    from autonomous_trader.real_market import RealMarketSource
    from types import SimpleNamespace
    from test_real_daily import NOW
    symbols=['S'+chr(65+i//26)+chr(65+i%26) for i in range(484)]
    rows=[{'symbol':symbol,'price':'1'} for symbol in symbols]+[{'symbol':'CIG C','price':'1'}]
    source=object.__new__(RealMarketSource)
    source.clock=lambda:NOW;source.bar_cache={}
    source.http=SimpleNamespace(read=lambda request:rows)
    result=source.discover()
    assert result==tuple(symbols)
    assert len(result)==484
    assert 'CIG C' not in result


def test_real_history_batches_keep_exact_identity_and_five_bar_limit():
    from autonomous_trader.real_market import RealMarketSource
    from types import SimpleNamespace
    from test_real_daily import NOW
    from autonomous_trader.mvp.config import reviewed_calendar
    from autonomous_trader.reference import NEW_YORK
    import pytest
    refs=[SimpleNamespace(ticker='S'+chr(65+i),instrument_id=str(i)) for i in range(23)]
    calls=[]
    class Http:
        def read(self,request):
            symbols=request.get_body_params()['symbols'];calls.append(symbols)
            return {'result':[{'symbol':r.ticker,'instrument_id':r.instrument_id,'result':[
                {'time':NOW.isoformat(),'open':'1','high':'1','low':'1','close':'1','volume':'10'}]}
                for r in refs if r.ticker in symbols]}
    source=object.__new__(RealMarketSource);source.clock=lambda:NOW;source.bar_cache={};source.http=Http()
    session=reviewed_calendar().session(NOW.astimezone(NEW_YORK).date())
    source._refresh_batch_bars(refs,session)
    assert [len(group) for group in calls]==[10,10,3]
    assert len(source.bar_cache)==23 and all(len(bars)==1 for bars in source.bar_cache.values())
    source._refresh_batch_bars(refs,session)
    assert len(calls)==3
    source.bar_cache={};source.http=SimpleNamespace(read=lambda request:{'result':[]})
    with pytest.raises(ValueError,match='REAL_HISTORY_BATCH_IDENTITY'):source._refresh_batch_bars(refs,session)


def test_real_snapshot_observed_twenty_symbol_batch_keeps_request_guards():
    from autonomous_trader.real_market import snapshot_request
    import pytest
    symbols=tuple('S'+chr(65+i) for i in range(20))
    r=snapshot_request(symbols)
    assert r.get_query_params()['symbols'].split(',')==list(symbols)
    for invalid in (symbols+('ZZ',),('AAPL','AAPL'),('CIG C',),()):
        with pytest.raises(ValueError,match='REAL_SNAPSHOT_SYMBOLS_INVALID'):snapshot_request(invalid)



def test_descending_broker_bars_normalize_latest_for_cache():
    from autonomous_trader.real_market import RealMarketSource
    from types import SimpleNamespace
    from test_real_daily import NOW
    from autonomous_trader.mvp.config import reviewed_calendar
    from autonomous_trader.reference import NEW_YORK
    from datetime import timedelta
    ref=SimpleNamespace(ticker='TEST',instrument_id='x')
    rows=[{'time':(NOW-timedelta(minutes=i)).isoformat(),'open':'1','high':'1','low':'1','close':'1','volume':'10'} for i in range(5)]
    source=object.__new__(RealMarketSource);source.clock=lambda:NOW;source.bar_cache={}
    source.http=SimpleNamespace(read=lambda request:{'result':[{'symbol':'TEST','instrument_id':'x','result':rows}]})
    source._refresh_batch_bars([ref],reviewed_calendar().session(NOW.astimezone(NEW_YORK).date()))
    assert source.bar_cache['TEST'][-1].at==NOW
    assert [b.at for b in source.bar_cache['TEST']]==sorted(b.at for b in source.bar_cache['TEST'])
