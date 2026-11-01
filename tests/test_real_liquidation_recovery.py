from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from uuid import uuid4
import json

import pytest
import requests
from psycopg import sql

from autonomous_trader import real_liquidation as prior
from autonomous_trader import real_liquidation_recovery as recovery
from autonomous_trader.real_state import binding, provision
from autonomous_trader.durable.ledger import LocalDatabase
from autonomous_trader.real_secrets import RealCredentials
from autonomous_trader.real_liquidation_recovery_broker import RecoveryBroker
from autonomous_trader import real_liquidation_recovery_broker as recovery_boundary


NOW = datetime(2026, 9, 29, 14, tzinfo=timezone.utc)
ACCOUNT = binding('fixture-account')
QTY = dict(recovery.EXPECTED_QUANTITIES)
LAST = dict(zip(recovery.ALLOWLIST, map(D, ('606.67','167.42','68.58','68.31'))))


@pytest.fixture
def database():
    db = LocalDatabase('postgres','m2_admin',schema='real_readiness_test_'+uuid4().hex)
    provision(db)
    try:
        yield db
    finally:
        with db.connect() as c:
            c.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(db.schema)))


def populate_prior(db):
    journal = prior.Journal(db)
    journal.append('STARTED', dict(campaign=prior.CAMPAIGN, account_binding=ACCOUNT,
        observation={'positions':dict({s:str(q) for s,q in QTY.items()}, ICLN='0.05675')}))
    journal.append('SUBMISSION_INTENT', prior.order_for('ICLN',D('0.05675'),'a'*32))
    journal.append('CAPABILITY_CONSUMED', {'token_hash':'fixture'})
    journal.append('FILL', dict(symbol='ICLN',quantity='0.05675',price='16.82',fees_usd='0.02',
        expected_net_cash_usd='0.93',status='FILLED'))
    journal.append('BLOCKED', {'status':'BLOCKED','blocker':'CASH_RECONCILIATION_REQUIRED'})


def snapshot(*, matched=False):
    exact = sum((QTY[s]*LAST[s] for s in QTY),D(0))
    return recovery.Snapshot(ACCOUNT,NOW,dict(QTY),dict(LAST),D(500),D('.93'),D('505.43'),
        D('500.93'),(),{s:dict(category='US_ETF',bid=D(20),ask=D('20.01'),at=NOW,
        received_at=NOW) for s in QTY},D('4.49'),D('500.9345804'),totals_match=matched,
        display_rounding_proven=True)


def test_exact_independent_rounding_identity_accepts_observed_cent_difference():
    value=snapshot()
    recovery.validate_snapshot(value,QTY,ACCOUNT,NOW)
    assert not value.totals_match and recovery._rounding_identity(value)


@pytest.mark.parametrize('change',[dict(equity=D('505.44')),dict(precision_cash_evidence=D('500.92')),
    dict(broker_market_value=D('4.50')),dict(display_rounding_proven=False)])
def test_rounding_allowance_rejects_any_unproven_account_difference(change):
    with pytest.raises(recovery.Blocked,match='CASH_RECONCILIATION'):
        recovery.validate_snapshot(replace(snapshot(),**change),QTY,ACCOUNT,NOW)


def test_recovery_requires_exact_prior_blocked_campaign_and_exact_four(database):
    journal=recovery.Journal(database)
    class Broker:
        def refresh(self):return snapshot()
    with pytest.raises(recovery.Blocked,match='PRIOR_BLOCKED'):
        recovery.dry_run(Broker(),journal,ACCOUNT,clock=lambda:NOW)
    populate_prior(database)
    result=recovery.dry_run(Broker(),journal,ACCOUNT,clock=lambda:NOW)
    assert result['status']=='PENDING_REVIEW'
    assert result['blockers']==['JUDICATOR_APPROVAL_REQUIRED']
    assert len(result['planned_actions'])==4 and not result['activated']
    assert set(result['market_evidence'])==set(recovery.ALLOWLIST)
    assert all(row['category']=='US_ETF' and row['bid']=='20' and row['ask']=='20.01'
               for row in result['market_evidence'].values())


