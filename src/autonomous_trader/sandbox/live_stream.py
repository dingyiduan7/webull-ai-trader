"""Bounded read-only Webull MQTT cache for the shortlist and owned symbols.

The stream carries market evidence only.  It has no ledger, risk, order, or
execution handle, and a local receipt timestamp can never replace broker source
time for entry authority.
"""
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from math import floor, log
from threading import Event, RLock, Thread
import time
from uuid import uuid4

from google.protobuf.json_format import MessageToDict
from webull.data.data_streaming_client import DataStreamingClient
from webull.data.quotes.subscribe import message_pb2 as market_pb

from .http import HTTP_HOST, MQTT_HOST, SYMBOL_RE, SandboxHttp, MAX_STREAM_SYMBOLS
from .streams import ProtobufObservationDecoder


WINDOW_SECONDS=(1,5,10,60)
MAX_WATCH_SYMBOLS=MAX_STREAM_SYMBOLS
MAX_ENTRY_AGE=timedelta(seconds=2)


def _decimal(value):
    try:
        result=Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation,TypeError,ValueError):return None


class RollingMarketCache:
    """Thread-safe source-time event cache with transport epoch invalidation."""
    def __init__(self,clock=lambda:datetime.now(timezone.utc)):
        self.clock=clock;self.lock=RLock();self.connected=False;self.epoch=0
        self.events=defaultdict(lambda:deque(maxlen=4096));self.quotes={};self.snapshots={}
        self.revisions=defaultdict(int);self.raw_events=defaultdict(int);self.material_signatures={}
        self.last_error=None;self.last_received_at=None

    def connection_opened(self):
        with self.lock:self.connected=True;self.epoch+=1;self.last_error=None

    def connection_lost(self,reason='STREAM_DISCONNECTED'):
        with self.lock:self.connected=False;self.epoch+=1;self.last_error=reason

    def connection_idle(self):
        with self.lock:self.connected=False;self.epoch+=1;self.last_error=None

    @staticmethod
    def _source_at(basic):
        raw=basic.get('timestamp') if isinstance(basic,dict) else None
        try:return datetime.fromtimestamp(int(raw)/1000,timezone.utc)
        except (TypeError,ValueError,OverflowError,OSError):return None

    def ingest(self,topic,payload,received_at=None):
        received_at=received_at or self.clock()
        if received_at.tzinfo is None:raise ValueError('STREAM_RECEIPT_TZ_REQUIRED')
        if topic not in ('quote','snapshot','tick') or not isinstance(payload,dict):return False
        basic=payload.get('basic');source_at=self._source_at(basic)
        symbol=basic.get('symbol') if isinstance(basic,dict) else None
        instrument_id=basic.get('instrument_id') if isinstance(basic,dict) else None
        if (not isinstance(symbol,str) or not SYMBOL_RE.fullmatch(symbol) or not instrument_id
                or source_at is None or source_at>received_at+timedelta(seconds=2)):return False
        bid=ask=bid_size=ask_size=price=volume=None
        if topic=='quote':
            bids=payload.get('bids') or ();asks=payload.get('asks') or ()
            if bids and asks:
                bid=_decimal(bids[0].get('price'));ask=_decimal(asks[0].get('price'))
                bid_size=_decimal(bids[0].get('size'));ask_size=_decimal(asks[0].get('size'))
                if bid is None or ask is None or not 0<bid<=ask:return False
                price=(bid+ask)/2
        elif topic=='tick':
            price=_decimal(payload.get('price'));volume=_decimal(payload.get('volume'))
            if price is None or price<=0:return False
        else:
            price=_decimal(payload.get('price') or payload.get('ext_price') or payload.get('ovn_price'))
            volume=_decimal(payload.get('volume') or payload.get('ext_volume') or payload.get('ovn_volume'))
        with self.lock:
            if not self.connected:return False
            event={'topic':topic,'symbol':symbol,'instrument_id':str(instrument_id),'source_at':source_at,
                'received_at':received_at,'epoch':self.epoch,'price':price,'volume':volume,
                'bid':bid,'ask':ask,'bid_size':bid_size,'ask_size':ask_size}
            if topic=='quote':
                old=self.quotes.get(symbol)
                if old is None or source_at>=old['source_at']:self.quotes[symbol]=event
            elif topic=='snapshot':
                old=self.snapshots.get(symbol)
                if old is None or source_at>=old['source_at']:self.snapshots[symbol]=event
            self.events[symbol].append(event);cutoff=source_at-timedelta(seconds=60)
            while self.events[symbol] and self.events[symbol][0]['source_at']<cutoff:self.events[symbol].popleft()
            # This timestamp is deliberately separate from broker source time
            # and transport receipt time.  It proves when the rolling evidence
            # became visible to the decision loop without granting freshness.
            event['window_updated_at']=max(received_at,self.clock())
            self.raw_events[symbol]+=1
            material=self._signature_locked(symbol,received_at)
            if material!=self.material_signatures.get(symbol):
                self.material_signatures[symbol]=material;self.revisions[symbol]+=1
            self.last_received_at=received_at
        return True

    def revision(self,symbol):
        with self.lock:return self.revisions.get(symbol,0)

    def fresh_quote(self,symbol,now=None):
        now=now or self.clock()
        with self.lock:
            row=self.quotes.get(symbol)
            if (not self.connected or row is None or row['epoch']!=self.epoch
                    or not timedelta(0)<=now-row['source_at']<=MAX_ENTRY_AGE
                    or not timedelta(0)<=now-row['received_at']<=MAX_ENTRY_AGE):return None
            snapshot=self.snapshots.get(symbol)
            return {**row,'volume':snapshot.get('volume') if snapshot and snapshot['epoch']==self.epoch else None,
                'windows':self._windows_locked(symbol,now),'material_signature':self._signature_locked(symbol,now),
                'window_updated_at':row['window_updated_at']}

    def _windows_locked(self,symbol,now):
        result={}
        rows=[x for x in self.events.get(symbol,()) if x['epoch']==self.epoch and x['source_at']<=now]
        for seconds in WINDOW_SECONDS:
            window=[x for x in rows if x['source_at']>=now-timedelta(seconds=seconds)]
            prices=[x['price'] for x in window if x['price'] is not None]
            ticks=[x for x in window if x['topic']=='tick' and x['volume'] is not None]
            quotes=[x for x in window if x['topic']=='quote']
            first,last=(prices[0],prices[-1]) if prices else (None,None)
            change=None if first is None or first==0 else ((last-first)/first*10000).quantize(Decimal('.001'))
            latest_quote=quotes[-1] if quotes else None
            result[str(seconds)]={'source_start_at':window[0]['source_at'].isoformat() if window else None,
                'source_end_at':window[-1]['source_at'].isoformat() if window else None,'event_count':len(window),
                'first_price':None if first is None else str(first),'last_price':None if last is None else str(last),
                'high_price':None if not prices else str(max(prices)),'low_price':None if not prices else str(min(prices)),
                'change_bps':None if change is None else str(change),
                'tick_volume':str(sum((x['volume'] for x in ticks),Decimal(0))),
                'bid':None if latest_quote is None else str(latest_quote['bid']),
                'ask':None if latest_quote is None else str(latest_quote['ask']),
                'bid_size':None if latest_quote is None or latest_quote['bid_size'] is None else str(latest_quote['bid_size']),
                'ask_size':None if latest_quote is None or latest_quote['ask_size'] is None else str(latest_quote['ask_size'])}
        return result

    def _signature_locked(self,symbol,now):
        windows=self._windows_locked(symbol,now)
        def bucket(value,step):
            if value is None:return None
            return int(Decimal(value)/Decimal(step))
        quote=self.quotes.get(symbol)
        def log_bucket(value,scale):
            return None if value is None or value<=0 else floor(log(float(value))*scale)
        absolute=('quote',None,None,None,None,None)
        if quote is not None and quote['epoch']==self.epoch:
            spread=(quote['ask']-quote['bid'])/quote['bid']*10000
            absolute=('quote',log_bucket(quote['bid'],400),log_bucket(quote['ask'],400),
                floor(float(spread)/2),log_bucket(quote['bid_size'],10),log_bucket(quote['ask_size'],10))
        return (absolute,)+tuple((seconds,bucket(windows[str(seconds)]['change_bps'],'10' if seconds<=5 else '25'),
            bucket(windows[str(seconds)]['tick_volume'],'100')) for seconds in WINDOW_SECONDS)

    def health(self):
        with self.lock:return {'connected':self.connected,'epoch':self.epoch,'last_error':self.last_error,
            'last_received_at':self.last_received_at.isoformat() if self.last_received_at else None,
            'symbols':sorted(k for k,v in self.quotes.items() if v['epoch']==self.epoch),
            'raw_events':{k:v for k,v in self.raw_events.items() if k in self.quotes and self.quotes[k]['epoch']==self.epoch},
            'material_revisions':{k:v for k,v in self.revisions.items() if k in self.quotes and self.quotes[k]['epoch']==self.epoch}}


