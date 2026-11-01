from datetime import datetime, timezone, timedelta
from decimal import Decimal
import json
from pathlib import Path
from uuid import uuid4
import pytest
from autonomous_trader.sandbox.http import HttpObservation
from autonomous_trader.sandbox.secrets import Credentials, PreflightError
from autonomous_trader.sandbox.certification.controls import MODE, exact_decimal
from autonomous_trader.sandbox.certification.round_trip import (
    RoundTripJournal, RoundTripHttp, AUTHORIZATION, marketable_order, owned_share, checked_detail, account_state)

NOW = datetime(2026,9,11,19,30,tzinfo=timezone.utc)


def snapshot():
    return [{'symbol':'AAPL','bid':'300.00','ask':'300.02','quote_time':str(int(NOW.timestamp()*1000))}]


def release(journal):
    old = journal.prepare('PLACE', {'client_order_id':'original'})
    journal.claim(old,'PLACE',{'client_order_id':'original'})
    evidence={'authorization':AUTHORIZATION,'cancelled_zero_fill':True,'matching_identifiers':True,
        'flat_unchanged_cash':True,'released_cash':'324.34'}
    journal.release_prior(evidence)
    journal.release_prior(evidence)


def test_release_is_append_only_idempotent_and_not_a_rearm(tmp_path):
    j=RoundTripJournal(tmp_path/'state.sqlite3','account');release(j)
    assert j.db.execute("SELECT COUNT(*) FROM events WHERE kind='OWNER_PRIOR_RESERVATION_RELEASE'").fetchone()==(1,)
    assert j.db.execute("SELECT claimed FROM intents WHERE kind='PLACE'").fetchone()==(1,)
    with pytest.raises(PreflightError):j.prepare('PLACE',{'client_order_id':'another'})
    j.close()


OBSERVED = Path(__file__).parents[1]/'fixtures/webull_m3b/20260911T193156104102Z'


def observed(name):
    return json.loads((OBSERVED/(name+'.json')).read_text())


@pytest.mark.parametrize('side,price', [('buy','332.76'),('sell','332.75')])
def test_actual_essential_rest_fills(side,price):
    report=observed('manifest')
    order=report[side+'_order']
    submitted=observed(side+'_submit')['body']
    row=checked_detail(observed('final_'+side+'_detail')['body'],order,submitted['order_id'])
    assert row['status']=='FILLED' and row['filled_quantity']=='1'
    assert row['filled_price']==price and row['total_quantity']=='1'
    if side=='sell':
        assert [f['actual_value'] for f in row['fees']]==['0.01','0.01']
        assert [f['receivable_value'] for f in row['fees']]==['0.01','0.01']
    else:
        assert row['fees']==[]
    assert row['commission']=={}  # observed empty structure, not inferred fee finality


def test_actual_owned_share_and_flat_cash_settlement_are_distinct():
    assert owned_share(observed('holding_positions')['body'])['cost_price']=='332.76'
    assert observed('after_positions')['body']==[]
    assert observed('after_orders')['body']=={'data':[]}
    cash=observed('after_balance')['body']['account_currency_assets'][0]
    assert exact_decimal(cash['settled_cash'])==Decimal('999667.24')
    assert exact_decimal(cash['unsettled_cash'])==Decimal('332.73')
    assert exact_decimal(cash['buying_power'])==Decimal('999999.97')
    assert exact_decimal(cash['cash_balance'])==exact_decimal(cash['settled_cash'])+exact_decimal(cash['unsettled_cash'])
    assert exact_decimal(cash['day_profit_loss'])==Decimal('-.03')
    assert observed('manifest')['round_trip_obligation_closed'] is True


def test_actual_grpc_fill_observations_match_rest_without_being_gates():
    report=observed('manifest')
    events=[e for e in report['events'] if e['event_type']=='ORDER']
    assert len(events)==2 and report['grpc_blocking'] is False
    for event, side in zip(events,('buy','sell')):
        row=report[side+'_fill']; payload=event['payload']
        assert event['event_type_code']==1024
        assert payload['client_order_id']==row['client_order_id']
        assert payload['order_id']==row['order_id']
        assert payload['scene_type']=='FINAL_FILLED' and payload['order_status']==row['status']=='FILLED'
        assert exact_decimal(payload['filled_qty'])==exact_decimal(row['filled_quantity'])==1
        assert payload['filled_price']==row['filled_price']
    # Preserve the observed differing fee key; do not silently map to REST.
    assert 'receivable_fee' in events[1]['payload']['fees'][0]
    assert 'receivable_value' not in events[1]['payload']['fees'][0]


def test_actual_history_preserves_prior_cancel_and_both_fills():
    rows=observed('after_history')['body']['data']
    assert len(rows)==3
    assert [g['orders'][0]['status'] for g in rows]==['CANCELLED','FILLED','FILLED']
    assert [g['orders'][0]['filled_quantity'] for g in rows]==['0','1','1']
    report=observed('manifest')
    for side in ('buy','sell'):
        order=report[side+'_order']
        group=next(g for g in rows if g['client_order_id']==order['client_order_id'])
        assert checked_detail(group,order,report[side+'_fill']['order_id'])['status']=='FILLED'


