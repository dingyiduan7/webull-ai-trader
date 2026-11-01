from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import socket

_CONNECT = socket.socket.connect
_CREATE_CONNECTION = socket.create_connection

import pytest
from pydantic import ValidationError

import autonomous_trader.real_market as real_market
from autonomous_trader.real_readiness import account_readiness, save_readiness, dashboard_readiness
from autonomous_trader.monitoring.views import overlay_real_runtime
from autonomous_trader.real_strategy_broker import RealStrategyHttp
import autonomous_trader.real_strategy_broker as real_broker
from webull.trade.request.get_activities_request_v2 import GetActivitiesRequestV2
from autonomous_trader.real_cash_activity import sanitize_cash_activities
from autonomous_trader.sandbox.http import Operation, build_request
from autonomous_trader.sandbox.sanitize import Sanitizer
import scripts.webull_real_readiness_packet as readiness_packet

NOW = datetime(2026, 9, 29, 15, tzinfo=timezone.utc)


def _entitlement(tmp_path):
    folder=tmp_path/'.local'/'real-readiness';folder.mkdir(parents=True,exist_ok=True)
    screenshot=folder/'portal.jpg';screenshot.write_bytes(b'current authenticated portal proof')
    from hashlib import sha256
    return {'account_binding':'real-test','build_sha256':'build','session_label':'2026-09-29',
        'status':'VERIFIED','product':'NASDAQ_BASIC_OPENAPI_NONDISPLAY','observed_at':NOW.isoformat(),
        'source_evidence':{'kind':'WEBULL_OPENAPI_ENTITLEMENT','source':'WEBULL_DEVELOPER_PORTAL_AUTHENTICATED',
            'environment':'PRODUCTION','http_host':'api.webull.com','non_display':True,'enabled':True,
            'product':'NASDAQ_BASIC_OPENAPI_NONDISPLAY','displayed_authorization':'Free Authorized',
            'displayed_expiration':'2027-09-29','screenshot_sha256':sha256(screenshot.read_bytes()).hexdigest().upper(),
            'screenshot_path':'.local/real-readiness/portal.jpg',
            'official_documentation':dict(readiness_packet.REALTIME_ENTITLEMENT_DOCS),
            'provider_response':{'subscription_name':'NASDAQ_BASIC_OPENAPI_NONDISPLAY','market':'NASDAQ',
                'usage':'NON_DISPLAY_OPENAPI','status':'ACTIVE'}}}


@pytest.mark.parametrize('case',['missing-portal','sandbox','unknown-doc','expired'])
def test_documented_realtime_entitlement_fails_closed(case,tmp_path):
    value=_entitlement(tmp_path)
    if case=='missing-portal':value['source_evidence'].pop('displayed_authorization')
    elif case=='sandbox':value['source_evidence']['environment']='SANDBOX'
    elif case=='unknown-doc':value['source_evidence']['official_documentation']['UNKNOWN']='https://example.invalid/'
    elif case=='expired':value['source_evidence']['displayed_expiration']='2026-09-28'
    with pytest.raises(ValueError,match='REAL_ENTITLEMENT_UNVERIFIED'):
        readiness_packet.validate_realtime_entitlement(value,{'account_binding':'real-test','build_sha256':'build',
            'market_session_label':'2026-09-29'},NOW,tmp_path)


def test_documented_realtime_entitlement_binds_actual_screenshot_bytes(tmp_path):
    value=_entitlement(tmp_path);prepare={'account_binding':'real-test','build_sha256':'build',
        'market_session_label':'2026-09-29'}
    assert readiness_packet.validate_realtime_entitlement(value,prepare,NOW,tmp_path)==value
    (tmp_path/'.local/real-readiness/portal.jpg').write_bytes(b'tampered')
    with pytest.raises(ValueError,match='REAL_ENTITLEMENT_UNVERIFIED'):
        readiness_packet.validate_realtime_entitlement(value,prepare,NOW,tmp_path)


def test_preserved_portal_evidence_remains_valid_until_expiry_but_not_from_future(tmp_path):
    value=_entitlement(tmp_path);prepare={'account_binding':'real-test','build_sha256':'build',
        'market_session_label':'2026-09-29'}
    value['observed_at']=(NOW-timedelta(days=90)).isoformat()
    assert readiness_packet.validate_realtime_entitlement(value,prepare,NOW,tmp_path)==value
    value['observed_at']=(NOW+timedelta(microseconds=1)).isoformat()
    with pytest.raises(ValueError,match='REAL_ENTITLEMENT_UNVERIFIED'):
        readiness_packet.validate_realtime_entitlement(value,prepare,NOW,tmp_path)


