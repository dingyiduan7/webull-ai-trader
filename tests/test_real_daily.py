"""Offline multi-session authorization; no credentials, sockets or orders."""
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import replace
from datetime import datetime,timedelta,timezone
from decimal import Decimal
from hashlib import sha256
import json

import pytest

from autonomous_trader import real_daily as daily
from autonomous_trader import real_trading as trading
from autonomous_trader.contracts import AlphaContext,AlphaInstrumentContext
from autonomous_trader.mvp.config import reviewed_calendar
from scripts import run_real_daily as launcher
from test_real_offline_authority import evidence,NOW


def fixture(tmp_path,monkeypatch):
    legacy,receipt,_=evidence(tmp_path,monkeypatch)
    import autonomous_trader.real_flat_checkpoint as flat
    monkeypatch.setattr(flat,'load_checkpoint',lambda *a,**kw:({},'checkpoint'))
    authority=dict(schema='REAL_PERSISTENT_AUTHORITY_V1',verdict='GO',authority_id='owner-review-1',
        account_binding=receipt['account_binding'],build_sha256='build',scope=daily.scope(),
        flat_checkpoint_sha256=receipt['flat_checkpoint_sha256'],entitlement_evidence=legacy['entitlement_evidence'],
        issued_at=(NOW-timedelta(minutes=1)).isoformat(),expires_at=(NOW+timedelta(days=20)).isoformat(),revoked=False)
    from owner_authority_fixture import install_test_authority
    publish=install_test_authority(tmp_path,monkeypatch)
    def save():
        (daily.folder(tmp_path)/daily.AUTHORITY_NAME).write_text(json.dumps(authority))
        publish(authority)
    save()
    def bundle(now):
        session=reviewed_calendar().session(now.date())
        from autonomous_trader.contracts import Instrument
        instrument=Instrument(instrument_id='instrument-a',ticker='TEST',country='US',asset_type='STOCK',
            listing_exchange='NAS',liquid=True,tradable=True,quantity_increment='1',price_increment_usd='0.01',minimum_quantity='1')
        quote=AlphaInstrumentContext(instrument_id=instrument.instrument_id,symbol='TEST',quote_at=now,
            bid_usd='10',ask_usd='10.01',price_increment_usd='0.01',quantity_increment='1')
        context=AlphaContext(as_of=now,expires_at=now+timedelta(seconds=30),session_open=session.opens_at,
            session_close=session.closes_at,liquidation_at=session.liquidation_at,instruments=(quote,),
            settled_cash_usd='500',remaining_risk_usd='0',reconciliation_ready=True,settlement_known=True)
        entitlement=daily.session_entitlement(authority,now,tmp_path)
        proof=dict(status='VERIFIED_CURRENT_NASDAQ_STOCK_COVERAGE',host='api.webull.com',
            session_label=session.label.isoformat(),asset_scope='US_LISTED_STOCK',nasdaq_symbols=['TEST'],
            quote_max_age_ms=0,receipt_max_age_ms=0,bar_max_age_seconds=60,
            entitlement_evidence_sha256=daily.digest(entitlement))
        packet=trading.RealPacket(account_binding=authority['account_binding'],observed_at=now,
            settled=Decimal(500),unsettled=Decimal(0),equity=Decimal(500),positions={},open_client_ids=(),
            instruments=(instrument,),context=context,evidence_id='evidence-'+now.date().isoformat(),
            quote_received_at={instrument.instrument_id:now},market_data_evidence=proof)
        state=dict(account_id=authority['account_binding'],version=1,policy_hash=authority['scope']['risk_policy_sha256'],
            cash=dict(settled_usd='500',unsettled_usd='0',liquidation_equity_usd='500'),positions={},orders={},
            reservations={},settlements={},incidents={},latches=dict(daily=False,drawdown=False,incident=False,terminated=False),
            reconciliation=dict(ready=True,settlement_known=True),session=session.model_dump(mode='json'),
            config=dict(settlement_days=['2026-09-29','2026-09-30','2026-10-01']))
        samples=deepcopy(legacy['coverage_samples'])
        for row in samples:
            row.update(session_label=session.label.isoformat(),quote_at=now.isoformat(),quote_received_at=now.isoformat(),
                bar_at=(now-timedelta(seconds=60)).isoformat())
        return packet,state,samples
    return authority,save,bundle


