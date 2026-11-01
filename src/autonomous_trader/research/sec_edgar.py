"""Point-in-time SEC EDGAR normalization with no trading authority."""
from dataclasses import dataclass
from datetime import date,datetime,timedelta,timezone
from decimal import Decimal,InvalidOperation
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo
import requests

NY=ZoneInfo('America/New_York')
ALLOWED_HOSTS=frozenset({'www.sec.gov','data.sec.gov'})
FORMS=frozenset({'10-Q','10-K'})

class EdgarError(ValueError):pass

@dataclass(frozen=True)
class EdgarConfig:
    organization:str
    contact:str
    enabled:bool=False
    def __post_init__(self):
        if self.enabled and (not self.organization.strip() or not self.contact.strip()):raise EdgarError('SEC_USER_AGENT_REQUIRED')
    @property
    def user_agent(self):
        if not self.organization.strip() or not self.contact.strip():raise EdgarError('SEC_USER_AGENT_REQUIRED')
        return f'{self.organization.strip()} {self.contact.strip()}'

class EdgarHttp:
    def __init__(self,config:EdgarConfig,*,session_factory=requests.Session):self.config,self.session_factory=config,session_factory
    def get_json(self,url):
        if not self.config.enabled:raise EdgarError('SEC_NETWORK_NOT_ENABLED')
        target=urlsplit(url)
        if target.scheme!='https' or target.hostname not in ALLOWED_HOSTS or target.username or target.password:
            raise EdgarError('SEC_ENDPOINT_REJECTED')
        with self.session_factory() as session:
            response=session.get(url,headers={'User-Agent':self.config.user_agent,'Accept-Encoding':'gzip, deflate'},
                timeout=(5,15),allow_redirects=False)
            if response.status_code!=200:raise EdgarError('SEC_HTTP_ERROR')
            try:return response.json()
            except Exception:raise EdgarError('SEC_JSON_INVALID') from None

@dataclass(frozen=True)
class Issuer:
    ticker:str;cik:str;issuer:str;exchange:str;retrieved_at:datetime;provenance:str

@dataclass(frozen=True)
class FactComponent:
    taxonomy:str;tag:str;unit:str;value:Decimal;accession:str;form:str;fiscal_year:int;fiscal_period:str
    start:date|None;end:date;filed:date;accepted_at:datetime;available_at:datetime;raw_provenance:dict[str,Any]

@dataclass(frozen=True)
class FiledFact:
    metric:str;value:Decimal;unit:str;accession:str;form:str;fiscal_year:int;fiscal_period:str
    start:date|None;end:date;filed:date;accepted_at:datetime;available_at:datetime;retrieved_at:datetime
    source_url:str;taxonomy:str;tag:str;components:tuple[FactComponent,...];raw_provenance:dict[str,Any]

@dataclass(frozen=True)
class EdgarSnapshot:
    issuer:Issuer;as_of:datetime;accession:str;form:str;fiscal_year:int;fiscal_period:str
    period_end:date;facts:dict[str,FiledFact]

METRICS={
    'revenue':('USD',('RevenueFromContractWithCustomerExcludingAssessedTax','Revenues','SalesRevenueNet')),
    'operating_income':('USD',('OperatingIncomeLoss',)),
    'operating_cash_flow':('USD',('NetCashProvidedByUsedInOperatingActivities',)),
    'cash':('USD',('CashAndCashEquivalentsAtCarryingValue','CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents')),
    'debt_current':('USD',('LongTermDebtCurrent','CurrentPortionOfLongTermDebt','ShortTermBorrowings')),
    'debt_noncurrent':('USD',('LongTermDebtNoncurrent',)),
    'current_assets':('USD',('AssetsCurrent',)),
    'current_liabilities':('USD',('LiabilitiesCurrent',)),
    'shares':('shares',('EntityCommonStockSharesOutstanding','CommonStockSharesOutstanding')),
}
DURATION=frozenset({'revenue','operating_income','operating_cash_flow'})

def _utc(value):
    try:
        parsed=datetime.fromisoformat(str(value).replace('Z','+00:00'))
        if parsed.tzinfo is None:raise ValueError()
        return parsed.astimezone(timezone.utc)
    except (TypeError,ValueError):raise EdgarError('SEC_ACCEPTANCE_TIME_INVALID') from None

def _day(value,code='SEC_DATE_INVALID'):
    try:return date.fromisoformat(str(value))
    except (TypeError,ValueError):raise EdgarError(code) from None

def _cik(value):
    raw=str(value).strip()
    if not raw.isdigit() or len(raw)>10:raise EdgarError('SEC_CIK_INVALID')
    return raw.zfill(10)

