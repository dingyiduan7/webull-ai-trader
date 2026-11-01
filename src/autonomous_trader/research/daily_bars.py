"""Offline normalization of sandbox-certified Webull D/250 batch bars.

Daily history is research evidence only. It never supplies an executable mark.
"""
from dataclasses import dataclass
from datetime import date,datetime,timedelta,timezone
from decimal import Decimal,InvalidOperation
from hashlib import sha256
import json

from ..contracts import DailyMetricObservation,DailyResearchContext,utc

SOURCE_ID='WEBULL_SANDBOX_BATCH_DAILY_D250'
SOURCE_PATH='/market-data/stocks/bars/list'
METHODOLOGY=('returns=latest_close/prior_close-1; median_dollar_volume=median(adjusted_close*volume); '
    'atr_20_ratio=mean_20(max(high-low,abs(high-prior_close),abs(low-prior_close)))/latest_close; '
    'distance_sma=latest_close/mean_close-1; relative_strength=candidate_return-SPY_return_on_identical_sessions')
METRIC_SPECS=(
    ('return_1_session','RATIO',1,2),('return_5_session','RATIO',5,6),
    ('return_20_session','RATIO',20,21),('return_60_session','RATIO',60,61),
    ('median_dollar_volume_20','USD_DOLLAR_VOLUME',20,20),
    ('atr_20_ratio','RATIO',20,21),('distance_sma_20','RATIO',20,20),
    ('distance_sma_50','RATIO',50,50),('high_20','USD',20,20),('low_20','USD',20,20),
    ('relative_strength_spy_1','RATIO',1,2),('relative_strength_spy_5','RATIO',5,6),
    ('relative_strength_spy_20','RATIO',20,21),('relative_strength_spy_60','RATIO',60,61))


class DailyBarError(ValueError):pass


@dataclass(frozen=True)
class DailyBar:
    session:date
    source_at:datetime
    open:Decimal
    high:Decimal
    low:Decimal
    close:Decimal
    volume:Decimal
    raw:dict


@dataclass(frozen=True)
class DailySeries:
    instrument_id:str
    symbol:str
    received_at:datetime
    as_of:datetime
    bars:tuple[DailyBar,...]
    excluded_count:int
    payload_hash:str


def _decimal(value):
    try:result=Decimal(str(value))
    except (InvalidOperation,TypeError,ValueError):raise DailyBarError('DAILY_BAR_NUMERIC_INVALID') from None
    if not result.is_finite():raise DailyBarError('DAILY_BAR_NUMERIC_INVALID')
    return result


def _source_time(value):
    try:return utc(value)
    except (TypeError,ValueError):raise DailyBarError('DAILY_BAR_TIME_INVALID') from None


def _payload_hash(value):
    return sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()


def _latest_completed_session(calendar,as_of):
    local=as_of.astimezone(__import__('zoneinfo').ZoneInfo('America/New_York'))
    for offset in range(370):
        label=local.date()-timedelta(days=offset)
        try:session=calendar.session(label)
        except Exception:raise DailyBarError('DAILY_CALENDAR_COVERAGE_REQUIRED') from None
        if session is not None and session.closes_at<=as_of:return label
    raise DailyBarError('DAILY_CALENDAR_COVERAGE_REQUIRED')


def normalize_daily_batch(body,*,received_at,as_of,calendar,adjustment_basis):
    """Normalize one bounded response without network access or price authority."""
    received_at=utc(received_at);as_of=utc(as_of)
    if received_at>as_of:raise DailyBarError('DAILY_RECEIPT_AFTER_AS_OF')
    if adjustment_basis!='FORWARD_ADJUSTED':raise DailyBarError('DAILY_ADJUSTMENT_BASIS_REQUIRED')
    try:rows=body['result']
    except (KeyError,TypeError):raise DailyBarError('DAILY_BATCH_SHAPE_INVALID') from None
    if not isinstance(rows,list) or not 1<=len(rows)<=10:raise DailyBarError('DAILY_BATCH_SHAPE_INVALID')
    result={}
    for outer in rows:
        if not isinstance(outer,dict):raise DailyBarError('DAILY_BATCH_SHAPE_INVALID')
        symbol=outer.get('symbol');instrument_id=str(outer.get('instrument_id',''))
        raw_bars=outer.get('result')
        if not isinstance(symbol,str) or not symbol or not instrument_id or not isinstance(raw_bars,list) or len(raw_bars)>250:
            raise DailyBarError('DAILY_BATCH_SHAPE_INVALID')
        if symbol in result:raise DailyBarError('DAILY_SYMBOL_DUPLICATE')
        bars=[];excluded=0;seen=set()
        for raw in raw_bars:
            if not isinstance(raw,dict):raise DailyBarError('DAILY_BAR_SHAPE_INVALID')
            try:
                source_at=_source_time(raw['time']);label=date.fromisoformat(str(raw['time'])[:10])
            except (KeyError,ValueError):raise DailyBarError('DAILY_BAR_TIME_INVALID') from None
            try:session=calendar.session(label)
            except Exception:raise DailyBarError('DAILY_CALENDAR_COVERAGE_REQUIRED') from None
            if session is None:raise DailyBarError('DAILY_BAR_NON_SESSION_DATE')
            if raw.get('trading_session') not in (None,'RTH'):raise DailyBarError('DAILY_BAR_SESSION_INVALID')
            if source_at>as_of or session.closes_at>as_of:
                excluded+=1;continue
            if label in seen:raise DailyBarError('DAILY_BAR_SESSION_DUPLICATE')
            seen.add(label)
            o,h,l,c,v=(_decimal(raw.get(k)) for k in ('open','high','low','close','volume'))
            if not (o>0 and c>0 and l>0 and h>=max(o,c) and l<=min(o,c) and v>=0):
                raise DailyBarError('DAILY_BAR_GEOMETRY_INVALID')
            bars.append(DailyBar(label,source_at,o,h,l,c,v,dict(raw)))
        bars.sort(key=lambda x:x.session)
        result[symbol]=DailySeries(instrument_id,symbol,received_at,as_of,tuple(bars),excluded,_payload_hash(outer))
    return result


