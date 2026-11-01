"""REAL strategy transport: exact M2 permit at POST, never a generic write API."""
from datetime import datetime, timezone
from decimal import Decimal
import json
from urllib.parse import urlsplit
from dataclasses import dataclass
import requests
import re
from webull.trade.request.v3.place_order_request import PlaceOrderRequest
from webull.trade.request.v3.get_order_detail_request import OrderDetailRequest
from webull.trade.request.v3.cancel_order_request import CancelOrderRequest
from webull.trade.request.get_activities_request_v2 import GetActivitiesRequestV2
from .real_preflight import RealReadOnlyHttp, REAL_READ_PATHS
from .real_state import binding
from .sandbox.http import HISTORY_BATCH_PATH,SYMBOL_RE
from .sandbox.quiet import quiet_sdk
from .real_read_budget import shared_budget

EXTRA_READS=frozenset(('/trading/instruments/stocks/profiles/list','/market-data/stocks/bars/get',
    '/market-data/screeners/top-actives/list','/market-data/screeners/gainers-losers/list','/trading/orders/get'))
CASH_ACTIVITY_PATH='/trading/activities/cash-activities/list'
CASH_ACTIVITY_START_AT=datetime(2026,9,29,14,58,44,tzinfo=timezone.utc)


def _webull_activity_time(value):
    if not isinstance(value,datetime) or value.tzinfo is None:raise ValueError('REAL_CASH_ACTIVITY_TIME')
    value=value.astimezone(timezone.utc)
    return value.strftime('%Y-%m-%dT%H:%M:%S.')+f'{value.microsecond//1000:03d}Z'


CASH_ACTIVITY_START=_webull_activity_time(CASH_ACTIVITY_START_AT)


def _bar_count(value,low,high):
    """Whole number in [low,high]; the SDK accepts int or digit-string counts."""
    if isinstance(value,bool):return False
    if isinstance(value,str):value=int(value) if value.isdigit() and len(value)<=4 else None
    return isinstance(value,int) and low<=value<=high


def _read_only_history_batch(request):
    """Exact read-only bar shapes the market source builds: M1 (real-time, 5..400) or D (1..250)."""
    parsed=urlsplit(request.get_url());body=request.get_body_params() or {};symbols=body.get('symbols')
    if not (request.get_method()=='POST' and not parsed.scheme and not parsed.netloc
            and parsed.path==HISTORY_BATCH_PATH
            and isinstance(symbols,list) and 1<=len(symbols)<=10 and len(symbols)==len(set(symbols))
            and all(isinstance(symbol,str) and SYMBOL_RE.fullmatch(symbol) for symbol in symbols)
            and body.get('category')=='US_STOCK' and 'count' in body):return False
    keys=set(body)
    if body.get('timespan')=='M1':
        return (keys=={'symbols','category','timespan','count','real_time_required'}
            and _bar_count(body['count'],5,400) and body['real_time_required'] is True)
    if body.get('timespan')=='D':
        return (keys in ({'symbols','category','timespan','count'},{'symbols','category','timespan','count','real_time_required'})
            and _bar_count(body['count'],1,250) and body.get('real_time_required',True) is True)
    return False


def _cash_activity_request(request,account_id,end_time,cursor=None):
    parsed=urlsplit(request.get_url());query=request.get_query_params() or {}
    expected={'account_id':account_id,'start_time':CASH_ACTIVITY_START,'end_time':end_time}
    if cursor is not None:expected['pagination_key']=cursor
    return (request.get_method()=='GET' and not parsed.scheme and not parsed.netloc
        and parsed.path==CASH_ACTIVITY_PATH and request.get_endpoint() in (None,'api.webull.com')
        and not request.get_body_params() and query==expected)


