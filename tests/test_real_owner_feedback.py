from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
import json
import pytest
from autonomous_trader.contracts import AlphaContext,AlphaInstrumentContext
from autonomous_trader.real_runner import RealRunner
from autonomous_trader.real_feedback import owner_review_feedback,supplied_feedback_identity,trading_manual_feedback,supplied_manual_identity
from autonomous_trader.decision_api.astra_probe import fast_alpha_request

NOW=datetime(2026,10,1,18,tzinfo=timezone.utc)
class Connection:
 def __enter__(self):return self
 def __exit__(self,*args):pass
 def execute(self,*args):return self
 def fetchall(self):return []

@pytest.mark.parametrize('mode',['ENTRY','POSITION'])
def test_real_context_to_actual_request_and_evidence_contains_owner_memory_and_original_thesis(mode):
 from autonomous_trader.decision_api.brain import DECISION_MODEL
 r=object.__new__(RealRunner);r.clock=lambda:NOW
 r.store=SimpleNamespace(db=SimpleNamespace(connect=Connection),model_budget_evidence=lambda *a,**kw:[])
 state={'cash':{'liquidation_equity_usd':'505.44','reserved_usd':'0'},'day_start_equity':'505.44',
  'reconciliation':{'ready':True},'orders':{'buy':{'intent':{'plan_id':'plan'}}},
  'positions':{'stock-0':{'quantity':'1','basis_usd':'10','mark_usd':'10','stop_usd':'9',
   'originating_orders':['buy'],'open_risk_usd':'1'}},'executions':{},'instruments':{},'daily_realized':{}}
 r.controller=SimpleNamespace(state=state)
 r.plan=lambda _:SimpleNamespace(instrument_id='stock-0',symbol='S0',latest_exit_at=NOW+timedelta(hours=1),
  target_usd=12,thesis='Immutable entry thesis: catalyst evidence must persist',strategy_label='discretionary',
  entry_limit_usd=10,original_stop_usd=9,next_review_at=NOW+timedelta(minutes=5),version=2)
 quote=AlphaInstrumentContext(instrument_id='stock-0',symbol='S0',quote_at=NOW,bid_usd='10',ask_usd='10.01',
  price_increment_usd='0.01',quantity_increment='1',reference_price_usd='10',spread_bps='10')
 original=AlphaContext(as_of=NOW,expires_at=NOW+timedelta(seconds=30),session_open=NOW-timedelta(hours=3),
  session_close=NOW+timedelta(hours=2),liquidation_at=NOW+timedelta(hours=2,minutes=-5),instruments=(quote,),
  positions=(),settled_cash_usd='505.44',remaining_risk_usd='0',reconciliation_ready=True,settlement_known=True)
 context=r.context(SimpleNamespace(context=original,settled=505.44))
 requests=[];events=[];r.emit=lambda kind,payload:events.append((kind,payload))
 class Brain:
  last_call={'model_work_id':'fake-work'}
  def fast_decide(self,context,*,mode,hard_policy,clock):
   requests.append(fast_alpha_request(context,mode=mode,hard_policy=hard_policy,model=DECISION_MODEL))
   value={'decision':{'action':'ABSTAIN' if mode=='ENTRY' else 'HOLD'},'model':'gpt-6-astra'}
   self.decision_evidence_sink(value);return value
 r.brain_factory=Brain;r._call(context,mode)
 payload=json.loads(requests[0]['input'][1]['content'])['context']
 # R4: Claude decisions carry the playbook in the cached system prompt; the
 # per-call memory is the runtime scope, strategy scoreboard and economics.
 kinds=[json.loads(block).get('kind') for block in payload['recent_outcomes']]
 assert kinds[:3]==['runtime_scope_snapshot','lessons_ledger','strategy_scoreboard']
 assert not any(block in payload['recent_outcomes'] for block in owner_review_feedback()+trading_manual_feedback())
 assert events[0][1]['trading_manual'] is None and events[0][1]['owner_review_feedback'] is None
 assert payload['positions'][0]['original_thesis']=='Immutable entry thesis: catalyst evidence must persist'
 assert events[0][1]['model_work_id']=='fake-work'
 assert requests[0]['store'] is False

def test_feedback_is_bounded_classified_and_not_claimed_when_not_supplied():
 blocks=owner_review_feedback()
 assert all(len(block)<=2000 for block in blocks)
 assert len(blocks)==7
 assert len({json.loads(block)['sha256'] for block in blocks})==1
 assert supplied_feedback_identity(SimpleNamespace(recent_outcomes=blocks[:-1])) is None
 assert supplied_feedback_identity(SimpleNamespace(recent_outcomes=blocks)) is not None
 assert all('account_id' not in json.loads(block) for block in blocks)


def test_manual_compact_identity_requires_exact_supplied_digest():
 blocks=trading_manual_feedback()
 assert len(blocks)==3
 assert sum(len(block) for block in blocks)<=6500
 assert all(len(block)<=2600 for block in blocks)
 identity=supplied_manual_identity(SimpleNamespace(recent_outcomes=blocks))
 assert identity['version']=='trading-manual-v1.3-20261002'
 assert identity['source_sha256']=='6363321c15cfac29e2e97110e0050fbca8a5ccb5261755a1d22137cf61a0fd77'
 assert identity['delivery']=='COMPACT_DIGEST_NOT_FULL_MANUAL'
 assert supplied_manual_identity(SimpleNamespace(recent_outcomes=blocks[:-1])) is None
 assert supplied_manual_identity(SimpleNamespace(recent_outcomes=(blocks[0]+' ',blocks[1]))) is None
 assert all(json.loads(block)['authority']=='RESEARCH_CONTEXT_NOT_EXECUTION_AUTHORITY' for block in blocks)
