"""One bounded REAL read-only context packet. Never calls a model or broker write."""
from argparse import ArgumentParser
from datetime import datetime, timezone
from decimal import Decimal
import json, os, sys
import re
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))

from autonomous_trader.durable.ledger import LocalDatabase
from autonomous_trader.real_flat_checkpoint import checkpoint_digest
from autonomous_trader.real_market import RealMarketSource
from autonomous_trader.real_strategy_broker import RealStrategyHttp
from autonomous_trader.real_secrets import RealCredentials,load_real_credentials
from autonomous_trader.real_state import RealObservationLedger
from autonomous_trader.real_trading import (REALTIME_ENTITLEMENT_DOCS,RealGate,build_digest,
    validate_realtime_entitlement)
from autonomous_trader.real_state import digest
from autonomous_trader.sandbox.http import build_request,Operation
from autonomous_trader.mvp.config import reviewed_calendar
from autonomous_trader.reference import NEW_YORK

CHECKPOINT_NAME='flat-checkpoint-v2-20260930.json'
CHECKPOINT_SHA256='0ADC634776FFC6D532A55831427199F43DA87DD6B36C47C91DEC8E0058B0A82F'


def credentials(path):
    labels={}
    try:
        for line in Path(path).read_text(encoding='utf-8-sig').splitlines():
            if ':' in line:key,value=line.split(':',1)
            elif '=' in line:key,value=line.split('=',1)
            else:continue
            labels[key.strip().lower()]=value.strip()
    except OSError:raise ValueError('REAL_CREDENTIAL_FILE_UNAVAILABLE') from None
    key=labels.get('real app key','');secret=labels.get('real app sec','')
    if not key or not secret:raise ValueError('REAL_CREDENTIAL_FILE_SCHEMA')
    try:
        stored=load_real_credentials(ROOT)
        if stored.app_key==key and stored.app_secret==secret:return stored
    except ValueError:pass
    return RealCredentials(key,secret)


def clean_json(value,forbidden):
    text=json.dumps(value,indent=2,default=lambda x:str(x),ensure_ascii=False)
    lowered=text.lower()
    if any(secret and secret in text for secret in forbidden):raise ValueError('REAL_SECRET_SANITIZATION_FAILURE')
    if (any(word in lowered for word in ('app_secret','app key','access_token','x-signature','bearer '))
            or re.search(r'"authorization"\s*:',lowered)):
        raise ValueError('REAL_SECRET_SANITIZATION_FAILURE')
    return text


def entitlement(path,prepare,now):
    try:value=json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError,ValueError):raise ValueError('REAL_ENTITLEMENT_EVIDENCE_REQUIRED') from None
    return validate_realtime_entitlement(value,prepare,now,ROOT)


def _time(value):
    if isinstance(value,(int,Decimal)) or isinstance(value,str) and value.isdigit():
        return datetime.fromtimestamp(int(value)/1000,timezone.utc)
    result=datetime.fromisoformat(str(value).replace('Z','+00:00'))
    if result.tzinfo is None:raise ValueError('REAL_MARKET_COVERAGE_TIME')
    return result


def coverage_delay_evidence(quote):
    delay=str(quote.get('delay_type') or quote.get('quote_type') or '').upper().replace('-','_')
    if quote.get('is_delayed') is True or 'DELAY' in delay:
        raise ValueError('REAL_COVERAGE_EXPLICITLY_DELAYED')
    return ('EXPLICIT_RESPONSE' if quote.get('is_delayed') is False or delay in ('REAL_TIME','REALTIME') else
        'DOCUMENTED_PRODUCTION_OPENAPI_SUBSCRIPTION_PLUS_CURRENT_EFFECTIVENESS')


