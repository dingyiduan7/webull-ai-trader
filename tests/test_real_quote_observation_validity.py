"""Actual broker-lag regression; no socket, credentials, model call or order."""
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from autonomous_trader.durable.projection import fresh_mark, Rejected
from autonomous_trader.mvp.config import reviewed_calendar
from autonomous_trader.real_market import _quote_is_fresh
from scripts import run_real_daily as launcher
from test_real_daily import fixture
from test_real_offline_authority import NOW


@pytest.mark.parametrize('age',[2.37389,2.659426,7])
def test_current_direct_observation_preserves_original_source(tmp_path,monkeypatch,age):
    _,_,bundle=fixture(tmp_path,monkeypatch)
    p,_,_=bundle(NOW)
    source=NOW-timedelta(seconds=age)
    rows=[dict(symbol=s,instrument_id='instrument-a' if s=='TEST' else s,
        bid='1.33',ask='1.34',quote_time=source.isoformat()) for s in ('TEST','AAPL','QQQ')]
    updated=launcher.aligned_preparation_packet(p,rows,NOW,NOW)
    assert updated.context.instruments[0].quote_at==source
    assert updated.quote_received_at['instrument-a']==NOW
    assert updated.market_data_evidence['quote_max_age_ms']==pytest.approx(age*1000)
    updated.validate(NOW,entry=True)


@pytest.mark.parametrize('bad',['previous_session','stale_source','future','old_receipt','expired_context','old_account'])
def test_invalid_observation_does_not_gain_authority(tmp_path,monkeypatch,bad):
    _,_,bundle=fixture(tmp_path,monkeypatch);p,_,_=bundle(NOW)
    q=p.context.instruments[0]
    if bad=='previous_session':p=replace(p,context=p.context.model_copy(update={'instruments':(q.model_copy(update={'quote_at':NOW-timedelta(days=1)}),)}))
    if bad=='stale_source':p=replace(p,context=p.context.model_copy(update={'instruments':(q.model_copy(update={'quote_at':NOW-timedelta(seconds=60)}),)}))
    if bad=='future':p=replace(p,context=p.context.model_copy(update={'instruments':(q.model_copy(update={'quote_at':NOW+timedelta(seconds=1)}),)}))
    if bad=='old_receipt':p=replace(p,quote_received_at={'instrument-a':NOW-timedelta(seconds=1)})
    if bad=='expired_context':p=replace(p,context=p.context.model_copy(update={'expires_at':NOW}))
    if bad=='old_account':p=replace(p,observed_at=NOW-timedelta(seconds=11))
    with pytest.raises(ValueError):p.validate(NOW,entry=True)


@pytest.mark.parametrize('bad',['previous_session','future','invalid_bid'])
def test_optional_unusable_candidate_removed_with_matching_proof(tmp_path,monkeypatch,bad):
    _,_,bundle=fixture(tmp_path,monkeypatch);p,_,_=bundle(NOW)
    original=p.context.instruments[0]
    other=original.model_copy(update={'instrument_id':'instrument-b','symbol':'OTHER'})
    p=replace(p,instruments=p.instruments+(p.instruments[0].model_copy(update={'instrument_id':'instrument-b','ticker':'OTHER'}),),
        context=p.context.model_copy(update={'instruments':(original,other)}),
        quote_received_at={'instrument-a':NOW,'instrument-b':NOW})
    rows=[dict(symbol=s,instrument_id={'TEST':'instrument-a','OTHER':'instrument-b'}.get(s,s),
        bid='10',ask='10.01',quote_time=(NOW-timedelta(days=1) if s=='OTHER' else NOW).isoformat())
        for s in ('TEST','OTHER','AAPL','QQQ')]
    if bad=='stale_source':p=replace(p,context=p.context.model_copy(update={'instruments':(q.model_copy(update={'quote_at':NOW-timedelta(seconds=60)}),)}))
    if bad=='future':rows[1]['quote_time']=(NOW+timedelta(seconds=1)).isoformat()
    if bad=='invalid_bid':rows[1].update(quote_time=NOW.isoformat(),bid='0')
    updated=launcher.aligned_preparation_packet(p,rows,NOW,NOW)
    assert {q.instrument_id for q in updated.context.instruments}=={'instrument-a'}
    assert {i.instrument_id for i in updated.instruments}=={'instrument-a'}
    assert set(updated.quote_received_at)=={'instrument-a'}
    assert updated.market_data_evidence['nasdaq_symbols']==['TEST']
    held=replace(p,positions={'instrument-b':Decimal(1)})
    with pytest.raises(ValueError):launcher.aligned_preparation_packet(held,rows,NOW,NOW)