def test_daily_fixture_still_rejects_invalid_signature(tmp_path,monkeypatch):
    fixture(tmp_path,monkeypatch)
    from autonomous_trader import owner_authorization as owner
    path=owner.owner_dir(tmp_path)/owner.RECORD_NAME
    record=json.loads(path.read_text())
    signature=owner.decode(record['signature'])
    record['signature']=owner.encode(signature[:-1]+bytes([signature[-1]^1]))
    path.write_text(json.dumps(record))
    with pytest.raises(ValueError,match='REAL_OWNER_SIGNATURE_INVALID'):
        daily.load_authority(tmp_path,NOW)


def test_release_rebinds_build_without_owner_signature(tmp_path,monkeypatch):
    authority,_,_=fixture(tmp_path,monkeypatch)
    monkeypatch.setattr(trading,'build_digest',lambda root:'next-release')
    value=daily.load_authority(tmp_path,NOW)
    assert value['build_sha256']=='next-release'
    assert value['authority_id']==authority['authority_id']


def test_unchanged_persistent_approval_works_next_session_only_with_new_evidence(tmp_path,monkeypatch):
    authority,_,bundle=fixture(tmp_path,monkeypatch)
    p,s,samples=bundle(NOW)
    daily.persist_certificate(tmp_path,daily.certificate(authority,p,s,samples,NOW,tmp_path))
    today=trading.RealGate.offline_receipt(tmp_path,NOW)
    tomorrow=NOW+timedelta(days=1)
    assert daily.load_authority(tmp_path,tomorrow)==authority
    with pytest.raises(ValueError):trading.RealGate.offline_receipt(tmp_path,tomorrow)
    p,s,samples=bundle(tomorrow)
    daily.persist_certificate(tmp_path,daily.certificate(authority,p,s,samples,tomorrow,tmp_path))
    next_day=trading.RealGate.offline_receipt(tmp_path,tomorrow)
    assert today['daily_authority_sha256']==next_day['daily_authority_sha256']
    assert today['market_readiness_sha256']!=next_day['market_readiness_sha256']
    assert next_day['market_session_label']=='2026-09-30'
    assert not (daily.folder(tmp_path)/'judicator-real-go.json').exists()


# 'build' is no longer an owner-scope change: releases are engineer-controlled (owner decision 2026-10-12).
@pytest.mark.parametrize('change',['account','risk','overnight','calls','month','capital','revoked','expiry','entitlement'])
def test_scope_change_fails_before_runtime(tmp_path,monkeypatch,change):
    authority,save,_=fixture(tmp_path,monkeypatch)
    if change=='account':authority['account_binding']='real-other'
    elif change=='risk':authority['scope']['risk_policy_sha256']='other'
    elif change=='overnight':authority['scope']['overnight']=not authority['scope']['overnight']
    elif change=='calls':authority['scope']['astra_calls_per_session']=3
    elif change=='month':authority['scope']['monthly_model_usd']='6'
    elif change=='capital':authority['scope']['capital_usd']='501'
    elif change=='revoked':authority['revoked']=True
    elif change=='expiry':authority['expires_at']=NOW.isoformat()
    elif change=='entitlement':authority['entitlement_evidence']['source_evidence']['enabled']=False
    save()
    monkeypatch.setattr(launcher,'launcher_lock',lambda *a:pytest.fail('lock before approval'))
    monkeypatch.setattr(launcher,'ensure_database',lambda *a:pytest.fail('DB before approval'))
    with pytest.raises(ValueError):launcher.run(tmp_path,lambda:NOW)


