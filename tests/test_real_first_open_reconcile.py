"""A freshly created REAL ledger is unreconciled; first open must record one exact-flat reconciliation
without leaving a lease that blocks the runner (regression: REAL_DAILY_SETTLEMENT_UNKNOWN on day one)."""
import time
from datetime import timedelta
from decimal import Decimal as D
from uuid import uuid4
from psycopg import sql
from autonomous_trader.durable.ledger import Ledger,LocalDatabase,migrate
from autonomous_trader.durable.models import CreateAccount,Reconcile
from autonomous_trader.real_state import binding


def test_first_open_reconciliation_marks_settlement_known_and_releases_lease(now,calendar,context):
    db=LocalDatabase('postgres','m2_admin',schema='real_trader_test_'+uuid4().hex);migrate(db)
    ledger=Ledger(db,execution_scope='REAL');aid=binding('first-open')
    days=tuple(now.date()+timedelta(days=i) for i in range(30) if calendar.settlement_day(now.date()+timedelta(days=i)))
    caps=context.capabilities.model_copy(update={'environment':'REAL'})
    try:
        ledger.create(CreateAccount(account_id=aid,experiment_id='real-test',owner_id='owner',initial_settled_usd='512.41',
            session=calendar.session(now.date()),instruments=(context.instrument,),capabilities=caps,settlement_days=days,
            mark_max_age_seconds=2,reconciliation_max_age_seconds=10,opening_receivables=()),event_id=uuid4().hex,at=now)
        assert ledger.rebuild(aid)['reconciliation']['settlement_known'] is False
        holder='real-first-open'
        leader=ledger.acquire_leader(aid,holder,event_id=uuid4().hex,at=now,ttl_seconds=1)
        ledger.execute(aid,Reconcile(settled_usd=D('512.41'),unsettled_usd=D('0'),positions=(),outstanding_intent_ids=(),
            evidence_id='first-open',settlement_known=True),event_id=uuid4().hex,expected_version=leader['version'],
            holder=holder,token=leader['leader']['token'],at=now)
        state=ledger.rebuild(aid)
        assert state['reconciliation']['settlement_known'] is True and not state['incidents']
        assert not any(state['latches'].values())
        time.sleep(1.5)
        other=ledger.acquire_leader(aid,'runner',event_id=uuid4().hex,at=now,ttl_seconds=30)
        assert other['leader']['holder']=='runner'
    finally:
        with db.connect() as c:c.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(db.schema)))