def test_old_campaign_cannot_be_reopened_and_recovery_is_separate(database):
    populate_prior(database)
    with pytest.raises(prior.Blocked,match='CONSUMED'):
        with prior.Journal(database).once():
            pytest.fail('old campaign reopened')
    recovery_journal=recovery.Journal(database)
    assert recovery_journal.events()==[]
    assert recovery_journal.namespace != prior.Journal(database).namespace


class Broker:
    def __init__(self):
        self.state=replace(snapshot(matched=True),equity=D('505.42'))
        self.sent=[]
        self.reads=0
    def refresh(self):return self.state
    def sell(self,order,observed,*,capability):
        self.journal.consume(capability,order,ACCOUNT,observed,NOW)
        self.sent.append(order)
        return {'client_order_id':order['client_order_id'],'order_id':'fixture'}
    def detail(self,client_id):
        self.reads+=1
        order=self.sent[-1]; qty=D(order['quantity'])
        positions=dict(self.state.positions); prices=dict(self.state.position_last_prices)
        del positions[order['symbol']];del prices[order['symbol']]
        proceeds=(qty*D(20)).quantize(D('.01'))
        self.state=replace(self.state,positions=positions,position_last_prices=prices,
            unsettled=self.state.unsettled+proceeds,cash=self.state.cash+proceeds,
            equity=self.state.equity)
        return {'client_order_id':client_id,'orders':[dict(order,order_id='fixture',status='FILLED',
            total_quantity=str(qty),filled_quantity=str(qty),filled_price='20',fees=[],commission={})]}


def test_separate_campaign_exact_four_one_use_no_retry(database):
    populate_prior(database)
    journal=recovery.Journal(database); broker=Broker();broker.journal=journal
    journal.append('DRY_RUN',{'review':1});journal.append('DRY_RUN',{'review':2})
    result=recovery.execute_once(broker,journal,ACCOUNT,clock=lambda:NOW,sleep=lambda _:None)
    assert result['status']=='COMPLETE'
    assert [row['symbol'] for row in broker.sent]==list(recovery.ALLOWLIST)
    assert broker.state.positions=={} and broker.reads==4
    with pytest.raises(recovery.Blocked,match='CONSUMED'):
        recovery.execute_once(Broker(),recovery.Journal(database),ACCOUNT,clock=lambda:NOW)


def test_execution_requires_exact_two_reviewed_dry_runs(database):
    populate_prior(database);journal=recovery.Journal(database);broker=Broker();broker.journal=journal
    with pytest.raises(recovery.Blocked,match='EXACT_REVIEWED'):
        recovery.execute_once(broker,journal,ACCOUNT,clock=lambda:NOW)
    journal.append('DRY_RUN',{'review':1})
    with pytest.raises(recovery.Blocked,match='EXACT_REVIEWED'):
        recovery.execute_once(broker,journal,ACCOUNT,clock=lambda:NOW)


def test_no_other_symbol_or_quantity_can_enter_recovery_order():
    with pytest.raises(recovery.Blocked):
        recovery.order_for('ICLN',D('0.05675'),'a'*32)
    with pytest.raises(recovery.Blocked):
        recovery.order_for('SMH',D('0.1'),'a'*32)
    assert not recovery.EXECUTION_APPROVED


def test_stale_quote_and_nonfinal_order_still_fail_closed():
    stale=snapshot();stale.quotes['SMH']['at']=NOW-timedelta(seconds=11)
    with pytest.raises(recovery.Blocked,match='QUOTE'):
        recovery.validate_snapshot(stale,QTY,ACCOUNT,NOW)
    with pytest.raises(recovery.Blocked,match='OPEN_OR_UNCERTAIN'):
        recovery.validate_snapshot(replace(snapshot(),open_orders=({'status':'PENDING'},)),QTY,ACCOUNT,NOW)


def test_stale_lit_does_not_block_fresh_current_smh():
    value=snapshot();value.quotes['LIT']['at']=NOW-timedelta(seconds=65)
    recovery.validate_snapshot(value,QTY,ACCOUNT,NOW,fresh_symbols=('SMH',))
    with pytest.raises(recovery.Blocked,match='STALE'):
        recovery.validate_snapshot(value,QTY,ACCOUNT,NOW,fresh_symbols=('LIT',))


