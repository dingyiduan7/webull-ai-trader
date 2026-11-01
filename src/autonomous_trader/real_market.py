"""Current Webull-native REAL portfolio packet; no model or order authority."""
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
from uuid import uuid4
import json
import re
from .carry import exit_horizon
from .real_strategy_broker import RealStrategyHttp
from .real_preflight import select_single_individual_cash
from .real_state import binding
from .real_trading import RealPacket,RealExitPacket,RealOwnershipPacket,RealAccountOrderPacket
from .sandbox.http import build_request,Operation,SYMBOL_RE
from .mvp.context import listed_equity,instrument
from .mvp.config import reviewed_calendar
from .reference import NEW_YORK
from .risk import POLICY
from .contracts import AlphaContext,AlphaInstrumentContext,AlphaBar,AlphaPositionContext


REAL_COST_EVIDENCE = "REAL cost evidence, verified 2026-10-02. Expected broker commission: documented $0 for US-exchange-listed stocks/ETFs using this gateway's standard NORMAL whole-share LIMIT orders; not specialized algo order types. Webull OpenAPI FAQ says API trading fees match the app, with no additional API access charge. Sources: https://developer.webull.com/apis/docs/faq/ and https://www.webull.com/pricing . Regulatory/exchange pass-through amounts, rounding and instrument-specific ADR/depositary charges are not calibrated here; unknown is not zero. Specialized algo fees are not a rate for these standard orders. Spread is observable in bid/ask; slippage remains uncertain. Known model costs reduce net economics. Separately, $1 + $0.02/share per side is an internal conservative cash/risk stress reserve, NOT expected charged broker expense. It still constrains admission/available capital; do not subtract that synthetic reserve as an expected broker fee. Actual realized fees remain those reported by the broker; this evidence does not change ledger accounting or reserves."

def _bind_profiles(body,group,profiles,ambiguous):
    if not isinstance(body,dict) or not isinstance(body.get('data'),list):raise ValueError('REAL_PROFILE_SHAPE')
    for profile in body['data']:
        symbol=profile.get('symbol')
        if symbol not in group or symbol in ambiguous:continue
        if symbol in profiles:
            profiles.pop(symbol);ambiguous.add(symbol);continue
        profiles[symbol]=profile


def _single_batch_bar_rows(body,symbol,instrument_id):
    groups=body.get('result') if isinstance(body,dict) else None
    if not isinstance(groups,list) or len(groups)!=1 or groups[0].get('symbol')!=symbol:
        raise ValueError('REAL_HISTORY_BATCH_IDENTITY')
    group=groups[0]
    if group.get('instrument_id') is not None and str(group['instrument_id'])!=str(instrument_id):
        raise ValueError('REAL_HISTORY_BATCH_IDENTITY')
    rows=group.get('result')
    if not isinstance(rows,list):raise ValueError('REAL_HISTORY_BATCH_SHAPE')
    return rows


NASDAQ_LISTINGS=('NAS','NMS','NSQ')
# NYSE, NYSE American, NYSE Arca (owner-approved 2026-10-21). Every quote must still prove itself fresh
# (<= 10 s source age) and carry fresh session bars, so a delayed listing never reaches a decision.
NYSE_LISTINGS=('NYSE','ASE','PSE')
TRADABLE_LISTINGS=NASDAQ_LISTINGS+NYSE_LISTINGS


def _candidate_prices(quote, *, held=False):
    try:
        bid,ask=Decimal(str(quote['bid'])),Decimal(str(quote['ask']))
        if not (bid.is_finite() and ask.is_finite() and 0<bid<=ask):raise ValueError()
        return bid,ask
    except (KeyError,TypeError,ValueError,InvalidOperation):
        if held:raise ValueError('INVALID_REAL_QUOTE') from None
        return None


# Liquidity floor for NEW entries (held names are never filtered). Sub-$2 and very wide-spread
# names halt, gap through stops and cost more in spread than a normal target pays.
MIN_ENTRY_PRICE=Decimal('2');MAX_ENTRY_SPREAD_BPS=Decimal('100')


def _liquid_enough(bid,ask):
    return (bid+ask)/2>=MIN_ENTRY_PRICE and (ask-bid)/bid*Decimal(10000)<=MAX_ENTRY_SPREAD_BPS


def _spread_bps(bid,ask):
    return ((ask-bid)/bid*Decimal(10000)).quantize(Decimal('0.000001'))


def _quote_is_fresh(quote,received_at,now,limit_seconds=10):
    try:quote_at=datetime.fromtimestamp(int(quote['quote_time'])/1000,timezone.utc)
    except (KeyError,TypeError,ValueError,OverflowError):return False
    if limit_seconds is None:limit_seconds=10
    return (0<=(now-quote_at).total_seconds()<=limit_seconds
        and 0<=(now-received_at).total_seconds()<=limit_seconds)