class LiveQuoteStream:
    """One reconnecting SDK connection for a bounded live watch set."""
    def __init__(self,credentials,*,clock=lambda:datetime.now(timezone.utc),client_factory=DataStreamingClient,on_material=lambda:None):
        self.credentials=credentials;self.clock=clock;self.client_factory=client_factory
        self.cache=RollingMarketCache(clock);self.lock=RLock();self.desired=();self.subscribed=()
        self.stop_event=Event();self.quote_event=Event();self.thread=None;self.rotations=0;self.subscription_calls=0;self.on_material=on_material

    def start(self):
        if self.thread and self.thread.is_alive():return
        self.stop_event.clear();self.thread=Thread(target=self._run,name='webull-hot-stream',daemon=True);self.thread.start()

    def close(self):
        self.stop_event.set()
        if self.thread:self.thread.join(timeout=5)

    def watch(self,symbols):
        symbols=tuple(dict.fromkeys(symbols))
        if len(symbols)>MAX_WATCH_SYMBOLS or any(not isinstance(x,str) or not SYMBOL_RE.fullmatch(x) for x in symbols):
            raise ValueError('INVALID_STREAM_WATCH_SET')
        with self.lock:self.desired=symbols

    def revision(self,symbol):return self.cache.revision(symbol)
    def fresh_quote(self,symbol,now=None):return self.cache.fresh_quote(symbol,now)
    def wait_fresh_quote(self,symbol,timeout=1.0):
        """Wait for actual source-fresh BBO arrival, never re-date old data."""
        deadline=time.monotonic()+timeout
        while symbol in self.active() and not self.stop_event.is_set():
            self.quote_event.clear()
            row=self.fresh_quote(symbol)
            if row is not None:return row
            remaining=deadline-time.monotonic()
            if remaining<=0:return None
            self.quote_event.wait(remaining)
        return None
    def active(self):
        with self.lock:return self.desired

    def health(self):
        with self.lock:extra={'desired':list(self.desired),'subscribed':list(self.subscribed),
            'rotations':self.rotations,'subscription_calls':self.subscription_calls}
        return {**self.cache.health(),**extra}

    def _new_client(self):
        client=self.client_factory(self.credentials.app_key,self.credentials.app_secret,'us',uuid4().hex,
            http_host=HTTP_HOST,mqtt_host=MQTT_HOST,mqtt_port=1883,tls_enable=True)
        client._api_client=SandboxHttp(self.credentials)
        client.disable_logger()
        client.register_payload_decoder('snapshot',ProtobufObservationDecoder(market_pb.Snapshot))
        client.register_payload_decoder('quote',ProtobufObservationDecoder(market_pb.Quote))
        client.register_payload_decoder('tick',ProtobufObservationDecoder(market_pb.Tick))
        return client

    def _run(self):
        while not self.stop_event.is_set():
            if not self.active():
                self.stop_event.wait(.5);continue
            client=self._new_client();connected=False;subscribe_ack=[False]
            def on_connect(c,*_):
                nonlocal connected
                connected=True;self.cache.connection_opened()
            def on_message(_client,topic,payload):
                data=MessageToDict(payload,preserving_proto_field_name=True) if hasattr(payload,'DESCRIPTOR') else payload
                symbol=(data.get('basic') or {}).get('symbol') if isinstance(data,dict) else None
                before=self.cache.revision(symbol)
                accepted=self.cache.ingest(topic,data,self.clock())
                if accepted and topic=='quote':self.quote_event.set()
                if self.cache.revision(symbol)>before:self.on_material()
            def on_subscribe(*_):subscribe_ack[0]=True
            client.on_connect_success=on_connect;client.on_quotes_message=on_message
            client.on_subscribe_success=on_subscribe
            idle=False
            try:
                client._quotes_connect(MQTT_HOST,1883)
                while not self.stop_event.is_set():
                    rc=client.loop(timeout=.25)
                    if rc:raise ValueError('STREAM_LOOP_FAILED')
                    if not connected:continue
                    with self.lock:desired=self.desired;subscribed=self.subscribed
                    if not desired and not subscribed:
                        idle=True;break
                    if desired==subscribed:continue
                    removed=[s for s in subscribed if s not in desired]
                    added=[s for s in desired if s not in subscribed]
                    if removed:
                        client.unsubscribe(removed,'US_STOCK',['QUOTE','SNAPSHOT','TICK'])
                        with self.lock:self.subscription_calls+=1
                    if added:
                        subscribe_ack[0]=False
                        client.subscribe(added,'US_STOCK',['QUOTE','SNAPSHOT','TICK'])
                        if not subscribe_ack[0]:raise ValueError('STREAM_SUBSCRIBE_REJECTED')
                        with self.lock:self.subscription_calls+=1
                    with self.lock:self.subscribed=desired;self.rotations+=1
            except Exception:
                self.cache.connection_lost('STREAM_RECONNECTING')
            finally:
                with self.lock:self.subscribed=()
                try:client.disconnect();client._sock_close()
                except Exception:pass
                if idle:self.cache.connection_idle()
            if not self.stop_event.wait(3):continue