def test_documented_realtime_entitlement_requires_preserved_screenshot_file(tmp_path):
    value=_entitlement(tmp_path);prepare={'account_binding':'real-test','build_sha256':'build',
        'market_session_label':'2026-09-29'}
    (tmp_path/'.local/real-readiness/portal.jpg').unlink()
    with pytest.raises(ValueError,match='REAL_ENTITLEMENT_UNVERIFIED'):
        readiness_packet.validate_realtime_entitlement(value,prepare,NOW,tmp_path)


def test_missing_delay_metadata_uses_documented_proof_but_explicit_delayed_conflict_fails():
    assert readiness_packet.coverage_delay_evidence({})==(
        'DOCUMENTED_PRODUCTION_OPENAPI_SUBSCRIPTION_PLUS_CURRENT_EFFECTIVENESS')
    assert readiness_packet.coverage_delay_evidence({'is_delayed':False})=='EXPLICIT_RESPONSE'
    with pytest.raises(ValueError,match='REAL_COVERAGE_EXPLICITLY_DELAYED'):
        readiness_packet.coverage_delay_evidence({'is_delayed':True})
    with pytest.raises(ValueError,match='REAL_COVERAGE_EXPLICITLY_DELAYED'):
        readiness_packet.coverage_delay_evidence({'delay_type':'DELAYED'})


@pytest.mark.parametrize('wrap', [False, True])
def test_real_discovery_accepts_official_array_and_legacy_wrapped_shapes(monkeypatch, wrap):
    now = datetime(2026, 9, 30, 14, tzinfo=timezone.utc)
    responses = [[{'symbol': symbol, 'price': price}]
        for symbol, price in (('AAA', '1'), ('BBB', '2'), ('CCC', '3'))]

    class Http:
        def read(self, request):
            rows = responses.pop(0)
            return {'data': rows} if wrap else rows

    monkeypatch.setattr(real_market, 'RealStrategyHttp', lambda credentials: Http())
    source = real_market.RealMarketSource(object(), 'real-' + 'a' * 64, clock=lambda: now)
    assert source.discover() == ('AAA', 'BBB', 'CCC')
    assert responses == []


def test_real_profile_binding_excludes_ambiguous_entry_symbol_and_ignores_unrequested_rows():
    profiles, ambiguous = {}, set()
    real_market._bind_profiles({'data': [
        {'symbol': 'EXTRA', 'instrument_id': 'outside', 'status': 'OC'},
        {'symbol': 'AAA', 'instrument_id': 'old', 'status': 'NT'},
        {'symbol': 'AAA', 'instrument_id': 'current', 'status': 'OC'},
        {'symbol': 'BBB', 'instrument_id': 'unique', 'status': 'OC'},
    ]}, ('AAA', 'BBB'), profiles, ambiguous)
    assert profiles == {'BBB': {'symbol': 'BBB', 'instrument_id': 'unique', 'status': 'OC'}}
    assert ambiguous == {'AAA'}


def test_real_context_history_uses_exact_identity_bound_batch_shape():
    rows=[{'time':NOW.isoformat(),'open':'1','high':'2','low':'1','close':'2','volume':'100'}]
    body={'result':[{'symbol':'AAPL','instrument_id':'apple','result':rows}]}
    assert real_market._single_batch_bar_rows(body,'AAPL','apple')==rows
    for invalid in ({'result':[]},
            {'result':[{'symbol':'QQQ','instrument_id':'apple','result':rows}]},
            {'result':[{'symbol':'AAPL','instrument_id':'wrong','result':rows}]},
            {'result':[{'symbol':'AAPL','instrument_id':'apple','result':rows},
                {'symbol':'AAPL','instrument_id':'apple','result':rows}]}):
        with pytest.raises(ValueError,match='REAL_HISTORY_BATCH_IDENTITY'):
            real_market._single_batch_bar_rows(invalid,'AAPL','apple')


def test_real_quote_spread_is_normalized_to_contract_precision():
    from decimal import Decimal
    assert real_market._spread_bps(Decimal('2.25'),Decimal('2.26'))==Decimal('44.444444')