def _quote_limit(market_authority):
    return 10


def snapshot_request(symbols,*,urgent=False):
    """Twenty-symbol REAL snapshot batch observed on 2026-10-05; no claim for other endpoints."""
    from webull.data.request.get_snapshot_request import GetSnapshotRequest
    symbols=tuple(symbols)
    if not 1<=len(symbols)<=20 or len(set(symbols))!=len(symbols) or not all(SYMBOL_RE.fullmatch(s) for s in symbols):
        raise ValueError('REAL_SNAPSHOT_SYMBOLS_INVALID')
    request=GetSnapshotRequest();request.set_symbols(list(symbols));request.set_category('US_STOCK')
    request._aitrader_urgent_read=urgent
    return request


BAR_REFRESH_SECONDS=120
MAX_SESSION_BARS=400
MARKET_SYMBOL='SPY'


def _bars_needed(cached,session,now):
    """1-minute bars to request: missing minutes plus overlap, bounded by the SDK maximum."""
    since=cached[-1].at if cached else session.opens_at
    return max(5,min(MAX_SESSION_BARS,int((now-since).total_seconds()//60)+3))


class RealMarketSource:
    def __init__(self,credentials,account_binding,clock=lambda:datetime.now(timezone.utc),market_authority=None):
        self.http=RealStrategyHttp(credentials);self.account_binding=account_binding;self.clock=clock
        self.account_id=None;self.symbols=();self.bar_cache={};self.market_authority=market_authority
        self.profile_cache={};self.focused=False
        self.cash_adjuster=None;self.last_cash_variance=None

    def _adjust_cash(self,cash):
        adjuster=getattr(self,'cash_adjuster',None)
        return adjuster(dict(cash)) if adjuster else cash

    def bind_account(self):
        account=select_single_individual_cash(self.http.read(build_request(Operation.ACCOUNTS)))
        if binding(account['account_id'])!=self.account_binding:raise ValueError('REAL_ACCOUNT_BINDING_MISMATCH')
        self.account_id=account['account_id']
        return self.account_id

    def account_observation(self):
        """Fresh account/order/position facts usable outside the quote session."""
        account=select_single_individual_cash(self.http.read(build_request(Operation.ACCOUNTS)))
        receipts=[self.http.last_received_at]
        if binding(account['account_id'])!=self.account_binding:raise ValueError('REAL_ACCOUNT_BINDING_MISMATCH')
        self.account_id=account['account_id']
        positions=self.http.read(build_request(Operation.POSITIONS,self.account_id));receipts.append(self.http.last_received_at)
        orders=self.http.read(build_request(Operation.OPEN_ORDERS,self.account_id));receipts.append(self.http.last_received_at)
        balance=self.http.read(build_request(Operation.BALANCE,self.account_id));receipts.append(self.http.last_received_at)
        if (not isinstance(positions,list) or not isinstance(orders,dict)
                or not isinstance(orders.get('data'),list) or orders.get('pagination_key')):
            raise ValueError('REAL_ACCOUNT_SHAPE_OR_PAGINATION')
        cash=balance.get('account_currency_assets') if isinstance(balance,dict) else None
        if not isinstance(cash,list) or len(cash)!=1 or cash[0].get('currency')!='USD':
            raise ValueError('REAL_CASH_SHAPE')
        clients=tuple(row.get('client_order_id') for row in orders['data'])
        if any(not isinstance(value,str) or not value for value in clients) or len(clients)!=len(set(clients)):
            raise ValueError('REAL_OPEN_ORDER_IDENTITY')
        return dict(account_binding=self.account_binding,positions=tuple(positions),open_client_ids=clients,
            cash=self._adjust_cash(cash[0]),observed_at=min(receipts),received_at=max(receipts))

    def account_order_packet(self,state,instrument_id):
        """Exact account/order state for cancel-before-liquidation, even while flat."""
        account=self.account_observation()
        if instrument_id not in state['instruments']:raise ValueError('REAL_EXIT_INSTRUMENT_UNKNOWN')
        symbol=state['instruments'][instrument_id]['ticker']
        positions={}
        by_symbol={row['ticker']:iid for iid,row in state['instruments'].items()}
        for row in account['positions']:
            quantity=Decimal(row['quantity'])
            if quantity==0:continue
            if (row.get('instrument_type')!='EQUITY' or quantity<=0 or row.get('symbol') not in by_symbol):
                raise ValueError('REAL_UNEXPECTED_HOLDING')
            iid=by_symbol[row['symbol']]
            if iid in positions:raise ValueError('REAL_DUPLICATE_POSITION')
            positions[iid]=quantity
        cash=account['cash'];settled=Decimal(cash['settled_cash']);unsettled=Decimal(cash['unsettled_cash'])
        if settled+unsettled!=Decimal(cash['cash_balance']):raise ValueError('REAL_CASH_MISMATCH')
        return RealAccountOrderPacket(account_binding=self.account_binding,observed_at=account['observed_at'],
            settled=settled,unsettled=unsettled,equity=Decimal(cash['net_liquidation_value']),positions=positions,
            open_client_ids=account['open_client_ids'],instrument_id=instrument_id,symbol=symbol,
            evidence_id=uuid4().hex)

    def ownership_packet(self,state,instrument_id):
        """Narrow mandatory-exit view: exact account ownership, no quote dependency."""
        packet=self.account_order_packet(state,instrument_id)
        if instrument_id not in packet.positions or packet.positions[instrument_id]<=0:
            raise ValueError('REAL_EXIT_OWNERSHIP_REQUIRED')
        return RealOwnershipPacket(**packet.__dict__)

    def exit_packet(self,state,instrument_id):
        """Stop/model exit view: exact ownership plus one fresh exact broker quote."""
        owned=self.ownership_packet(state,instrument_id)
        body=self.http.read(snapshot_request((owned.symbol,),urgent=True));received=self.http.last_received_at
        if not isinstance(body,list) or len(body)!=1:raise ValueError('REAL_EXIT_QUOTE_SHAPE')
        quote=body[0]
        if quote.get('symbol')!=owned.symbol or str(quote.get('instrument_id'))!=instrument_id:
            raise ValueError('REAL_EXIT_QUOTE_IDENTITY')
        return RealExitPacket(account_binding=owned.account_binding,observed_at=owned.observed_at,
            settled=owned.settled,unsettled=owned.unsettled,equity=owned.equity,positions=owned.positions,
            open_client_ids=owned.open_client_ids,instrument_id=instrument_id,symbol=owned.symbol,
            bid_usd=Decimal(quote['bid']),ask_usd=Decimal(quote['ask']),
            quote_at=datetime.fromtimestamp(int(quote['quote_time'])/1000,timezone.utc),
            quote_received_at=received,evidence_id=uuid4().hex)

    def discover(self):
        now=self.clock();session=reviewed_calendar().session(now.astimezone(NEW_YORK).date())
        if session is None or not session.permits_entry(now):raise ValueError('REAL_DISCOVERY_SESSION_REQUIRED')
        symbols=[];surfaces=[]
        # Existing native surfaces, ranked by the broker. No per-candidate model call.
        for op,rank in ((Operation.TOP_ACTIVE,'VOLUME'),(Operation.TOP_ACTIVE,'RELATIVE_VOLUME_10D'),
                        (Operation.GAINERS_LOSERS,'DAY_1')):
            body=self.http.read(build_request(op,rank_type=rank))
            rows=body if isinstance(body,list) else body.get('data') if isinstance(body,dict) else None
            if not isinstance(rows,list):raise ValueError('REAL_DISCOVERY_SHAPE_UNVERIFIED')
            ranked=[]
            for row in rows:
                symbol=row.get('symbol');price=Decimal(str(row.get('price','0')))
                if isinstance(symbol,str) and SYMBOL_RE.fullmatch(symbol) and price>0 and symbol not in ranked:ranked.append(symbol)
            surfaces.append(ranked)
        # Lab-tested picks first (owner-approved 2026-10-20): they only join the candidates the models see;
        # every eligibility filter, entry rule, risk limit and the two-model consensus still apply.
        from .lab.picks import load as load_lab_picks
        self.lab_picks=load_lab_picks(now,max_price=getattr(self,'last_symbol_cap',None))
        for symbol in self.lab_picks:
            if SYMBOL_RE.fullmatch(symbol) and symbol not in symbols:symbols.append(symbol)
        # Interleave surfaces by broker rank so the bounded shortlist keeps
        # volume, relative-volume and mover evidence instead of one surface.
        for depth in range(max(map(len,surfaces),default=0)):
            for ranked in surfaces:
                if depth<len(ranked) and ranked[depth] not in symbols:symbols.append(ranked[depth])
        # Discovery stays deterministic and free of model fan-out. Affordability
        # is checked against current cash/equity after the account refresh.
        self.symbols=tuple(symbols)
        self.bar_cache={symbol:bars for symbol,bars in self.bar_cache.items() if symbol in symbols}
        if not self.symbols:raise ValueError('NO_AFFORDABLE_REAL_CANDIDATES')
        return self.symbols

    def _refresh_quote(self,symbol,instrument_id,q,received,limit):
        # Retry only an exact stale instrument, not discovery/bars/account.
        # Source time and the existing executable freshness limit remain mandatory.
        for attempt in range(3):
            checked=self.clock()
            self.last_quote_refresh_evidence.append(dict(symbol=symbol,instrument_id=instrument_id,
                attempt=attempt,raw_quote_time=q.get('quote_time'),received_at=received.isoformat(),
                checked_at=checked.isoformat(),maximum_age_seconds=limit,
                fresh=_quote_is_fresh(q,received,checked,limit)))
            if _quote_is_fresh(q,received,checked,limit) or attempt==2:break
            body=self.http.read(snapshot_request((symbol,),urgent=True))
            received=self.http.last_received_at
            if not isinstance(body,list) or len(body)!=1:raise ValueError('REAL_QUOTE_SHAPE')
            q=body[0]
            if q.get('symbol')!=symbol or str(q.get('instrument_id'))!=instrument_id:
                raise ValueError('REAL_PROFILE_QUOTE_BINDING')
        return q,received

    def _refresh_batch_bars(self,refs,session):
        needed=[ref for ref in refs if not self.bar_cache.get(ref.ticker)
            or (self.clock()-self.bar_cache[ref.ticker][-1].at).total_seconds()>BAR_REFRESH_SECONDS]
        # NYSE listings are batched apart (owner-approved 2026-10-21): if real-time bars are refused for them, those
        # symbols are set aside for the session instead of failing the cycle. Nasdaq batches behave as before.
        nasdaq=[ref for ref in needed if getattr(ref,'listing_exchange',None) not in NYSE_LISTINGS]
        nyse=[ref for ref in needed if getattr(ref,'listing_exchange',None) in NYSE_LISTINGS]
        groups=[(nasdaq[i:i+10],False) for i in range(0,len(nasdaq),10)]+[(nyse[i:i+10],True) for i in range(0,len(nyse),10)]
        for group,optional in groups:
            symbols=tuple(ref.ticker for ref in group)
            request=build_request(Operation.HISTORY_BATCH,symbols=symbols);request.set_real_time_required(True)
            # Incremental: the whole session once, then only the missing minutes.
            request.set_count(str(max(_bars_needed(self.bar_cache.get(ref.ticker),session,self.clock()) for ref in group)))
            try:
                body=self.http.read(request)
                rows=body.get('result') if isinstance(body,dict) else None
                if not isinstance(rows,list) or len(rows)!=len(group) or {row.get('symbol') for row in rows}!=set(symbols):
                    raise ValueError('REAL_HISTORY_BATCH_IDENTITY')
            except Exception as exc:
                # Held positions are never set aside: their monitoring must fail loudly, as before.
                if (not optional or type(exc).__name__=='ReadRateLimited'
                        or set(symbols)&set(getattr(self,'held_symbols',()))):raise
                blocked=self.__dict__.setdefault('listing_blocked',set());blocked.update(symbols)
                for symbol in symbols:self.bar_cache.pop(symbol,None)
                continue
            by_symbol={row['symbol']:row for row in rows}
            for ref in group:
                data=_single_batch_bar_rows({'result':[by_symbol[ref.ticker]]},ref.ticker,ref.instrument_id)
                parsed=[]
                for row in data:
                    try:
                        bar=AlphaBar(at=row['time'],open_usd=row['open'],high_usd=row['high'],
                            low_usd=row['low'],close_usd=row['close'],volume=row['volume'])
                        if session.opens_at<=bar.at<=self.clock():parsed.append(bar)
                    except (ValueError,KeyError,TypeError):pass
                merged={bar.at:bar for bar in self.bar_cache.get(ref.ticker,()) if session.opens_at<=bar.at}
                merged.update((bar.at,bar) for bar in parsed)
                self.bar_cache[ref.ticker]=tuple(sorted(merged.values(),key=lambda bar:bar.at)[-MAX_SESSION_BARS:])

    def _refresh_daily(self,refs,session):
        """Prior-session daily bars, once per session per symbol. Evidence only:
        a failed read leaves the symbol without daily features, never blocks."""
        cache=self.daily_cache=getattr(self,'daily_cache',{})
        label=session.label.isoformat()
        needed=[ref for ref in refs if cache.get(ref.ticker,(None,))[0]!=label]
        for start in range(0,len(needed),10):
            group=needed[start:start+10]
            try:
                request=build_request(Operation.HISTORY_DAILY_BATCH,symbols=tuple(ref.ticker for ref in group))
                request.set_count('25')
                body=self.http.read(request)
                rows={row.get('symbol'):row.get('result') for row in body.get('result',[]) if isinstance(row,dict)}
            except Exception:
                rows={}
            for ref in group:
                if isinstance(rows.get(ref.ticker),list):cache[ref.ticker]=(label,tuple(rows[ref.ticker]))

    def _with_features(self,items,refs,session):
        from .real_features import instrument_features
        self._refresh_daily(refs,session)
        now=self.clock();daily=getattr(self,'daily_cache',{});enriched=[]
        for item in items:
            try:source=json.loads(item.market_context or '{}')
            except ValueError:source={}
            features=instrument_features(self.bar_cache.get(item.symbol,()),
                daily.get(item.symbol,(None,()))[1],now,session.opens_at)
            features['bars_through']=(self.bar_cache[item.symbol][-1].at.isoformat(timespec='minutes')
                if self.bar_cache.get(item.symbol) else None)
            # Compact rebuild (Text <= 2000 chars): provenance, affordability, features.
            context={'provenance':source.get('market_provenance'),
                'affordability_ceiling_usd':source.get('affordability_ceiling_usd'),'features':features,
                'not_supplied':['news','earnings_calendar','fundamentals','level2']}
            lab=getattr(self,'lab_picks',{}).get(item.symbol)
            if lab:context['lab_signal']=lab
            text=json.dumps(context,separators=(',',':'))
            if len(text)>2000 and lab:      # contracts.Text limit: the lab note is dropped before any feature
                context.pop('lab_signal');text=json.dumps(context,separators=(',',':'))
            enriched.append(item.model_copy(update={'market_context':text}))
        return enriched

    def _market_regime(self,session):
        """SPY session/daily features as broad-market backdrop. Never blocks."""
        from .real_features import instrument_features
        now=self.clock()
        try:
            cached=getattr(self,'market_bars',())
            if not cached or (now-cached[-1].at).total_seconds()>BAR_REFRESH_SECONDS:
                request=build_request(Operation.HISTORY_BATCH,symbols=(MARKET_SYMBOL,));request.set_real_time_required(True)
                request.set_count(str(_bars_needed(cached,session,now)))
                body=self.http.read(request)
                rows=[r for r in body['result'] if r.get('symbol')==MARKET_SYMBOL][0]['result']
                merged={bar.at:bar for bar in cached if session.opens_at<=bar.at}
                for row in rows:
                    try:
                        bar=AlphaBar(at=row['time'],open_usd=row['open'],high_usd=row['high'],low_usd=row['low'],
                            close_usd=row['close'],volume=row['volume'])
                    except (ValueError,KeyError,TypeError):continue
                    if session.opens_at<=bar.at<=now:merged[bar.at]=bar
                self.market_bars=tuple(sorted(merged.values(),key=lambda bar:bar.at)[-MAX_SESSION_BARS:])
            self._refresh_daily([type('Ref',(),{'ticker':MARKET_SYMBOL})()],session)
            daily=getattr(self,'daily_cache',{}).get(MARKET_SYMBOL,(None,()))[1]
            return json.dumps({'symbol':MARKET_SYMBOL,'role':'BROAD_MARKET_BACKDROP_NOT_TRADABLE_HERE',
                'features':instrument_features(self.market_bars,daily,now,session.opens_at)},separators=(',',':'))
        except Exception:
            return json.dumps({'symbol':MARKET_SYMBOL,'unavailable':True},separators=(',',':'))

    def refresh_instrument(self,state,instrument_id):
        """Selected entry plus every actual holding, through the same broker checks."""
        if instrument_id not in state['instruments']:raise ValueError('REAL_FOCUS_INSTRUMENT_UNKNOWN')
        previous=self.symbols;previous_focus=getattr(self,'focused',False)
        self.focused=True
        self.symbols=(state['instruments'][instrument_id]['ticker'],)
        try:return self.refresh(state)
        finally:self.symbols=previous;self.focused=previous_focus

    def _profiles(self,symbols,held):
        profiles={};ambiguous_profiles=set()
        # Reuse discovery metadata briefly; every held/selected instrument is
        # re-read directly before execution or position supervision.
        cache=self.profile_cache=getattr(self,'profile_cache',{})
        for symbol in symbols:
            old=cache.get(symbol)
            if (old and symbol not in held and not getattr(self,'focused',False)
                    and 0<=(self.clock()-old[0]).total_seconds()<60):profiles[symbol]=old[1]
        needed=tuple(symbol for symbol in symbols if symbol not in profiles)
        for start in range(0,len(needed),10):
            group=needed[start:start+10]
            body=self.http.read(build_request(Operation.INSTRUMENTS,symbols=group))
            _bind_profiles(body,group,profiles,ambiguous_profiles)
            for symbol in group:
                cache.pop(symbol,None)
                if symbol in profiles:cache[symbol]=(self.clock(),profiles[symbol])
        self.profile_cache={symbol:value for symbol,value in cache.items() if symbol in symbols}
        return profiles

    def refresh(self,state=None):
        self.last_quote_refresh_evidence=[]
        now=self.clock();session=reviewed_calendar().session(now.astimezone(NEW_YORK).date())
        if session is None or not session.contains(now):raise ValueError('REGULAR_SESSION_REQUIRED')
        receipts=[]
        account=select_single_individual_cash(self.http.read(build_request(Operation.ACCOUNTS)));receipts.append(self.http.last_received_at)
        if binding(account['account_id'])!=self.account_binding:raise ValueError('REAL_ACCOUNT_BINDING_MISMATCH')
        self.account_id=account['account_id']
        positions=self.http.read(build_request(Operation.POSITIONS,self.account_id));receipts.append(self.http.last_received_at)
        orders=self.http.read(build_request(Operation.OPEN_ORDERS,self.account_id));receipts.append(self.http.last_received_at)
        balance=self.http.read(build_request(Operation.BALANCE,self.account_id));receipts.append(self.http.last_received_at)
        if not isinstance(positions,list) or not isinstance(orders.get('data'),list) or orders.get('pagination_key'):
            raise ValueError('REAL_ACCOUNT_SHAPE_OR_PAGINATION')
        held=[p['symbol'] for p in positions if Decimal(p['quantity'])!=0]
        self.held_symbols=tuple(held)
        symbols=tuple(dict.fromkeys(held+list(self.symbols)))
        if not symbols:raise ValueError('REAL_PORTFOLIO_CONTEXT_MISSING')
        profiles=self._profiles(symbols,held);quotes={};quote_receipts={}
        # Apply the same static instrument/coverage checks before spending quote calls.
        symbols=tuple(symbol for symbol in symbols if symbol in held or (
            listed_equity(profiles.get(symbol)) and Decimal(str(profiles[symbol].get('lot_size',0)))==1
            and (not self.market_authority or profiles[symbol].get('exchange_code') in TRADABLE_LISTINGS)
            and symbol not in getattr(self,'listing_blocked',set())))
        def read_quotes(group):
            body=self.http.read(snapshot_request(group,urgent=getattr(self,'focused',False) or any(s in held for s in group)))
            received=self.http.last_received_at
            if not isinstance(body,list):raise ValueError('REAL_QUOTE_SHAPE')
            for q in body:
                if q.get('symbol') not in group or q['symbol'] in quotes:raise ValueError('REAL_QUOTE_IDENTITY')
                quotes[q['symbol']]=q;quote_receipts[q['symbol']]=received
        for start in range(0,len(symbols),20):
            group=symbols[start:start+20]
            try:read_quotes(group)
            except ValueError as exc:
                # One symbol Webull refuses (4xx) rejects the whole batch (2026-10-30 daily start). Candidates are
                # then re-read one by one and the refused ones set aside for the session. Held symbols and every
                # other failure (rate limits, transport, shape) still fail loudly, as before.
                if not re.fullmatch(r'REAL_HTTP_4\d\d_[A-Z0-9_\-]{1,80}',str(exc)) or len(group)==1:raise
                for symbol in group:
                    try:read_quotes((symbol,))
                    except ValueError as single:
                        if symbol in held or not re.fullmatch(r'REAL_HTTP_4\d\d_[A-Z0-9_\-]{1,80}',str(single)):raise
                        self.__dict__.setdefault('listing_blocked',set()).add(symbol)
        cash=balance.get('account_currency_assets')
        if not isinstance(cash,list) or len(cash)!=1 or cash[0].get('currency')!='USD':raise ValueError('REAL_CASH_SHAPE')
        cash=self._adjust_cash(cash[0]);settled=Decimal(cash['settled_cash']);unsettled=Decimal(cash['unsettled_cash'])
        if settled+unsettled!=Decimal(cash['cash_balance']):raise ValueError('REAL_CASH_MISMATCH')
        equity=Decimal(cash['net_liquidation_value']);symbol_cap=min(settled,equity*POLICY.stock_exposure)
        self.last_symbol_cap=symbol_cap
        history_refs=[]
        for symbol in symbols:
            p,q=profiles.get(symbol),quotes.get(symbol)
            if not p or not q or str(p.get('instrument_id'))!=str(q.get('instrument_id')):continue
            if not listed_equity(p) or Decimal(str(p.get('lot_size',0)))!=1:continue
            prices=_candidate_prices(q,held=symbol in held)
            if prices is None:continue
            bid,ask=prices
            if symbol not in held and (p.get('status')!='OC' or q.get('list_status')!='LISTED'
                    or not 0<bid<=ask or not _liquid_enough(bid,ask) or ask+Decimal('1.02')>symbol_cap):continue
            history_refs.append(instrument(p))
        # Only the instruments that can reach the bounded shortlist need bars.
        from .real_triggers import MAX_SHORTLIST
        held_refs=[r for r in history_refs if r.ticker in held]
        history_refs=held_refs+[r for r in history_refs if r.ticker not in held][:max(0,MAX_SHORTLIST-len(held_refs))+MAX_SHORTLIST]
        self._refresh_batch_bars(history_refs,session)
        refs=[];items=[]
        for symbol in symbols:
            p,q=profiles.get(symbol),quotes.get(symbol)
            if not p:
                if symbol in held:raise ValueError('REAL_HELD_PROFILE_IDENTITY')
                continue
            if not q or str(p.get('instrument_id'))!=str(q.get('instrument_id')):
                if symbol in held:raise ValueError('REAL_PROFILE_QUOTE_BINDING')
                continue
            if not listed_equity(p) or Decimal(str(p.get('lot_size',0)))!=1:
                if symbol in held:raise ValueError('REAL_HELD_STOCK_CAPABILITY_UNVERIFIED')
                continue
            prices=_candidate_prices(q,held=symbol in held)
            if prices is None:continue
            bid,ask=prices
            tradable=p.get('status')=='OC' and q.get('list_status')=='LISTED' and 0<bid<=ask
            if not tradable and symbol not in held:continue
            if symbol not in held and not _liquid_enough(bid,ask):continue
            # $1 + $0.02/share is the existing conservative per-side reserve.
            if symbol not in held and ask+Decimal('1.02')>symbol_cap:continue
            ref=instrument(p)
            if self.market_authority and ref.listing_exchange not in TRADABLE_LISTINGS:
                if symbol in held:raise ValueError('REAL_HELD_MARKET_COVERAGE_UNVERIFIED')
                continue
            # A NYSE candidate without fresh session bars is not proven real-time this cycle: leave it out.
            if (symbol not in held and ref.listing_exchange in NYSE_LISTINGS
                    and (symbol in getattr(self,'listing_blocked',set()) or not self.bar_cache.get(symbol))):
                continue
            refs.append(ref)
            session_bars=self.bar_cache.get(symbol,());bars=session_bars[-5:]
            gaps=['financial_quality','earnings','daily_research','REAL_fee_schedule']
            if not bars:gaps.append('recent_bars')
            items.append(AlphaInstrumentContext(instrument_id=ref.instrument_id,symbol=symbol,
                quote_at=datetime.fromtimestamp(int(q['quote_time'])/1000,timezone.utc),bid_usd=bid,ask_usd=ask,
                price_increment_usd=ref.price_increment_usd,quantity_increment=ref.quantity_increment,
                reference_price_usd=(bid+ask)/2,spread_bps=_spread_bps(bid,ask),
                bid_size=q.get('bid_size'),ask_size=q.get('ask_size'),volume=q.get('volume'),bars=bars,
                market_context=json.dumps(dict(market_provenance='WEBULL_REAL_READ_ONLY',category='US_STOCK',
                    gaps=gaps,quality='Astra must evaluate supplied evidence; unavailable fields are not favorable evidence',
                    affordability_ceiling_usd=str(symbol_cap),
                    nasdaq_basic='Not asserted. Endpoint access, entitlement, freshness and coverage remain explicit evidence gaps'))))
        if not items:raise ValueError('NO_ELIGIBLE_REAL_PORTFOLIO_CONTEXT')
        # Bounded shortlist: held instruments first (symbols order), then the
        # interleaved discovery rank. Bounds tokens per call and quote reads.
        from .real_triggers import MAX_SHORTLIST
        kept=[item for item in items if item.symbol in held]
        # At most MAX_IN_SHORTLIST eligible lab picks (owner-approved 2026-10-20); the rest stays broker-ranked.
        from .lab.picks import MAX_IN_SHORTLIST
        lab=set(getattr(self,'lab_picks',{}))
        lab_items=[item for item in items if item.symbol not in held and item.symbol in lab][:MAX_IN_SHORTLIST]
        rest=[item for item in items if item.symbol not in held and item not in lab_items and item.symbol not in lab]
        kept+= (lab_items+rest)[:max(0,MAX_SHORTLIST-len(kept))]
        items=kept;refs=[ref for ref in refs if ref.ticker in {item.symbol for item in items}]
        market_regime=None
        if getattr(self,'enrich_context',False):
            items=self._with_features(items,refs,session);market_regime=self._market_regime(session)
        # Bars are slower than account/quote reads. Re-read the account first,
        # then the exact compact quote set, so both final observations remain
        # current without substituting receipt time for market source time.
        final_account=self.account_observation();positions=final_account['positions']
        cash=final_account['cash'];settled=Decimal(cash['settled_cash']);unsettled=Decimal(cash['unsettled_cash'])
        if settled+unsettled!=Decimal(cash['cash_balance']):raise ValueError('REAL_CASH_MISMATCH')
        equity=Decimal(cash['net_liquidation_value'])
        selected=tuple(x.symbol for x in items)
        refreshed={}
        for start in range(0,len(selected),20):
            group=selected[start:start+20]
            body=self.http.read(snapshot_request(group,urgent=getattr(self,'focused',False) or any(s in held for s in group)));received=self.http.last_received_at
            if not isinstance(body,list):raise ValueError('REAL_QUOTE_SHAPE')
            for q in body:
                if q.get('symbol') not in group or q['symbol'] in refreshed:raise ValueError('REAL_QUOTE_IDENTITY')
                refreshed[q['symbol']]=q;quote_receipts[q['symbol']]=received
        updated=[]
        for item in items:
            q=refreshed.get(item.symbol)
            if not q or str(q.get('instrument_id'))!=item.instrument_id:
                if item.symbol in held:raise ValueError('REAL_PROFILE_QUOTE_BINDING')
                continue
            limit=_quote_limit(self.market_authority)
            if item.symbol in held:
                q,quote_receipts[item.symbol]=self._refresh_quote(item.symbol,item.instrument_id,q,quote_receipts[item.symbol],limit)
            else:
                # A bad discovery quote is not a reason to exhaust the broker quota.
                self.last_quote_refresh_evidence.append(dict(symbol=item.symbol,instrument_id=item.instrument_id,
                    attempt=0,raw_quote_time=q.get('quote_time'),received_at=quote_receipts[item.symbol].isoformat(),
                    checked_at=self.clock().isoformat(),maximum_age_seconds=limit,
                    fresh=_quote_is_fresh(q,quote_receipts[item.symbol],self.clock(),limit)))
            if not _quote_is_fresh(q,quote_receipts[item.symbol],self.clock(),limit):
                if item.symbol in held:raise ValueError('STALE_REAL_HELD_QUOTE')
                continue
            prices=_candidate_prices(q,held=item.symbol in held)
            if prices is None:continue
            bid,ask=prices
            if not 0<bid<=ask:raise ValueError('INVALID_REAL_QUOTE')
            updated.append(item.model_copy(update=dict(
                quote_at=datetime.fromtimestamp(int(q['quote_time'])/1000,timezone.utc),bid_usd=bid,ask_usd=ask,
                reference_price_usd=(bid+ask)/2,spread_bps=_spread_bps(bid,ask),
                bid_size=q.get('bid_size'),ask_size=q.get('ask_size'),volume=q.get('volume'))))
        items=updated
        active_symbols={item.symbol for item in items};refs=tuple(ref for ref in refs if ref.ticker in active_symbols)
        if not refs:raise ValueError('NO_FRESH_REAL_PORTFOLIO_CONTEXT')
        refs_by_symbol={i.ticker:i for i in refs};owned={}
        for p in positions:
            qty=Decimal(p['quantity'])
            if qty==0:continue
            if p['symbol'] not in refs_by_symbol or p.get('instrument_type')!='EQUITY' or qty<=0:
                raise ValueError('REAL_UNEXPECTED_HOLDING')
            iid=refs_by_symbol[p['symbol']].instrument_id
            if iid in owned:raise ValueError('REAL_DUPLICATE_POSITION')
            owned[iid]=qty
        clients=final_account['open_client_ids']
        at=self.clock()
        context=AlphaContext(as_of=at,expires_at=at+timedelta(seconds=30),session_open=session.opens_at,
            session_close=session.closes_at,liquidation_at=session.liquidation_at,instruments=tuple(items),
            positions=(),settled_cash_usd=min(Decimal(500),settled),remaining_risk_usd='0',
            reconciliation_ready=False,settlement_known=True,
            execution_cost_evidence=REAL_COST_EVIDENCE,market_regime=market_regime,
            exit_horizon_at=exit_horizon(session))
        received_by_id={i.instrument_id:quote_receipts[i.ticker] for i in refs}
        authority=self.market_authority or {}
        nasdaq=[item for item in items if refs_by_symbol[item.symbol].listing_exchange in TRADABLE_LISTINGS]
        bar_ages=[(at-max(item.bars,key=lambda bar:bar.at).at).total_seconds() for item in nasdaq if item.bars]
        proof={} if not nasdaq or len(bar_ages)!=len(nasdaq) else dict(
            status='VERIFIED_CURRENT_NASDAQ_STOCK_COVERAGE',host='api.webull.com',
            session_label=session.label.isoformat(),asset_scope='US_LISTED_STOCK',
            nasdaq_symbols=[item.symbol for item in nasdaq],
            quote_max_age_ms=max((at-item.quote_at).total_seconds()*1000 for item in nasdaq),
            receipt_max_age_ms=max((at-quote_receipts[item.symbol]).total_seconds()*1000 for item in nasdaq),
            bar_max_age_seconds=max(bar_ages),
            entitlement_evidence_sha256=authority.get('entitlement_evidence_sha256',''))
        packet=RealPacket(account_binding=self.account_binding,observed_at=final_account['observed_at'],settled=settled,
            unsettled=unsettled,equity=equity,positions=owned,open_client_ids=clients,instruments=tuple(refs),
            context=context,evidence_id=uuid4().hex,quote_received_at=received_by_id,market_data_evidence=proof)
        packet.validate(self.clock())
        return packet