class FixedDateTime(datetime):
    @classmethod
    def now(cls,tz=None):return NOW


def wire_recovery_broker(monkeypatch,journal=None):
    monkeypatch.setattr(recovery_boundary,'datetime',FixedDateTime)
    if journal is not None:
        monkeypatch.setattr(recovery_boundary,'RECOVERY_NAMESPACE',journal.namespace)
    broker=RecoveryBroker(RealCredentials('fixture-key','fixture-secret'),ACCOUNT,journal=journal)
    broker._account_id='fixture-account';broker._latest=snapshot(matched=True)
    return broker


def test_recovery_transport_cannot_be_constructed_into_write_authority(monkeypatch):
    broker=wire_recovery_broker(monkeypatch)
    monkeypatch.setattr(broker.http,'_make_http_response',lambda *a:pytest.fail('unsigned boundary bypass'))
    with pytest.raises(recovery.Blocked,match='CAPABILITY'):
        broker.sell(recovery.order_for('SMH',QTY['SMH'],'a'*32),broker._latest)
    assert not recovery.EXECUTION_APPROVED


@pytest.mark.parametrize('transport_failure',[False,True])
def test_recovery_post_consumes_capability_before_single_send(monkeypatch,database,transport_failure):
    populate_prior(database)
    journal=recovery.Journal(database)
    monkeypatch.setattr(recovery,'EXECUTION_APPROVED',True)
    broker=wire_recovery_broker(monkeypatch,journal)
    sent=[]
    class Response:
        status_code=200
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def iter_content(self,size):yield b'{"client_order_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","order_id":"fixture"}'
    class Session(requests.Session):
        def send(self,prepared,**kwargs):
            body=json.loads(prepared.body)
            assert prepared.method=='POST' and body['new_orders'][0]['symbol']=='SMH'
            assert sum(e=='CAPABILITY_CONSUMED' for e,_ in journal.events())==1
            sent.append(1)
            if transport_failure:raise TimeoutError()
            return Response()
    monkeypatch.setattr(recovery_boundary.requests,'Session',Session)
    order=recovery.order_for('SMH',QTY['SMH'],'a'*32)
    with journal.once():
        prior_hash=recovery.prior_blocked_evidence(database,ACCOUNT)
        journal.append('STARTED',dict(campaign=recovery.CAMPAIGN,account_binding=ACCOUNT,
            prior_evidence_hash=prior_hash,observation=snapshot().evidence()))
        journal.append('SUBMISSION_INTENT',order);cap=journal.issue(order,ACCOUNT,broker._latest,NOW)
        if transport_failure:
            with pytest.raises(recovery.Blocked,match='UNCERTAIN'):
                broker.sell(order,broker._latest,capability=cap)
        else:
            assert broker.sell(order,broker._latest,capability=cap)['order_id']=='fixture'
        with pytest.raises(recovery.Blocked,match='CONSUMED'):
            broker.sell(order,broker._latest,capability=cap)
        assert len(sent)==1


def start_recovery(journal):
    prior_hash=recovery.prior_blocked_evidence(journal.db,ACCOUNT)
    journal.append('STARTED',dict(campaign=recovery.CAMPAIGN,account_binding=ACCOUNT,
        prior_evidence_hash=prior_hash,observation=snapshot().evidence()))


def issue_consume_ack(journal,symbol='SMH'):
    order=recovery.order_for(symbol,QTY[symbol],uuid4().hex)
    journal.append('SUBMISSION_INTENT',order)
    cap=journal.issue(order,ACCOUNT,snapshot(),NOW)
    journal.consume(cap,order,ACCOUNT,snapshot(),NOW)
    journal.append('ACKNOWLEDGED',{'client_order_id':order['client_order_id']})
    return order,cap


def fill_payload(order):
    qty=D(order['quantity']);price=D(20);fees=D(0)
    return dict(symbol=order['symbol'],quantity=str(qty),price=str(price),fees_usd=str(fees),
        expected_net_cash_usd=str((qty*price-fees).quantize(D('.01'))),status='FILLED',
        client_order_id=order['client_order_id'])


