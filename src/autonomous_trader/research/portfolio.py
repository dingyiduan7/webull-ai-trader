"""Offline research evidence assembly with no execution authority."""
from dataclasses import dataclass
from datetime import datetime,timezone
from decimal import Decimal
import json

from ..contracts import DailyResearchContext
from .daily_bars import DailyBarError,build_daily_research,normalize_daily_batch
from .sec_edgar import EdgarSnapshot,METRICS


class PortfolioResearchError(ValueError):pass


@dataclass(frozen=True)
class PortfolioResearchContext:
    daily:DailyResearchContext|None
    financial_context:str


def _number(value):
    text=format(Decimal(value).quantize(Decimal('.00000001')),'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


class OfflinePortfolioResearch:
    """Preloaded adapter outputs only; this class never performs network I/O."""
    def __init__(self,daily_by_instrument=None,sec_by_instrument=None,gaps_by_instrument=None):
        self.daily=dict(daily_by_instrument or {});self.sec=dict(sec_by_instrument or {})
        self.gaps={key:tuple(value) for key,value in (gaps_by_instrument or {}).items()}

    def context_for(self,observation,cutoff):
        if cutoff.tzinfo is None:raise PortfolioResearchError('RESEARCH_CUTOFF_TIMEZONE_REQUIRED')
        cutoff=cutoff.astimezone(timezone.utc)
        try:observation.require_fresh(cutoff)
        except ValueError as exc:raise PortfolioResearchError(str(exc)) from None
        instrument=observation.instrument
        if (instrument.country!='US' or instrument.asset_type!='STOCK'
                or instrument.ticker!=observation.ticker or not instrument.listing_exchange):
            raise PortfolioResearchError('RESEARCH_BROKER_IDENTITY_INVALID')

        gaps=list(self.gaps.get(observation.instrument_id,()));daily=self.daily.get(observation.instrument_id);daily_values={}
        if daily is None:gaps.append('DAILY_HISTORY_UNAVAILABLE')
        elif (daily.instrument_id!=observation.instrument_id or daily.symbol!=observation.ticker):
            gaps.append('DAILY_HISTORY_IDENTITY_MISMATCH');daily=None
        elif daily.as_of>cutoff or daily.received_at>cutoff:
            gaps.append('DAILY_HISTORY_AFTER_CUTOFF');daily=None
        elif any(metric.as_of>cutoff or metric.received_at>cutoff
                or (metric.source_at is not None and metric.source_at>cutoff) for metric in daily.metrics):
            gaps.append('DAILY_HISTORY_AFTER_CUTOFF');daily=None
        elif any(metric.value is None for metric in daily.metrics):
            unavailable={}
            for metric in daily.metrics:
                if metric.value is None:unavailable.setdefault(str(metric.unavailable_reason),[]).append(metric.metric)
            gaps.extend('DAILY_METRICS_UNAVAILABLE:'+reason+':'+','.join(sorted(metrics))
                for reason,metrics in sorted(unavailable.items()))
            daily_values={metric.metric:metric.value for metric in daily.metrics if metric.value is not None}
        else:daily_values={metric.metric:metric.value for metric in daily.metrics}

        snapshot=self.sec.get(observation.instrument_id);facts=[];derived=[];filing=None;identity=None;sources={}
        if snapshot is None:gaps.append('SEC_FINANCIALS_UNAVAILABLE')
        else:
            issuer=snapshot.issuer
            if (issuer.ticker!=observation.ticker
                    or issuer.exchange.upper()!=instrument.listing_exchange.upper()):
                gaps.append('SEC_IDENTITY_MISMATCH');snapshot=None
            elif snapshot.as_of>cutoff or issuer.retrieved_at>cutoff:
                gaps.append('SEC_EVIDENCE_AFTER_CUTOFF');snapshot=None
        if snapshot is not None:
            issuer=snapshot.issuer;identity={'ticker':issuer.ticker,'cik':issuer.cik,
                'issuer':issuer.issuer,'exchange':issuer.exchange,'security':'US_LISTED_COMMON_SHARE'}
            filing={'accession':snapshot.accession,'form':snapshot.form,'fiscal_year':snapshot.fiscal_year,
                'fiscal_period':snapshot.fiscal_period,'period_end':snapshot.period_end.isoformat(),
                'snapshot_as_of':snapshot.as_of.isoformat(),'retrieved_at':issuer.retrieved_at.isoformat(),
                'fact_available_at':sorted({fact.available_at.isoformat() for fact in snapshot.facts.values()}),
                'fact_source_ref':'companyfacts'}
            values={}
            for metric in METRICS:
                fact=snapshot.facts.get(metric)
                if fact is None:
                    gaps.append('SEC_'+metric.upper()+'_UNAVAILABLE');continue
                if (fact.available_at>cutoff or fact.retrieved_at>cutoff
                        or fact.accepted_at>fact.available_at):
                    gaps.append('SEC_'+metric.upper()+'_AFTER_CUTOFF');continue
                if (fact.accession!=snapshot.accession or fact.form!=snapshot.form
                        or fact.fiscal_year!=snapshot.fiscal_year or fact.fiscal_period!=snapshot.fiscal_period
                        or fact.end!=snapshot.period_end):
                    gaps.append('SEC_'+metric.upper()+'_PERIOD_MISMATCH');continue
                values[metric]=fact.value
                facts.append({'metric':metric,'value':str(fact.value),'unit':fact.unit,'tag':fact.tag})
                sources['companyfacts']=fact.source_url
            required=set(METRICS)
            if required<=set(values):
                debt=values['debt_current']+values['debt_noncurrent']
                if values['revenue']!=0:
                    derived.extend((
                        {'metric':'operating_margin','value':_number(values['operating_income']/values['revenue']),
                            'definition':'operating_income/revenue'},
                        {'metric':'operating_cash_flow_margin','value':_number(values['operating_cash_flow']/values['revenue']),
                            'definition':'operating_cash_flow/revenue'}))
                else:gaps.append('DERIVED_MARGIN_REVENUE_ZERO')
                derived.extend((
                    {'metric':'total_debt_usd','value':_number(debt),'definition':'debt_current+debt_noncurrent'},
                    {'metric':'net_cash_after_debt_usd','value':_number(values['cash']-debt),'definition':'cash-total_debt'},
                    {'metric':'market_cap_at_quote_usd','value':_number(values['shares']*((observation.bid+observation.ask)/2)),
                        'definition':'reported_shares*quote_midpoint'}))
                if values['current_liabilities']!=0:
                    derived.append({'metric':'current_ratio','value':_number(values['current_assets']/values['current_liabilities']),
                        'definition':'current_assets/current_liabilities'})
                else:gaps.append('DERIVED_CURRENT_RATIO_DENOMINATOR_ZERO')

        derived.append({'metric':'quote_spread_bps','value':_number(observation.features()['spread_bps']),
            'definition':'(ask-bid)/bid*10000'})
        if daily_values.get('median_dollar_volume_20') is not None:
            derived.append({'metric':'median_dollar_volume_20','value':_number(daily_values['median_dollar_volume_20']),
                'definition':'median(adjusted_close*volume),20 completed sessions'})
        if observation.bid_size is None:gaps.append('BID_SIZE_UNAVAILABLE')
        else:derived.append({'metric':'bid_book_notional_usd','value':_number(observation.bid*observation.bid_size),'definition':'bid*bid_size'})
        if observation.ask_size is None:gaps.append('ASK_SIZE_UNAVAILABLE')
        else:derived.append({'metric':'ask_book_notional_usd','value':_number(observation.ask*observation.ask_size),'definition':'ask*ask_size'})
        payload={'evidence_type':'POINT_IN_TIME_FINANCIAL_AND_LIQUIDITY_CONTEXT','cutoff_at':cutoff.isoformat(),
            'identity':identity,'filing':filing,'facts':facts,'derived_observations':derived,
            'sources':sources,'explicit_gaps':sorted(set(gaps)),
            'authority':'EVIDENCE_ONLY_ASTRA_RETAINS_ALPHA_JUDGMENT'}
        encoded=json.dumps(payload,sort_keys=True,separators=(',',':'))
        if len(encoded)>2000:
            # Keep every raw value and causal timestamp; definitions and tags are secondary.
            for fact in facts:fact.pop('tag',None)
            for item in derived:item.pop('definition',None)
            encoded=json.dumps(payload,sort_keys=True,separators=(',',':'))
        if len(encoded)>2000:raise PortfolioResearchError('RESEARCH_CONTEXT_TOO_LARGE')
        return PortfolioResearchContext(daily,encoded)


class SessionPortfolioResearch:
    """Injected fixture/provider payloads cached for one trading session.

    ``daily_payload_loader`` and ``sec_snapshot_loader`` are supplied by the
    owner-controlled composition root. This class has no network client.
    """
    def __init__(self,daily_payload_loader,calendar,*,sec_snapshot_loader=None,
            sec_organization='',sec_contact=''):
        self.daily_payload_loader=daily_payload_loader;self.calendar=calendar
        self.sec_snapshot_loader=(sec_snapshot_loader
            if sec_organization.strip() and sec_contact.strip() else None)
        self.session=None;self.daily={};self.sec={};self.gaps={};self.completed=set()

    def prepare(self,observations,cutoff,session_label):
        observations=tuple(observations)
        if len(observations)>8:raise PortfolioResearchError('RESEARCH_SHORTLIST_UNBOUNDED')
        if self.session!=str(session_label):
            self.session=str(session_label);self.daily={};self.sec={};self.gaps={};self.completed=set()
        pending=[item for item in observations if item.instrument_id not in self.completed]
        if not pending:return
        symbols=tuple(dict.fromkeys([item.ticker for item in pending]+['SPY']))
        try:
            body,received_at=self.daily_payload_loader(symbols)
            if isinstance(body,list):body={'result':body}
            series=normalize_daily_batch(body,received_at=received_at,as_of=cutoff,calendar=self.calendar,
                adjustment_basis='FORWARD_ADJUSTED')
            research=build_daily_research(series,calendar=self.calendar)
            for item in pending:
                value=research.get(item.instrument_id)
                if value is None:self.gaps.setdefault(item.instrument_id,[]).append('DAILY_SHORTLIST_SYMBOL_UNAVAILABLE')
                else:self.daily[item.instrument_id]=value
        except (DailyBarError,ValueError) as exc:
            code=str(exc) if str(exc) else type(exc).__name__.upper()
            for item in pending:self.gaps.setdefault(item.instrument_id,[]).append('DAILY_RESEARCH_ERROR:'+code)
        for item in pending:
            if self.sec_snapshot_loader is None:
                self.gaps.setdefault(item.instrument_id,[]).append('SEC_USER_AGENT_NOT_CONFIGURED')
            else:
                try:
                    value=self.sec_snapshot_loader(item,cutoff)
                    if value is None:self.gaps.setdefault(item.instrument_id,[]).append('SEC_FINANCIALS_UNAVAILABLE')
                    else:self.sec[item.instrument_id]=value
                except ValueError as exc:self.gaps.setdefault(item.instrument_id,[]).append('SEC_RESEARCH_ERROR:'+str(exc))
            self.completed.add(item.instrument_id)

    def context_for(self,observation,cutoff):
        return OfflinePortfolioResearch(self.daily,self.sec,self.gaps).context_for(observation,cutoff)