@dataclass(frozen=True)
class RealWriteAuthority:
    account_id: str
    intent: object
    controller: object
    gate: object
    packet: object
    nonce: str | None
    order: dict | None
    exit_reason: str | None=None

    def consume(self,request):
        from .real_trading import RealGate, RealController
        from .contracts import OrderIntent
        if (type(self.gate) is not RealGate or type(self.controller) is not RealController
                or type(self.intent) is not OrderIntent or self.controller.ledger.execution_scope!='REAL'
                or self.controller.ledger.config.schema!='real_trader'
                or self.intent.environment!='REAL' or binding(self.account_id)!=self.intent.account_id
                or self.controller.account_id!=self.intent.account_id or self.gate.account!=self.intent.account_id):
            raise ValueError('REAL_WRITE_AUTHORITY_REQUIRED')
        path=urlsplit(request.get_url()).path
        if self.intent.side=='BUY' and self.order is not None:
            self.gate.check(self.packet,self.controller.clock())
        elif self.intent.side=='SELL_OWNED' and self.order is not None:
            from .real_trading import RealOwnershipPacket,RealAccountOrderPacket
            from .real_strategy_gateway import QUOTE_FREE_EXIT_REASONS
            if isinstance(self.packet,(RealOwnershipPacket,RealAccountOrderPacket)) and self.exit_reason not in QUOTE_FREE_EXIT_REASONS:
                raise ValueError('REAL_QUOTE_REQUIRED_FOR_EXIT')
            self.gate.check_exit(self.packet,self.controller.clock(),self.intent,self.controller.state)
        elif self.order is None and self.exit_reason is not None:
            from .real_strategy_gateway import QUOTE_FREE_EXIT_REASONS,SAFETY_CANCEL_REASONS
            # Before a safety exit: cancel the owned buys, or a resting planned-exit LIMIT sell whose
            # reservation would otherwise hold the shares the safety MARKET sell needs.
            buy_cancel=self.intent.side=='BUY' and self.exit_reason in QUOTE_FREE_EXIT_REASONS
            planned_sell_cancel=(self.intent.side=='SELL_OWNED' and self.intent.order_type=='LIMIT'
                and self.exit_reason in SAFETY_CANCEL_REASONS)
            if not (buy_cancel or planned_sell_cancel) or self.packet.instrument_id!=self.intent.instrument_id:
                raise ValueError('REAL_EXIT_CANCEL_AUTHORITY_REQUIRED')
            self.gate.check_buy_cancel(self.packet,self.controller.clock(),self.controller.state)
        else:
            self.gate.check_recovery(self.packet,self.controller.clock())
        state=self.controller.state
        order=state['orders'][self.intent.intent_id]
        if order['intent']!=self.intent.model_dump(mode='json') or order['final']:raise ValueError('REAL_INTENT_BINDING')
        if self.order is not None:
            expected=dict(client_order_id=self.intent.client_order_id,combo_type='NORMAL',
                symbol=state['instruments'][self.intent.instrument_id]['ticker'],instrument_type='EQUITY',market='US',
                order_type=self.intent.order_type,quantity=str(self.intent.quantity),
                side='BUY' if self.intent.side=='BUY' else 'SELL',time_in_force='DAY',support_trading_session='CORE',entrust_type='QTY')
            if self.intent.limit_price_usd is not None:expected['limit_price']=str(self.intent.limit_price_usd)
            if (path!='/trading/orders/place' or self.order!=expected
                    or request.get_body_params()!={'account_id':self.account_id,'new_orders':[expected]}):
                raise ValueError('REAL_WRITE_ROUTE_OR_BODY')
            permit=state['permits'].get(self.nonce)
            if not permit or permit['environment']!='REAL' or permit['account_id']!=self.intent.account_id:
                raise ValueError('REAL_PERMIT_BINDING')
            if self.intent.side=='BUY':self.packet.validate(self.controller.clock(),entry=True)
            else:self.packet.validate(self.controller.clock())
            self.controller.consume(self.intent,self.nonce)
        else:
            mandatory=self.exit_reason is not None
            if (path!='/trading/orders/cancel' or (not mandatory and self.controller.clock()<self.intent.deadline)
                    or order['phase'] not in ('ACKNOWLEDGED','PARTIALLY_FILLED')
                    or request.get_body_params()!={'account_id':self.account_id,'client_order_id':self.intent.client_order_id}):
                raise ValueError('REAL_CANCEL_AUTHORITY_REQUIRED')
            from .durable.models import OrderObservation
            self.controller.send(OrderObservation(intent_id=self.intent.intent_id,status='CANCEL_PENDING',
                evidence_id='real-cancel-'+self.intent.intent_id))


def _lab_history_batch(request):
    """Strategy-lab only (owner-approved 2026-10-19): read-only DAILY bars with an explicit time window, for
    backtests. Same endpoint and symbol rules as the trader's reads; adds only start_time/end_time (ms). Used by
    scripts/strategy_lab.py through read_lab_history; the trading paths never accept this shape."""
    parsed=urlsplit(request.get_url());body=request.get_body_params() or {};symbols=body.get('symbols')
    if not (request.get_method()=='POST' and not parsed.scheme and not parsed.netloc
            and parsed.path==HISTORY_BATCH_PATH
            and isinstance(symbols,list) and 1<=len(symbols)<=10 and len(symbols)==len(set(symbols))
            and all(isinstance(symbol,str) and SYMBOL_RE.fullmatch(symbol) for symbol in symbols)
            and body.get('category')=='US_STOCK' and body.get('timespan')=='D'
            and 'count' in body and _bar_count(body['count'],1,250)):return False
    keys=set(body)
    window=keys&{'start_time','end_time'}
    if not window or not keys<={'symbols','category','timespan','count','start_time','end_time'}:return False
    for name in window:
        value=body[name]
        if isinstance(value,bool) or not isinstance(value,int) or not 0<value<10**14:return False
    return True