def post_after_smh(*,wrong_positions=False,wrong_cash=False):
    remaining={s:q for s,q in QTY.items() if s!='SMH'}
    prices={s:LAST[s] for s in remaining}
    net=(QTY['SMH']*D(20)).quantize(D('.01'))
    value=recovery.Snapshot(ACCOUNT,NOW,remaining,prices,D(500),D('.93')+net,
        D('505.42'),D('500.93')+net,(),{},totals_match=True).evidence()
    if wrong_positions:value['positions']['SMH']=str(QTY['SMH'])
    if wrong_cash:value['unsettled']='99.99';value['cash']='599.99'
    return value


def test_first_capability_must_be_smh_not_esgu(database):
    populate_prior(database);journal=recovery.Journal(database)
    with journal.once():
        start_recovery(journal)
        order=recovery.order_for('ESGU',QTY['ESGU'],uuid4().hex)
        journal.append('SUBMISSION_INTENT',order)
        with pytest.raises(recovery.Blocked,match='SYMBOL_SEQUENCE'):
            journal.issue(order,ACCOUNT,snapshot(),NOW)


def test_second_capability_rejected_after_only_consume_and_ack(database):
    populate_prior(database);journal=recovery.Journal(database)
    with journal.once():
        start_recovery(journal);issue_consume_ack(journal)
        order=recovery.order_for('ESGU',QTY['ESGU'],uuid4().hex)
        journal.append('SUBMISSION_INTENT',order)
        with pytest.raises(recovery.Blocked,match='FILL_EVIDENCE'):
            journal.issue(order,ACCOUNT,snapshot(),NOW)


def test_second_capability_rejected_after_fill_without_post_fill_observation(database):
    populate_prior(database);journal=recovery.Journal(database)
    with journal.once():
        start_recovery(journal);first,_=issue_consume_ack(journal)
        journal.append('FILL',fill_payload(first))
        order=recovery.order_for('ESGU',QTY['ESGU'],uuid4().hex)
        journal.append('SUBMISSION_INTENT',order)
        with pytest.raises(recovery.Blocked,match='POST_FILL'):
            journal.issue(order,ACCOUNT,snapshot(),NOW)


@pytest.mark.parametrize('defect',['positions','cash'])
def test_second_capability_rejected_on_wrong_post_fill_reconciliation(database,defect):
    populate_prior(database);journal=recovery.Journal(database)
    with journal.once():
        start_recovery(journal);first,_=issue_consume_ack(journal)
        journal.append('FILL',fill_payload(first))
        journal.append('POST_FILL_OBSERVATION',post_after_smh(
            wrong_positions=defect=='positions',wrong_cash=defect=='cash'))
        order=recovery.order_for('ESGU',QTY['ESGU'],uuid4().hex)
        journal.append('SUBMISSION_INTENT',order)
        with pytest.raises(recovery.Blocked,match='POST_FILL'):
            journal.issue(order,ACCOUNT,snapshot(),NOW)


def test_event_reordering_is_rejected(database):
    populate_prior(database);journal=recovery.Journal(database)
    with journal.once():
        start_recovery(journal)
        order=recovery.order_for('SMH',QTY['SMH'],uuid4().hex)
        journal.append('ACKNOWLEDGED',{'client_order_id':order['client_order_id']})
        journal.append('SUBMISSION_INTENT',order)
        with pytest.raises(recovery.Blocked,match='SEQUENCE'):
            journal.issue(order,ACCOUNT,snapshot(),NOW)


def test_capability_has_sequence_and_head_binding_and_concurrent_consume_has_one_winner(database):
    from concurrent.futures import ThreadPoolExecutor
    populate_prior(database);journal=recovery.Journal(database)
    with journal.once():
        start_recovery(journal)
        order=recovery.order_for('SMH',QTY['SMH'],uuid4().hex)
        journal.append('SUBMISSION_INTENT',order)
        head=journal._event_head(journal.events())
        cap=journal.issue(order,ACCOUNT,snapshot(),NOW)
        assert cap.sequence_index==0 and cap.previous_event_head_hash==head
        def consume(_):
            try:journal.consume(cap,order,ACCOUNT,snapshot(),NOW);return 'CONSUMED'
            except recovery.Blocked as exc:return str(exc)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results=list(pool.map(consume,range(4)))
        assert results.count('CONSUMED')==1
        assert results.count('JOURNAL_CAPABILITY_ALREADY_CONSUMED')==3