@pytest.mark.parametrize('mutation',['stale','wrong_account','open_order','wrong_position'])
def test_sell_account_state_cannot_weaken_existing_restrictions(mutation):
    data={}
    for name in ('accounts','balance','positions','orders'):
        item=observed('holding_'+name)
        data[name]=HttpObservation(item['path'],200,NOW.isoformat(),{},item['body'])
    account=next(a['account_id'] for a in data['accounts'].body if a['account_class']=='INDIVIDUAL_CASH')
    now=NOW
    if mutation=='stale':now+=timedelta(seconds=31)
    if mutation=='wrong_account':account='not-designated'
    if mutation=='open_order':data['orders'].body['data'].append({'status':'SUBMITTED'})
    if mutation=='wrong_position':data['positions'].body[0]['quantity']='2'
    with pytest.raises(PreflightError):account_state(data,account,now,owned=True)


def test_round_trip_only_one_intent_per_side_and_no_sell_without_fill(tmp_path):
    path=tmp_path/'state.sqlite3';j=RoundTripJournal(path,'account')
    buy=marketable_order(snapshot(),'BUY',NOW);sell=marketable_order(snapshot(),'SELL',NOW)
    with pytest.raises(PreflightError):j.prepare_leg(buy)
    release(j)
    with pytest.raises(PreflightError):j.prepare_leg(sell)
    intent=j.prepare_leg(buy);j.claim(intent,'ROUND_TRIP_BUY',buy);j.close()
    j=RoundTripJournal(path,'account')
    with pytest.raises(PreflightError):j.prepare_leg(marketable_order(snapshot(),'BUY',NOW))
    with pytest.raises(PreflightError):j.claim(intent,'ROUND_TRIP_BUY',buy)
    j.record('ROUND_TRIP_BUY_FILLED_RECONCILED',{'quantity':'1'})
    j.prepare_leg(sell)
    with pytest.raises(PreflightError):j.prepare_leg(sell)
    j.close()


def test_outward_collar_and_freshness():
    assert marketable_order(snapshot(),'BUY',NOW)['limit_price']=='300.63'
    assert marketable_order(snapshot(),'SELL',NOW)['limit_price']=='299.40'
    for now in (NOW+timedelta(seconds=3), NOW-timedelta(seconds=1)):
        with pytest.raises(PreflightError):marketable_order(snapshot(),'BUY',now)
    q=snapshot();q[0]['ask']='310'
    with pytest.raises(PreflightError):marketable_order(q,'BUY',NOW)


@pytest.mark.parametrize('rows', [[],[{'symbol':'AAPL'}],[{'symbol':'AAPL','quantity':'2'}],
    [{'symbol':'AAPL','quantity':'1','available_quantity':'0'}],[{'symbol':'MSFT','quantity':'1'}],
    [{'symbol':'AAPL','quantity':'1','side':'SHORT'}],[{'symbol':'AAPL','qty':'1','quantity':'0'}]])
def test_no_oversell_from_missing_conflicting_or_wrong_positions(rows):
    with pytest.raises(PreflightError):owned_share(rows)


def test_exact_held_share():
    assert owned_share([{'symbol':'AAPL','quantity':'1.00000','category':'US_STOCK'}])['quantity']=='1.00000'


def detail(order):
    return {'client_order_id':order['client_order_id'],'combo_order_id':'broker',
        'orders':[dict(order,order_id='broker',total_quantity='1',filled_quantity='1',filled_price='300.00',status='FILLED')]}


@pytest.mark.parametrize('side',['BUY','SELL'])
def test_direct_fill_checks_ids_side_limit_and_quantity(side):
    order=marketable_order(snapshot(),side,NOW)
    body=detail(order)
    assert checked_detail(body,order,'broker')['status']=='FILLED'
    body['orders'][0]['filled_quantity']='0'
    with pytest.raises(PreflightError):checked_detail(body,order,'broker')
    body=detail(order);body['orders'][0]['filled_price']='500' if side=='BUY' else '1'
    with pytest.raises(PreflightError):checked_detail(body,order,'broker')
    with pytest.raises(PreflightError):checked_detail(detail(order),order,'different')


def test_no_grpc_gate_and_timeout_cannot_retry(tmp_path,monkeypatch):
    from autonomous_trader.sandbox.certification import round_trip as module
    class Clock:
        @staticmethod
        def now(tz):return NOW
        fromisoformat=staticmethod(datetime.fromisoformat)
        fromtimestamp=staticmethod(datetime.fromtimestamp)
    monkeypatch.setattr(module,'datetime',Clock)
    folder=Path(__file__).parents[1]/'fixtures/webull_m3b/20260911T185152917591Z'
    observations={}
    for name in ('accounts','balance','positions','orders'):
        data=json.loads((folder/(name+'.json')).read_text())
        observations[name]=HttpObservation(data['path'],200,NOW.isoformat(),{},data['body'])
    account=next(a['account_id'] for a in observations['accounts'].body if a['account_class']=='INDIVIDUAL_CASH')
    j=RoundTripJournal(tmp_path/'state.sqlite3',account);release(j)
    client=RoundTripHttp(Credentials('synthetic-key','synthetic-secret',account),mode=MODE,journal=j)
    payload=marketable_order(snapshot(),'BUY',NOW)
    intent=j.prepare_leg(payload)
    seen=[]
    def timeout(request,path):
        seen.append(path)
        assert j.db.execute('SELECT claimed FROM intents WHERE intent_id=?',(intent,)).fetchone()==(1,)
        raise PreflightError('HTTP_TRANSPORT_FAILED')
    monkeypatch.setattr(client,'_send_bounded',timeout)
    quote=HttpObservation('/snapshot',200,NOW.isoformat(),{},snapshot())
    with pytest.raises(PreflightError):client.submit_leg(intent,payload,observations,quote)
    with pytest.raises(PreflightError):client.submit_leg(intent,payload,observations,quote)
    assert len(seen)==1
    j.close()