def test_real_quote_freshness_rejects_old_future_and_old_receipt_without_relaxing_limit():
    fresh={'quote_time':int(NOW.timestamp()*1000)}
    assert real_market._quote_is_fresh(fresh,NOW,NOW)
    assert not real_market._quote_is_fresh({'quote_time':int((NOW-timedelta(seconds=10, microseconds=1)).timestamp()*1000)},NOW,NOW)
    assert not real_market._quote_is_fresh({'quote_time':int((NOW+timedelta(seconds=1)).timestamp()*1000)},NOW,NOW)
    assert not real_market._quote_is_fresh(fresh,NOW-timedelta(seconds=11),NOW)
    assert real_market._quote_limit({'session_label':'2026-09-29'}) == 10
    assert real_market._quote_limit(None)==10


def _coverage_samples():
    return [{'symbol':'AAPL','instrument_id':'apple'}, {'symbol':'QQQ','instrument_id':'fund'}]


def _coverage_bars():
    return {'result':[
        {'symbol':'AAPL','instrument_id':'apple','result':[{'time':NOW.isoformat()}]},
        {'symbol':'QQQ','instrument_id':'fund','result':[{'time':NOW.isoformat()}]},
    ]}


def test_real_coverage_batch_bars_requires_exact_complete_symbol_groups():
    result=readiness_packet.coverage_batch_bars(_coverage_bars(),_coverage_samples(),NOW,NOW)
    assert set(result)=={'AAPL','QQQ'}
    assert all(row['bars_http_status']==200 for row in result.values())


@pytest.mark.parametrize('case,error',[('missing','REAL_COVERAGE_BARS_MISSING'),
    ('duplicate','REAL_COVERAGE_BARS_DUPLICATE'),('unrequested','REAL_COVERAGE_BARS_UNREQUESTED'),
    ('stale','REAL_MARKET_COVERAGE_STALE')])
def test_real_coverage_batch_bars_rejects_incomplete_ambiguous_or_stale_groups(case,error):
    body=_coverage_bars()
    if case=='missing':body['result'].pop()
    elif case=='duplicate':body['result'].append(dict(body['result'][0]))
    elif case=='unrequested':body['result'][0]['symbol']='MSFT'
    elif case=='stale':body['result'][0]['result'][0]['time']=(NOW-timedelta(seconds=181)).isoformat()
    with pytest.raises(ValueError,match=error):
        readiness_packet.coverage_batch_bars(body,_coverage_samples(),NOW,NOW)


def test_real_transport_allows_only_exact_read_only_batch_bars_post(monkeypatch):
    transport=object.__new__(RealStrategyHttp);calls=[]
    monkeypatch.setattr(transport,'_send',lambda request,**kwargs:(calls.append(kwargs) or {'result':[]}))
    request=build_request(Operation.HISTORY_BATCH,symbols=('AAPL','QQQ'))
    request.set_real_time_required(True)
    assert transport.read(request)=={'result':[]}
    assert calls==[{'read_only_post':True}]
    request.add_body_params('count',5000)
    with pytest.raises(ValueError,match='REAL_READ_ROUTE_REQUIRED'):transport.read(request)


def test_real_cash_activity_read_is_exact_get_bound_and_completely_paginated(monkeypatch):
    transport=object.__new__(RealStrategyHttp);requests=[];call_options=[]
    pages=[{'data':[{'id':'a'}],'pagination_key':'next'},{'data':[{'id':'b'}]}]
    def send(request,**kwargs):requests.append(request);call_options.append(kwargs);return pages.pop(0)
    monkeypatch.setattr(transport,'_send',send)
    rows,count,end=transport.cash_activity_pages('acct',NOW,max_pages=3)
    assert rows==[{'id':'a'},{'id':'b'}] and count==2 and end=='2026-09-29T15:00:00.000Z'
    assert [request.get_method() for request in requests]==['GET','GET']
    assert requests[0].get_query_params()=={'account_id':'acct','start_time':'2026-09-29T14:58:44.000Z','end_time':end}
    assert requests[1].get_query_params()=={**requests[0].get_query_params(),'pagination_key':'next'}
    assert all(request.get_body_params() is None for request in requests)
    assert call_options==[{},{}]


