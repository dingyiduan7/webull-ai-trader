from datetime import datetime, timezone, timedelta, date
from decimal import Decimal
import json
import sqlite3
from pathlib import Path
from uuid import uuid4
import pytest

from autonomous_trader.sandbox.secrets import Credentials, PreflightError
from autonomous_trader.sandbox.http import HttpObservation, SandboxHttp, HTTP_HOST
from autonomous_trader.sandbox.certification.controls import *
from autonomous_trader.sandbox.certification.journal import CertificationJournal
from autonomous_trader.sandbox.certification.discovery import DiscoveryHttp, calendar_request
from autonomous_trader.sandbox.certification.preview import PreviewHttp
from autonomous_trader.sandbox.certification.first_order import FirstOrderHttp, exact_order_detail
from autonomous_trader.sandbox.certification.sanitize import OrderSanitizer
from webull.trade.request.v3.place_order_request import PlaceOrderRequest


NOW = datetime(2026, 9, 11, 18, 0, tzinfo=timezone.utc)


@pytest.fixture
def journal(tmp_path):
    result = CertificationJournal(tmp_path / 'journal.sqlite3', 'test-account')
    yield result
    result.close()


def order():
    return dict(client_order_id=uuid4().hex, combo_type='NORMAL', symbol='AAPL', instrument_type='EQUITY',
        market='US', order_type='LIMIT', limit_price='280.01', quantity='1', side='BUY',
        time_in_force='DAY', support_trading_session='CORE', entrust_type='QTY')


def bundle():
    root = Path(__file__).parents[1] / 'fixtures/webull_m3b/20260911T185152917591Z'
    result = {}
    for name in ('accounts', 'balance', 'positions', 'orders', 'instruments', 'snapshot'):
        data = json.loads((root / (name+'.json')).read_text())
        result[name] = HttpObservation(data['path'], 200, NOW.isoformat(), {}, data['body'])
    return result


def selected_id(observations):
    return next(a['account_id'] for a in observations['accounts'].body if a['account_class']=='INDIVIDUAL_CASH')


def test_actual_sanitized_flat_account_prerequisites():
    observations = bundle()
    result = verify_flat(MODE, selected_id(observations), observations, NOW)
    assert result.settled == Decimal('1000000.00')
    validate_single_order(order(), result, NOW)


@pytest.mark.parametrize('at', ['2026-09-11T13:29:59+00:00', '2026-09-11T19:55:00+00:00',
                              '2026-09-12T14:00:00+00:00', '2026-09-11T20:00:00+00:00'])
def test_session_is_finite_and_has_no_extended_hours(at):
    with pytest.raises(PreflightError): regular_session(datetime.fromisoformat(at))


@pytest.mark.parametrize('field,value', [('side','SELL'), ('side','SHORT'), ('quantity','2'), ('quantity','0.5'),
    ('symbol','MSFT'), ('instrument_type','OPTION'), ('order_type','MARKET'), ('support_trading_session','ALL'),
    ('time_in_force','GTC'), ('combo_type','OCO'), ('limit_price','280.001'), ('limit_price','NaN')])
def test_payload_cannot_expand_first_sequence(field,value):
    payload=order(); payload[field]=value
    p=FlatPrerequisites('test-account', NOW, Decimal('1000000'), Decimal('1000000'))
    with pytest.raises(PreflightError): validate_single_order(payload,p,NOW)


def test_old_or_wrong_account_observations_fail():
    data=bundle()
    with pytest.raises(PreflightError): verify_flat('LIVE', selected_id(data),data,NOW)
    with pytest.raises(PreflightError): verify_flat(MODE,'other',data,NOW)
    with pytest.raises(PreflightError): verify_flat(MODE,selected_id(data),data,NOW+timedelta(seconds=31))
    with pytest.raises(PreflightError): verify_flat(MODE,selected_id(data),data,NOW-timedelta(seconds=1))


def test_nonempty_or_missing_account_fields_cannot_authorize():
    data=bundle(); data['positions'].body.append({'quantity':'1'})
    with pytest.raises(PreflightError): verify_flat(MODE,selected_id(data),data,NOW)
    data=bundle(); del data['balance'].body['account_currency_assets'][0]['settled_cash']
    with pytest.raises((PreflightError, KeyError)): verify_flat(MODE,selected_id(data),data,NOW)


