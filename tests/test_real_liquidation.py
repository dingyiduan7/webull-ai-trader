from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from uuid import uuid4

import pytest
from psycopg import sql
from autonomous_trader import real_liquidation as w
from autonomous_trader.real_state import binding, provision
from autonomous_trader.durable.ledger import LocalDatabase
from autonomous_trader.real_liquidation_broker import LiquidationBroker
from autonomous_trader.real_secrets import RealCredentials
from autonomous_trader import real_liquidation_broker as boundary
from autonomous_trader.real_preflight import RealObservation, Operation
import json
import requests

NOW = datetime(2026, 9, 29, 14, tzinfo=timezone.utc)
ACCOUNT = binding('fixture-account')
QTY = dict(zip(w.ALLOWLIST, map(D, ('0.05675','0.00188','0.00719','0.01733','0.01408'))))


def snapshot():
    return w.Snapshot(ACCOUNT, NOW, dict(QTY), D(500), D(0), D('505.44'), D(500), (),
        {s:dict(category='US_ETF', bid=D(20), ask=D('20.01'), at=NOW, received_at=NOW) for s in w.ALLOWLIST})


@pytest.fixture
def journal():
    db = LocalDatabase('postgres','m2_admin',schema='real_readiness_test_'+uuid4().hex)
    provision(db)
    try: yield w.Journal(db)
    finally:
        with db.connect() as c: c.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(db.schema)))


class Broker:
    def __init__(self, behavior='FILLED'):
        self.state = snapshot()
        self.behavior = behavior
        self.sent = []
        self.reads = 0
    def refresh(self): return self.state
    def sell(self, order, observed, *, capability):
        w.validate_order(order, self.state.positions)
        self.journal.consume(capability, order, ACCOUNT)
        self.sent.append(order)
        if self.behavior == 'CRASH': raise RuntimeError('secret-must-not-escape')
        return dict(client_order_id=order['client_order_id'],order_id='broker-fixture')
    def detail(self, client_id):
        self.reads += 1
        order = self.sent[-1]
        qty = D(order['quantity'])
        status = self.behavior if self.behavior not in ('CASH_MISMATCH','WRONG_ID','UNKNOWN_FEES') else 'FILLED'
        filled = qty if status == 'FILLED' else qty/2 if status == 'PARTIAL_FILLED' else D(0)
        row = dict(order, order_id='broker-fixture', status=status, total_quantity=str(qty),
            filled_quantity=str(filled), filled_price='20', fees=[], commission={})
        if self.behavior == 'WRONG_ID': row['symbol']='OTHER'
        if self.behavior == 'UNKNOWN_FEES': del row['fees']
        if status == 'FILLED':
            positions = dict(self.state.positions)
            del positions[order['symbol']]
            proceeds = (qty*20).quantize(D('.01'))
            if self.behavior == 'CASH_MISMATCH': proceeds += 1
            self.state = replace(self.state, positions=positions, unsettled=self.state.unsettled+proceeds,
                cash=self.state.cash+proceeds)
        return dict(client_order_id=client_id,orders=[row])


def run(broker,journal):
    broker.journal=journal
    return w.execute_once(broker,journal,ACCOUNT,clock=lambda:NOW,sleep=lambda _:None)


def test_complete_exact_five_then_permanent_no_reentry(journal):
    broker=Broker()
    assert run(broker,journal)['status']=='COMPLETE'
    assert [o['symbol'] for o in broker.sent]==list(w.ALLOWLIST)
    assert all(D(o['quantity'])==QTY[o['symbol']] for o in broker.sent)
    assert broker.state.positions=={}
    with pytest.raises(w.Blocked,match='CONSUMED'): run(broker,w.Journal(journal.db))
    assert len(broker.sent)==5
    assert w.dashboard_status(journal.db)['status']=='COMPLETE'
    with pytest.raises(Exception,match='APPEND_ONLY'):
        with journal.db.connect() as c: c.execute('DELETE FROM liquidation_journal')


@pytest.mark.parametrize('behavior', ['CRASH','PARTIAL_FILLED','FAILED','CANCELLED','PENDING',
                                    'CASH_MISMATCH','WRONG_ID','UNKNOWN_FEES'])
def test_failure_stops_all_remaining_sells_and_never_retries(journal,behavior):
    broker=Broker(behavior)
    with pytest.raises(w.Blocked) as exc: run(broker,journal)
    assert 'secret' not in str(exc.value)
    assert len(broker.sent)==1 and broker.reads<=3
    with pytest.raises(w.Blocked,match='CONSUMED'): run(Broker(),w.Journal(journal.db))
    events=journal.events()
    assert events[1][0]=='SUBMISSION_INTENT' and events[-1][0]=='BLOCKED'


