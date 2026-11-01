from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
import pytest
from autonomous_trader.real_market import RealMarketSource,_quote_is_fresh
from autonomous_trader.real_runner import RealRunner

NOW=datetime(2026,10,1,18,tzinfo=timezone.utc)
def quote(age=0,symbol='AAA',iid='1'):
 return {'symbol':symbol,'instrument_id':iid,'quote_time':int((NOW-timedelta(seconds=age)).timestamp()*1000)}

def source(rows):
 s=object.__new__(RealMarketSource);s.clock=lambda:NOW;s.last_quote_refresh_evidence=[]
 calls=[]
 def read(request):calls.append(request);return rows.pop(0)
 s.http=SimpleNamespace(read=read,last_received_at=NOW)
 return s,calls

def test_exact_stale_quote_retry_recovers_without_relaxing_limit():
 s,calls=source([[quote()]])
 q,received=s._refresh_quote('AAA','1',quote(7),NOW,2)
 assert _quote_is_fresh(q,received,NOW,2) and len(calls)==1
 assert [x['fresh'] for x in s.last_quote_refresh_evidence]==[False,True]

def test_stale_quote_retry_is_bounded_and_still_fails_freshness():
 s,calls=source([[quote(7)],[quote(7)]])
 q,received=s._refresh_quote('AAA','1',quote(7),NOW,2)
 assert not _quote_is_fresh(q,received,NOW,2) and len(calls)==2
 assert len(s.last_quote_refresh_evidence)==3

def test_retry_never_accepts_wrong_instrument():
 s,_=source([[quote(iid='wrong')]])
 with pytest.raises(ValueError,match='REAL_PROFILE_QUOTE_BINDING'):
  s._refresh_quote('AAA','1',quote(7),NOW,2)

def test_fresh_quote_does_not_add_reads():
 s,calls=source([]);s._refresh_quote('AAA','1',quote(),NOW,2)
 assert calls==[]

@pytest.mark.parametrize('rejected',[True,False])
def test_paid_decision_is_retained_before_later_validation(rejected):
 events=[];r=object.__new__(RealRunner);r.clock=lambda:NOW
 r.emit=lambda kind,payload:events.append((kind,payload))
 decision={'action':'ABSTAIN','reason':'No net edge after costs'}
 class Brain:
  last_call={'model_work_id':'work','validation_stage':'SEMANTICS'}
  def fast_decide(self,*args,**kwargs):
   self.decision_evidence_sink({'decision':decision,'model':'gpt-6-astra'})
   if rejected:raise ValueError('STALE_MARKET')
   return {'decision':decision}
 r.brain_factory=Brain
 context=SimpleNamespace(as_of=NOW,expires_at=NOW+timedelta(seconds=30),instruments=())
 if rejected:
  with pytest.raises(ValueError,match='STALE_MARKET'):r._call(context,'ENTRY')
 else:r._call(context,'ENTRY')
 assert events[0][0]=='REAL_PAID_DECISION_EVIDENCE'
 assert events[0][1]['decision']==decision
 assert events[1][1]['outcome']==('REJECTED' if rejected else 'MODEL_VALIDATED')

def test_actual_client_retains_json_before_canonical_rejection(monkeypatch):
 import autonomous_trader.decision_api.astra_probe as api
 client=object.__new__(api.AstraClient);client.model='gpt-6-astra';client.reasoning='medium'
 client.last_call={};seen=[];client.decision_evidence_sink=seen.append
 client._structured_call=lambda request:('{"action":"CLOSE","reason":"Exit thesis invalidated"}',10,{},'0.01')
 monkeypatch.setattr(api,'fast_alpha_request',lambda *args,**kwargs:{})
 context=SimpleNamespace(as_of=NOW,expires_at=NOW+timedelta(seconds=30))
 with pytest.raises(api.PreflightError):client.fast_decide(context,mode='POSITION',hard_policy='',clock=lambda:NOW)
 assert seen[0]['decision']['reason']=='Exit thesis invalidated'
