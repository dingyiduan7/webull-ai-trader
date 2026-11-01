from datetime import date,datetime,timedelta,timezone
from decimal import Decimal

import pytest

from autonomous_trader.mvp.config import reviewed_calendar
from autonomous_trader.contracts import Instrument
from autonomous_trader.decision_api.astra_probe import deep_alpha_request
from autonomous_trader.mvp.alpha import build_alpha_context
from autonomous_trader.mvp.context import Observation,UniverseObservation
from autonomous_trader.research.daily_bars import (
    DailyBarError,
    build_daily_research,
    normalize_daily_batch,
)
from autonomous_trader.sandbox.http import Operation,build_request,validate_request


CALENDAR=reviewed_calendar()


def sessions_through(end,count):
    rows=[];day=end
    while len(rows)<count:
        if CALENDAR.session(day) is not None:rows.append(day)
        day-=timedelta(days=1)
    return list(reversed(rows))


def raw_bar(label,close,volume):
    close=Decimal(close)
    return {'time':label.isoformat()+'T00:00:00+00:00','open':str(close-1),'high':str(close+2),
        'low':str(close-2),'close':str(close),'volume':str(volume),'trading_session':'RTH'}


@pytest.fixture
def daily_payload():
    labels=sessions_through(date(2026,9,14),66)
    completed=labels[:-1]
    rows=[]
    for symbol,instrument,multiple in (('QUAL','quality-1',Decimal(2)),('SPY','spy-1',Decimal(1))):
        bars=[]
        for index,label in enumerate(completed):
            bars.append(raw_bar(label,(Decimal(100)+index)*multiple,1000+index))
        # The current regular session and a future session are returned by the
        # source but cannot enter a decision made before the current close.
        bars.append(raw_bar(labels[-1],Decimal(165)*multiple,2000))
        bars.append(raw_bar(date(2026,9,15),Decimal(166)*multiple,2100))
        rows.append({'symbol':symbol,'instrument_id':instrument,'result':list(reversed(bars))})
    return {'result':rows}


def normalize(payload,at=datetime(2026,9,14,15,tzinfo=timezone.utc)):
    return normalize_daily_batch(payload,received_at=at,as_of=at,calendar=CALENDAR,
        adjustment_basis='FORWARD_ADJUSTED')


def metrics(context):
    return {row.metric:row for row in context.metrics}


def test_certified_daily_request_is_separate_and_bounded():
    request=build_request(Operation.HISTORY_DAILY_BATCH,symbols=('QUAL','SPY'))
    assert request.get_body_params()=={'symbols':['QUAL','SPY'],'category':'US_STOCK','timespan':'D','count':250}
    assert validate_request(request)=='/market-data/stocks/bars/list'
    request.set_count(251)
    with pytest.raises(Exception,match='READ_ONLY_ENDPOINT_REJECTED'):
        validate_request(request)


def test_no_lookahead_excludes_current_and_future_sessions(daily_payload):
    series=normalize(daily_payload)
    assert series['QUAL'].bars[-1].session==date(2026,9,11)
    assert series['QUAL'].excluded_count==2
    context=build_daily_research(series,calendar=CALENDAR)['quality-1']
    assert context.completed_bar_count==65
    assert context.excluded_incomplete_or_future_bars==2
    assert all(row.end_session==date(2026,9,11) for row in context.metrics if row.value is not None)
    assert build_daily_research(series,calendar=CALENDAR)['spy-1'].symbol=='SPY'


def test_indicator_arithmetic_and_provenance(daily_payload):
    series=normalize(daily_payload);context=build_daily_research(series,calendar=CALENDAR)['quality-1'];m=metrics(context)
    latest=Decimal(328)
    q=Decimal('.00000001')
    assert m['return_1_session'].value==(latest/Decimal(326)-1).quantize(q)
    assert m['return_5_session'].value==(latest/Decimal(318)-1).quantize(q)
    assert m['return_20_session'].value==(latest/Decimal(288)-1).quantize(q)
    assert m['return_60_session'].value==(latest/Decimal(208)-1).quantize(q)
    last20=[(Decimal(100)+i)*2 for i in range(45,65)]
    dollar_volumes=[close*Decimal(1000+i) for i,close in zip(range(45,65),last20)]
    ordered=sorted(dollar_volumes)
    assert m['median_dollar_volume_20'].value==(ordered[9]+ordered[10])/2
    assert m['atr_20_ratio'].value==(Decimal(4)/latest).quantize(q)
    assert m['distance_sma_20'].value==(latest/(sum(last20,Decimal(0))/20)-1).quantize(q)
    assert m['distance_sma_50'].value==(latest/(sum(((Decimal(100)+i)*2 for i in range(15,65)),Decimal(0))/50)-1).quantize(q)
    assert m['high_20'].value==Decimal(330) and m['low_20'].value==Decimal(288)
    for window in (1,5,20,60):assert m[f'relative_strength_spy_{window}'].value==0
    for value in context.metrics:
        assert value.source_id=='WEBULL_SANDBOX_BATCH_DAILY_D250'
        assert value.received_at==context.received_at and value.as_of==context.as_of
        assert value.source_at==series['QUAL'].bars[-1].source_at
        assert value.observed_bar_count==65
    assert len(context.source_payload_sha256)==64 and context.adjustment_basis=='FORWARD_ADJUSTED'
    assert 'atr_20_ratio=mean_20' in context.methodology