def test_real_cash_activity_request_rejects_wrong_account_time_or_extra_query():
    end='2026-09-29T15:00:00.000Z'
    def request():
        value=GetActivitiesRequestV2();value.set_account_id('acct')
        value.set_start_time(real_broker.CASH_ACTIVITY_START);value.set_end_time(end);return value
    assert real_broker._cash_activity_request(request(),'acct',end)
    for key,value in (('account_id','other'),('start_time','2026-09-29T14:58:45Z'),
            ('end_time','2026-09-29T15:01:00Z'),('activity_types','FEE')):
        changed=request();changed.add_query_param(key,value)
        assert not real_broker._cash_activity_request(changed,'acct',end)


@pytest.mark.parametrize('pages,error',[
    ([{'data':[],'pagination_key':'same'},{'data':[],'pagination_key':'same'}],'REAL_CASH_ACTIVITY_PAGINATION'),
    ([{'data':[],'pagination_key':'more'}],'REAL_CASH_ACTIVITY_PAGINATION'),
    ([{'rows':[]}],'REAL_CASH_ACTIVITY_SHAPE')])
def test_real_cash_activity_pagination_fails_closed(monkeypatch,pages,error):
    transport=object.__new__(RealStrategyHttp)
    monkeypatch.setattr(transport,'_send',lambda *args,**kwargs:pages.pop(0))
    with pytest.raises(ValueError,match=error):transport.cash_activity_pages('acct',NOW,max_pages=1 if len(pages)==1 else 3)


def test_cash_activity_sanitization_retains_economic_fields_and_hashes_reference():
    rows=[{'id':'private-reference','account_id':'acct','activity_type':'INTERESTS',
        'activity_sub_type':'ADJUSTMENT','currency':'USD','net_amount':'0.09',
        'trade_date':'2026-09-29','biz_time':NOW.isoformat()}]
    clean,net=sanitize_cash_activities(rows,NOW-timedelta(seconds=1),NOW,'acct')
    assert net==Decimal('0.09') and clean[0]['amount_usd']=='0.09'
    assert clean[0]['status'] is None and clean[0]['reference_sha256'] and 'private-reference' not in json.dumps(clean)


@pytest.mark.parametrize('change,error',[
    ({'net_amount':None},'REAL_CASH_ACTIVITY_AMOUNT'),({'currency':'EUR'},'REAL_CASH_ACTIVITY_CURRENCY'),
    ({'biz_time':'bad'},'REAL_CASH_ACTIVITY_TIME'),({'account_id':'other'},'REAL_CASH_ACTIVITY_ACCOUNT')])
def test_cash_activity_sanitization_rejects_unknown_amount_non_usd_and_bad_time(change,error):
    row={'id':'a','account_id':'acct','activity_type':'INTERESTS','activity_sub_type':'ADJUSTMENT',
        'currency':'USD','net_amount':'0.09','trade_date':'2026-09-29','biz_time':NOW.isoformat()};row.update(change)
    with pytest.raises(ValueError,match=error):sanitize_cash_activities([row],NOW-timedelta(seconds=1),NOW,'acct')




def sample():
    return account_readiness({'account_id': 'PRIVATE', 'account_currency_assets': [{
        'currency': 'USD', 'settled_cash': '500', 'unsettled_cash': '0',
        'cash_balance': '500', 'net_liquidation_value': '505'}]},
        [{'symbol': 'TEST', 'quantity': '.01', 'market_value': '5', 'account_id': 'PRIVATE'}],
        {'data': []}, NOW)


def test_observation_never_grants_external_position_or_entry_authority(tmp_path):
    save_readiness(tmp_path, sample(), Sanitizer(('PRIVATE',)))
    result = dashboard_readiness(tmp_path, NOW)
    assert result['status'] == 'RECENT_READ_ONLY_OBSERVATION'
    assert result['account_totals_match']
    assert not result['reconciliation_ready'] and not result['production_enabled']
    assert not result['unattended_overnight_enabled']
    assert result['external_holdings'][0]['automation_authority'] == 'NONE'
    assert result['real_model_spend_usd'] is None
    assert 'PRIVATE' not in json.dumps(result)
    assert (result['entries_per_session'], result['round_trips_per_exchange_week']) == (None, None)
    assert (result['portfolio_calls_per_session'], result['portfolio_calls_per_week']) == (None, None)