@pytest.mark.parametrize('delta',[timedelta(days=-1),timedelta(seconds=1)])
def test_held_source_is_not_optional(tmp_path,monkeypatch,delta):
    _,_,bundle=fixture(tmp_path,monkeypatch);p,_,_=bundle(NOW)
    q=p.context.instruments[0].model_copy(update={'quote_at':NOW+delta})
    p=replace(p,positions={'instrument-a':Decimal(1)},context=p.context.model_copy(update={'instruments':(q,)}))
    with pytest.raises(ValueError,match='STALE_REAL_QUOTE'):p.validate(NOW)


def test_real_mark_validity_not_legacy_two_second_clock():
    session=reviewed_calendar().session(NOW.date())
    quote={'bid_usd':'100','observed_at':(NOW-timedelta(seconds=7)).isoformat(),
        'received_at':NOW.isoformat(),'valid_until':(NOW+timedelta(seconds=5)).isoformat()}
    s={'execution_scope':'REAL','session':session.model_dump(mode='json'),
        'config':{'mark_max_age_seconds':2},'marks':{'x':quote}}
    assert fresh_mark(s,'x',NOW)==Decimal(100)
    with pytest.raises(Rejected,match='STALE_MARKET'):fresh_mark(s,'x',NOW+timedelta(seconds=5))
    s['execution_scope']='OFFLINE'
    with pytest.raises(Rejected,match='STALE_MARKET'):fresh_mark(s,'x',NOW)
    s['execution_scope']='REAL';quote.pop('valid_until');quote.pop('received_at')
    with pytest.raises(Rejected,match='STALE_MARKET'):fresh_mark(s,'x',NOW)


def test_direct_quote_previous_session_and_future_rejected():
    assert _quote_is_fresh({'quote_time':int((NOW-timedelta(seconds=7)).timestamp()*1000)},NOW,NOW,None)
    for delta in (timedelta(days=-1),timedelta(seconds=-60),timedelta(seconds=1)):
        assert not _quote_is_fresh({'quote_time':int((NOW+delta).timestamp()*1000)},NOW,NOW,None)


def test_optional_sparse_history_does_not_veto_current_quote_authority(tmp_path,monkeypatch):
    _,_,bundle=fixture(tmp_path,monkeypatch);p,_,_=bundle(NOW)
    p=replace(p,market_data_evidence={**p.market_data_evidence,'bar_max_age_seconds':2317})
    p.validate(NOW,entry=True);p.validate_market_data_authority(NOW)
    q=p.context.instruments[0].model_copy(update={'quote_at':NOW-timedelta(seconds=11)})
    p=replace(p,context=p.context.model_copy(update={'instruments':(q,)}))
    with pytest.raises(ValueError,match='STALE_REAL_QUOTE'):p.validate(NOW,entry=True)


def test_optional_candidate_expiry_cannot_poison_fresh_or_owned_context(tmp_path,monkeypatch):
    _,_,bundle=fixture(tmp_path,monkeypatch);p,_,_=bundle(NOW)
    original=p.context.instruments[0]
    other=original.model_copy(update={'instrument_id':'other','symbol':'OTHER','quote_at':NOW-timedelta(seconds=7)})
    p=replace(p,instruments=p.instruments+(p.instruments[0].model_copy(update={'instrument_id':'other','ticker':'OTHER'}),),
        context=p.context.model_copy(update={'instruments':(original,other)}),quote_received_at={'instrument-a':NOW,'other':NOW})
    updated=p.current_candidates(NOW+timedelta(seconds=4))
    assert [q.symbol for q in updated.context.instruments]==['TEST']
    assert updated.context.as_of==p.context.as_of and updated.observed_at==p.observed_at
    assert updated.context.instruments[0].quote_at==original.quote_at
    updated.validate(NOW+timedelta(seconds=4),entry=True)
    held=replace(p,positions={'other':Decimal(1)}).current_candidates(NOW+timedelta(seconds=4))
    with pytest.raises(ValueError,match='STALE_REAL_QUOTE'):held.validate(NOW+timedelta(seconds=4))


