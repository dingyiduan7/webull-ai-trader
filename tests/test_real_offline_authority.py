"""Public-file authority validation; no database, broker, secret or model calls."""
from datetime import datetime, timezone, timedelta
from hashlib import sha256
import json

import pytest

import autonomous_trader.real_trading as subject

NOW=datetime(2026,9,29,15,tzinfo=timezone.utc)


def evidence(tmp_path,monkeypatch):
    folder=tmp_path/'.local/real-readiness';folder.mkdir(parents=True)
    portal=folder/'portal.jpg';portal.write_bytes(b'current authenticated portal proof')
    portal_sha=sha256(portal.read_bytes()).hexdigest().upper()
    monkeypatch.setattr(subject,'build_digest',lambda root:'build')
    checkpoint_value={'cash':{'settled_usd':'500','unsettled_usd':'0','equity_usd':'500'}}
    monkeypatch.setattr(subject,'load_checkpoint',lambda *args,**kwargs:(checkpoint_value,'checkpoint'))
    checkpoint=folder/'flat-checkpoint-test.json';checkpoint.write_text('{}')
    checkpoint_sha=sha256(checkpoint.read_bytes()).hexdigest().upper()
    packet={'schema':'REAL_ZERO_ORDER_READINESS_PACKET_V2','environment':'REAL_READ_ONLY','build_sha256':'build',
        'account_binding':'real-test','flat_checkpoint_sha256':checkpoint_sha,
        'created_at':NOW.isoformat(),'freshness':{'account_observed_at':NOW.isoformat()},
        'account':{'type':'INDIVIDUAL_CASH','flat':True,'open_order_count':0},
        'cash':{'settled_usd':'500','unsettled_usd':'0','cash_balance_usd':'500','equity_usd':'500'},
        'authority':{'astra_called':False,'broker_mutations':0,'ledger_created':False,
            'runner_started':False,'strategy_order_authority':False,'judicator_go_required':True},
        'market_data_evidence':{'session_label':'2026-09-29','host':'api.webull.com',
            'status':'VERIFIED_CURRENT_NASDAQ_STOCK_COVERAGE',
            'asset_scope':'US_LISTED_STOCK_AND_ETF','nasdaq_symbols':['TEST','FUND'],
            'quote_max_age_ms':0,'receipt_max_age_ms':0,'bar_max_age_seconds':60},
        'entitlement_evidence':{'account_binding':'real-test','build_sha256':'build','session_label':'2026-09-29',
            'status':'VERIFIED','product':'NASDAQ_BASIC_OPENAPI_NONDISPLAY','observed_at':NOW.isoformat(),
            'source_evidence':{'kind':'WEBULL_OPENAPI_ENTITLEMENT','non_display':True,'enabled':True,
                'product':'NASDAQ_BASIC_OPENAPI_NONDISPLAY','source':'WEBULL_DEVELOPER_PORTAL_AUTHENTICATED',
                'environment':'PRODUCTION','http_host':'api.webull.com','displayed_authorization':'Free Authorized',
                'displayed_expiration':'2027-09-29','screenshot_sha256':portal_sha,
                'screenshot_path':'.local/real-readiness/portal.jpg',
                'official_documentation':dict(subject.REALTIME_ENTITLEMENT_DOCS),
                'provider_response':{'subscription_name':'NASDAQ_BASIC_OPENAPI_NONDISPLAY','market':'NASDAQ',
                    'usage':'NON_DISPLAY_OPENAPI','status':'ACTIVE'}}},
        'coverage_samples':[
            {'symbol':'TEST','asset_type':'STOCK','exchange':'NSQ','snapshot_http_status':200,
                'bars_http_status':200,'delayed':False,'delay_evidence':'EXPLICIT_RESPONSE',
                'bars_real_time_required':True,'session_label':'2026-09-29',
                'quote_at':NOW.isoformat(),'quote_received_at':NOW.isoformat(),
                'bar_at':(NOW-timedelta(seconds=60)).isoformat(),
                'quote_age_ms':'0','receipt_age_ms':'0','bar_age_seconds':'60'},
            {'symbol':'FUND','asset_type':'ETF','exchange':'NSQ','snapshot_http_status':200,
                'bars_http_status':200,'delayed':False,
                'delay_evidence':'DOCUMENTED_PRODUCTION_OPENAPI_SUBSCRIPTION_PLUS_CURRENT_EFFECTIVENESS',
                'bars_real_time_required':True,'session_label':'2026-09-29',
                'quote_at':NOW.isoformat(),'quote_received_at':NOW.isoformat(),
                'bar_at':(NOW-timedelta(seconds=60)).isoformat(),
                'quote_age_ms':'0','receipt_age_ms':'0','bar_age_seconds':'60'}]}
    packet['market_data_evidence']['entitlement_evidence_sha256']=subject.digest(packet['entitlement_evidence'])
    receipt={'verdict':'GO','scope':'REAL_CASH_500_V1','account_binding':'real-test','build_sha256':'build',
        'flat_checkpoint_sha256':checkpoint_sha,'overnight_enabled':False,
        'market_readiness_sha256':'','market_session_label':'2026-09-29',
        'issued_at':NOW.isoformat(),'expires_at':(NOW+timedelta(hours=5)).isoformat(),
        'recovery_scope':'REAL_RECONCILE_REDUCE_ONLY_V1',
        'recovery_not_after':(NOW+timedelta(days=7)).isoformat()}
    def save():
        path=folder/'zero-order-packet-test.json';path.write_text(json.dumps(packet))
        receipt['market_readiness_sha256']=sha256(path.read_bytes()).hexdigest().upper()
        (folder/'judicator-real-go.json').write_text(json.dumps(receipt))
    return packet,receipt,save