def coverage_sample(http,symbol,asset_type,session,now):
    profiles=http.read(build_request(Operation.INSTRUMENTS,symbols=(symbol,)));profile_at=http.last_received_at
    snapshots=http.read(build_request(Operation.SNAPSHOT,symbols=(symbol,)));quote_at=http.last_received_at
    rows=profiles.get('data') if isinstance(profiles,dict) else None
    if not isinstance(rows,list) or len(rows)!=1 or rows[0].get('symbol')!=symbol:raise ValueError('REAL_COVERAGE_PROFILE')
    profile=rows[0]
    if not isinstance(snapshots,list) or len(snapshots)!=1 or snapshots[0].get('symbol')!=symbol:
        raise ValueError('REAL_COVERAGE_SNAPSHOT')
    quote=snapshots[0]
    exchange=profile.get('exchange_code') or profile.get('exchange')
    subtype=str(profile.get('sub_category','')).upper()
    is_etf=subtype in ('ETF','EXCHANGE_TRADED_FUND')
    if exchange not in ('NAS','NMS','NSQ') or (asset_type=='ETF')!=is_etf:
        raise ValueError('REAL_COVERAGE_ASSET_IDENTITY')
    delay_evidence=coverage_delay_evidence(quote)
    source_at=_time(quote['quote_time'])
    return {'symbol':symbol,'asset_type':asset_type,'exchange':exchange,'session_label':session.label.isoformat(),
        'snapshot_http_status':200,'delayed':False,'delay_evidence':delay_evidence,
        'instrument_id':str(profile['instrument_id']),
        'quote_at':source_at.isoformat(),'quote_received_at':quote_at.isoformat(),
        'profile_received_at':profile_at.isoformat()}


def coverage_batch_bars(body,samples,received_at,now):
    rows=body.get('result') if isinstance(body,dict) else None
    if not isinstance(rows,list):raise ValueError('REAL_COVERAGE_BARS')
    expected={sample['symbol']:sample['instrument_id'] for sample in samples};groups={}
    for row in rows:
        symbol=row.get('symbol')
        if symbol not in expected:raise ValueError('REAL_COVERAGE_BARS_UNREQUESTED')
        if symbol in groups:raise ValueError('REAL_COVERAGE_BARS_DUPLICATE')
        if row.get('instrument_id') is not None and str(row['instrument_id'])!=expected[symbol]:
            raise ValueError('REAL_COVERAGE_BARS_IDENTITY')
        bars=row.get('result')
        if not isinstance(bars,list) or not bars:raise ValueError('REAL_COVERAGE_BARS')
        bar_at=max(_time(bar['time']) for bar in bars);bar_age=(now-bar_at).total_seconds()
        if not 0<=bar_age<=180:raise ValueError('REAL_MARKET_COVERAGE_STALE')
        groups[symbol]={'bars_http_status':200,'bar_at':bar_at.isoformat(),
            'bar_received_at':received_at.isoformat(),'bar_age_seconds':str(bar_age)}
    if set(groups)!=set(expected):raise ValueError('REAL_COVERAGE_BARS_MISSING')
    return groups


