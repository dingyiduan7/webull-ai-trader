from datetime import datetime,timezone
from decimal import Decimal
from types import SimpleNamespace
import json
from autonomous_trader.real_feedback import economic_feedback
class Connection:
 def __enter__(self):return self
 def __exit__(self,*args):pass
 def execute(self,*args):return self
 def fetchall(self):return []

def test_economics_feed_preserves_costs_and_unknown_attribution():
 store=SimpleNamespace(db=SimpleNamespace(connect=Connection),model_budget_evidence=lambda *a,**kw:[
  ('MODEL_WORK_CLAIM',{'id':'paid','reserved_usd':'1'}),
  ('MODEL_WORK_RESULT',{'id':'paid','cost_usd':'0.10','ai':{'model_work_id':'paid','estimated_cost_usd':'0.10'}})])
 state={'executions':{},'orders':{},'instruments':{},'daily_realized':{'2026-10-01':'-2.00'}}
 memory=economic_feedback(state,store,datetime(2026,10,1,20,tzinfo=timezone.utc))
 rows=[json.loads(r) for r in memory]
 assert len(rows)==2 and rows[0]['realized_after_broker_fees']=='-2.00'
 assert Decimal(rows[0]['net_after_known_cost'])==Decimal('-2.10')
 assert rows[1]['period']=='exchange_week'
 assert all(len(r)<=2000 for r in memory)

def test_round_trip_capacity_rolls_exchange_week_and_preserves_busted_used_slot():
 from autonomous_trader.durable.projection import round_trip_capacity
 state={'session':{'label':'2026-10-01','opens_at':'2026-10-01T13:30:00Z',
  'closes_at':'2026-10-01T20:00:00Z'},'orders':{}}
 for i,day in enumerate(('2026-09-25','2026-09-28','2026-10-01')):
  state['orders'][str(i)]={'intent':{'side':'BUY','deadline':day+'T15:00:00Z'},
   'filled_quantity':'0','round_trip_used':True,'final':True,'phase':'FILLED'}
 assert round_trip_capacity(state,datetime(2026,10,1,tzinfo=timezone.utc))==(2,0)