# Retired 2026-10-12 (owner approved): four tests of the removed judicator-real-go.json
# fallback. The current runtime never reads that file, so they failed or passed vacuously.
# Equivalent coverage through the live path is in test_real_daily.py (scope changes, invalid
# evidence, next-day replay) and test_real_account_incident_recovery.py (recovery windows).


def test_readiness_collection_requires_current_offline_preparation_first(tmp_path,monkeypatch):
    folder=tmp_path/'.local/real-readiness';folder.mkdir(parents=True)
    monkeypatch.setattr(subject,'build_digest',lambda root:'build')
    monkeypatch.setattr(subject,'load_checkpoint',lambda *args,**kwargs:({},'checkpoint'))
    checkpoint=folder/'flat-checkpoint-test.json';checkpoint.write_text('{}')
    checkpoint_sha=sha256(checkpoint.read_bytes()).hexdigest().upper()
    value={'verdict':'PREPARE','scope':'REAL_READ_ONLY_READINESS_V2','account_binding':'real-test',
        'build_sha256':'build','flat_checkpoint_sha256':checkpoint_sha,'market_session_label':'2026-09-29',
        'issued_at':NOW.isoformat(),'expires_at':(NOW+timedelta(minutes=30)).isoformat()}
    (folder/'judicator-real-prepare.json').write_text(json.dumps(value))
    assert subject.RealGate.offline_preparation(tmp_path,NOW)==value
    with pytest.raises(ValueError):subject.RealGate.offline_preparation(tmp_path,NOW+timedelta(hours=1))


def test_after_hours_account_read_requires_exact_build_offline_preparation(tmp_path,monkeypatch):
    folder=tmp_path/'.local/real-readiness';folder.mkdir(parents=True)
    monkeypatch.setattr(subject,'build_digest',lambda root:'build')
    monkeypatch.setattr(subject,'load_checkpoint',lambda *args,**kwargs:({},'checkpoint'))
    checkpoint=folder/'flat-checkpoint-test.json';checkpoint.write_text('{}')
    checkpoint_sha=sha256(checkpoint.read_bytes()).hexdigest().upper()
    value={'verdict':'PREPARE','scope':'REAL_ACCOUNT_READ_ONLY_AFTER_HOURS_V1','account_binding':'real-test',
        'build_sha256':'build','flat_checkpoint_sha256':checkpoint_sha,
        'issued_at':NOW.isoformat(),'expires_at':(NOW+timedelta(minutes=30)).isoformat()}
    (folder/'judicator-real-account-prepare.json').write_text(json.dumps(value))
    assert subject.RealGate.offline_account_preparation(tmp_path,NOW)==value
    value['build_sha256']='old';(folder/'judicator-real-account-prepare.json').write_text(json.dumps(value))
    with pytest.raises(ValueError):subject.RealGate.offline_account_preparation(tmp_path,NOW)