def main():
    parser=ArgumentParser();parser.add_argument('--credential-file',required=True)
    parser.add_argument('--entitlement-evidence',required=True)
    parser.add_argument('--stock-symbol',default='AAPL');parser.add_argument('--etf-symbol',default='QQQ');args=parser.parse_args()
    now=datetime.now(timezone.utc)
    prepare=RealGate.offline_preparation(ROOT,now)
    entitlement_value=entitlement(args.entitlement_evidence,prepare,now)
    creds=credentials(args.credential_file)
    db=LocalDatabase('postgres','m2_admin',schema='real_readiness')
    observation=RealObservationLedger(db).verify_replay()
    if not observation or not observation.get('observation_reconciled'):
        raise ValueError('REAL_OBSERVATION_LEDGER_NOT_RECONCILED')
    account_binding=observation['account_binding']
    checkpoint=ROOT/'.local'/'real-readiness'/CHECKPOINT_NAME
    if checkpoint_digest(checkpoint)!=CHECKPOINT_SHA256:raise ValueError('REAL_FLAT_CHECKPOINT_DIGEST')
    authority={'session_label':prepare['market_session_label'],'entitlement_evidence_sha256':digest(entitlement_value)}
    source=RealMarketSource(creds,account_binding,market_authority=authority)
    discovered=source.discover();packet=source.refresh()
    packet.validate_market_data_authority(datetime.now(timezone.utc))
    gate=RealGate(ROOT,account_binding,db)
    gate.readiness(packet,datetime.now(timezone.utc),initial=True,checkpoint_sha256=CHECKPOINT_SHA256)
    session=reviewed_calendar().session(now.astimezone(NEW_YORK).date())
    coverage_http=RealStrategyHttp(creds)
    samples=[coverage_sample(coverage_http,args.stock_symbol,'STOCK',session,datetime.now(timezone.utc)),
        coverage_sample(coverage_http,args.etf_symbol,'ETF',session,datetime.now(timezone.utc))]
    bars_request=build_request(Operation.HISTORY_BATCH,symbols=(args.stock_symbol,args.etf_symbol))
    bars_request.set_real_time_required(True)
    bars=coverage_http.read(bars_request)
    bars_received=coverage_http.last_received_at;created=datetime.now(timezone.utc)
    bar_groups=coverage_batch_bars(bars,samples,bars_received,created)
    samples=[{**sample,**bar_groups[sample['symbol']],'bars_real_time_required':True} for sample in samples]
    for row in samples:
        row['quote_age_ms']=str((created-datetime.fromisoformat(row['quote_at'])).total_seconds()*1000)
        row['receipt_age_ms']=str((created-datetime.fromisoformat(row['quote_received_at'])).total_seconds()*1000)
        row['bar_age_seconds']=str((created-datetime.fromisoformat(row['bar_at'])).total_seconds())
        if not session.opens_at<=datetime.fromisoformat(row['quote_at'])<=created or not session.opens_at<=datetime.fromisoformat(row['quote_received_at'])<=created:
            raise ValueError('REAL_MARKET_COVERAGE_STALE')
        row.pop('instrument_id')
    market=dict(status='VERIFIED_CURRENT_NASDAQ_STOCK_COVERAGE',host='api.webull.com',
        session_label=session.label.isoformat(),asset_scope='US_LISTED_STOCK_AND_ETF',
        nasdaq_symbols=[row['symbol'] for row in samples],
        quote_max_age_ms=str(max(Decimal(row['quote_age_ms']) for row in samples)),
        receipt_max_age_ms=str(max(Decimal(row['receipt_age_ms']) for row in samples)),
        bar_max_age_seconds=str(max(Decimal(row['bar_age_seconds']) for row in samples)),
        entitlement_evidence_sha256=digest(entitlement_value))
    value={'schema':'REAL_ZERO_ORDER_READINESS_PACKET_V2','environment':'REAL_READ_ONLY',
        'created_at':created.isoformat(),'build_sha256':build_digest(ROOT),'account_binding':account_binding,
        'flat_checkpoint_sha256':CHECKPOINT_SHA256,'account':{'type':packet.account_type,
            'sole_account_binding_verified':True,'flat':not packet.positions,'open_order_count':len(packet.open_client_ids)},
        'cash':{'settled_usd':str(packet.settled),'unsettled_usd':str(packet.unsettled),
            'cash_balance_usd':str(packet.settled+packet.unsettled),'equity_usd':str(packet.equity)},
        'discovery':{'native_symbols_considered':len(discovered),'context_symbols':len(packet.context.instruments),
            'symbols':[x.symbol for x in packet.context.instruments]},
        'context':packet.context.model_dump(mode='json'),'freshness':{
            'account_observed_at':packet.observed_at.isoformat(),
            'quote_received_at':{next(i.ticker for i in packet.instruments if i.instrument_id==iid):at.isoformat()
                for iid,at in packet.quote_received_at.items()}},'market_data_evidence':market,
        'entitlement_evidence':entitlement_value,'coverage_samples':samples,
        'authority':{'astra_called':False,'broker_mutations':0,'ledger_created':False,
            'runner_started':False,'strategy_order_authority':False,'judicator_go_required':True}}
    text=clean_json(value,(creds.app_key,creds.app_secret,creds.account_id,creds.access_token))
    folder=ROOT/'.local'/'real-readiness';folder.mkdir(parents=True,exist_ok=True)
    path=folder/('zero-order-packet-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'.json')
    temporary=path.with_suffix('.tmp')
    with temporary.open('w',encoding='utf-8') as out:
        out.write(text);out.flush();os.fsync(out.fileno())
    os.replace(temporary,path)
    print(json.dumps({'status':'REAL_READ_ONLY_PACKET_VALID','path':str(path),'context_symbols':len(packet.context.instruments),
        'positions':len(packet.positions),'open_orders':len(packet.open_client_ids),'broker_mutations':0,'astra_calls':0}))


def cli():
    try:
        main()
        return 0
    except ValueError as exc:
        if str(exc)=='REAL_DISCOVERY_SESSION_REQUIRED':
            print(json.dumps({'status':'REAL_DISCOVERY_SESSION_REQUIRED',
                'next':'RUN_AFTER_HOURS_ACCOUNT_PACKET_OR_RETRY_DURING_CORE_SESSION'},separators=(',',':')))
            return 2
        raise


if __name__=='__main__':raise SystemExit(cli())