def test_pretransmission_claim_is_durable_and_never_rearmed(tmp_path):
    path=tmp_path/'journal.sqlite3'; j=CertificationJournal(path,'test-account'); payload=order()
    intent=j.prepare('PLACE',payload); j.claim(intent,'PLACE',payload); j.close()
    j=CertificationJournal(path,'test-account')
    with pytest.raises(PreflightError): j.claim(intent,'PLACE',payload)
    with pytest.raises(PreflightError): j.prepare('PLACE',order())
    assert j.db.execute("SELECT kind FROM events ORDER BY seq").fetchall()==[('PREPARED',),('TRANSMISSION_UNCERTAIN',)]
    with pytest.raises(sqlite3.IntegrityError): j.db.execute('UPDATE intents SET claimed=0')
    with pytest.raises(sqlite3.IntegrityError): j.db.execute('DELETE FROM events')
    j.close()


def test_competing_connections_cannot_claim_twice(tmp_path):
    path=tmp_path/'journal.sqlite3'; a=CertificationJournal(path,'test-account'); b=CertificationJournal(path,'test-account')
    payload=order(); intent=a.prepare('PLACE',payload)
    with a.transaction():
        with pytest.raises(sqlite3.OperationalError): b.claim(intent,'PLACE',payload)
    a.claim(intent,'PLACE',payload)
    with pytest.raises(PreflightError): b.claim(intent,'PLACE',payload)
    a.close(); b.close()


def test_changed_payload_or_account_cannot_consume(journal,tmp_path):
    payload=order(); intent=journal.prepare('PREVIEW',payload)
    changed=dict(payload,quantity='2')
    with pytest.raises(PreflightError): journal.claim(intent,'PREVIEW',changed)
    journal.claim(intent,'PREVIEW',payload)
    path=tmp_path/'bound.sqlite3'; j=CertificationJournal(path,'one');j.close()
    with pytest.raises(PreflightError): CertificationJournal(path,'two')


def test_rollback_does_not_leave_half_event(journal):
    with pytest.raises(RuntimeError):
        with journal.transaction():
            journal._event('TEST',None,{'synthetic':True})
            raise RuntimeError('injected crash before commit')
    assert journal.db.execute('SELECT COUNT(*) FROM events').fetchone()==(0,)


@pytest.mark.parametrize('cls',[SandboxHttp,DiscoveryHttp,PreviewHttp,FirstOrderHttp])
def test_generic_request_route_never_opens_order_writes(cls,journal,monkeypatch):
    credentials=Credentials('test-key','test-secret','test-account')
    kwargs={'mode':MODE,'journal':journal} if cls in (PreviewHttp,FirstOrderHttp) else {}
    client=cls(credentials,**kwargs)
    monkeypatch.setattr(client,'_make_http_response',lambda *a: pytest.fail('must reject before signing'))
    with pytest.raises(PreflightError): client.get_response(PlaceOrderRequest())


def test_calendar_query_path_is_split_and_foreign_host_rejected(monkeypatch):
    client=DiscoveryHttp(Credentials('test-key','test-secret'))
    request=calendar_request(date(2026,9,11))
    seen=[]
    monkeypatch.setattr(client,'_send_bounded',lambda r,p: seen.append(p))
    client.get_response(request)
    assert seen==['/trade/calendar']
    request.set_endpoint('api.webull.com')
    with pytest.raises(PreflightError): client.get_response(request)


def test_actual_calendar_404_grants_no_authority():
    path=Path(__file__).parents[1]/'fixtures/webull_m3b/20260911T184723691556Z/calendar.json'
    data=json.loads(path.read_text()); assert data['status_code']==404
    assert data['path']=='/trade/calendar' and data['error_topics']==['PATH']


def test_numeric_fees_preserved_and_secrets_private_ids_removed():
    s=OrderSanitizer(['secret-value'])
    result=s.clean({'estimated_cost':'280.01','estimated_transaction_fee':'0.0032',
        'client_order_id':'client-private','execution_id':'execution-private','app_secret':'secret-value',
        'limit_price':'secret-value','order_status':'CANCELLED','commission':{'actual_value':'0.10'}})
    assert result['estimated_cost']=='280.01' and result['estimated_transaction_fee']=='0.0032'
    assert result['client_order_id'].startswith('private-') and result['execution_id'].startswith('private-')
    assert 'secret-value' not in json.dumps(result) and 'app_secret' not in result


def test_empty_list_or_missing_filled_quantity_is_not_terminal_detail():
    with pytest.raises(PreflightError): exact_order_detail({'data':[]},'test-client')
    row={'client_order_id':'test-client','order_id':'test-broker','symbol':'AAPL','side':'BUY',
         'order_type':'LIMIT','total_quantity':'1','status':'CANCELLED'}
    with pytest.raises(PreflightError): exact_order_detail({'orders':[row]},'test-client')
    row['filled_quantity']='0'; assert exact_order_detail({'orders':[row]},'test-client')['status']=='CANCELLED'


