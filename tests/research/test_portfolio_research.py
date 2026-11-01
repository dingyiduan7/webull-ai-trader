import json
from dataclasses import replace
from datetime import date,datetime,timedelta,timezone
from decimal import Decimal
from pathlib import Path

import pytest

from autonomous_trader.contracts import Instrument
from autonomous_trader.mvp.alpha import build_alpha_context
from autonomous_trader.mvp.config import reviewed_calendar
from autonomous_trader.mvp.context import Observation,UniverseObservation
from autonomous_trader.research.daily_bars import build_daily_research,normalize_daily_batch
from autonomous_trader.research.portfolio import (OfflinePortfolioResearch,PortfolioResearchError,
    SessionPortfolioResearch)
from autonomous_trader.research.sec_edgar import select_snapshot


CALENDAR=reviewed_calendar();FIXTURES=Path(__file__).parents[1]/'fixtures'/'sec_edgar'
CUTOFF=datetime(2026,11,4,18,tzinfo=timezone.utc)


def sessions(end,count):
    result=[];cursor=end
    while len(result)<count:
        if CALENDAR.session(cursor):result.append(cursor)
        cursor-=timedelta(days=1)
    return tuple(reversed(result))


def daily_body(*,missing=None):
    labels=sessions(date(2026,11,3),65);rows=[]
    for symbol,instrument,multiplier in (('QUAL','quality-1',Decimal('1.1')),('SPY','spy-1',Decimal(1))):
        bars=[]
        for index,label in enumerate(labels):
            if symbol=='QUAL' and label==missing:continue
            close=(Decimal(100)+index)*multiplier
            bars.append({'time':label.isoformat()+'T00:00:00+00:00','open':str(close-1),
                'high':str(close+1),'low':str(close-2),'close':str(close),'volume':str(100000+index),
                'trading_session':'RTH'})
        rows.append({'symbol':symbol,'instrument_id':instrument,'result':bars})
    return {'result':rows}


def daily_context(*,missing=None):
    series=normalize_daily_batch(daily_body(missing=missing),received_at=CUTOFF,as_of=CUTOFF,calendar=CALENDAR,
        adjustment_basis='FORWARD_ADJUSTED')
    return build_daily_research(series,calendar=CALENDAR)


def sec_snapshot():
    load=lambda name:json.loads((FIXTURES/name).read_text(encoding='utf-8'))
    return select_snapshot('QUAL','NYSE',load('company_tickers_exchange.json'),load('submissions.json'),
        load('companyfacts.json'),as_of=CUTOFF,retrieved_at=CUTOFF,calendar=CALENDAR)


def observation(*,quote_at=None,instrument=None):
    instrument=instrument or Instrument(instrument_id='quality-1',ticker='QUAL',country='US',asset_type='STOCK',
        listing_exchange='NYSE',liquid=True,tradable=True,quantity_increment='1',price_increment_usd='0.01',minimum_quantity='1')
    return Observation('quality-1',Decimal('164.99'),Decimal('165'),quote_at or CUTOFF-timedelta(seconds=1),CUTOFF,
        Decimal('100000'),Decimal(0),Decimal('100000'),{},(),'quote',(),True,CUTOFF,'QUAL',instrument,
        Decimal('1000000'),True,Decimal('1000'),Decimal('900'))


def state():
    return {'positions':{},'orders':{},'reservations':{},'cash':{'liquidation_equity_usd':'100000',
        'settled_usd':'100000'},'day_start_equity':'100000','reconciliation':{'ready':True,'settlement_known':True},
        'session':{'opens_at':datetime(2026,11,4,14,30,tzinfo=timezone.utc).isoformat(),
            'closes_at':datetime(2026,11,4,21,tzinfo=timezone.utc).isoformat()}}


def test_verified_facts_daily_observations_and_derivations_reach_astra_context():
    daily=daily_context();gate=OfflinePortfolioResearch(daily,{'quality-1':sec_snapshot()})
    observed=observation();research=gate.context_for(observed,CUTOFF);payload=json.loads(research.financial_context)
    assert research.daily==daily['quality-1'] and payload['identity']['cik']=='0000000001'
    assert len(payload['facts'])==9 and {row['metric'] for row in payload['facts']}=={
        'revenue','operating_income','operating_cash_flow','cash','debt_current','debt_noncurrent',
        'current_assets','current_liabilities','shares'}
    assert payload['filing']['accession'] and payload['filing']['fact_available_at']
    assert payload['filing']['fact_source_ref']=='companyfacts' and payload['sources']['companyfacts']
    assert {'operating_margin','current_ratio','median_dollar_volume_20'}<={
        row['metric'] for row in payload['derived_observations']}
    universe=UniverseObservation((observed,),CUTOFF,Decimal('100000'),Decimal(0),Decimal('100000'),{},(),'u')
    context=build_alpha_context(universe,state(),CUTOFF,daily_research={'quality-1':research.daily},
        financial_research={'quality-1':research.financial_context})
    assert context.instruments[0].daily_research is not None
    assert json.loads(context.instruments[0].sector_context)['cutoff_at']==CUTOFF.isoformat()