def resolve_issuer(ticker,exchange,tickers,submissions,*,retrieved_at):
    if retrieved_at.tzinfo is None:raise EdgarError('SEC_RETRIEVAL_TIME_INVALID')
    try:
        fields=tickers['fields'];records=[dict(zip(fields,row)) for row in tickers['data']]
    except (KeyError,TypeError):raise EdgarError('SEC_TICKER_FILE_INVALID') from None
    matches=[row for row in records if str(row.get('ticker','')).upper()==ticker.upper()
        and str(row.get('exchange','')).upper()==exchange.upper()]
    if len(matches)!=1:raise EdgarError('SEC_ISSUER_MAPPING_AMBIGUOUS')
    row=matches[0];cik=_cik(row.get('cik'))
    if _cik(submissions.get('cik'))!=cik:raise EdgarError('SEC_CIK_MISMATCH')
    pairs={(str(t).upper(),str(e).upper()) for t,e in zip(submissions.get('tickers',()),submissions.get('exchanges',()))}
    if (ticker.upper(),exchange.upper()) not in pairs:raise EdgarError('SEC_SUBMISSION_IDENTITY_MISMATCH')
    issuer=str(row.get('name','')).strip()
    if not issuer or not str(submissions.get('name','')).strip():raise EdgarError('SEC_ISSUER_NAME_MISSING')
    return Issuer(ticker.upper(),cik,issuer,exchange.upper(),retrieved_at.astimezone(timezone.utc),
        'https://www.sec.gov/files/company_tickers_exchange.json')

def submission_rows(submissions):
    try:recent=submissions['filings']['recent'];keys=tuple(recent);lengths={len(recent[key]) for key in keys}
    except (KeyError,TypeError):raise EdgarError('SEC_SUBMISSIONS_INVALID') from None
    if len(lengths)!=1:raise EdgarError('SEC_SUBMISSIONS_INVALID')
    return tuple(dict(zip(keys,values)) for values in zip(*(recent[key] for key in keys)))

def next_regular_session(accepted_at,calendar):
    accepted=_utc(accepted_at);local=accepted.astimezone(NY)
    for offset in range(370):
        session=calendar.session(local.date()+timedelta(days=offset))
        if session and session.opens_at>accepted:return session.opens_at.astimezone(timezone.utc)
    raise EdgarError('SEC_SESSION_CALENDAR_EXHAUSTED')

def _filings(submissions,calendar,as_of):
    rows=[]
    for raw in submission_rows(submissions):
        form=str(raw.get('form',''))
        if form not in FORMS and not form.endswith('/A'):continue
        accepted=_utc(raw.get('acceptanceDateTime'));available=next_regular_session(accepted,calendar)
        if available>as_of.astimezone(timezone.utc):continue
        rows.append({**raw,'accepted_at':accepted,'available_at':available,
            'report_date':_day(raw.get('reportDate'),'SEC_REPORT_DATE_INVALID')})
    return rows

def _fact_records(companyfacts,taxonomy,tag,unit,filings):
    concept=companyfacts.get('facts',{}).get(taxonomy,{}).get(tag)
    if concept is None:return ()
    units=concept.get('units')
    if not isinstance(units,dict):raise EdgarError('SEC_FACT_UNITS_INVALID')
    filing_by_acc={x['accessionNumber']:x for x in filings};result=[]
    for raw_unit,rows in units.items():
        if raw_unit!=unit and rows:raise EdgarError('SEC_FACT_UNIT_AMBIGUOUS')
        if raw_unit!=unit:continue
        for raw in rows:
            filing=filing_by_acc.get(raw.get('accn'))
            if filing is None:continue
            form=str(raw.get('form',''))
            if form.endswith('/A') or form not in FORMS:raise EdgarError('SEC_AMENDMENT_UNSUPPORTED')
            if form!=filing['form']:raise EdgarError('SEC_FACT_SUBMISSION_MISMATCH')
            try:value=Decimal(str(raw['val']))
            except (KeyError,InvalidOperation):raise EdgarError('SEC_FACT_VALUE_INVALID') from None
            if not value.is_finite():raise EdgarError('SEC_FACT_VALUE_INVALID')
            filed=_day(raw.get('filed'))
            if filed!=_day(filing.get('filingDate')):raise EdgarError('SEC_FACT_SUBMISSION_MISMATCH')
            result.append((raw,filing,value,_day(raw['start']) if raw.get('start') else None,
                _day(raw.get('end')),filed))
    return tuple(result)

def _choose_tag(companyfacts,metric,filings,target):
    unit,tags=METRICS[metric];choices=[]
    for taxonomy in ('us-gaap','dei'):
        for tag in tags:
            records=[row for row in _fact_records(companyfacts,taxonomy,tag,unit,filings)
                if row[0].get('accn')==target['accessionNumber'] and row[4]==target['report_date']]
            if records:choices.append((taxonomy,tag,records))
    if len(choices)>1:raise EdgarError('SEC_FACT_TAG_AMBIGUOUS')
    if not choices:raise EdgarError('SEC_REQUIRED_FACT_MISSING:'+metric.upper())
    return unit,choices[0]

def _period_match(metric,fp,start,end):
    if metric not in DURATION:return start is None
    if start is None:return False
    days=(end-start).days+1
    if fp in ('Q1','Q2','Q3') and metric!='operating_cash_flow':return 60<=days<=120
    if fp=='Q1':return 60<=days<=120
    if fp=='Q2':return 150<=days<=210
    if fp=='Q3':return 240<=days<=300
    if fp=='FY':return 300<=days<=400
    return False