@pytest.mark.parametrize('change', [dict(account_binding=binding('other')),dict(host='api.sandbox.webull.com'),
    dict(account_type='MARGIN'),dict(open_orders=({'status':'PENDING'},)),dict(totals_match=False),
    dict(received_at=NOW-timedelta(seconds=11)),dict(settled=D(-1)),dict(positions={'OTHER':D('.1')})])
def test_account_safety_failures_never_submit(journal,change):
    broker=Broker();broker.state=replace(broker.state,**change)
    with pytest.raises(w.Blocked):run(broker,journal)
    assert broker.sent==[]


@pytest.mark.parametrize('field,value', [('side','BUY'),('side','SHORT'),('symbol','AAPL'),
    ('quantity','0.1'),('quantity','0'),('quantity','-0.1'),('quantity','NaN'),
    ('support_trading_session','ALL'),('order_type','LIMIT'),('entrust_type','AMOUNT')])
def test_order_boundary_only_exact_fractional_allowlist_sells(field,value):
    order=w.order_for('ICLN',QTY['ICLN'],'a'*32);order[field]=value
    with pytest.raises(w.Blocked):w.validate_order(order,QTY)


@pytest.mark.parametrize('delta', [-1,11])
def test_stale_future_market_data_blocks(journal,delta):
    broker=Broker();broker.state.quotes['ICLN']['at']=NOW-timedelta(seconds=delta)
    with pytest.raises(w.Blocked,match='QUOTE'):run(broker,journal)
    assert broker.sent==[]


def test_closed_session_dry_run_is_sanitized_not_activation(journal):
    broker=Broker();closed=NOW.replace(hour=7)
    broker.state=replace(broker.state,received_at=closed)
    result=w.dry_run(broker,journal,ACCOUNT,clock=lambda:closed)
    assert result['status']=='PENDING_REVIEW' and not result['activated']
    assert 'REGULAR_SESSION_REQUIRED' in result['blockers']
    assert len(result['planned_actions'])==5 and broker.sent==[]
    assert all('client_order_id' not in row for row in result['planned_actions'])
    with pytest.raises(w.Blocked,match='REGULAR_SESSION'):w.regular(closed)


def test_real_transport_cannot_be_activated_by_constructing_it():
    client=LiquidationBroker(RealCredentials('fixture-key','fixture-secret'),ACCOUNT)
    with pytest.raises(w.Blocked,match='CAPABILITY'):
        client.sell(w.order_for('ICLN',QTY['ICLN'],'a'*32),snapshot())
    assert not w.EXECUTION_APPROVED and w.FRACTIONAL_SELL_RULES_VERIFIED


def test_crash_after_persisted_intent_cannot_resume(journal):
    journal.append('STARTED',{'account_binding':ACCOUNT})
    journal.append('SUBMISSION_INTENT',w.order_for('ICLN',QTY['ICLN'],'a'*32))
    assert w.dashboard_status(journal.db)['status']=='INCOMPLETE_UNCERTAIN'
    with pytest.raises(w.Blocked,match='CONSUMED'):run(Broker(),w.Journal(journal.db))


def test_concurrent_invocation_has_one_writer(journal):
    with journal.once():
        with pytest.raises(w.Blocked,match='WRITER_BUSY'):
            with w.Journal(journal.db).once():pytest.fail('second writer acquired')


def claim(journal, order, account=ACCOUNT):
    journal.append('STARTED',dict(campaign=w.CAMPAIGN,account_binding=account,observation=snapshot().evidence()))
    journal.append('SUBMISSION_INTENT',order)


def test_capability_requires_writer_claim_and_exact_intent(journal):
    order=w.order_for('ICLN',QTY['ICLN'],'a'*32)
    with pytest.raises(w.Blocked,match='WRITER'):journal.issue(order,ACCOUNT)
    with journal.once():
        with pytest.raises(w.Blocked,match='CLAIM'):journal.issue(order,ACCOUNT)
        journal.append('STARTED',dict(campaign=w.CAMPAIGN,account_binding=ACCOUNT,observation=snapshot().evidence()))
        with pytest.raises(w.Blocked,match='INTENT'):journal.issue(order,ACCOUNT)
        journal.append('SUBMISSION_INTENT',order)
        cap=journal.issue(order,ACCOUNT)
        assert cap.token not in repr(cap) and cap.token not in json.dumps(journal.events())
        with pytest.raises(w.Blocked,match='ALREADY_ISSUED'):journal.issue(order,ACCOUNT)
        journal.consume(cap,order,ACCOUNT)
        with pytest.raises(w.Blocked,match='ALREADY_CONSUMED'):journal.consume(cap,order,ACCOUNT)
    with pytest.raises(w.Blocked,match='WRITER'):journal.consume(cap,order,ACCOUNT)