def test_market_closed_does_not_touch_runtime_or_credentials(tmp_path,monkeypatch):
    fixture(tmp_path,monkeypatch)
    monkeypatch.setattr(launcher,'launcher_lock',lambda *a:pytest.fail('closed runtime touch'))
    assert launcher.run(tmp_path,lambda:NOW.replace(hour=22))=='CLOSED'


@pytest.mark.parametrize('change',['stale-account','stale-quote','coverage','local-position','broker-position','local-order',
    'unknown-order','broker-order','cash','unsettled','equity','settlement','incident','daily','drawdown','terminated','policy'])
def test_invalid_evidence_never_certifies(tmp_path,monkeypatch,change):
    authority,_,bundle=fixture(tmp_path,monkeypatch);p,s,samples=bundle(NOW)
    if change=='stale-account':p=replace(p,observed_at=NOW-timedelta(seconds=11))
    elif change=='stale-quote':p=replace(p,context=p.context.model_copy(update={'instruments':(
        p.context.instruments[0].model_copy(update={'quote_at':NOW-timedelta(days=1)}),)}))
    elif change=='coverage':samples[0]['quote_at']=(NOW-timedelta(days=1)).isoformat()
    elif change=='local-position':s['positions']['a']={'quantity':'1'}
    elif change=='broker-position':p=replace(p,positions={'instrument-a':Decimal(1)})
    elif change=='local-order':s['orders']['a']={'final':False,'phase':'ACKNOWLEDGED'}
    elif change=='unknown-order':s['orders']['a']={'final':True,'phase':'UNKNOWN'}
    elif change=='broker-order':p=replace(p,open_client_ids=('a',))
    elif change=='cash':p=replace(p,settled=Decimal(499))
    elif change=='unsettled':p=replace(p,unsettled=Decimal(1))
    elif change=='equity':p=replace(p,equity=Decimal(501))
    elif change=='settlement':s['reconciliation']['settlement_known']=False
    elif change=='incident':s['incidents']['a']={'reason':'OTHER','pending_correction':False}
    elif change=='policy':s['policy_hash']='different'
    else:s['latches'][change]=True
    with pytest.raises(ValueError):daily.certificate(authority,p,s,samples,NOW,tmp_path)


def test_only_approved_incident_can_pass_certificate_without_being_cleared(tmp_path,monkeypatch):
    a,_,bundle=fixture(tmp_path,monkeypatch);p,s,samples=bundle(NOW)
    s['incidents']['original']={'reason':daily.RECOVERABLE,'pending_correction':False};s['latches']['incident']=True
    value=daily.certificate(a,p,s,samples,NOW,tmp_path)
    assert value['ledger_proof']['incident_ids']==['original'] and s['latches']['incident']
    assert 'original' in s['incidents']


def test_due_settlement_is_explained_without_rewriting_ledger(tmp_path,monkeypatch):
    a,_,bundle=fixture(tmp_path,monkeypatch);p,s,samples=bundle(NOW+timedelta(days=1))
    s['cash'].update(settled_usd='450',unsettled_usd='50')
    s['settlements']={'sale':{'outstanding_usd':'50','settlement_date':'2026-09-30'}}
    prior=deepcopy(s)
    proof=daily.verify_flat_account(s,p,a,NOW+timedelta(days=1))
    assert proof['settlement_ids']==['sale'] and s==prior


def test_duplicate_launcher_cannot_acquire_lock(tmp_path):
    with launcher.launcher_lock(tmp_path):
        with pytest.raises(ValueError,match='ALREADY_ACTIVE'):
            with launcher.launcher_lock(tmp_path):pytest.fail('duplicate entered')


def test_prepare_only_does_not_spawn_and_uses_one_collection(tmp_path,monkeypatch):
    a,_,bundle=fixture(tmp_path,monkeypatch);events=[]
    monkeypatch.setattr(launcher,'launcher_lock',lambda *a:nullcontext())
    monkeypatch.setattr(launcher,'ensure_database',lambda *a:events.append('db'))
    monkeypatch.setattr(launcher,'collect',lambda *a:events.append('read'))
    monkeypatch.setattr(launcher,'supervise',lambda *a:pytest.fail('no activation'))
    assert launcher.run(tmp_path,lambda:NOW,prepare_only=True)=='PREPARED'
    assert events==['db','read']