def _component(taxonomy,tag,unit,row):
    raw,filing,value,start,end,filed=row
    return FactComponent(taxonomy,tag,unit,value,str(raw['accn']),str(raw['form']),int(raw['fy']),str(raw['fp']),
        start,end,filed,filing['accepted_at'],filing['available_at'],
        {'company_fact':dict(raw),'submission':{k:v for k,v in filing.items() if k not in ('accepted_at','available_at')}})

def _select_metric(companyfacts,metric,filings,target,retrieved_at):
    unit,(taxonomy,tag,records)=_choose_tag(companyfacts,metric,filings,target)
    fp=str(records[0][0].get('fp'));fy=records[0][0].get('fy')
    if any(str(row[0].get('fp'))!=fp or row[0].get('fy')!=fy for row in records):raise EdgarError('SEC_FACT_PERIOD_AMBIGUOUS')
    candidates=[row for row in records if _period_match(metric,fp,row[3],row[4])]
    unique={(row[2],row[3],row[4]) for row in candidates}
    if len(unique)!=1:raise EdgarError('SEC_FACT_PERIOD_AMBIGUOUS')
    current=next(row for row in candidates if (row[2],row[3],row[4]) in unique)
    components=[_component(taxonomy,tag,unit,current)];value=current[2];start=current[3]
    if metric=='operating_cash_flow' and fp in ('Q2','Q3'):
        prior_fp='Q1' if fp=='Q2' else 'Q2';prior_rows=[]
        for row in _fact_records(companyfacts,taxonomy,tag,unit,filings):
            raw=row[0]
            if raw.get('fy')==fy and str(raw.get('fp'))==prior_fp and row[3]==start and row[4]<current[4]:prior_rows.append(row)
        prior_unique={(row[2],row[3],row[4]) for row in prior_rows}
        if len(prior_unique)!=1:raise EdgarError('SEC_CUMULATIVE_CASH_FLOW_AMBIGUOUS')
        prior=next(row for row in prior_rows if (row[2],row[3],row[4]) in prior_unique)
        value-=prior[2];components.append(_component(taxonomy,tag,unit,prior));start=prior[4]+timedelta(days=1)
    component=components[0]
    return FiledFact(metric,value,unit,component.accession,component.form,component.fiscal_year,component.fiscal_period,
        start,component.end,component.filed,component.accepted_at,component.available_at,retrieved_at,
        f'https://data.sec.gov/api/xbrl/companyfacts/CIK{_cik(companyfacts.get("cik"))}.json',taxonomy,tag,
        tuple(components),{'derivation':'current cumulative less prior cumulative' if len(components)==2 else 'reported fact'})

def select_snapshot(ticker,exchange,tickers,submissions,companyfacts,*,as_of,retrieved_at,calendar,
        maximum_fact_age=timedelta(days=200)):
    if as_of.tzinfo is None or retrieved_at.tzinfo is None:raise EdgarError('SEC_TIMEZONE_REQUIRED')
    issuer=resolve_issuer(ticker,exchange,tickers,submissions,retrieved_at=retrieved_at)
    if _cik(companyfacts.get('cik'))!=issuer.cik:raise EdgarError('SEC_CIK_MISMATCH')
    if set(companyfacts.get('facts',{}))-{'us-gaap','dei'}:raise EdgarError('SEC_CUSTOM_TAXONOMY_UNSUPPORTED')
    filings=_filings(submissions,calendar,as_of);eligible=[row for row in filings if row['form'] in FORMS]
    if not eligible:raise EdgarError('SEC_NO_POINT_IN_TIME_FILING')
    target=max(eligible,key=lambda row:(row['report_date'],row['accepted_at']))
    if any(row['form'].endswith('/A') and row['report_date']==target['report_date'] for row in filings):
        raise EdgarError('SEC_AMENDMENT_UNSUPPORTED')
    if as_of.astimezone(timezone.utc)-target['available_at']>maximum_fact_age:raise EdgarError('SEC_FACTS_STALE')
    facts={metric:_select_metric(companyfacts,metric,filings,target,retrieved_at.astimezone(timezone.utc)) for metric in METRICS}
    if any(fact.accession!=target['accessionNumber'] for fact in facts.values()):raise EdgarError('SEC_FACT_ACCESSION_MISMATCH')
    first=next(iter(facts.values()))
    identity={(fact.form,fact.fiscal_year,fact.fiscal_period,fact.end) for fact in facts.values()}
    if identity!={(target['form'],first.fiscal_year,first.fiscal_period,target['report_date'])}:
        raise EdgarError('SEC_FACT_PERIOD_MISMATCH')
    return EdgarSnapshot(issuer,as_of.astimezone(timezone.utc),target['accessionNumber'],target['form'],
        first.fiscal_year,first.fiscal_period,target['report_date'],facts)
