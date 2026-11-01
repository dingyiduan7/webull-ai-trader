from datetime import timedelta
from decimal import Decimal as D
from uuid import uuid4
import pytest
from psycopg import sql
from autonomous_trader.durable.ledger import Ledger,LocalDatabase,migrate
from autonomous_trader.durable.models import CreateAccount,OpeningReceivable,Mark,Reconcile,Admit,ConsumePermit,ConfirmSettlement
from autonomous_trader.real_state import binding
from autonomous_trader.contracts import OrderIntent


@pytest.fixture
def real(now,calendar,context):
    db=LocalDatabase('postgres','m2_admin',schema='real_trader_test_'+uuid4().hex)
    migrate(db)
    ledger=Ledger(db,execution_scope='REAL')
    aid=binding('fixture-real')
    days=tuple(now.date()+timedelta(days=i) for i in range(30) if calendar.settlement_day(now.date()+timedelta(days=i)))
    caps=context.capabilities.model_copy(update={'environment':'REAL'})
    create=CreateAccount(account_id=aid,experiment_id='real-test',owner_id='owner',initial_settled_usd='500',
        session=calendar.session(now.date()),instruments=(context.instrument,),capabilities=caps,settlement_days=days,
        mark_max_age_seconds=2,reconciliation_max_age_seconds=10,
        opening_receivables=(OpeningReceivable(evidence_id='liquidation',amount_usd='5.44',settlement_date=days[1]),))
    ledger.create(create,event_id=uuid4().hex,at=now)
    leader=ledger.acquire_leader(aid,'writer',event_id=uuid4().hex,at=now,ttl_seconds=60)
    def send(command,at=now):
        return ledger.execute(aid,command,event_id=uuid4().hex,expected_version=ledger.snapshot(aid)['version'],
            holder='writer',token=leader['leader']['token'],at=at)
    send(Mark(instrument_id=context.instrument.instrument_id,bid_usd='100',observed_at=now,source_id='WEBULL_REAL_READ_ONLY'))
    send(Reconcile(settled_usd='500',unsettled_usd='5.44',positions=(),outstanding_intent_ids=(),evidence_id='real-observed',settlement_known=True))
    try:yield ledger,aid,send,create
    finally:
        with db.connect() as c:c.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(db.schema)))


def test_real_opening_receivable_never_counts_as_profit_or_settled_cash(real):
    ledger,aid,send,create=real
    state=ledger.snapshot(aid)
    assert state['execution_scope']=='REAL'
    assert D(state['cash']['settled_usd'])==500 and D(state['cash']['unsettled_usd'])==D('5.44')
    assert D(state['cash']['realized_net_usd'])==0 and D(state['cash']['liquidation_equity_usd'])==D('505.44')
    assert ledger.rebuild(aid)==state
    with pytest.raises(ValueError):Ledger(ledger.config,execution_scope='OFFLINE')
    with pytest.raises(ValueError):Ledger(LocalDatabase('postgres','m2_admin',schema='mvp_sandbox'),execution_scope='REAL')


def test_real_current_observation_admits_without_retimestamping_source(real,intent,now):
    from autonomous_trader.durable.projection import fresh_mark,Rejected
    ledger,aid,send,create=real
    received=now+timedelta(seconds=7)
    send(Mark(instrument_id=create.instruments[0].instrument_id,bid_usd='100',observed_at=now,
        received_at=received,valid_until=now+timedelta(seconds=10),source_id='WEBULL_REAL_READ_ONLY'),at=received)
    send(Reconcile(settled_usd='500',unsettled_usd='5.44',positions=(),outstanding_intent_ids=(),
        evidence_id='new-current-observation',settlement_known=True),at=received)
    order=OrderIntent.model_validate({**intent(price='100',stop='99.5').model_dump(),
        'environment':'REAL','account_id':aid,'instrument_id':create.instruments[0].instrument_id,
        'quantity':'1','capability_version':create.capabilities.version,'deadline':now+timedelta(seconds=30)})
    state=send(Admit(intent=order,fee_allowance_usd='0',exit_cost_usd='0',stress_allowance_usd='0',
        latest_exit_at=create.session.liquidation_at,max_holding_seconds=300),at=received)
    assert order.intent_id in state['orders']
    from datetime import datetime
    assert datetime.fromisoformat(state['marks'][order.instrument_id]['observed_at'])==now
    assert ledger.rebuild(aid)==state
    with pytest.raises(Rejected,match='STALE_MARKET'):
        fresh_mark(state,order.instrument_id,now+timedelta(seconds=15))


@pytest.mark.parametrize('defect',['future-source','old-session','future-receipt','expired','past-close'])
def test_real_invalid_receipt_bound_mark_is_rejected(real,now,defect):
    ledger,aid,send,create=real
    received=now+timedelta(seconds=7)
    values=dict(instrument_id=create.instruments[0].instrument_id,bid_usd='100',observed_at=now,
        received_at=received,valid_until=now+timedelta(seconds=10),source_id='WEBULL_REAL_READ_ONLY')
    if defect=='future-source':values['observed_at']=received+timedelta(seconds=1)
    if defect=='old-session':values['observed_at']=now-timedelta(days=1)
    if defect=='future-receipt':values['received_at']=received+timedelta(seconds=1)
    if defect=='expired':values['valid_until']=received
    if defect=='past-close':values['valid_until']=create.session.closes_at+timedelta(seconds=1)
    before=ledger.snapshot(aid)
    with pytest.raises(ValueError):send(Mark(**values),at=received)
    after=ledger.snapshot(aid)
    assert after['marks']==before['marks'] and after['cash']==before['cash']
    assert ledger.rebuild(aid)==after