def test_token_rejection_never_creates_certificate_or_starts(tmp_path,monkeypatch):
    fixture(tmp_path,monkeypatch)
    monkeypatch.setattr(launcher,'launcher_lock',lambda *a:nullcontext())
    monkeypatch.setattr(launcher,'ensure_database',lambda *a:None)
    def reject(*args):raise ValueError('REAL_HTTP_401_TOKEN_EXPIRED')
    monkeypatch.setattr(launcher,'collect',reject)
    monkeypatch.setattr(launcher,'supervise',lambda *a:pytest.fail('auth rejection activated'))
    with pytest.raises(ValueError,match='TOKEN_EXPIRED'):launcher.run(tmp_path,lambda:NOW)
    assert not (daily.folder(tmp_path)/daily.POINTER_NAME).exists()


def test_collector_has_no_token_creation_or_plaintext_credential_path():
    import inspect
    text=inspect.getsource(launcher.collect)
    assert 'load_real_credentials(root)' in text and 'if not creds.access_token' in text
    for forbidden in ('create_token','webull_real_token_setup','credential-file','brain_factory','protective_exit'):
        assert forbidden not in text


def test_closed_receipt_recovery_cannot_be_entry_authority(tmp_path,monkeypatch):
    a,_,bundle=fixture(tmp_path,monkeypatch);p,s,samples=bundle(NOW)
    daily.persist_certificate(tmp_path,daily.certificate(a,p,s,samples,NOW,tmp_path))
    with pytest.raises(ValueError):daily.daily_receipt(tmp_path,NOW.replace(hour=21))
    assert daily.daily_receipt(tmp_path,NOW.replace(hour=21),recovery=True)['overnight_enabled'] is daily.OVERNIGHT_HOLDING


@pytest.mark.parametrize('change',['authority','expiry','missing','digest','wrong-session'])
def test_certificate_tampering_or_missing_evidence_fails_closed(tmp_path,monkeypatch,change):
    a,_,bundle=fixture(tmp_path,monkeypatch);p,s,samples=bundle(NOW)
    value=daily.certificate(a,p,s,samples,NOW,tmp_path)
    if change=='authority':value['authority_sha256']='other'
    elif change=='expiry':value['expires_at']=(NOW-timedelta(seconds=1)).isoformat()
    elif change=='wrong-session':value['market_session_label']='2026-09-30'
    pointer=daily.persist_certificate(tmp_path,value)
    path=daily.folder(tmp_path)/pointer['file']
    if change=='missing':path.unlink()
    elif change=='digest':path.write_text('{}')
    with pytest.raises(ValueError):trading.RealGate.offline_receipt(tmp_path,NOW)


def test_invalid_persistent_approval_never_falls_back_to_legacy_go(tmp_path,monkeypatch):
    a,save,_=fixture(tmp_path,monkeypatch);a['revoked']=True;save()
    with pytest.raises(ValueError,match='PERSISTENT_AUTHORITY_INVALID'):
        trading.RealGate.offline_receipt(tmp_path,NOW)


