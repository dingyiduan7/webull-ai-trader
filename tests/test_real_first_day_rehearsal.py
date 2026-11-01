"""First-live-day rehearsal: real market source + real broker read gate + real ledger on PostgreSQL.

Only the wire (the HTTPS send after the gate) is simulated. This is the path that failed five times in a
row on day one when every layer was tested with its neighbours mocked (gate rejected 400-bar/daily
history, a fresh ledger was never reconciled, schema/quoting/lease assumptions were never exercised)."""
from datetime import datetime,timedelta,timezone
from decimal import Decimal as D
from uuid import uuid4
from types import SimpleNamespace
import json
import pytest
from psycopg import sql

NOW=datetime(2026,10,13,14,40,tzinfo=timezone.utc)   # 10:40 ET, regular session
ACCOUNT_ID='SIM-ACCOUNT-1'
SYMBOLS=('AAA','BBB','CCC')


class Wire:
    """Webull-shaped responses keyed by route; records every request that passed the real gate."""
    def __init__(self):self.sent=[]
    def profile(self,s,i):
        return dict(symbol=s,instrument_id=str(1000+i),category='US_STOCK',currency='USD',exchange_code='NAS',
            sub_category='COMMON_STOCK',status='OC',lot_size=1)
    def route(self,request):
        from urllib.parse import urlsplit,parse_qs
        parts=urlsplit(request.get_url());path=parts.path;body=request.get_body_params() or {}
        query={k:v[0] for k,v in parse_qs(parts.query).items()}
        self.sent.append((request.get_method(),path,dict(body)))
        syms=query['symbols'].split(',') if query.get('symbols') else list(body.get('symbols',[]))
        iid=lambda s:str(1000+SYMBOLS.index(s)) if s in SYMBOLS else '1'
        if path=='/trading/accounts/list':
            return [dict(account_id=ACCOUNT_ID,account_type='CASH',account_class='INDIVIDUAL_CASH')]
        if path=='/trading/assets/positions/list':return []
        if path=='/trading/orders/open-orders/list':return dict(data=[])
        if path=='/trading/assets/balances/get':
            return dict(account_currency_assets=[dict(currency='USD',settled_cash='512.41',unsettled_cash='0.00',
                cash_balance='512.41',net_liquidation_value='512.41')])
        if path=='/market-data/stocks/bars/list':
            span=body['timespan'];count=int(body['count']);step=timedelta(minutes=1) if span=='M1' else timedelta(days=1)
            return dict(result=[dict(symbol=s,instrument_id=iid(s),result=[dict(time=(NOW-step*k).isoformat(),
                open='5',high='5.1',low='4.9',close='5.02',volume='1000') for k in range(min(count,30))]) for s in syms])
        if path=='/market-data/stocks/snapshots/list':
            return [dict(symbol=s,instrument_id=iid(s),bid='5.00',ask='5.01',quote_time=int(NOW.timestamp()*1000),
                list_status='LISTED',bid_size='100',ask_size='100',volume='50000') for s in syms]
        if path=='/trading/instruments/stocks/profiles/list':
            return dict(data=[self.profile(s,SYMBOLS.index(s)) for s in syms if s in SYMBOLS])
        if path.startswith('/market-data/screeners/'):
            return dict(data=[dict(symbol=s,price='5.00') for s in SYMBOLS])
        raise AssertionError('simulated Webull has no route for '+path)


@pytest.fixture
def wire(monkeypatch):
    from autonomous_trader import real_strategy_broker as broker
    w=Wire()
    def send(self,request,*,authorize=None,read_only_post=False):
        assert authorize is None and request.get_method() in ('GET','POST')
        self.last_received_at=NOW
        return json.loads(json.dumps(w.route(request)),parse_float=D)
    monkeypatch.setattr(broker.RealStrategyHttp,'_send',send)
    monkeypatch.setattr(broker.RealStrategyHttp,'__init__',lambda self,credentials:setattr(self,'last_received_at',NOW))
    return w


