from datetime import timedelta
from dataclasses import replace
from types import SimpleNamespace
import pytest
from test_real_market_waiting import make_runner
from test_real_daily import NOW
from autonomous_trader.mvp.config import reviewed_calendar
from autonomous_trader.reference import NEW_YORK


def setup(tmp_path,monkeypatch):
    runner,a,packet,state,samples,account,reconciled,failures=make_runner(tmp_path,monkeypatch)
    starts=[];events=[];checks=[]
    runner.prepare_waiting_market=lambda:packet
    def check(p,now):
        checks.append(now);p.validate(now,entry=True);return {}
    def read_checked(fetch,clock,**kwargs):
        # Current runtime contract: authority audit, acquire, then current check.
        value=fetch();return value,runner.gate.check(value,clock())
    runner.gate=SimpleNamespace(check=check,read_checked=read_checked)
    runner.controller.refresh=lambda p:None
    runner._record_reconciliation_success=lambda *a:False
    runner.context=lambda p:p.context
    runner._start=lambda context,mode:starts.append((context,mode))
    runner.emit=lambda kind,payload,**kw:events.append((kind,payload))
    class Connection:
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def execute(self,*a):return self
        def fetchone(self):return None
    runner.store=SimpleNamespace(db=SimpleNamespace(connect=Connection))
    runner.source.refresh=lambda *a:pytest.fail('same-cycle packet must not be discarded for another refresh')
    runner.source.discover=lambda:pytest.fail('same-cycle discovery must not be repeated')
    return runner,packet,starts,events,checks


def test_ready_packet_reaches_natural_slot_without_next_poll_or_rediscovery(tmp_path,monkeypatch):
    r,p,starts,events,checks=setup(tmp_path,monkeypatch)
    assert r._wait_for_market_data()=='PORTFOLIO_EVALUATION'
    assert starts==[(p.context,'ENTRY')]
    assert checks==[NOW]
    assert [kind for kind,_ in events]==['REAL_PORTFOLIO_SLOT']
    assert not r.controller.waiting_for_market_data


def test_aged_packet_returns_waiting_without_recursive_preparation(tmp_path,monkeypatch):
    r,p,starts,events,checks=setup(tmp_path,monkeypatch)
    prepared=[]
    def prepare():
        prepared.append(True);r.clock=lambda:NOW+timedelta(seconds=31)
        return replace(p,observed_at=NOW+timedelta(seconds=31))
    r.prepare_waiting_market=prepare
    assert r._wait_for_market_data()=='WAITING_FOR_MARKET_DATA'
    assert len(prepared)==1 and not starts and r.controller.waiting_for_market_data
    assert events[-1][1]['reason']=='STALE_REAL_CONTEXT'


@pytest.mark.parametrize('blocked',['latch','recovery','used-slot','closed-entry'])
def test_same_cycle_handoff_preserves_normal_entry_controls(tmp_path,monkeypatch,blocked):
    r,p,starts,events,checks=setup(tmp_path,monkeypatch)
    r.controller.waiting_for_market_data=False
    if blocked=='latch':r.controller.state['latches']['daily']=True
    if blocked=='recovery':r.controller.recovery_only=True
    if blocked=='used-slot':
        class Connection:
            def __enter__(self):return self
            def __exit__(self,*a):pass
            def execute(self,*a):return self
            def fetchone(self):return (1,)
        r.store.db.connect=Connection
    session=reviewed_calendar().session(NOW.astimezone(NEW_YORK).date())
    if blocked=='closed-entry':monkeypatch.setattr(type(session),'permits_entry',lambda self,now:False)
    r._advance_packet(p,session,prepared=True)
    assert not starts


def test_ordinary_cycle_still_discovers_before_entry(tmp_path,monkeypatch):
    r,p,starts,events,checks=setup(tmp_path,monkeypatch);calls=[]
    r.controller.waiting_for_market_data=False
    r.source.discover=lambda:calls.append('discover')
    r.source.refresh=lambda state:calls.append('refresh') or p
    session=reviewed_calendar().session(NOW.astimezone(NEW_YORK).date())
    assert r._advance_packet(p,session)=='PORTFOLIO_EVALUATION'
    assert calls==['discover','refresh'] and len(starts)==1


def test_certificate_refresh_batches_broad_candidates_without_truncation():
    from scripts.run_real_daily import coverage_snapshots
    from datetime import timedelta
    symbols=tuple('S'+chr(65+i) for i in range(23))
    calls=[]
    class Http:
        last_received_at=NOW
        def read(self,request):
            params=request.get_query_params()
            group=params['symbols']
            if isinstance(group,str):group=group.split(',')
            calls.append(tuple(group))
            self.last_received_at=NOW+timedelta(seconds=len(calls))
            return [{'symbol':symbol} for symbol in group]
    quotes,receipts=coverage_snapshots(Http(),symbols)
    assert [len(group) for group in calls]==[20,3]
    assert tuple(row['symbol'] for row in quotes)==symbols
    assert receipts[symbols[0]]==NOW+timedelta(seconds=1)
    assert receipts[symbols[-1]]==NOW+timedelta(seconds=2)


def test_prepared_handoff_does_not_repeat_slow_gate_or_reconciliation(tmp_path,monkeypatch):
    r,p,starts,events,checks=setup(tmp_path,monkeypatch)
    now=[NOW+timedelta(seconds=5)];reconciliations=[]
    r.clock=lambda:now[0]
    def check(packet,at):
        checks.append(at);packet.validate(at,entry=True)
        now[0]+=timedelta(seconds=3)
        return {}
    def reconcile(packet):
        packet.validate(now[0],entry=True);reconciliations.append(now[0])
        now[0]+=timedelta(seconds=1)
    r.gate.check=check;r.controller.refresh=reconcile
    assert r._wait_for_market_data()=='PORTFOLIO_EVALUATION'
    assert len(checks)==len(reconciliations)==len(starts)==1
    assert now[0]==NOW+timedelta(seconds=9)