@pytest.mark.parametrize('locked',[False,True])
def test_collector_reuses_dpapi_token_and_never_writes_ledger(tmp_path,monkeypatch,locked):
    from types import SimpleNamespace
    from autonomous_trader.durable import ledger as dbmod
    from autonomous_trader import real_secrets,real_market,real_strategy_broker
    from scripts import webull_real_readiness_packet as coverage
    a,_,bundle=fixture(tmp_path,monkeypatch);now=[NOW];events=[]
    class Connection:
        autocommit=False
        def execute(self,*args):return SimpleNamespace(fetchone=lambda:(not locked,))
        def close(self):events.append('lock-closed')
    monkeypatch.setattr(dbmod.LocalDatabase,'connect',lambda *args:Connection())
    class Ledger:
        def __init__(self,*args,**kwargs):pass
        def rebuild(self,*args):return bundle(now[0])[1]
        def snapshot(self,*args):return bundle(now[0])[1]
    monkeypatch.setattr(dbmod,'Ledger',Ledger)
    sentinel=SimpleNamespace(app_key='fixture-key',app_secret='fixture-secret',access_token='fixture-token',account_id='')
    def credentials(root):events.append('stored-token');return sentinel
    monkeypatch.setattr(real_secrets,'load_real_credentials',credentials)
    class Source:
        def __init__(self,creds,*args,**kwargs):assert creds is sentinel
        def discover(self):return ('TEST',)
        def refresh(self,state):return bundle(now[0])[0]
        def account_observation(self):
            return dict(account_binding=a['account_binding'],observed_at=now[0],received_at=now[0],positions=[],open_client_ids=(),cash=dict(settled_cash='500',unsettled_cash='0',cash_balance='500',net_liquidation_value='500'))
    monkeypatch.setattr(real_market,'RealMarketSource',Source)
    class Http:
        def __init__(self,creds):assert creds is sentinel
        @property
        def last_received_at(self):return now[0]
        def read(self,request):return [dict(symbol=s,instrument_id='instrument-a' if s=='TEST' else s,bid='10',ask='10.01',quote_time=int(now[0].timestamp()*1000)) for s in ('AAPL','QQQ','TEST')]
    monkeypatch.setattr(real_strategy_broker,'RealStrategyHttp',Http)
    def sample(http,symbol,asset,*args):
        return dict(symbol=symbol,instrument_id=symbol,asset_type=asset,exchange='NAS',
            session_label=now[0].date().isoformat(),snapshot_http_status=200,delayed=False)
    monkeypatch.setattr(coverage,'coverage_sample',sample)
    monkeypatch.setattr(coverage,'coverage_batch_bars',lambda *args:{s:dict(bars_http_status=200,
        bar_at=(now[0]-timedelta(seconds=60)).isoformat()) for s in ('AAPL','QQQ')})
    monkeypatch.setattr(trading.RealGate,'checkpoint',lambda *args:None)
    if locked:
        with pytest.raises(ValueError,match='ALREADY_ACTIVE'):launcher.collect(tmp_path,a,lambda:now[0])
        assert events==['lock-closed']
    else:
        first=launcher.collect(tmp_path,a,lambda:now[0]);now[0]+=timedelta(days=1)
        second=launcher.collect(tmp_path,a,lambda:now[0])
        assert events==['stored-token','lock-closed','stored-token','lock-closed']
        assert first['market_session_label']!=second['market_session_label']
        assert second['broker_mutations']==second['astra_calls']==0


def test_flat_daily_expiry_remains_reduce_only_and_supervisor_accepts_downgrade(monkeypatch):
    from types import SimpleNamespace
    from autonomous_trader.real_runner import RealRunner
    from scripts import run_real_supervisor as supervisor
    from test_real_launcher import supervisor_heartbeat
    session=reviewed_calendar().session(NOW.date());when=session.closes_at-timedelta(seconds=30)
    state={'positions':{},'orders':{},'reservations':{},'incidents':{},'latches':{'incident':False}}
    controller=SimpleNamespace(state=state,recovery_only=False,
        downgrade_to_recovery=lambda *a:pytest.fail('flat routine close must not latch'))
    receipt=dict(daily_authority_sha256='authority',expires_at=session.closes_at.isoformat(),
        build_sha256='build',account_binding='real-'+'a'*64,market_session_label=NOW.date().isoformat())
    runner=SimpleNamespace(controller=controller,authority_receipt=receipt)
    RealRunner._authority_guard(runner,when)
    assert controller.recovery_only is True
    heartbeat=supervisor_heartbeat(when,status='RECOVERY_UNRESOLVED',production_enabled=False,recovery_only=True)
    # Bind the production heartbeat fixture to this reviewed daily session.
    heartbeat.update(authority_build_sha256='build',account_id=receipt['account_binding'],
        market_session_label=receipt['market_session_label'])
    assert supervisor._heartbeat_matches(heartbeat,receipt,False,when)
    assert not supervisor._heartbeat_matches(heartbeat,receipt,False,NOW)