def test_v2_checkpoint_write_requires_exact_build_and_artifact_scope_before_any_runtime_touch(tmp_path,monkeypatch):
    from autonomous_trader.real_flat_checkpoint import (BASE_CHECKPOINT_SHA256,ACCOUNT_DIAGNOSTIC_SHA256,
        CASH_ACTIVITY_DIAGNOSTIC_SHA256)
    folder=tmp_path/'.local'/'real-readiness';folder.mkdir(parents=True)
    monkeypatch.setattr(subject,'build_digest',lambda root:'build')
    value={'verdict':'PREPARE','scope':'REAL_FLAT_CHECKPOINT_V2_CREATE','account_binding':'real-test',
        'build_sha256':'build','base_checkpoint_sha256':BASE_CHECKPOINT_SHA256,
        'account_diagnostic_sha256':ACCOUNT_DIAGNOSTIC_SHA256,
        'cash_activity_diagnostic_sha256':CASH_ACTIVITY_DIAGNOSTIC_SHA256,
        'issued_at':NOW.isoformat(),'expires_at':(NOW+timedelta(minutes=30)).isoformat()}
    path=folder/'judicator-real-checkpoint-prepare.json';path.write_text(json.dumps(value))
    monkeypatch.setattr(subject,'LocalDatabase',lambda *args,**kwargs:pytest.fail('DB touched'))
    assert subject.RealGate.offline_checkpoint_preparation(tmp_path,NOW)==value
    for field,replacement in (('build_sha256','old'),('scope','REAL_READ_ONLY_READINESS_V2'),
            ('base_checkpoint_sha256','0'*64),('account_diagnostic_sha256','0'*64),
            ('cash_activity_diagnostic_sha256','0'*64)):
        changed=dict(value);changed[field]=replacement;path.write_text(json.dumps(changed))
        with pytest.raises(ValueError,match='CHECKPOINT_PREPARATION_INVALID'):
            subject.RealGate.offline_checkpoint_preparation(tmp_path,NOW)


def test_v2_checkpoint_prepare_cannot_be_replayed_after_expiry(tmp_path,monkeypatch):
    from autonomous_trader.real_flat_checkpoint import (BASE_CHECKPOINT_SHA256,ACCOUNT_DIAGNOSTIC_SHA256,
        CASH_ACTIVITY_DIAGNOSTIC_SHA256)
    folder=tmp_path/'.local'/'real-readiness';folder.mkdir(parents=True)
    monkeypatch.setattr(subject,'build_digest',lambda root:'build')
    value={'verdict':'PREPARE','scope':'REAL_FLAT_CHECKPOINT_V2_CREATE','account_binding':'real-test',
        'build_sha256':'build','base_checkpoint_sha256':BASE_CHECKPOINT_SHA256,
        'account_diagnostic_sha256':ACCOUNT_DIAGNOSTIC_SHA256,
        'cash_activity_diagnostic_sha256':CASH_ACTIVITY_DIAGNOSTIC_SHA256,
        'issued_at':NOW.isoformat(),'expires_at':(NOW+timedelta(minutes=30)).isoformat()}
    (folder/'judicator-real-checkpoint-prepare.json').write_text(json.dumps(value))
    with pytest.raises(ValueError,match='CHECKPOINT_PREPARATION_INVALID'):
        subject.RealGate.offline_checkpoint_preparation(tmp_path,NOW+timedelta(minutes=31))