def test_exchange_holiday_is_not_treated_as_missing(daily_payload):
    series=normalize(daily_payload);context=build_daily_research(series,calendar=CALENDAR)['quality-1'];m=metrics(context)
    assert date(2026,9,7) not in {bar.session for bar in series['QUAL'].bars}
    assert m['return_5_session'].unavailable_reason is None


def test_missing_completed_session_marks_affected_metrics_unavailable(daily_payload):
    missing=date(2026,9,9)
    daily_payload['result'][0]['result']=[row for row in daily_payload['result'][0]['result']
        if not row['time'].startswith(missing.isoformat())]
    context=build_daily_research(normalize(daily_payload),calendar=CALENDAR)['quality-1'];m=metrics(context)
    assert m['return_1_session'].value is not None
    assert m['return_5_session'].value is None
    assert m['return_5_session'].unavailable_reason=='MISSING_COMPLETED_SESSION'
    assert m['median_dollar_volume_20'].unavailable_reason=='MISSING_COMPLETED_SESSION'


def test_spy_alignment_failure_does_not_hide_candidate_metrics(daily_payload):
    missing=date(2026,9,9)
    daily_payload['result'][1]['result']=[row for row in daily_payload['result'][1]['result']
        if not row['time'].startswith(missing.isoformat())]
    context=build_daily_research(normalize(daily_payload),calendar=CALENDAR)['quality-1'];m=metrics(context)
    assert m['return_20_session'].value is not None
    assert m['relative_strength_spy_20'].value is None
    assert m['relative_strength_spy_20'].unavailable_reason=='MISSING_COMPLETED_SESSION'


def test_insufficient_history_is_explicitly_unavailable(daily_payload):
    for outer in daily_payload['result']:outer['result']=outer['result'][:12]
    context=build_daily_research(normalize(daily_payload),calendar=CALENDAR)['quality-1'];m=metrics(context)
    assert m['return_5_session'].value is not None
    assert m['return_20_session'].value is None
    assert m['return_20_session'].unavailable_reason=='INSUFFICIENT_HISTORY'
    assert m['return_20_session'].required_bar_count==21


def test_current_session_becomes_available_only_at_close(daily_payload):
    before=normalize(daily_payload,datetime(2026,9,14,19,59,59,tzinfo=timezone.utc))
    after=normalize(daily_payload,datetime(2026,9,14,20,tzinfo=timezone.utc))
    assert before['QUAL'].bars[-1].session==date(2026,9,11)
    assert after['QUAL'].bars[-1].session==date(2026,9,14)
    assert build_daily_research(after,calendar=CALENDAR)['quality-1'].metrics[0].end_session==date(2026,9,14)


def test_adjustment_basis_and_session_dates_fail_closed(daily_payload):
    with pytest.raises(DailyBarError,match='DAILY_ADJUSTMENT_BASIS_REQUIRED'):
        normalize_daily_batch(daily_payload,received_at=datetime(2026,9,14,15,tzinfo=timezone.utc),
            as_of=datetime(2026,9,14,15,tzinfo=timezone.utc),calendar=CALENDAR,adjustment_basis='UNKNOWN')
    daily_payload['result'][0]['result'][0]['time']='2026-09-13T00:00:00+00:00'
    with pytest.raises(DailyBarError,match='DAILY_BAR_NON_SESSION_DATE'):
        normalize(daily_payload)


def test_observation_received_after_decision_time_fails_closed(daily_payload):
    at=datetime(2026,9,14,15,tzinfo=timezone.utc)
    with pytest.raises(DailyBarError,match='DAILY_RECEIPT_AFTER_AS_OF'):
        normalize_daily_batch(daily_payload,received_at=at+timedelta(seconds=1),as_of=at,
            calendar=CALENDAR,adjustment_basis='FORWARD_ADJUSTED')


def test_typed_daily_research_reaches_portfolio_astra_context(daily_payload):
    now=datetime(2026,9,14,15,tzinfo=timezone.utc)
    research=build_daily_research(normalize(daily_payload,now),calendar=CALENDAR)
    instrument=Instrument(instrument_id='quality-1',ticker='QUAL',country='US',asset_type='STOCK',
        listing_exchange='NYSE',liquid=True,tradable=True,quantity_increment='1',
        price_increment_usd='0.01',minimum_quantity='1')
    observation=Observation('quality-1',Decimal('327.99'),Decimal('328'),now,now,
        Decimal('1000000'),Decimal(0),Decimal('1000000'),{},(),'evidence',(),True,now,'QUAL',
        instrument,Decimal('100000'))
    universe=UniverseObservation((observation,),now,Decimal('1000000'),Decimal(0),Decimal('1000000'),{},(),'universe')
    state={'positions':{},'orders':{},'reservations':{},'cash':{
        'liquidation_equity_usd':'1000000','settled_usd':'1000000'},'day_start_equity':'1000000',
        'reconciliation':{'ready':True,'settlement_known':True},'session':{
        'opens_at':datetime(2026,9,14,13,30,tzinfo=timezone.utc).isoformat(),
        'closes_at':datetime(2026,9,14,20,tzinfo=timezone.utc).isoformat()}}
    context=build_alpha_context(universe,state,now,daily_research=research)
    assert context.instruments[0].daily_research.symbol=='QUAL'
    payload=deep_alpha_request(context,{'maximum_positions':5})
    encoded=payload['input'][1]['content']
    assert 'daily_research' in encoded and 'relative_strength_spy_60' in encoded
    assert context.instruments[0].sector_context is None
