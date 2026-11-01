from datetime import timedelta
from decimal import Decimal as D
from threading import RLock
from threading import Timer
from types import SimpleNamespace

import pytest

from test_cycle import NOW
from test_alpha_reset import focused_source
from autonomous_trader.monitoring.runner import Engine
from autonomous_trader.monitoring.opportunities import OpportunityQueue
from autonomous_trader.sandbox.live_stream import LiveQuoteStream,RollingMarketCache
from autonomous_trader.sandbox.http import validate_request,SUBSCRIBE_PATH,UNSUBSCRIBE_PATH
from autonomous_trader.sandbox.http import Operation
from autonomous_trader.sandbox.secrets import PreflightError
from webull.data.request.subscribe_request import SubscribeRequest
from webull.data.request.unsubscribe_request import UnsubcribeRequest


def basic(symbol='FOCUS',iid='focus-id',at=NOW):
    return {'symbol':symbol,'instrument_id':iid,'timestamp':str(int(at.timestamp()*1000))}


def test_source_timestamped_rolling_windows_and_depth():
    cache=RollingMarketCache(lambda:NOW);cache.connection_opened()
    assert cache.ingest('quote',{'basic':basic(at=NOW-timedelta(seconds=8)),
        'bids':[{'price':'9.98','size':'40'}],'asks':[{'price':'10.02','size':'50'}]},NOW-timedelta(seconds=8))
    assert cache.ingest('tick',{'basic':basic(at=NOW-timedelta(seconds=4)),'price':'10.05','volume':'30'},NOW-timedelta(seconds=4))
    assert cache.ingest('quote',{'basic':basic(),
        'bids':[{'price':'10.09','size':'80'}],'asks':[{'price':'10.11','size':'70'}]},NOW)
    row=cache.fresh_quote('FOCUS',NOW);windows=row['windows']
    assert (row['bid'],row['ask'],row['bid_size'],row['ask_size'])==(D('10.09'),D('10.11'),D('80'),D('70'))
    assert windows['1']['event_count']==1 and windows['5']['event_count']==2 and windows['10']['event_count']==3
    assert windows['5']['tick_volume']=='30' and windows['10']['source_start_at']==(NOW-timedelta(seconds=8)).isoformat()
    assert row['window_updated_at']==NOW


def test_disconnect_and_reconnect_invalidate_old_quote_until_new_epoch_event():
    cache=RollingMarketCache(lambda:NOW);cache.connection_opened()
    cache.ingest('quote',{'basic':basic(),'bids':[{'price':'10','size':'1'}],
        'asks':[{'price':'10.01','size':'1'}]},NOW)
    assert cache.fresh_quote('FOCUS',NOW)
    cache.connection_lost();assert cache.fresh_quote('FOCUS',NOW) is None
    cache.connection_opened();assert cache.fresh_quote('FOCUS',NOW) is None


def test_only_material_stream_changes_wake_decision_loop():
    current={'at':NOW};cache=RollingMarketCache(lambda:current['at']);cache.connection_opened()
    quote=lambda price:{'basic':basic(at=current['at']),'bids':[{'price':price,'size':'100'}],
        'asks':[{'price':str(D(price)+D('.01')),'size':'100'}]}
    cache.ingest('quote',quote('10.00'),current['at']);first=cache.revision('FOCUS')
    current['at']+=timedelta(milliseconds=100);cache.ingest('quote',quote('10.00'),current['at'])
    assert cache.revision('FOCUS')==first
    current['at']+=timedelta(milliseconds=100);cache.ingest('quote',quote('10.05'),current['at'])
    assert cache.revision('FOCUS')==first+1
    assert cache.health()['raw_events']['FOCUS']==3


def test_wait_for_fresh_stream_uses_new_arrival_and_never_redates_stale_quote():
    stream=LiveQuoteStream(SimpleNamespace(app_key='x',app_secret='y'),clock=lambda:NOW)
    stream.watch(('FOCUS',));stream.cache.connection_opened()
    def quote(at):return {'basic':basic(at=at),'bids':[{'price':'10','size':'100'}],
        'asks':[{'price':'10.01','size':'100'}]}
    stream.cache.ingest('quote',quote(NOW-timedelta(seconds=7)),NOW)
    assert stream.wait_fresh_quote('FOCUS',timeout=0) is None
    def arrive():
        stream.cache.ingest('quote',quote(NOW),NOW);stream.quote_event.set()
    timer=Timer(.02,arrive);timer.start()
    try:
        row=stream.wait_fresh_quote('FOCUS',timeout=1)
        assert row['source_at']==NOW and row['received_at']==NOW
    finally:timer.join()