def test_real_permit_is_one_use_and_offline_intent_is_rejected(real,intent,now):
    ledger,aid,send,create=real
    order=OrderIntent.model_validate({**intent().model_dump(),'environment':'REAL','account_id':aid,
        'instrument_id':create.instruments[0].instrument_id,'quantity':'1','limit_price_usd':'100','maximum_entry_usd':'100',
        'capability_version':create.capabilities.version,'deadline':now+timedelta(seconds=30)})
    command=Admit(intent=order,fee_allowance_usd='0',exit_cost_usd='0',stress_allowance_usd='0',
        latest_exit_at=create.session.liquidation_at,max_holding_seconds=300)
    with pytest.raises(ValueError,match='ENVIRONMENT'):send(command.model_copy(update={'intent':order.model_copy(update={'environment':'OFFLINE'})}))
    state=send(command);nonce=state['outbox'][order.intent_id]['nonce']
    assert state['permits'][nonce]['environment']=='REAL'
    send(ConsumePermit(nonce=nonce))
    with pytest.raises(ValueError,match='PERMIT_UNAVAILABLE'):send(ConsumePermit(nonce=nonce))


def test_real_symbol_bound_tracks_forty_percent_of_current_equity(real,intent,now):
    ledger,aid,send,create=real
    base=intent(quantity='2',price='101',stop='99.5')
    order=OrderIntent.model_validate({**base.model_dump(),'environment':'REAL','account_id':aid,
        'instrument_id':create.instruments[0].instrument_id,
        'capability_version':create.capabilities.version,'deadline':now+timedelta(seconds=30)})
    too_large=OrderIntent.model_validate({**intent(key='buy-2',quantity='2',price='101.09',stop='99.5').model_dump(),
        'environment':'REAL','account_id':aid,'instrument_id':create.instruments[0].instrument_id,
        'capability_version':create.capabilities.version,'deadline':now+timedelta(seconds=30)})
    with pytest.raises(ValueError,match='REAL_SYMBOL_SCOPE_LIMIT'):
        send(Admit(intent=too_large,fee_allowance_usd='0',exit_cost_usd='0',stress_allowance_usd='0',
            latest_exit_at=create.session.liquidation_at,max_holding_seconds=300))
    state=send(Admit(intent=order,fee_allowance_usd='0',exit_cost_usd='0',stress_allowance_usd='0',
        latest_exit_at=create.session.liquidation_at,max_holding_seconds=300))
    assert order.intent_id in state['orders']


def test_real_opening_receivable_cannot_settle_early_or_with_wrong_amount(real,now):
    ledger,aid,send,create=real
    due=create.opening_receivables[0].settlement_date
    with pytest.raises(ValueError,match='SETTLEMENT'):
        send(ConfirmSettlement(execution_id='external-opening-liquidation',amount_usd='5.44',effective_on=due,evidence_id='x'))
    assert D(ledger.snapshot(aid)['cash']['settled_usd'])==500


def test_real_selected_mark_is_durable_before_admission_without_candidate_writes(real,intent,now):
    from types import SimpleNamespace
    from autonomous_trader.real_trading import RealController,RealPacket
    from autonomous_trader.contracts import AlphaContext,AlphaInstrumentContext
    ledger,aid,send,create=real
    at=now+timedelta(seconds=7)
    item=create.instruments[0]
    quote=AlphaInstrumentContext(instrument_id=item.instrument_id,symbol=item.ticker,quote_at=now,
        bid_usd='100',ask_usd='100.01',price_increment_usd='0.01',quantity_increment='1')
    context=AlphaContext(as_of=at,expires_at=at+timedelta(seconds=20),session_open=create.session.opens_at,
        session_close=create.session.closes_at,liquidation_at=create.session.liquidation_at,
        instruments=(quote,),settled_cash_usd='500',remaining_risk_usd='0',reconciliation_ready=True,settlement_known=True)
    packet=RealPacket(account_binding=aid,observed_at=at,settled=D(500),unsettled=D('5.44'),equity=D('505.44'),
        positions={},open_client_ids=(),instruments=create.instruments,context=context,evidence_id='selected-current',
        quote_received_at={item.instrument_id:at},market_data_evidence={})
    controller=RealController(ledger,aid,lambda:at);controller.holder='writer'
    before=ledger.snapshot(aid)['marks']
    controller.refresh(packet)
    assert ledger.snapshot(aid)['marks']==before
    order=OrderIntent.model_validate({**intent(price='100',stop='99.5').model_dump(),
        'environment':'REAL','account_id':aid,'instrument_id':item.instrument_id,'quantity':'1',
        'capability_version':create.capabilities.version,'deadline':at+timedelta(seconds=30)})
    nonce=controller.admit(order,SimpleNamespace(latest_exit_at=create.session.liquidation_at))
    state=ledger.snapshot(aid)
    assert state['outbox'][order.intent_id]['nonce']==nonce
    assert state['marks'][item.instrument_id]['received_at']==at.isoformat().replace('+00:00','Z')
    assert state['marks'][item.instrument_id]['observed_at']==now.isoformat().replace('+00:00','Z')
    assert ledger.rebuild(aid)==state