@pytest.mark.parametrize('field,value',[('campaign','wrong'),('namespace','wrong'),
    ('account_binding',binding('other')),('symbol','SMH'),('quantity','0.1'),
    ('client_order_id','b'*32),('order_hash','wrong'),('token','forged')])
def test_capability_binding_or_forgery_rejected(journal,field,value):
    order=w.order_for('ICLN',QTY['ICLN'],'a'*32)
    with journal.once():
        claim(journal,order)
        cap=journal.issue(order,ACCOUNT)
        with pytest.raises(w.Blocked,match='CAPABILITY'):journal.consume(replace(cap,**{field:value}),order,ACCOUNT)
        assert not any(e=='CAPABILITY_CONSUMED' for e,_ in journal.events())


@pytest.mark.parametrize('event',['COMPLETE','BLOCKED','SUBMISSION_INTENT'])
def test_durable_authority_is_rechecked_at_consumption(journal,event):
    order=w.order_for('ICLN',QTY['ICLN'],'a'*32)
    with journal.once():
        claim(journal,order);cap=journal.issue(order,ACCOUNT)
        journal.append(event, w.order_for('SMH',QTY['SMH'],'b'*32) if event=='SUBMISSION_INTENT' else {})
        with pytest.raises(w.Blocked):journal.consume(cap,order,ACCOUNT)


def test_lost_database_writer_lock_revokes_capability(journal):
    order=w.order_for('ICLN',QTY['ICLN'],'a'*32)
    with journal.once():
        claim(journal,order);cap=journal.issue(order,ACCOUNT)
        journal._owner.execute('SELECT pg_advisory_unlock(481516,902)')
        with pytest.raises(w.Blocked,match='WRITER_LOST'):journal.consume(cap,order,ACCOUNT)


class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):return NOW


def wire_broker(monkeypatch,journal=None):
    monkeypatch.setattr(boundary,'datetime',FixedDateTime)
    if journal is not None:monkeypatch.setattr(boundary,'REAL_LIQUIDATION_NAMESPACE',journal.namespace)
    broker=LiquidationBroker(RealCredentials('fixture-key','fixture-secret'),ACCOUNT,journal=journal)
    broker._account_id='fixture-account';broker._latest=snapshot()
    return broker


def test_direct_broker_call_without_capability_fails_even_if_activation_were_approved(monkeypatch):
    monkeypatch.setattr(w,'EXECUTION_APPROVED',True)
    monkeypatch.setattr(w,'FRACTIONAL_SELL_RULES_VERIFIED',True)
    broker=wire_broker(monkeypatch)
    monkeypatch.setattr(broker.http,'_make_http_response',lambda *a:pytest.fail('unsigned gate bypass'))
    with pytest.raises(w.Blocked,match='CAPABILITY'):
        broker.sell(w.order_for('ICLN',QTY['ICLN'],'a'*32),broker._latest)


@pytest.mark.parametrize('transport_failure',[False,True])
def test_actual_post_boundary_consumes_durably_before_send_no_replay(monkeypatch,journal,transport_failure):
    monkeypatch.setattr(w,'EXECUTION_APPROVED',True)
    monkeypatch.setattr(w,'FRACTIONAL_SELL_RULES_VERIFIED',True)
    broker=wire_broker(monkeypatch,journal)
    sent=[]
    class Response:
        status_code=200
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def iter_content(self,size):yield b'{"client_order_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","order_id":"fixture"}'
    class Session(requests.Session):
        def send(self,prepared,**kwargs):
            assert prepared.method=='POST' and prepared.url=='https://api.webull.com/trading/orders/place'
            assert json.loads(prepared.body)['new_orders'][0]['side']=='SELL'
            assert sum(e=='CAPABILITY_CONSUMED' for e,_ in journal.events())==1
            sent.append(1)
            if transport_failure:raise TimeoutError()
            return Response()
    monkeypatch.setattr(boundary.requests,'Session',Session)
    order=w.order_for('ICLN',QTY['ICLN'],'a'*32)
    with journal.once():
        claim(journal,order);cap=journal.issue(order,ACCOUNT)
        if transport_failure:
            with pytest.raises(w.Blocked,match='UNCERTAIN'):broker.sell(order,broker._latest,capability=cap)
        else:assert broker.sell(order,broker._latest,capability=cap)['order_id']=='fixture'
        with pytest.raises(w.Blocked,match='CONSUMED'):broker.sell(order,broker._latest,capability=cap)
        assert len(sent)==1