def test_watch_set_covers_shortlist_and_held_symbols_with_bound():
    stream=LiveQuoteStream(SimpleNamespace(app_key='x',app_secret='y'))
    names=tuple('S'+chr(65+i//26)+chr(65+i%26) for i in range(35))
    stream.watch(names);assert stream.active()==names
    with pytest.raises(ValueError,match='INVALID_STREAM_WATCH_SET'):stream.watch(names+('EXTRA',))
    with pytest.raises(ValueError,match='INVALID_STREAM_WATCH_SET'):stream.watch(('bad symbol',))


def test_sandbox_transport_allows_only_bounded_market_stream_rotation():
    subscribe=SubscribeRequest();subscribe.set_session_id('session');subscribe.set_symbols(['VSA','FOXX'])
    subscribe.set_category('US_STOCK');subscribe.set_sub_types(['QUOTE','SNAPSHOT','TICK'])
    assert validate_request(subscribe)==SUBSCRIBE_PATH
    unsubscribe=UnsubcribeRequest();unsubscribe.set_session_id('session');unsubscribe.set_symbols(['VSA','FOXX'])
    unsubscribe.set_category('US_STOCK');unsubscribe.set_sub_types(['QUOTE','SNAPSHOT','TICK']);unsubscribe.set_unsubscribe_all(False)
    assert validate_request(unsubscribe)==UNSUBSCRIBE_PATH
    subscribe.set_symbols(['AAPL','TSLA','MSFT']);assert validate_request(subscribe)==SUBSCRIBE_PATH
    subscribe.set_symbols(['S'+chr(65+i//26)+chr(65+i%26) for i in range(36)])
    with pytest.raises(PreflightError,match='READ_ONLY_ENDPOINT_REJECTED'):validate_request(subscribe)


def test_focused_refresh_uses_fresh_stream_after_initial_http_certification():
    source=focused_source();reader=source.readers[next(op for op in source.readers if op.value=='snapshot')];calls=[]
    class CountingReader:
        def read(self,op,**kwargs):calls.append(tuple(kwargs['symbols']));return reader.read(op,**kwargs)
    source.readers[next(op for op in source.readers if op.value=='snapshot')]=CountingReader()
    source.refresh_focus({},'FOCUS');assert len(calls)==2
    windows={str(n):{'event_count':1,'change_bps':'0'} for n in (1,5,10,60)}
    source.stream=SimpleNamespace(fresh_quote=lambda symbol:{'instrument_id':'focus-id','bid':D('10.10'),'ask':D('10.11'),
        'bid_size':D('50'),'ask_size':D('40'),'source_at':NOW,'received_at':NOW,'volume':D('100050'),
        'windows':windows,'material_signature':(('1',0),),'window_updated_at':NOW})
    value=source.refresh_focus({},'FOCUS').select('FOCUS',NOW)
    assert len(calls)==2 and value.bid==D('10.10') and value.market_provenance=='MQTT_QUOTE'
    assert value.live_windows==windows
    assert value.stream_window_updated_at==NOW


def test_owned_quote_uses_live_cache_and_preserves_exact_identity_and_receipt():
    source=focused_source();initial=source.refresh_focus({},'FOCUS').select('FOCUS',NOW)
    state={'positions':{'focus-id':{'quantity':'1'}},'instruments':{'focus-id':initial.instrument.model_dump(mode='json')}}
    base=source.readers[Operation.POSITIONS]
    class PositionReader:
        def read(self,op,**kwargs):
            if op==Operation.POSITIONS:
                return SimpleNamespace(status_code=200,body=[{'symbol':'FOCUS','instrument_type':'EQUITY','quantity':'1'}],received_at=NOW.isoformat())
            return base.read(op,**kwargs)
    source.readers[Operation.POSITIONS]=PositionReader()
    class NoSnapshot:
        def read(self,*args,**kwargs):pytest.fail('fresh held stream should not require another snapshot')
    source.readers[Operation.SNAPSHOT]=NoSnapshot()
    source.stream=SimpleNamespace(fresh_quote=lambda symbol:{'instrument_id':'focus-id','bid':D('10.10'),'ask':D('10.11'),
        'bid_size':D('50'),'ask_size':D('40'),'source_at':NOW-timedelta(milliseconds=300),
        'received_at':NOW-timedelta(milliseconds=100),'volume':D('100050'),'windows':{},
        'material_signature':(('quote',1),),'window_updated_at':NOW})
    value=source.refresh_held(state).held('focus-id',NOW)
    assert value.bid==D('10.10') and value.market_provenance=='MQTT_QUOTE'
    assert value.quote_at==NOW-timedelta(milliseconds=300)
    assert value.quote_received_at==NOW-timedelta(milliseconds=100)


class Store:
    def __init__(self):self.saved=None
    def pipeline_state(self,_):return None
    def emit(self,kind,payload,**_):
        if kind=='OPPORTUNITY_QUEUE':self.saved=payload


def test_material_stream_revision_wakes_hot_job_without_bypassing_model_authority():
    store=Store();queue=OpportunityQueue(store,'DRY_RUN',lambda:NOW,executor=SimpleNamespace())
    item=SimpleNamespace(entry_eligible=True,instrument_id='focus-id',ticker='FOCUS')
    queue.refill([item],NOW.date());queue.promote_hot([{'symbol':'FOCUS','screen_at':NOW.isoformat(),'percent_per_minute':'2'}])
    job=next(iter(queue.jobs.values()));job.update(status='COMPLETED',reason='ABSTAIN',not_before=(NOW+timedelta(minutes=5)).isoformat())
    class Stream:
        def __init__(self):self.watched=()
        def watch(self,s):self.watched=tuple(s)
        def revision(self,_):return 3
        def fresh_quote(self,_):return {'source_at':NOW}
        def health(self):return {'connected':True}
    engine=Engine.__new__(Engine);engine.live_stream=Stream();engine.opportunities=queue;engine.clock=lambda:NOW
    engine.lock=RLock();engine.state={}
    engine._sync_stream_watch()
    assert engine.live_stream.watched==('FOCUS',)
    assert job['status']=='PENDING' and job['reason']=='STREAM_MATERIAL_CHANGE'
    assert job['stream_signaled_revision']==3 and 'stream_evaluated_revision' not in job


def test_nonhot_material_signal_wakes_abstention_and_watch_keeps_owned_symbol():
    store=Store();queue=OpportunityQueue(store,'DRY_RUN',lambda:NOW,executor=SimpleNamespace())
    queue.refill([SimpleNamespace(entry_eligible=True,instrument_id='focus-id',ticker='FOCUS')],NOW.date())
    job=next(iter(queue.jobs.values()))
    job.update(status='COMPLETED',reason='ABSTAIN',hot_epoch_blocked_signal_at=NOW.isoformat(),
        not_before=(NOW+timedelta(minutes=5)).isoformat())
    watched=[]
    engine=Engine.__new__(Engine);engine.clock=lambda:NOW;engine.opportunities=queue
    engine.live_stream=SimpleNamespace(watch=lambda s:watched.extend(s),revision=lambda _:3,
        fresh_quote=lambda _:{'source_at':NOW},health=lambda:{})
    engine.controller=SimpleNamespace(state={'positions':{'owned':{}},'instruments':{'owned':{'ticker':'HELD'}}})
    engine.lock=RLock();engine.state={}
    engine._sync_stream_watch()
    assert watched==['HELD','FOCUS'] and queue.eligible_jobs()==[job]
    assert not queue.futures and job['reason']=='STREAM_MATERIAL_CHANGE'


def test_transport_revision_counters_reset_without_replaying_model_claims():
    store=Store();store.pipeline_state=lambda _:{'jobs':{'job':{'key':'job','symbol':'FOCUS','instrument_id':'focus-id',
        'status':'RUNNING','updated_at':NOW.isoformat(),'stream_signaled_revision':500,'stream_evaluated_revision':490}}}
    queue=OpportunityQueue(store,'DRY_RUN',lambda:NOW,executor=SimpleNamespace())
    job=queue.jobs['job']
    assert job['status']=='QUARANTINED' and job['reason']=='ABANDONED_JOB_NO_REPLAY'
    assert 'stream_signaled_revision' not in job and 'stream_evaluated_revision' not in job