def test_submit_timeout_keeps_consumed_intent_and_never_retries(journal,monkeypatch):
    from autonomous_trader.sandbox.certification import first_order
    class Clock:
        @staticmethod
        def now(tz): return NOW
    monkeypatch.setattr(first_order,'datetime',Clock)
    client=FirstOrderHttp(Credentials('test-key','test-secret','test-account'),mode=MODE,journal=journal)
    payload=order(); intent=journal.prepare('PLACE',payload)
    prerequisite=FlatPrerequisites('test-account',NOW,Decimal('1000000'),Decimal('1000000'))
    sent=[]
    def timeout(request,path):
        assert journal.db.execute('SELECT claimed FROM intents WHERE intent_id=?',(intent,)).fetchone()==(1,)
        assert path=='/trading/orders/place' and request.get_body_params()=={'account_id':'test-account','new_orders':[payload]}
        sent.append(path)
        raise PreflightError('HTTP_TRANSPORT_FAILED')
    monkeypatch.setattr(client,'_send_bounded',timeout)
    with pytest.raises(PreflightError): client.submit_first(intent,payload,prerequisite,Decimal('400'),NOW,True)
    with pytest.raises(PreflightError): client.submit_first(intent,payload,prerequisite,Decimal('400'),NOW,True)
    assert len(sent)==1


def test_dead_stream_blocks_before_claim(journal,monkeypatch):
    from autonomous_trader.sandbox.certification import first_order
    class Clock:
        @staticmethod
        def now(tz): return NOW
    monkeypatch.setattr(first_order,'datetime',Clock)
    client=FirstOrderHttp(Credentials('test-key','test-secret','test-account'),mode=MODE,journal=journal)
    payload=order(); intent=journal.prepare('PLACE',payload)
    prerequisite=FlatPrerequisites('test-account',NOW,Decimal('1000000'),Decimal('1000000'))
    with pytest.raises(PreflightError): client.submit_first(intent,payload,prerequisite,Decimal('400'),NOW,False)
    assert journal.db.execute('SELECT claimed FROM intents WHERE intent_id=?',(intent,)).fetchone()==(0,)


@pytest.mark.parametrize('code,label',[(0,'SubscribeSuccess'),(1,'Ping'),(1024,'ORDER'),(9999,'UNKNOWN_EVENT_TYPE')])
def test_sdk_application_event_codes_outside_proto_enum_are_preserved(code,label):
    from autonomous_trader.sandbox.certification.events import decode_event
    from autonomous_trader.sandbox.streams import pb
    event=pb.SubscribeResponse(eventType=code,subscribeType=1,contentType='application/json',
        payload='{"scene_type":"CANCEL_SUCCESS","filled_qty":"0.000"}',timestamp=1789153260000)
    decoded=decode_event(event)
    assert decoded['event_type']==label and decoded['event_type_code']==code
    assert decoded['payload']['filled_qty']=='0.000'


@pytest.mark.parametrize('name,status',[('working_detail','SUBMITTED'),('terminal_detail','CANCELLED')])
def test_populated_actual_order_fixture_replay(name,status):
    root=Path(__file__).parents[1]/'fixtures/webull_m3b/20260911T190032474030Z'
    event=json.loads((root/(name+'.json')).read_text())
    submit=json.loads((root/'submit.json').read_text())['body']
    row=exact_order_detail(event['body'],submit['client_order_id'])
    assert row['order_id']==submit['order_id'] and row['status']==status
    assert row['total_quantity']=='1' and row['filled_quantity']=='0' and row['limit_price']=='299.34'
    assert row['fees']==[] and row['commission']=={}  # no inferred zero final fee
    assert row['support_trading_session']=='CORE' and row['time_in_force']=='DAY'


def test_canceled_list_is_not_used_to_release_retained_reservation():
    root=Path(__file__).parents[1]/'fixtures/webull_m3b/20260911T190032474030Z'
    report=json.loads((root/'manifest.json').read_text())
    assert json.loads((root/'after_positions.json').read_text())['body']==[]
    assert json.loads((root/'after_orders.json').read_text())['body']=={'data':[]}
    assert report['reservations_released'] is False and report['advanced_to_next_test'] is False
    assert report['order_submissions']==1 and report['cancel_transmissions']==1
