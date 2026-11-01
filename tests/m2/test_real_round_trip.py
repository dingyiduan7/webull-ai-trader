from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor
import pytest
from psycopg import sql
from autonomous_trader.durable.ledger import Ledger,LocalDatabase,migrate,grant_writer
from autonomous_trader.durable.models import Mark,RefreshPermit
from autonomous_trader.durable.projection import round_trip_capacity,Rejected

@pytest.fixture
def real_h(harness_factory,context):
 object.__setattr__(context,'capabilities',context.capabilities.model_copy(update={'environment':'REAL'}))
 schema='real_trader_test_'+uuid4().hex[:12]
 admin=LocalDatabase('postgres','m2_admin',schema=schema);migrate(admin);grant_writer(admin,'m2_writer')
 try:
  h=harness_factory(balance='500',account_prefix='real-',ledger=Ledger(LocalDatabase('postgres','m2_writer',schema=schema),execution_scope='REAL'))
  h.send(Mark(instrument_id='stock-0',bid_usd='1',observed_at=h.at,source_id='fixture'));h.reconcile()
  yield h
 finally:
  with admin.connect() as c:c.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))

def order(h,factory,side='BUY',symbol='stock-0',quantity='1'):
 return factory(h,quantity=quantity,price='1',stop='0.95',side=side,symbol=symbol).model_copy(update={'environment':'REAL'})

def complete(h,factory):
 h.trade(order(h,factory));h.trade(order(h,factory,'SELL_OWNED'))

def test_ten_reserved_round_trips_not_two_daily_entries(real_h,order_factory):
 h=real_h
 for _ in range(10):complete(h,order_factory)
 assert round_trip_capacity(h.state,h.at)==(10,0)
 h.admit(order(h,order_factory))
 assert h.ledger.rebuild(h.account_id)==h.state

def test_proven_zero_fill_releases_but_filled_exit_does_not(real_h,order_factory):
 h=real_h;first=order(h,order_factory);h.admit(first);h.finalize(first,'CANCELED');h.reconcile()
 assert round_trip_capacity(h.state,h.at)==(0,0)
 entry=order(h,order_factory);h.trade(entry)
 assert round_trip_capacity(h.state,h.at)==(1,0)
 h.trade(order(h,order_factory,'SELL_OWNED'))
 assert round_trip_capacity(h.state,h.at)==(1,0)
 assert h.ledger.rebuild(h.account_id)==h.state

def test_last_capacity_is_reserved_atomically(real_h,order_factory):
 h=real_h
 for _ in range(9):complete(h,order_factory)
 h.send(Mark(instrument_id='stock-1',bid_usd='1',observed_at=h.at,source_id='fixture'));h.reconcile()
 def attempt(i):
  try:h.admit(order(h,order_factory,symbol='stock-'+str(i)));return 'ACCEPTED'
  except Rejected as exc:return str(exc)
 with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(attempt,range(2)))
 assert results.count('ACCEPTED')>=1
 assert all(r in ('ACCEPTED','STALE_ACCOUNT_VERSION') for r in results)
 assert round_trip_capacity(h.state,h.at)==(9,results.count('ACCEPTED'))

def test_add_on_and_partial_exit_share_position_ticket_until_flat(real_h,order_factory):
 h=real_h;h.trade(order(h,order_factory));h.trade(order(h,order_factory))
 assert round_trip_capacity(h.state,h.at)==(1,0)
 h.trade(order(h,order_factory,'SELL_OWNED'))
 assert round_trip_capacity(h.state,h.at)==(1,0)
 h.trade(order(h,order_factory,'SELL_OWNED'))
 complete(h,order_factory)
 assert round_trip_capacity(h.state,h.at)==(2,0)
 assert h.ledger.rebuild(h.account_id)==h.state