def test_final_snapshot_replaces_portfolio_marks_aged_by_coverage_read(tmp_path,monkeypatch):
 a,_,bundle=fixture(tmp_path,monkeypatch);p,state,samples=bundle(NOW);final=NOW+timedelta(seconds=3)
 p.validate(final,entry=True)  # Current session source is not rejected for crossing two seconds.
 rows=[dict(symbol=s,instrument_id='instrument-a' if s=='TEST' else s,bid='10',ask='10.01',
  quote_time=int(final.timestamp()*1000)) for s in ('AAPL','QQQ','TEST')]
 updated=launcher.aligned_preparation_packet(p,rows,final,final)
 assert updated.observed_at==p.observed_at and updated.settled==p.settled and updated.positions==p.positions
 assert updated.context.instruments[0].quote_at==final
 assert updated.market_data_evidence['bar_max_age_seconds']==63
 updated.validate(final,flat=True,entry=True)

@pytest.mark.parametrize('defect',['stale','future','wrong_identity','duplicate','invalid_bid'])
def test_final_snapshot_keeps_real_data_guards(tmp_path,monkeypatch,defect):
 a,_,bundle=fixture(tmp_path,monkeypatch);p,_,_=bundle(NOW)
 rows=[dict(symbol=s,instrument_id='instrument-a' if s=='TEST' else s,bid='10',ask='10.01',
  quote_time=int(NOW.timestamp()*1000)) for s in ('AAPL','QQQ','TEST')]
 q=rows[-1]
 if defect=='stale':q['quote_time']-=86400000
 if defect=='future':q['quote_time']+=1000
 if defect=='wrong_identity':q['instrument_id']='wrong'
 if defect=='duplicate':rows[-1]=rows[0]
 if defect=='invalid_bid':q['bid']='0'
 with pytest.raises(ValueError):launcher.aligned_preparation_packet(p,rows,NOW,NOW)


@pytest.mark.parametrize('bid,ask',[('3.64','3.66'),('1.13','1.14'),('14.51','14.52')])
def test_aligned_snapshot_certificate_roundtrip_preserves_bounded_spread(tmp_path,monkeypatch,bid,ask):
 from autonomous_trader.real_market import _spread_bps
 a,_,bundle=fixture(tmp_path,monkeypatch);p,state,samples=bundle(NOW)
 rows=[dict(symbol=s,instrument_id='instrument-a' if s=='TEST' else s,bid=bid,ask=ask,
  quote_time=int(NOW.timestamp()*1000)) for s in ('AAPL','QQQ','TEST')]
 updated=launcher.aligned_preparation_packet(p,rows,NOW,NOW)
 decoded=daily.decode_packet(json.loads(json.dumps(daily.encode_packet(updated))))
 assert decoded.context.instruments[0].spread_bps==_spread_bps(Decimal(bid),Decimal(ask))
 assert decoded.context.instruments[0].quote_at==p.context.instruments[0].quote_at
 assert decoded.observed_at==p.observed_at and decoded.context.expires_at==p.context.expires_at
 decoded.validate(NOW,flat=True,entry=True)
 daily.persist_certificate(tmp_path,daily.certificate(a,updated,state,samples,NOW,tmp_path))
 value,pointer=daily.current_certificate(tmp_path,a,NOW)
 assert daily.decode_packet(value['packet']).context.instruments[0].spread_bps==_spread_bps(Decimal(bid),Decimal(ask))
 receipt=trading.RealGate.offline_receipt(tmp_path,NOW)
 assert receipt['market_readiness_sha256']==pointer['sha256'].upper()