def etf_reader_broker(monkeypatch,mutate=lambda rows:rows):
    broker=wire_broker(monkeypatch)
    rows=[dict(symbol=s,quantity=str(q),market_value=str((q*20).quantize(D('.01'))),last_price='20') for s,q in QTY.items()]
    mv=(sum(QTY.values())*20).quantize(D('.01'))
    cash=dict(currency='USD',settled_cash='500',unsettled_cash='0',cash_balance='500',
        market_value=str(mv),net_liquidation_value=str(500+mv))
    bodies={Operation.ACCOUNTS:[dict(account_id='fixture-account',account_type='CASH',account_class='INDIVIDUAL_CASH')],
        Operation.POSITIONS:rows,Operation.OPEN_ORDERS:{'data':[]},Operation.BALANCE:{'account_currency_assets':[cash]}}
    monkeypatch.setattr(broker.reader,'read',lambda op,**kw:RealObservation('',200,NOW.isoformat(),{},bodies[op]))
    def response(request):
        params=request.get_query_params()
        assert params['category']=='US_ETF'
        symbols=params['symbols']
        if isinstance(symbols,str):symbols=symbols.split(',')
        assert set(symbols)==set(w.ALLOWLIST) and len(symbols)==5
        assert request.get_method()=='GET' and request.get_url().split('?')[0]=='/market-data/stocks/snapshots/list'
        quotes=[dict(symbol=s,category='US_ETF',bid='20',ask='20.01',quote_time=int(NOW.timestamp()*1000)) for s in w.ALLOWLIST]
        return RealObservation('',200,NOW.isoformat(),{},mutate(quotes))
    monkeypatch.setattr(broker.http,'get_response',response)
    return broker


def test_all_five_etfs_exact_category_identity_and_freshness(monkeypatch):
    broker=etf_reader_broker(monkeypatch)
    state=broker.refresh()
    assert set(state.quotes)==set(w.ALLOWLIST)
    w.validate_snapshot(state,QTY,ACCOUNT,NOW)
    assert all(q['category']=='US_ETF' for q in state.quotes.values())


@pytest.mark.parametrize('defect',['missing','duplicate','wrong_symbol','wrong_category','stale','future'])
def test_etf_response_identity_category_and_staleness_fail_closed(monkeypatch,defect):
    def mutate(rows):
        if defect=='missing':return rows[:-1]
        if defect=='duplicate':return rows+[rows[0]]
        if defect=='wrong_symbol':rows[0]['symbol']='AAPL'
        if defect=='wrong_category':rows[0]['category']='US_STOCK'
        if defect in ('stale','future'):
            rows[0]['quote_time']=int((NOW+timedelta(seconds=-11 if defect=='stale' else 1)).timestamp()*1000)
        return rows
    broker=etf_reader_broker(monkeypatch,mutate)
    with pytest.raises(w.Blocked):
        w.validate_snapshot(broker.refresh(),QTY,ACCOUNT,NOW)


def test_different_client_id_cannot_rearm_same_campaign_symbol(journal):
    order=w.order_for('ICLN',QTY['ICLN'],'a'*32)
    with journal.once():
        claim(journal,order);cap=journal.issue(order,ACCOUNT)
        journal.consume(cap,order,ACCOUNT)
        retry=w.order_for('ICLN',QTY['ICLN'],'b'*32)
        journal.append('SUBMISSION_INTENT',retry)
        with pytest.raises(w.Blocked,match='ALREADY_ISSUED'):journal.issue(retry,ACCOUNT)


def test_broker_rejects_capability_from_non_real_namespace(monkeypatch,journal):
    broker=LiquidationBroker(RealCredentials('fixture-key','fixture-secret'),ACCOUNT,journal=journal)
    order=w.order_for('ICLN',QTY['ICLN'],'a'*32)
    with journal.once():
        claim(journal,order);cap=journal.issue(order,ACCOUNT)
        with pytest.raises(w.Blocked,match='NAMESPACE'):broker.sell(order,snapshot(),capability=cap)


def test_concurrent_capability_consumption_has_one_winner(journal):
    from concurrent.futures import ThreadPoolExecutor
    order=w.order_for('ICLN',QTY['ICLN'],'a'*32)
    with journal.once():
        claim(journal,order);cap=journal.issue(order,ACCOUNT)
        def consume(_):
            try:journal.consume(cap,order,ACCOUNT);return 'CONSUMED'
            except w.Blocked as exc:return str(exc)
        with ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(consume,range(4)))
        assert results.count('CONSUMED')==1
        assert results.count('JOURNAL_CAPABILITY_ALREADY_CONSUMED')==3