def _expected_labels(calendar,end,count):
    labels=[];cursor=end
    while len(labels)<count:
        try:session=calendar.session(cursor)
        except Exception:return None
        if session is not None:labels.append(cursor)
        cursor-=timedelta(days=1)
    return tuple(reversed(labels))


def _window(series,calendar,count,latest_completed):
    if not series.bars or series.bars[-1].session!=latest_completed:return None,'HISTORY_NOT_CURRENT'
    expected=_expected_labels(calendar,latest_completed,count)
    if expected is None:return None,'CALENDAR_COVERAGE_INSUFFICIENT'
    actual=tuple(x.session for x in series.bars[-count:])
    if len(actual)<count:return None,'INSUFFICIENT_HISTORY'
    if actual!=expected:return None,'MISSING_COMPLETED_SESSION'
    return series.bars[-count:],None


def _median(values):
    ordered=sorted(values);middle=len(ordered)//2
    return ordered[middle] if len(ordered)%2 else (ordered[middle-1]+ordered[middle])/2


def _metric(series,calendar,latest_completed,name,unit,window_sessions,required,spy=None):
    bars,reason=_window(series,calendar,required,latest_completed)
    value=None;start=end=None
    if reason is None:
        start,end=bars[0].session,bars[-1].session
        if name.startswith('return_'):value=bars[-1].close/bars[0].close-1
        elif name=='median_dollar_volume_20':value=_median([x.close*x.volume for x in bars])
        elif name=='atr_20_ratio':
            ranges=[max(current.high-current.low,abs(current.high-prior.close),abs(current.low-prior.close))
                for prior,current in zip(bars,bars[1:])]
            value=(sum(ranges,Decimal(0))/Decimal(20))/bars[-1].close
        elif name.startswith('distance_sma_'):
            value=bars[-1].close/(sum((x.close for x in bars),Decimal(0))/Decimal(len(bars)))-1
        elif name=='high_20':value=max(x.high for x in bars)
        elif name=='low_20':value=min(x.low for x in bars)
        elif name.startswith('relative_strength_spy_'):
            spy_bars,spy_reason=_window(spy,calendar,required,latest_completed) if spy else (None,'SPY_HISTORY_MISSING')
            if spy_reason is not None:reason=spy_reason
            elif tuple(x.session for x in bars)!=tuple(x.session for x in spy_bars):reason='SPY_SESSION_MISMATCH'
            else:value=(bars[-1].close/bars[0].close-1)-(spy_bars[-1].close/spy_bars[0].close-1)
    if value is not None:value=value.quantize(Decimal('.00000001'))
    return DailyMetricObservation(metric=name,value=value,unit=unit,window_sessions=window_sessions,
        required_bar_count=required,observed_bar_count=len(series.bars),start_session=start if reason is None else None,
        end_session=end if reason is None else None,source_id=SOURCE_ID,
        source_at=series.bars[-1].source_at if series.bars else None,received_at=series.received_at,as_of=series.as_of,
        unavailable_reason=reason)


def build_daily_research(series_by_symbol,*,calendar):
    """Return instrument-keyed, typed context for a bounded shortlist plus SPY."""
    spy=series_by_symbol.get('SPY');result={}
    for symbol,series in series_by_symbol.items():
        latest=_latest_completed_session(calendar,series.as_of)
        metrics=tuple(_metric(series,calendar,latest,*spec,spy=spy) for spec in METRIC_SPECS)
        result[series.instrument_id]=DailyResearchContext(instrument_id=series.instrument_id,symbol=symbol,
            adjustment_basis='FORWARD_ADJUSTED',source_id=SOURCE_ID,source_path=SOURCE_PATH,
            received_at=series.received_at,as_of=series.as_of,completed_bar_count=len(series.bars),
            excluded_incomplete_or_future_bars=series.excluded_count,source_payload_sha256=series.payload_hash,
            methodology=METHODOLOGY,metrics=metrics)
    return result