def test_unused_discovery_prices_are_not_written_as_ledger_marks(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from autonomous_trader.real_trading import RealController
    _,_,bundle=fixture(tmp_path,monkeypatch);p,state,_=bundle(NOW)
    state['instruments']={i.instrument_id:i.model_dump(mode='json') for i in p.instruments}
    state['marks']={}
    controller=object.__new__(RealController);controller.account_id=p.account_binding
    controller.ledger=SimpleNamespace(snapshot=lambda account:state);controller.clock=lambda:NOW
    controller.lease=lambda:None;commands=[]
    controller.send=lambda command:commands.append(command) or state
    controller.refresh(p)
    assert not any(c.kind=='MARK' for c in commands)
    assert any(c.kind=='RECONCILE' for c in commands)
    assert controller.current_market_packet is p


def test_focus_restores_broad_discovery_even_on_read_failure():
    from autonomous_trader.real_market import RealMarketSource
    source=object.__new__(RealMarketSource);source.symbols=('ONE','TWO','THREE')
    seen=[]
    def refresh(state):seen.append(source.symbols);raise ValueError('BROKER_READ_FAILED')
    source.refresh=refresh
    with pytest.raises(ValueError,match='BROKER_READ_FAILED'):
        source.refresh_instrument({'instruments':{'x':{'ticker':'TWO'}}},'x')
    assert seen==[('TWO',)] and source.symbols==('ONE','TWO','THREE')


def test_cash_refreshed_after_quotes_is_valid_without_retimestamping(tmp_path,monkeypatch):
    from scripts.run_real_daily import current_preparation_account
    _,_,bundle=fixture(tmp_path,monkeypatch);p,state,_=bundle(NOW)
    now=NOW+timedelta(seconds=1)
    account=dict(account_binding=p.account_binding,observed_at=now,received_at=now,positions=[],open_client_ids=(),
        cash=dict(settled_cash='500',unsettled_cash='0',cash_balance='500',net_liquidation_value='500'))
    authority={'account_binding':p.account_binding}
    updated=current_preparation_account(p,account,state,authority,now)
    assert updated.observed_at==now and updated.quote_received_at['instrument-a']==NOW
    assert updated.context.instruments[0].quote_at==NOW
    assert updated.context.expires_at==p.context.expires_at
    updated.validate(now,flat=True,entry=True)
    bad={**account,'positions':[{'symbol':'EXTERNAL','quantity':'1','instrument_type':'EQUITY'}]}
    with pytest.raises(ValueError):current_preparation_account(p,bad,state,authority,now)


@pytest.mark.parametrize('failure',[None,'old-account','external-position','old-quotes','expired-context'])
def test_preparation_refreshes_account_before_alignment_after_slow_coverage(tmp_path,monkeypatch,failure):
    """Replay serial coverage aging a valid packet past its account TTL."""
    from scripts import webull_real_readiness_packet as coverage
    from types import SimpleNamespace
    authority,_,bundle=fixture(tmp_path,monkeypatch)
    packet,state,samples=bundle(NOW)
    clock=[NOW];observations=[]
    def account():
        observations.append(clock[0])
        observed=NOW if failure=='old-account' else clock[0]
        return dict(account_binding=packet.account_binding,observed_at=observed,received_at=clock[0],
            positions=([dict(symbol='EXTERNAL',quantity='1',instrument_type='EQUITY')]
                       if failure=='external-position' else []),open_client_ids=(),
            cash=dict(settled_cash='500',unsettled_cash='0',cash_balance='500',net_liquidation_value='500'))
    source=SimpleNamespace(discover=lambda:None,refresh=lambda s:packet,account_observation=account)
    class Http:
        last_received_at=NOW
        def read(self,request):return {}
    monkeypatch.setattr(coverage,'coverage_sample',lambda http,symbol,*args:
        dict(symbol=symbol,instrument_id=symbol,asset_type=args[0],exchange='NAS',
            session_label=NOW.date().isoformat(),snapshot_http_status=200,delayed=False))
    monkeypatch.setattr(coverage,'coverage_batch_bars',lambda *args:
        {symbol:dict(bars_http_status=200,bar_at=(NOW-timedelta(seconds=60)).isoformat())
         for symbol in ('AAPL','QQQ')})
    def snapshots(http,symbols):
        clock[0]=NOW+timedelta(seconds=31 if failure=='expired-context' else 11)
        source_at=NOW if failure=='old-quotes' else clock[0]
        return ([dict(symbol=s,instrument_id='instrument-a' if s=='TEST' else s,
                      bid='10',ask='10.01',quote_time=source_at.isoformat(),delayed=False)
                 for s in symbols],dict.fromkeys(symbols,clock[0]))
    monkeypatch.setattr(launcher,'coverage_snapshots',snapshots)
    if failure:
        with pytest.raises(ValueError):
            launcher.prepare_market(tmp_path,authority,lambda:clock[0],source,state,http=Http())
        assert observations==[clock[0]]
        return
    result=launcher.prepare_market(tmp_path,authority,lambda:clock[0],source,state,http=Http())
    from autonomous_trader.real_daily import decode_packet,current_certificate,persist_certificate
    new=decode_packet(result['packet'])
    assert observations==[NOW+timedelta(seconds=11)]
    assert new.observed_at==clock[0]
    assert new.context.instruments[0].quote_at==clock[0]
    assert new.quote_received_at['instrument-a']==clock[0]
    assert new.context.expires_at==packet.context.expires_at
    persist_certificate(tmp_path,result)
    clock[0]+=timedelta(seconds=4)
    saved,_=current_certificate(tmp_path,authority,clock[0])
    decode_packet(saved['packet']).validate(clock[0],flat=True,entry=True)


@pytest.mark.parametrize('bad',['stale_receipt','receipt_before_quote'])
def test_candidate_with_unusable_receipt_removed_held_still_fails(tmp_path,monkeypatch,bad):
    """2026-10-30: one candidate whose receipt failed the 10 s check (STALE_REAL_QUOTE_RECEIPT) blocked the daily start."""
    _,_,bundle=fixture(tmp_path,monkeypatch);p,_,_=bundle(NOW)
    original=p.context.instruments[0]
    other=original.model_copy(update={'instrument_id':'instrument-b','symbol':'OTHER'})
    p=replace(p,instruments=p.instruments+(p.instruments[0].model_copy(update={'instrument_id':'instrument-b','ticker':'OTHER'}),),
        context=p.context.model_copy(update={'instruments':(original,other)}),
        quote_received_at={'instrument-a':NOW,'instrument-b':NOW})
    rows=[dict(symbol=s,instrument_id={'TEST':'instrument-a','OTHER':'instrument-b'}.get(s,s),
        bid='10',ask='10.01',quote_time=NOW.isoformat()) for s in ('TEST','OTHER','AAPL','QQQ')]
    received={s:NOW for s in ('TEST','OTHER','AAPL','QQQ')}
    if bad=='stale_receipt':
        rows[1]['quote_time']=(NOW-timedelta(seconds=11)).isoformat();received['OTHER']=NOW-timedelta(seconds=11)
    if bad=='receipt_before_quote':received['OTHER']=NOW-timedelta(seconds=1)
    updated=launcher.aligned_preparation_packet(p,rows,received,NOW)
    assert {q.instrument_id for q in updated.context.instruments}=={'instrument-a'}
    held=replace(p,positions={'instrument-b':Decimal(1)})
    with pytest.raises(ValueError):launcher.aligned_preparation_packet(held,rows,received,NOW)