def test_three_exact_fills_persist_when_lit_is_stale_when_reached(database):
    populate_prior(database);journal=recovery.Journal(database)
    journal.append('DRY_RUN',{'review':1});journal.append('DRY_RUN',{'review':2})
    broker=Broker();broker.journal=journal
    broker.state.quotes['LIT']['at']=NOW-timedelta(seconds=65)
    with pytest.raises(recovery.Blocked,match='STALE_OR_FUTURE_QUOTE'):
        recovery.execute_once(broker,journal,ACCOUNT,clock=lambda:NOW,sleep=lambda _:None)
    events=journal.events()
    assert [p['symbol'] for e,p in events if e=='FILL']==['SMH','ESGU','PHO']
    assert [row['symbol'] for row in broker.sent]==['SMH','ESGU','PHO']
    assert sum(e=='POST_FILL_OBSERVATION' for e,_ in events)==3
    assert not any(e=='SUBMISSION_INTENT' and p.get('symbol')=='LIT' for e,p in events)
    assert events[-1][0]=='BLOCKED' and events[-1][1]['blocker']=='STALE_OR_FUTURE_QUOTE'


def test_stale_current_symbol_cannot_issue_or_consume_capability(database):
    populate_prior(database);journal=recovery.Journal(database)
    stale=snapshot();stale.quotes['SMH']['at']=NOW-timedelta(seconds=11)
    with journal.once():
        start_recovery(journal)
        order=recovery.order_for('SMH',QTY['SMH'],uuid4().hex)
        journal.append('SUBMISSION_INTENT',order)
        with pytest.raises(recovery.Blocked,match='STALE'):
            journal.issue(order,ACCOUNT,stale,NOW)
    # A fresh capability is also unusable once its bound quote becomes stale.
    database2=LocalDatabase('postgres','m2_admin',schema='real_readiness_test_'+uuid4().hex)
    provision(database2)
    try:
        populate_prior(database2);journal2=recovery.Journal(database2)
        with journal2.once():
            start_recovery(journal2)
            order=recovery.order_for('SMH',QTY['SMH'],uuid4().hex)
            journal2.append('SUBMISSION_INTENT',order)
            cap=journal2.issue(order,ACCOUNT,snapshot(),NOW)
            with pytest.raises(recovery.Blocked,match='STALE'):
                journal2.consume(cap,order,ACCOUNT,stale,NOW)
            assert not any(e=='CAPABILITY_CONSUMED' for e,_ in journal2.events())
    finally:
        with database2.connect() as c:
            c.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(database2.schema)))


def test_stale_current_symbol_cannot_reach_post_boundary(monkeypatch,database):
    populate_prior(database);journal=recovery.Journal(database)
    monkeypatch.setattr(recovery,'EXECUTION_APPROVED',True)
    broker=wire_recovery_broker(monkeypatch,journal)
    with journal.once():
        start_recovery(journal)
        order=recovery.order_for('SMH',QTY['SMH'],uuid4().hex)
        journal.append('SUBMISSION_INTENT',order)
        cap=journal.issue(order,ACCOUNT,broker._latest,NOW)
        stale=replace(broker._latest,quotes={s:dict(q) for s,q in broker._latest.quotes.items()})
        stale.quotes['SMH']['at']=NOW-timedelta(seconds=11)
        broker._latest=stale
        monkeypatch.setattr(broker.http,'_make_http_response',lambda *a:pytest.fail('stale quote reached POST'))
        with pytest.raises(recovery.Blocked,match='STALE'):
            broker.sell(order,stale,capability=cap)
        assert not any(e=='CAPABILITY_CONSUMED' for e,_ in journal.events())