def test_age_not_dashboard_heartbeat_controls_real_observation_freshness(tmp_path):
    save_readiness(tmp_path, sample(), Sanitizer(()))
    assert dashboard_readiness(tmp_path, NOW + timedelta(seconds=31))['status'] == 'STALE_OBSERVATION'
    assert dashboard_readiness(tmp_path, NOW - timedelta(seconds=1))['status'] == 'STALE_OBSERVATION'
    assert dashboard_readiness(tmp_path / 'another-environment', NOW)['status'] == 'NO_OBSERVATION'


def test_unvalidated_copy_cannot_enable_production_or_smuggle_identifier(tmp_path):
    for update in ({'production_enabled': True}, {'account_id': 'PRIVATE'}, {'environment': 'SANDBOX'}):
        with pytest.raises(ValidationError):
            save_readiness(tmp_path, sample().model_copy(update=update), Sanitizer(()))


def test_entitlement_denial_is_recordable_without_fabricating_market_or_ledger_state(tmp_path):
    observation = sample().model_copy(update={'market_data': 'ENTITLEMENT_BLOCKED'})
    save_readiness(tmp_path, observation, Sanitizer(()))
    result = dashboard_readiness(tmp_path, NOW)
    assert result['market_data'] == 'ENTITLEMENT_BLOCKED'
    assert result['latches'] == 'NOT_INITIALIZED'
    assert result['reconciliation_ready'] is False


def test_account_mismatch_is_evidence_not_balance_rewrite():
    value = account_readiness({'account_currency_assets': [{'currency': 'USD',
        'settled_cash': '500', 'unsettled_cash': '0', 'cash_balance': '500',
        'net_liquidation_value': '999'}]}, [], {'data': []}, NOW)
    assert not value.account_totals_match and value.equity_usd == 999
    with pytest.raises(ValueError):
        account_readiness({'account_currency_assets': []}, [], {'data': []}, NOW)


