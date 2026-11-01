"""Deterministic session features computed in code, so Claude reads compact
facts instead of raw bars (fewer tokens, no arithmetic for the model to get wrong).

Inputs are broker bars only (1-minute bars for the current regular session,
daily bars for completed prior sessions). Every value is descriptive evidence;
nothing here is a signal, filter or order authority. Missing inputs produce
null fields, never a default that could read as favorable.
"""
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_EVEN
from math import sqrt

BPS = Decimal(10000)


def _q(value, places='0.0001'):
    return None if value is None else str(Decimal(value).quantize(Decimal(places), rounding=ROUND_HALF_EVEN))


def _bps(value):
    return None if value is None else int((Decimal(value) * BPS).to_integral_value(rounding=ROUND_HALF_EVEN))


def _change(new, old):
    return None if old is None or new is None or old == 0 else Decimal(new) / Decimal(old) - 1


def session_features(bars, now, session_open):
    """bars: AlphaBar-like objects (at, open_usd, high_usd, low_usd, close_usd, volume), any order."""
    bars = sorted((b for b in bars if session_open <= b.at <= now), key=lambda b: b.at)
    minutes = max(0, int((now - session_open).total_seconds() // 60))
    if not bars:
        return {'minutes_since_open': minutes, 'bars': 0}
    o = Decimal(bars[0].open_usd)
    hi = max(Decimal(b.high_usd) for b in bars)
    lo = min(Decimal(b.low_usd) for b in bars)
    last = Decimal(bars[-1].close_usd)
    vol = sum(Decimal(b.volume) for b in bars)
    vwap = (sum((Decimal(b.high_usd) + Decimal(b.low_usd) + Decimal(b.close_usd)) / 3 * Decimal(b.volume)
                for b in bars) / vol) if vol > 0 else None

    def close_ago(n):
        return Decimal(bars[-1 - n].close_usd) if len(bars) > n else None

    recent = bars[-15:]
    ranges = [(Decimal(b.high_usd) - Decimal(b.low_usd)) / Decimal(b.close_usd) for b in recent]
    returns = [float(Decimal(b.close_usd) / Decimal(a.close_usd) - 1) for a, b in zip(recent, recent[1:])]
    sigma = None
    if len(returns) >= 5:
        mean = sum(returns) / len(returns)
        sigma = Decimal(str(sqrt(sum((r - mean) ** 2 for r in returns) / (len(returns) - 1))))
    opening = [b for b in bars if (b.at - session_open).total_seconds() < 15 * 60]
    opening_range = None
    if minutes >= 15 and opening:
        orh = max(Decimal(b.high_usd) for b in opening)
        orl = min(Decimal(b.low_usd) for b in opening)
        opening_range = {'high': _q(orh), 'low': _q(orl),
                         'last_vs_range': 'ABOVE' if last > orh else 'BELOW' if last < orl else 'INSIDE'}
    high_bar = max(bars, key=lambda b: (Decimal(b.high_usd), b.at))
    last5 = bars[-5:]
    pace = None
    if len(bars) >= 10 and vol > 0:
        pace = (sum(Decimal(b.volume) for b in last5) / len(last5)) / (vol / len(bars))
    return {
        'minutes_since_open': minutes,
        'bars': len(bars),
        'open': _q(o), 'high': _q(hi), 'low': _q(lo), 'last': _q(last),
        'vwap': _q(vwap),
        'last_vs_vwap_bps': _bps(_change(last, vwap)),
        'change_from_open_bps': _bps(_change(last, o)),
        'change_5m_bps': _bps(_change(last, close_ago(5))),
        'change_15m_bps': _bps(_change(last, close_ago(15))),
        'change_30m_bps': _bps(_change(last, close_ago(30))),
        'day_range_position': _q((last - lo) / (hi - lo), '0.01') if hi > lo else None,
        'minutes_since_day_high': int((bars[-1].at - high_bar.at).total_seconds() // 60),
        'opening_range_15m': opening_range,
        'avg_1m_range_bps_15': _bps(sum(ranges) / len(ranges)) if ranges else None,
        'stdev_1m_return_bps_15': _bps(sigma) if sigma is not None else None,
        'session_volume': str(vol),
        'volume_pace_5m_vs_session': _q(pace, '0.01'),
        # Fewer bars than minutes means minutes without trades: thin liquidity.
        'bar_coverage': _q(Decimal(len(bars)) / Decimal(minutes), '0.01') if minutes else None,
    }


def daily_features(rows, today, session_volume=None, session_open_price=None, last=None):
    """rows: broker daily bar dicts (time, open, high, low, close, volume); only sessions before today count."""
    bars = []
    for row in rows or ():
        try:
            label = date.fromisoformat(str(row['time'])[:10])
            values = tuple(Decimal(str(row[k])) for k in ('open', 'high', 'low', 'close', 'volume'))
        except (KeyError, ValueError, ArithmeticError):
            continue
        if label < today and values[3] > 0:
            bars.append((label, *values))
    bars.sort()
    if not bars:
        return {'prior_sessions': 0}
    prior = bars[-1]
    prior_close = prior[4]
    trs = []
    for previous, current in zip(bars, bars[1:]):
        trs.append(max(current[2] - current[3], abs(current[2] - previous[4]), abs(current[3] - previous[4])))
    atr14 = sum(trs[-14:]) / len(trs[-14:]) if len(trs) >= 14 else None
    window = bars[-20:]
    avg_volume = sum(b[5] for b in window) / len(window)
    avg_dollar = sum(b[5] * b[4] for b in window) / len(window)
    hi20, lo20 = max(b[2] for b in window), min(b[3] for b in window)

    def ret(n):
        return _bps(_change(prior_close, bars[-1 - n][4])) if len(bars) > n else None

    return {
        'prior_sessions': len(bars),
        'prior_close': _q(prior_close), 'prior_high': _q(prior[2]), 'prior_low': _q(prior[3]),
        'gap_bps': _bps(_change(session_open_price, prior_close)),
        'change_vs_prior_close_bps': _bps(_change(last, prior_close)),
        'atr14_bps': _bps(atr14 / prior_close) if atr14 is not None else None,
        'avg_daily_volume_20': str(int(avg_volume)),
        'avg_daily_dollar_volume_20': str(int(avg_dollar)),
        'session_volume_vs_avg_daily': (_q(Decimal(session_volume) / avg_volume, '0.01')
                                        if session_volume is not None and avg_volume > 0 else None),
        'return_5d_bps': ret(5), 'return_20d_bps': ret(20) if len(bars) > 20 else None,
        'high_20d': _q(hi20), 'low_20d': _q(lo20),
        'last_vs_prior_high': (None if last is None else
                               'ABOVE' if Decimal(last) > prior[2] else 'BELOW_PRIOR_LOW' if Decimal(last) < prior[3] else 'INSIDE'),
    }


def instrument_features(bars, daily_rows, now, session_open):
    session = session_features(bars, now, session_open)
    daily = daily_features(daily_rows, session_open.date() if isinstance(session_open, datetime) else session_open,
                           session_volume=session.get('session_volume'), session_open_price=session.get('open'),
                           last=session.get('last'))
    return {'session': session, 'daily': daily}
