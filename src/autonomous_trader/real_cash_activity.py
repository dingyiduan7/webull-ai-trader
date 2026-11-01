"""Sanitize bounded REAL cash-activity evidence; never grants trading authority."""
from datetime import date,datetime,timezone
from decimal import Decimal
from hashlib import sha256

from .numeric import decimal


def _one(row,names,code):
    found=[row[name] for name in names if name in row and row[name] not in (None,'')]
    if len(found)!=1:raise ValueError(code)
    return found[0]


def _time(value):
    try:
        if isinstance(value,int) or isinstance(value,str) and value.isdigit():
            result=datetime.fromtimestamp(int(value)/1000,timezone.utc)
        else:result=datetime.fromisoformat(str(value).replace('Z','+00:00'))
    except (ValueError,TypeError,OverflowError):raise ValueError('REAL_CASH_ACTIVITY_TIME') from None
    if result.tzinfo is None:raise ValueError('REAL_CASH_ACTIVITY_TIME')
    return result.astimezone(timezone.utc)


def sanitize_cash_activities(rows,start_at,end_at,account_id):
    if (not isinstance(rows,list) or start_at.tzinfo is None or end_at.tzinfo is None or start_at>end_at
            or not isinstance(account_id,str) or not account_id):
        raise ValueError('REAL_CASH_ACTIVITY_SHAPE')
    result=[];references=set();net=Decimal(0)
    for row in rows:
        if not isinstance(row,dict):raise ValueError('REAL_CASH_ACTIVITY_SHAPE')
        if row.get('account_id')!=account_id:raise ValueError('REAL_CASH_ACTIVITY_ACCOUNT')
        currency=_one(row,('currency',),'REAL_CASH_ACTIVITY_CURRENCY')
        if currency!='USD':raise ValueError('REAL_CASH_ACTIVITY_CURRENCY')
        amount=decimal(_one(row,('net_amount',),'REAL_CASH_ACTIVITY_AMOUNT'))
        at=_time(_one(row,('biz_time',),'REAL_CASH_ACTIVITY_TIME'))
        if not start_at<=at<=end_at:raise ValueError('REAL_CASH_ACTIVITY_TIME')
        kind=_one(row,('activity_type',),'REAL_CASH_ACTIVITY_TYPE')
        subtype=_one(row,('activity_sub_type',),'REAL_CASH_ACTIVITY_TYPE')
        reference=str(_one(row,('id',),'REAL_CASH_ACTIVITY_REFERENCE'))
        status=row.get('status',row.get('activity_status'))
        try:trade_date=date.fromisoformat(str(row['trade_date']))
        except (KeyError,TypeError,ValueError):raise ValueError('REAL_CASH_ACTIVITY_DATE') from None
        if (not isinstance(kind,str) or not 1<=len(kind)<=128 or not isinstance(subtype,str)
                or not 1<=len(subtype)<=128 or not 1<=len(reference)<=256
                or status is not None and (not isinstance(status,str) or not 1<=len(status)<=128)):
            raise ValueError('REAL_CASH_ACTIVITY_SHAPE')
        reference_hash=sha256(reference.encode()).hexdigest()
        if reference_hash in references:raise ValueError('REAL_CASH_ACTIVITY_DUPLICATE')
        references.add(reference_hash);net+=amount
        result.append({'at':at.isoformat(),'trade_date':trade_date.isoformat(),'currency':'USD',
            'activity_type':kind,'activity_sub_type':subtype,'status':status,
            'amount_usd':str(amount),'reference_sha256':reference_hash})
    return tuple(result),net