def test_dashboard_http_exposes_real_evidence_separately_without_broker_calls(tmp_path, monkeypatch):
    from threading import Thread
    from urllib.request import urlopen
    from autonomous_trader.monitoring.server import DashboardServer
    def local_connect(sock, address):
        assert address[0] == '127.0.0.1'
        return _CONNECT(sock, address)
    monkeypatch.setattr(socket.socket, 'connect', local_connect)
    monkeypatch.setattr(socket, 'create_connection', _CREATE_CONNECTION)
    save_readiness(tmp_path, sample(), Sanitizer(()))
    class Reader:
        def snapshot(self):
            return {'system': {'mode': 'SANDBOX'}, 'real_readiness': dashboard_readiness(tmp_path, NOW)}
    server = DashboardServer(Reader(), port=0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urlopen(f'http://127.0.0.1:{server.server_port}/api/snapshot') as response:
            value = json.load(response)
        assert value['system']['mode'] == 'SANDBOX'
        assert value['real_readiness']['environment'] == 'REAL_READ_ONLY'
        assert value['real_readiness']['production_enabled'] is False
        with urlopen(f'http://127.0.0.1:{server.server_port}/') as response:
            assert b'REAL account' in response.read()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_authenticated_after_hours_packet_supersedes_historical_observation_for_display(tmp_path,monkeypatch):
    import autonomous_trader.durable.ledger as ledger_module
    import autonomous_trader.real_flat_checkpoint as checkpoint_module
    import autonomous_trader.real_liquidation as liquidation_module
    import autonomous_trader.real_state as state_module
    folder=tmp_path/'.local'/'real-readiness';folder.mkdir(parents=True)
    (folder/checkpoint_module.V2_FILENAME).write_text('{}',encoding='utf-8')
    packet={'schema':'REAL_AFTER_HOURS_ACCOUNT_PACKET_V1','created_at':NOW.isoformat(),
        'flat_checkpoint_sha256':'checkpoint-sha','account':{'flat':True,'positions_count':0,
        'open_nonfinal_order_count':0},'cash':{'settled_usd':'505.35','unsettled_usd':'0.00',
        'equity_usd':'505.35','continuity':'EXPECTED_SETTLEMENT_EVOLUTION'},
        'authority':{'production_enabled':False,'broker_mutations':0}}
    (folder/'after-hours-account-packet-test.json').write_text(json.dumps(packet),encoding='utf-8')
    checkpoint={'observed_at':'2026-09-29T14:58:44Z','cash':{'settled_usd':'500.00',
        'unsettled_usd':'5.35','equity_usd':'505.35'},'local_integrity':{'observation_ledger_version':1}}
    class DB:pass
    class Ledger:
        def __init__(self,db):pass
        def verify_replay(self):return {'account_binding':'real-'+'a'*64}
    monkeypatch.setattr(ledger_module,'LocalDatabase',lambda *args,**kwargs:DB())
    monkeypatch.setattr(state_module,'RealObservationLedger',Ledger)
    monkeypatch.setattr(checkpoint_module,'load_checkpoint',lambda *args,**kwargs:(checkpoint,'checkpoint-sha'))
    monkeypatch.setattr(liquidation_module,'dashboard_status',lambda db:{'status':'FLAT'})
    result=dashboard_readiness(tmp_path,NOW)
    assert result['status']=='REAL_DISABLED_NO_GO'
    assert result['current_evidence']=='AUTHENTICATED_AFTER_HOURS_PACKET'
    assert (result['settled_usd'],result['unsettled_usd'],result['equity_usd'])==('505.35','0.00','505.35')
    assert result['external_holdings']==[] and result['historical_observation_preserved']
    assert result['monthly_hard_cap_usd'] is None
    assert (result['position_calls_per_session'],result['position_calls_per_week'])==(None,None)
    assert (result['astra_calls_per_session'],result['astra_calls_per_week'])==(None,None)
    assert not result['production_enabled'] and result['authority_blocker']=='JUDICATOR_REAL_GO_REQUIRED'


def test_dashboard_marks_real_active_only_from_supervised_fresh_runtime_state():
    now=datetime(2026,9,29,16,tzinfo=timezone.utc)
    base={'system':{'mode':'REAL','status':'RUNNING','ledger_schema':'real_trader','heartbeat':'now',
        'authority_build_sha256':'build','market_session_label':'2026-09-29','reconciliation_ready':True,
        'recovery_only':False,'unresolved':False,'account_id':'real-account'},
        'account':{'reconciliation':{'ready':True,'valid_until':(now+timedelta(minutes=1)).isoformat()}},'risk':{'latches':{}},
        'real_readiness':{'production_enabled':False,'status':'REAL_DISABLED_NO_GO'}}
    supervisor={'status':'HEALTHY','build':'build','authority_session':'2026-09-29',
        'account_binding':'real-account','recovery_only':False,'unresolved':False}
    result=overlay_real_runtime(base,expected_build='build',expected_session='2026-09-29',
        receipt_valid=True,supervisor=supervisor,now=now)
    assert result['real_readiness']=={'production_enabled':True,'status':'REAL_SUPERVISED_ACTIVE',
        'authority_blocker':None,'reconciliation_ready':True,'latches':{},
        'current_evidence':'REAL_LEDGER_BUILD_SESSION_RECEIPT_AND_SUPERVISOR'}
    stale={'system':{'mode':'REAL','status':'ERROR','ledger_schema':'real_trader'},
        'real_readiness':{'production_enabled':False}}
    assert overlay_real_runtime(stale)['real_readiness']['production_enabled'] is False
    recovery={**base,'system':{**base['system'],'recovery_only':True},
        'real_readiness':{'production_enabled':False}}
    recovery_supervisor={**supervisor,'recovery_only':True}
    shown=overlay_real_runtime(recovery,expected_build='build',expected_session='2026-09-29',
        receipt_valid=True,supervisor=recovery_supervisor,now=now)['real_readiness']
    assert shown['status']=='REAL_RECOVERY_ACTIVE' and shown['production_enabled'] is False
    wrong={**base,'system':{**base['system'],'authority_build_sha256':'old'},
        'real_readiness':{'production_enabled':False}}
    shown=overlay_real_runtime(wrong,expected_build='build',expected_session='2026-09-29',
        receipt_valid=True,supervisor=supervisor,now=now)['real_readiness']
    assert shown['status']=='REAL_RUNTIME_EVIDENCE_INVALID' and shown['production_enabled'] is False
    expired={**base,'account':{'reconciliation':{'ready':True,
        'valid_until':(now-timedelta(seconds=1)).isoformat()}},'real_readiness':{'production_enabled':False}}
    shown=overlay_real_runtime(expired,expected_build='build',expected_session='2026-09-29',
        receipt_valid=True,supervisor=supervisor,now=now)['real_readiness']
    assert shown['production_enabled'] is False and shown['reconciliation_ready'] is False

