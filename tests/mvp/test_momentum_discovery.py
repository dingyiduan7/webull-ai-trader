"""Observed short-window discovery remains read-only and separate from admission."""
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from autonomous_trader.mvp.context import SandboxSource, listed_equity
from autonomous_trader.sandbox.http import Operation, build_request
from autonomous_trader.monitoring.runner import runner_evidence
from autonomous_trader.monitoring.views import top_runner_view

NOW=datetime(2026,9,21,15,0,tzinfo=timezone.utc)


def test_sdk_short_windows_and_listed_penny_stock_boundary():
    assert build_request(Operation.GAINERS_LOSERS,rank_type='MIN_3').get_query_params()['rank_type']=='MIN_3'
    assert build_request(Operation.GAINERS_LOSERS,rank_type='MIN_5').get_query_params()['rank_type']=='MIN_5'
    base={'category':'US_STOCK','currency':'USD','status':'OC','lot_size':'1'}
    assert listed_equity(base|{'exchange_code':'NAS','sub_category':'COMMON_STOCK'})
    assert not listed_equity(base|{'exchange_code':'PSE','sub_category':'ETF'})
    assert not listed_equity(base|{'exchange_code':'OTCQ','sub_category':'COMMON_STOCK'})
    assert not listed_equity(base|{'exchange_code':'PINL','sub_category':'COMMON_STOCK'})


def make_source(short_supported=True):
    class Reader:
        def read(self,op,*,symbols=('AAPL',),rank_type='VOLUME',direction='DESC'):
            status=200
            if op in (Operation.GAINERS_LOSERS,Operation.TOP_ACTIVE):
                if op==Operation.GAINERS_LOSERS and rank_type in ('MIN_3','MIN_5') and not short_supported:
                    return SimpleNamespace(status_code=417,body={},received_at=NOW.isoformat())
                body=[{'symbol':s,'currency':'USD','exchange_code':ex,'change_ratio':change,'price':'0.26',
                    'relative_volume_10d':'4','volume':'900000','amplitude':'0.3'}
                    for s,ex,change in (('PENNY','NAS','0.09'),('SPPL','NSQ','0.06'),
                        ('VOO','PSE','0.05'),('TCEHY','PINL','0.2'))]
            elif op==Operation.INSTRUMENTS:
                body={'data':[{'symbol':s,'instrument_id':'id-'+s,'category':'US_STOCK',
                    'sub_category':'ETF' if s=='VOO' else 'COMMON_STOCK','currency':'USD','status':'OC',
                    'exchange_code':'PSE' if s=='VOO' else 'NAS','lot_size':'1'} for s in symbols]}
            elif op==Operation.HISTORY_BATCH:
                body={'result':[{'symbol':s,'result':[{'time':NOW.isoformat(),'open':'0.25','high':'0.26',
                    'low':'0.24','close':'0.25','volume':'100000'}]} for s in symbols]}
            elif op==Operation.SNAPSHOT:
                body=[{'symbol':s,'instrument_id':'id-'+s,'bid':'0.25','ask':'0.26',
                    'quote_time':str(int(NOW.timestamp()*1000)),'volume':'900000',
                    'list_status':'LISTED','lot_size':'1'} for s in symbols]
            elif op==Operation.ACCOUNTS:body=[{'account_id':'paper','account_type':'CASH','account_class':'INDIVIDUAL_CASH'}]
            elif op==Operation.BALANCE:body={'account_currency_assets':[{'currency':'USD','settled_cash':'1000',
                'unsettled_cash':'0','net_liquidation_value':'1000'}]}
            elif op==Operation.POSITIONS:body=[]
            elif op==Operation.OPEN_ORDERS:body={'data':[],'pagination_key':None}
            return SimpleNamespace(status_code=status,body=body,received_at=NOW.isoformat())
    source=SandboxSource.__new__(SandboxSource);source.credentials=SimpleNamespace(account_id='paper')
    source.readers={op:Reader() for op in Operation};source._universe_symbols=();source._universe_at=0
    source._discovery_generation=0;source.discovery_stats={};source._discovery_metrics={};source._discovery_provenance={}
    return source