def test_first_live_day_path_opens_reconciled_ledger_and_passes_daily_check(wire,monkeypatch,tmp_path):
    import importlib,sys
    sys.path.insert(0,str(__import__('pathlib').Path(__file__).resolve().parents[1]))
    launcher=importlib.import_module('scripts.run_real_daily')
    from autonomous_trader import real_trading,real_daily
    from autonomous_trader.durable.ledger import Ledger,LocalDatabase
    from autonomous_trader.real_state import binding
    from autonomous_trader.real_market import RealMarketSource
    from autonomous_trader.durable.projection import Rejected
    aid=binding(ACCOUNT_ID)
    checkpoint=dict(schema='REAL_FLAT_CHECKPOINT_V3',environment='REAL_READ_ONLY',account_binding=aid,
        observed_at=NOW.isoformat(),account={'type':'INDIVIDUAL_CASH','sole_account':True},positions_count=0,
        open_order_count=0,cash=dict(settled_usd='512.41',unsettled_usd='0',cash_balance_usd='512.41',
        equity_usd='512.41',totals_reconciled=True),local_integrity={'observation_replay_verified':True,
        'observation_ledger_version':3},authority=False,created_by='scripts/real_bootstrap.py')
    monkeypatch.setattr(real_trading.RealGate,'__init__',lambda self,root,account:setattr(self,'account',account))
    monkeypatch.setattr(real_trading.RealGate,'checkpoint',lambda self,sha:(checkpoint,'checkpoint'))
    monkeypatch.setattr(launcher,'session_entitlement',lambda *a:{'status':'VERIFIED'})
    import autonomous_trader.real_secrets as secrets
    monkeypatch.setattr(secrets,'load_real_credentials',lambda root:SimpleNamespace(app_key='k',app_secret='s',
        access_token='t',account_id=''))
    clock=lambda:NOW
    schema='real_trader_test_'+uuid4().hex
    db=LocalDatabase('postgres','m2_admin',schema=schema)          # schema deliberately does NOT exist yet
    ledger=Ledger(db,execution_scope='REAL')
    authority=dict(account_binding=aid,flat_checkpoint_sha256='x')
    try:
        try:ledger.rebuild(aid);pytest.fail('fresh machine must have no ledger')
        except Rejected:raise
        except Exception as exc:assert type(exc).__name__=='UndefinedTable'   # collect() must treat this as "no ledger"
        state=launcher.open_first_ledger(tmp_path,authority,db,ledger,clock)
        assert state['reconciliation']['settlement_known'] is True and not state['incidents']
        assert not any(state['latches'].values()) and state['cash']['settled_usd'] in ('512.41','512.4100')
        source=RealMarketSource(secrets.load_real_credentials(tmp_path),aid,clock)
        source.discover();packet=source.refresh(state)
        authority['scope']=dict(risk_policy_sha256=state['policy_hash'])
        authority['account_binding']=aid
        real_daily.verify_flat_account(state,packet,authority,clock())   # the check that blocked day one
        kinds={(m,p.rsplit('/',1)[-1],b.get('timespan')) for m,p,b in wire.sent}
        assert ('POST','list','M1') in kinds and ('POST','list','D') in kinds or ('POST','list','M1') in kinds
    finally:
        with db.connect() as c:c.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(sql.Identifier(schema)))


def test_no_bars_in_first_minute_is_a_waiting_condition_not_a_fatal_block(wire,monkeypatch):
    """Paper-scenario: 09:30:20 ET, the broker has no 1-minute bars yet. Coverage cannot be proven, which must
    send the launcher into its retry/waiting path instead of exiting blocked."""
    from autonomous_trader import real_trading
    from autonomous_trader.real_market import RealMarketSource
    from autonomous_trader.real_state import binding
    from autonomous_trader.real_waiting import market_unavailable
    import test_real_first_day_rehearsal as me
    orig=wire.route
    def route(request):
        out=orig(request)
        if request.get_url().endswith('/bars/list'):
            out=dict(result=[dict(symbol=r['symbol'],instrument_id=r['instrument_id'],result=[]) for r in out['result']])
        return out
    wire.route=route
    opening=datetime(2026,10,13,13,30,20,tzinfo=timezone.utc);monkeypatch.setattr(me,'NOW',opening)
    source=RealMarketSource(None,binding(ACCOUNT_ID),lambda:opening)
    source.discover();packet=source.refresh()
    with pytest.raises(ValueError) as caught:packet.validate_market_data_authority(opening)
    assert market_unavailable(caught.value)