def test_missing_financials_are_explicit_gaps_and_do_not_exclude_candidate():
    research=OfflinePortfolioResearch(daily_context(),{}).context_for(observation(),CUTOFF)
    payload=json.loads(research.financial_context)
    assert research.daily is not None and payload['facts']==[]
    assert 'SEC_FINANCIALS_UNAVAILABLE' in payload['explicit_gaps']


def test_sec_identity_mismatch_is_not_presented_as_verified_fact():
    wrong=replace(sec_snapshot(),issuer=replace(sec_snapshot().issuer,exchange='NASDAQ'))
    research=OfflinePortfolioResearch(daily_context(),{'quality-1':wrong}).context_for(observation(),CUTOFF)
    payload=json.loads(research.financial_context)
    assert payload['identity'] is None and payload['facts']==[]
    assert 'SEC_IDENTITY_MISMATCH' in payload['explicit_gaps']


def test_filing_retrieved_after_cutoff_cannot_leak_into_context():
    future=replace(sec_snapshot(),issuer=replace(sec_snapshot().issuer,retrieved_at=CUTOFF+timedelta(seconds=1)))
    payload=json.loads(OfflinePortfolioResearch(daily_context(),{'quality-1':future})
        .context_for(observation(),CUTOFF).financial_context)
    assert payload['facts']==[] and 'SEC_EVIDENCE_AFTER_CUTOFF' in payload['explicit_gaps']


def test_daily_bar_gap_is_typed_and_visible_without_invented_value():
    missing=sessions(date(2026,11,3),10)[-5]
    research=(OfflinePortfolioResearch(daily_context(missing=missing),{'quality-1':sec_snapshot()})
        .context_for(observation(),CUTOFF))
    unavailable=[metric for metric in research.daily.metrics if metric.value is None]
    payload=json.loads(research.financial_context)
    assert unavailable and all(metric.unavailable_reason=='MISSING_COMPLETED_SESSION' for metric in unavailable)
    assert any(gap.startswith('DAILY_METRICS_UNAVAILABLE:MISSING_COMPLETED_SESSION:') for gap in payload['explicit_gaps'])


def test_stale_exact_quote_remains_a_hard_data_quality_failure():
    gate=OfflinePortfolioResearch(daily_context(),{'quality-1':sec_snapshot()})
    with pytest.raises(PortfolioResearchError,match='STALE_MARKET_OR_ACCOUNT'):
        gate.context_for(observation(quote_at=CUTOFF-timedelta(seconds=3)),CUTOFF)


def test_invalid_broker_identity_remains_ineligible_and_empty_universe_has_no_context():
    bad=Instrument(instrument_id='quality-1',ticker='QUAL',country='CA',asset_type='STOCK',
        listing_exchange='NYSE',liquid=True,tradable=True,quantity_increment='1',price_increment_usd='0.01',minimum_quantity='1')
    gate=OfflinePortfolioResearch(daily_context(),{'quality-1':sec_snapshot()})
    with pytest.raises(PortfolioResearchError,match='RESEARCH_BROKER_IDENTITY_INVALID'):
        gate.context_for(observation(instrument=bad),CUTOFF)
    with pytest.raises(ValueError,match='ALPHA_CONTEXT_REQUIRES_INSTRUMENT'):
        build_alpha_context(UniverseObservation((),CUTOFF,Decimal(0),Decimal(0),Decimal(0),{},(),'empty'),state(),CUTOFF)


def test_session_provider_is_bounded_cached_and_uses_fixture_adapter_outputs_only():
    calls={'daily':0,'sec':0}
    def daily_loader(symbols):
        calls['daily']+=1;assert symbols==('QUAL','SPY');return daily_body(),CUTOFF
    def sec_loader(item,cutoff):
        calls['sec']+=1;assert item.ticker=='QUAL' and cutoff==CUTOFF;return sec_snapshot()
    provider=SessionPortfolioResearch(daily_loader,CALENDAR,sec_snapshot_loader=sec_loader,
        sec_organization='Fixture Research',sec_contact='owner@example.test')
    observed=observation();provider.prepare((observed,),CUTOFF,'2026-11-04')
    first=provider.context_for(observed,CUTOFF)
    provider.prepare((observed,),CUTOFF,'2026-11-04')
    second=provider.context_for(observed,CUTOFF)
    assert calls=={'daily':1,'sec':1} and first==second
    assert first.daily is not None and json.loads(first.financial_context)['facts']


def test_missing_data_and_unconfigured_sec_identity_are_gaps_not_exclusions():
    calls={'daily':0,'sec':0}
    def missing_daily(symbols):
        calls['daily']+=1;raise ValueError('FIXTURE_DAILY_UNAVAILABLE')
    def forbidden_sec(*_):
        calls['sec']+=1;raise AssertionError('SEC loader must not run without configured User-Agent')
    provider=SessionPortfolioResearch(missing_daily,CALENDAR,sec_snapshot_loader=forbidden_sec)
    observed=observation();provider.prepare((observed,),CUTOFF,'2026-11-04')
    payload=json.loads(provider.context_for(observed,CUTOFF).financial_context)
    assert calls=={'daily':1,'sec':0}
    assert 'DAILY_RESEARCH_ERROR:FIXTURE_DAILY_UNAVAILABLE' in payload['explicit_gaps']
    assert 'SEC_USER_AGENT_NOT_CONFIGURED' in payload['explicit_gaps']