def test_low_price_listed_stock_visible_and_short_window_fallback():
    source=make_source()
    result=source.refresh({},portfolio=True)
    assert {x.ticker for x in result.candidates}=={'PENNY','SPPL'}
    assert result.select('PENNY',NOW).ask==Decimal('.26')
    assert all(x['symbol']!='TCEHY' for x in source.discovery_stats['shortlist'])
    assert all(x['symbol']!='VOO' for x in source.discovery_stats['top_runners'])
    assert source.discovery_stats['top_runners'][0]['percent_per_minute']=='3.000000'
    assert [x['symbol'] for x in source.discovery_stats['screen_runners']][:2]==['PENNY','SPPL']
    assert source.discovery_stats['screen_runners'][0]['latest_price']=='0.26'
    assert all(x['symbol']!='TCEHY' for x in source.discovery_stats['screen_runners'])
    fallback=make_source(short_supported=False);result=fallback.refresh({},portfolio=True)
    assert {x.ticker for x in result.candidates}=={'PENNY','SPPL'}
    assert fallback.discovery_stats['top_runners']==[]
    assert fallback.discovery_stats['screen_runners']==[]
    assert fallback.discovery_stats['unsupported_rank_sources']==[
        'GAINERS_LOSERS:MIN_3:DESC','GAINERS_LOSERS:MIN_3:ASC',
        'GAINERS_LOSERS:MIN_5:DESC','GAINERS_LOSERS:MIN_5:ASC']


def test_runner_rank_freshness_untraded_and_dashboard_projection():
    rows=[{'symbol':s,'instrument_id':'id-'+s,'percent_per_minute':str(rate),'window_minutes':3,
        'window_change_percent':str(rate*3),'latest_price':'0.26','volume':'900000',
        'relative_volume_10d':'4','quote_at':NOW.isoformat(),'screen_at':NOW.isoformat(),
        'observed_at':NOW.isoformat()} for s,rate in (('PENNY',3),('SPPL',2))]
    evidence=runner_evidence(rows,NOW)
    assert len(evidence)==2 and all(json.loads(x)['current'] for x in evidence)
    assert json.loads(evidence[0])['authority']=='DISCOVERY_ONLY_REQUIRES_OWN_FRESH_EVALUATION'
    screen_rows=[{k:v for k,v in row.items() if k not in ('quote_at','latest_price','instrument_id','observed_at')}
        |{'source':'GAINERS_LOSERS:MIN_3:DESC'} for row in rows]
    day_rows=[{'symbol':'DAYONE','exchange_code':'NYSE','daily_change_percent':'24.5',
        'screen_rank':1,'screen_at':NOW.isoformat(),'source':'GAINERS_LOSERS:DAY_1:DESC',
        'latest_price':'4.25','volume':'1200000','relative_volume_10d':'5.5'}]
    projection=top_runner_view({'screen_runners':screen_rows,'day_gainers':day_rows,
        'observed_rows':400,'eligible_symbols':300},NOW,
        positions=[{'ticker':'SPPL'}],pipeline=[{'symbol':'PENNY','status':'RUNNING'}])
    assert [x['symbol'] for x in projection['rows']]==['PENNY','SPPL']
    assert projection['rows'][0]['under_evaluation'] and not projection['rows'][0]['held']
    assert projection['rows'][1]['held'] and not projection['rows'][1]['under_evaluation']
    assert projection['observed_count']==2 and 'Only 2' in projection['reason']
    assert 'receipt-time freshness only' in projection['coverage']
    assert projection['day_gainer_coverage'].startswith('Webull GAINERS_LOSERS · DAY_1 · DESC')
    assert projection['day_gainers'][0]['symbol']=='DAYONE'
    assert projection['day_gainers'][0]['latest_price']=='4.25'
    assert projection['day_gainers'][0]['daily_change_percent']=='24.5'
    assert 'quote_age_seconds' not in projection['rows'][0]
    later=NOW+timedelta(seconds=121)
    assert not json.loads(runner_evidence(rows,later)[0])['current']
    assert not top_runner_view({'screen_runners':screen_rows},later)['rows'][0]['fresh']
    assert runner_evidence(rows,NOW+timedelta(seconds=301))==()


def test_dashboard_labels_day_gainers_as_observed_non_authoritative_data():
    from pathlib import Path
    html=(Path(__file__).parents[2]/'src/autonomous_trader/monitoring/dashboard.html').read_text(encoding='utf-8')
    assert 'Webull 1-day gainers · DAY_1 DESC' in html
    assert "['Observed price','price']" in html and "['Observed','fresh']" in html
    assert 'Both lists guide attention only; neither authorizes an order or imposes a strategy.' in html
    assert "linkRunnerSymbols('day-gainers',dayView)" in html


def test_new_scan_replaces_old_short_window_measurement():
    source=make_source();source.refresh({},portfolio=True)
    assert source.discovery_stats['top_runners'][0]['percent_per_minute']=='3.000000'
    original=source.readers[Operation.GAINERS_LOSERS]
    class Changed:
        def read(self,*args,**kwargs):
            result=original.read(*args,**kwargs)
            if result.status_code==200:
                for row in result.body:
                    if row['symbol']=='PENNY':row['change_ratio']='0.12'
            return result
    source.readers[Operation.GAINERS_LOSERS]=Changed();source._universe_at=0
    source.refresh({},portfolio=True)
    assert source.discovery_stats['top_runners'][0]['percent_per_minute']=='4.000000'
