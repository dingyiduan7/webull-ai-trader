"""Every history request the market source builds must pass the read-only broker gate.

Regression: the source requested 400-bar M1 and daily batches while the gate only
allowed count==5, so the first live preparation died with REAL_READ_ROUTE_REQUIRED
(and daily/regime reads failed silently)."""
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
import pytest
from autonomous_trader import real_market as rm
from autonomous_trader.real_strategy_broker import RealStrategyHttp,_read_only_history_batch
from autonomous_trader.sandbox.http import build_request,Operation


class Recorder(RealStrategyHttp):
    def __init__(self):self.sent=[];self.last_received_at=None
    def _send(self,request,*,authorize=None,read_only_post=False):
        self.sent.append(request.get_body_params())
        return {'result':[]}


def source(http):
    now=datetime(2026,10,13,15,0,tzinfo=timezone.utc)
    s=rm.RealMarketSource.__new__(rm.RealMarketSource)
    s.http=http;s.clock=lambda:now;s.bar_cache={};s.profile_cache={};s.focused=False
    session=SimpleNamespace(opens_at=now-timedelta(minutes=330),label=now.date())
    refs=[SimpleNamespace(ticker=t,instrument_id=str(i)) for i,t in enumerate(('AAA','BBB'))]
    return s,session,refs


def swallow(call,*a):
    try:call(*a)
    except Exception:pass


def test_session_minute_bars_pass_gate():
    http=Recorder();s,session,refs=source(http);swallow(s._refresh_batch_bars,refs,session)
    assert http.sent and http.sent[0]['timespan']=='M1' and int(http.sent[0]['count'])>5


def test_daily_bars_pass_gate():
    http=Recorder();s,session,refs=source(http);s._refresh_daily(refs,session)
    assert http.sent and http.sent[0]['timespan']=='D'


def test_market_regime_bars_pass_gate():
    http=Recorder();s,session,refs=source(http);swallow(s._market_regime,session)
    assert http.sent and http.sent[0]['symbols']==[rm.MARKET_SYMBOL]


@pytest.mark.parametrize('mutate',[
    lambda r:r.set_count('401'),lambda r:r.set_count('4'),lambda r:r.set_count('-5'),lambda r:r.set_count(True),
    lambda r:r.set_timespan('M5'),lambda r:r.set_category('US_OPTION'),lambda r:r.set_symbols([]),
    lambda r:r.set_symbols(['A']*2),lambda r:r.set_symbols(['AAA','bad symbol'])])
def test_gate_still_rejects_other_shapes(mutate):
    r=build_request(Operation.HISTORY_BATCH,symbols=('AAA',));r.set_real_time_required(True);mutate(r)
    assert not _read_only_history_batch(r)


def test_minute_bars_require_real_time_flag():
    r=build_request(Operation.HISTORY_BATCH,symbols=('AAA',));r.set_count('50')
    assert not _read_only_history_batch(r)


@pytest.mark.parametrize('build',[
    lambda:build_request(Operation.ACCOUNTS),
    lambda:build_request(Operation.POSITIONS,'acct'),
    lambda:build_request(Operation.OPEN_ORDERS,'acct'),
    lambda:build_request(Operation.BALANCE,'acct'),
    lambda:build_request(Operation.INSTRUMENTS,symbols=('AAA','BBB')),
    lambda:build_request(Operation.SNAPSHOT,symbols=('AAA','BBB')),
    lambda:rm.snapshot_request(('AAA',)),lambda:rm.snapshot_request(('AAA',),urgent=True),
    *[(lambda rank=rank:build_request(Operation.TOP_ACTIVE,rank_type=rank)) for rank in
      ('VOLUME','RELATIVE_VOLUME_10D','TURNOVER','TURNOVER_RATE','AMPLITUDE')],
    *[(lambda rank=rank:build_request(Operation.GAINERS_LOSERS,rank_type=rank)) for rank in ('MIN_3','MIN_5','DAY_1')]])
def test_every_read_the_market_source_builds_passes_gate(build):
    http=Recorder();http.read(build())
    assert len(http.sent)==1