class RealStrategyHttp:
    def __init__(self,credentials):
        self.client=RealReadOnlyHttp(credentials);self.last_received_at=None
        self.read_budget=shared_budget(credentials.app_key)

    @quiet_sdk()
    def read(self,request):
        parsed=urlsplit(request.get_url())
        read_only_post=_read_only_history_batch(request)
        if ((request.get_method()!='GET' or parsed.scheme or parsed.netloc
                or parsed.path not in REAL_READ_PATHS|EXTRA_READS or request.get_body_params()) and not read_only_post):
            raise ValueError('REAL_READ_ROUTE_REQUIRED')
        return self._send(request,read_only_post=read_only_post)

    @quiet_sdk()
    def read_lab_history(self,request):
        """Strategy lab only: read-only daily bars with a time window (see _lab_history_batch)."""
        if not _lab_history_batch(request):raise ValueError('REAL_LAB_HISTORY_ROUTE_REQUIRED')
        return self._send(request,read_only_post=True,lab_history=True)

    def cash_activity_pages(self,account_id,end_at,*,max_pages=10):
        if (not isinstance(account_id,str) or not account_id or end_at.tzinfo is None
                or end_at.astimezone(timezone.utc)<=CASH_ACTIVITY_START_AT
                or not isinstance(max_pages,int) or not 1<=max_pages<=10):
            raise ValueError('REAL_CASH_ACTIVITY_BOUNDARY')
        end_time=_webull_activity_time(end_at)
        cursor=None;seen=set();rows=[]
        for page in range(max_pages):
            request=GetActivitiesRequestV2();request.set_account_id(account_id)
            request.set_start_time(CASH_ACTIVITY_START);request.set_end_time(end_time)
            if cursor is not None:request.set_pagination_key(cursor)
            if not _cash_activity_request(request,account_id,end_time,cursor):
                raise ValueError('REAL_CASH_ACTIVITY_ROUTE')
            body=self._send(request)
            if not isinstance(body,dict) or not set(body).issubset({'data','pagination_key'}) or 'data' not in body:
                raise ValueError('REAL_CASH_ACTIVITY_SHAPE')
            data=body['data'];next_cursor=body.get('pagination_key')
            if not isinstance(data,list):raise ValueError('REAL_CASH_ACTIVITY_SHAPE')
            rows.extend(data)
            if next_cursor in (None,''):return rows,page+1,end_time
            if (not isinstance(next_cursor,str) or len(next_cursor)>2048 or next_cursor in seen
                    or page+1==max_pages):raise ValueError('REAL_CASH_ACTIVITY_PAGINATION')
            seen.add(next_cursor);cursor=next_cursor
        raise ValueError('REAL_CASH_ACTIVITY_PAGINATION')

    def _send(self,request,*,authorize=None,read_only_post=False,lab_history=False):
        parsed=urlsplit(request.get_url())
        if parsed.scheme or parsed.netloc or request.get_endpoint() not in (None,'api.webull.com'):
            raise ValueError('REAL_ENDPOINT_REQUIRED')
        if request.get_method()=='POST' and type(authorize) is not RealWriteAuthority:
            if not read_only_post or not (_read_only_history_batch(request)
                    or (lab_history and _lab_history_batch(request))):raise ValueError('REAL_WRITE_CAPABILITY_REQUIRED')
        if read_only_post and authorize is not None:raise ValueError('REAL_READ_ROUTE_REQUIRED')
        if request.get_method() not in ('GET','POST'):raise ValueError('REAL_ROUTE_REJECTED')
        if authorize is None:
            self.read_budget.acquire(parsed.path,urgent=getattr(request,'_aitrader_urgent_read',False))
        wire=self.client._make_http_response('api.webull.com',request,10,5)
        try:
            with requests.Session() as session:
                session.trust_env=False
                prepared=session.prepare_request(requests.Request(request.get_method(),'https://api.webull.com'+wire.get_url(),
                    headers=wire.get_headers(),data=wire.get_body()))
                if authorize:authorize.consume(request)
                with session.send(prepared,allow_redirects=False,verify=True,timeout=(5,10),stream=True) as response:
                    if response.status_code==429 and authorize is None:
                        raise self.read_budget.rejected(parsed.path,response.headers.get('Retry-After'))
                    if response.status_code!=200:
                        try:
                            problem=response.json();code=str(problem.get('code') or problem.get('error_code') or 'REJECTED')
                            code=re.sub(r'[^A-Z0-9_\-]','_',code.upper())[:80]
                        except Exception:code='REJECTED'
                        raise ValueError(f'REAL_HTTP_{response.status_code}_{code}')
                    size=0;chunks=[]
                    for chunk in response.iter_content(65536):
                        size+=len(chunk)
                        if size>2_000_000:raise ValueError('REAL_RESPONSE_UNCERTAIN')
                        chunks.append(chunk)
                    value=json.loads(b''.join(chunks),parse_float=Decimal,
                        parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
                    self.last_received_at=datetime.now(timezone.utc)
                    return value
        except ValueError:raise
        except Exception:raise ValueError('REAL_TRANSPORT_UNCERTAIN_NO_RETRY') from None


class RealStrategyTransport:
    def __init__(self,credentials,account_id):
        self._account=account_id;self.http=RealStrategyHttp(credentials)

    def submit(self,intent,symbol,controller,nonce,packet,gate,*,exit_reason=None):
        if (controller.ledger.execution_scope!='REAL' or controller.ledger.config.schema!='real_trader'
                or intent.environment!='REAL' or intent.account_id!=binding(self._account)
                or controller.account_id!=intent.account_id or gate.account!=intent.account_id):
            raise ValueError('REAL_TRANSPORT_BINDING')
        if intent.order_type not in ('LIMIT','MARKET') or (intent.order_type=='MARKET' and intent.side!='SELL_OWNED'):
            raise ValueError('REAL_ORDER_TYPE_UNSUPPORTED')
        if symbol!=controller.state['instruments'][intent.instrument_id]['ticker']:raise ValueError('REAL_SYMBOL_BINDING')
        order=dict(client_order_id=intent.client_order_id,combo_type='NORMAL',symbol=symbol,instrument_type='EQUITY',
            market='US',order_type=intent.order_type,quantity=str(intent.quantity),side='BUY' if intent.side=='BUY' else 'SELL',
            time_in_force='DAY',support_trading_session='CORE',entrust_type='QTY')
        if intent.limit_price_usd is not None:order['limit_price']=str(intent.limit_price_usd)
        request=PlaceOrderRequest();request.set_account_id(self._account);request.set_new_orders([order]);request.add_custom_headers_from_order([order])
        authorize=RealWriteAuthority(self._account,intent,controller,gate,packet,nonce,order,exit_reason)
        return self.http._send(request,authorize=authorize)

    def detail(self,client_id):
        request=OrderDetailRequest();request.set_account_id(self._account);request.set_client_order_id(client_id)
        return self.http.read(request)

    def cancel_expired(self,intent,controller,gate,packet):
        # Exactly one cancellation attempt for a known locally dispatched order.
        state=controller.state;order=state['orders'][intent.intent_id]
        if (controller.ledger.execution_scope!='REAL' or controller.account_id!=binding(self._account)
                or order['intent']!=intent.model_dump(mode='json') or order['final']
                or controller.clock()<intent.deadline or order['phase'] not in ('ACKNOWLEDGED','PARTIALLY_FILLED')):
            raise ValueError('REAL_CANCEL_AUTHORITY_REQUIRED')
        request=CancelOrderRequest();request.set_account_id(self._account);request.set_client_order_id(intent.client_order_id)
        authorize=RealWriteAuthority(self._account,intent,controller,gate,packet,None,None)
        return self.http._send(request,authorize=authorize)

    def cancel_planned_sell_for_safety(self,intent,controller,gate,packet,reason):
        from .real_strategy_gateway import SAFETY_CANCEL_REASONS
        state=controller.state;order=state['orders'][intent.intent_id]
        if (reason not in SAFETY_CANCEL_REASONS or intent.side!='SELL_OWNED' or intent.order_type!='LIMIT'
                or intent.instrument_id!=packet.instrument_id or order['final']
                or order['phase'] not in ('ACKNOWLEDGED','PARTIALLY_FILLED')):
            raise ValueError('REAL_EXIT_CANCEL_AUTHORITY_REQUIRED')
        request=CancelOrderRequest();request.set_account_id(self._account);request.set_client_order_id(intent.client_order_id)
        authorize=RealWriteAuthority(self._account,intent,controller,gate,packet,None,None,reason)
        return self.http._send(request,authorize=authorize)

    def cancel_owned_buy_for_exit(self,intent,controller,gate,packet,reason):
        from .real_strategy_gateway import QUOTE_FREE_EXIT_REASONS
        state=controller.state;order=state['orders'][intent.intent_id]
        if (reason not in QUOTE_FREE_EXIT_REASONS or intent.side!='BUY'
                or intent.instrument_id!=packet.instrument_id or order['final']
                or order['phase'] not in ('ACKNOWLEDGED','PARTIALLY_FILLED')):
            raise ValueError('REAL_EXIT_CANCEL_AUTHORITY_REQUIRED')
        request=CancelOrderRequest();request.set_account_id(self._account);request.set_client_order_id(intent.client_order_id)
        authorize=RealWriteAuthority(self._account,intent,controller,gate,packet,None,None,reason)
        return self.http._send(request,authorize=authorize)
