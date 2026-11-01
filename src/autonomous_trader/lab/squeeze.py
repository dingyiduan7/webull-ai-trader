"""Short-squeeze research (owner request 2026-10-21; paper only). Tests whether crowded shorts that break out
on heavy volume behave differently from ordinary breakouts, using free public FINRA data:

- short volume ratio: Reg SHO daily short volume / total volume (published each evening), 20-session average;
- short interest days-to-cover: twice-monthly settlement data, treated as known only AVAILABLE_LAG days after
  the settlement date (FINRA publishes it about a week later), so there is no look-ahead.

Variants (all decide at day i's close, enter at the next open, same exits):
  breakout_control      price/volume breakout, no short data (does crowding add anything?)
  squeeze_short_volume  breakout + 20-day short-volume ratio in the top decile of the universe that day
  squeeze_days_to_cover breakout + latest known days-to-cover >= 5
Exits: stop 2 ATR below entry; a close below the prior 5-session low; or 15 sessions.
"""
from bisect import bisect_right
from datetime import date, timedelta

from .backtest import cost_per_side, Trade
from .indicators import atr, highest, sma
from .strategies import _liquid

AVAILABLE_LAG = 10            # calendar days from settlement until short interest is public
BREAKOUT_DAYS = 20
VOLUME_MULTIPLE = 2.0
DAY_GAIN = 0.03
STOP_ATR = 2.0
MAX_DAYS = 15
DTC_MIN = 5.0


def breakout(bars, i):
    if i < 21:
        return False
    c = [b['close'] for b in bars[i - 20:i + 1]]
    v = [b['volume'] for b in bars[i - 20:i]]
    avg_v = sum(v) / len(v)
    return (c[-1] >= max(c) and c[-1] >= c[-2] * (1 + DAY_GAIN) and avg_v > 0
            and bars[i]['volume'] >= VOLUME_MULTIPLE * avg_v)


def short_ratio_series(rows):
    """rows [(date, short, total)] -> {date: 20-row average short/total ratio}."""
    rows = sorted(rows)
    out, window = {}, []
    for d, s, t in rows:
        window.append(s / t if t else None)
        window = window[-20:]
        vals = [x for x in window if x is not None]
        if len(vals) >= 15:
            out[d] = sum(vals) / len(vals)
    return out


def top_decile_by_date(ratio_by_symbol, decile=0.10):
    by_date = {}
    for symbol, series in ratio_by_symbol.items():
        for d, r in series.items():
            by_date.setdefault(d, []).append((r, symbol))
    out = {}
    for d, rows in by_date.items():
        if len(rows) < 30:
            continue
        rows.sort(reverse=True)
        out[d] = frozenset(s for _, s in rows[:max(1, int(len(rows) * decile))])
    return out


def days_to_cover_known(si_rows, day):
    """Latest days-to-cover whose settlement is at least AVAILABLE_LAG days before `day` (ISO), else None."""
    if not si_rows:
        return None
    cutoff = (date.fromisoformat(day) - timedelta(days=AVAILABLE_LAG)).isoformat()
    dates = [r[0] for r in si_rows]
    k = bisect_right(dates, cutoff) - 1
    return None if k < 0 else si_rows[k][3]


def run(symbol, bars, entry_ok, variant):
    """Trades for one symbol: entry_ok(i) adds the crowding condition to the breakout."""
    trades, i, n = [], 0, len(bars)
    while i < n - 1:
        if not (breakout(bars, i) and _liquid(dict(bars=bars), i, 5e6) and entry_ok(i)):
            i += 1
            continue
        a = atr(bars, 14, i)
        if not a:
            i += 1
            continue
        k = i + 1
        entry = bars[k]['open']
        stop = entry - STOP_ATR * a
        hi, lo, j, exit_price, reason = entry, entry, k, None, None
        last = min(n - 1, k + MAX_DAYS - 1)
        while j <= last:
            b = bars[j]
            hi, lo = max(hi, b['high']), min(lo, b['low'])
            if b['low'] <= stop:
                exit_price, reason = (b['open'] if b['open'] <= stop else stop), 'STOP'
                break
            if j - 1 >= k and b['close'] < min(x['low'] for x in bars[max(0, j - 5):j]):
                exit_price, reason = b['close'], 'SIGNAL'
                break
            if j == last:
                exit_price, reason = b['close'], 'TIME' if j == k + MAX_DAYS - 1 else 'END_OF_DATA'
                break
            j += 1
        if reason == 'END_OF_DATA':
            break
        trades.append(Trade(symbol, variant, bars[k]['date'], entry, bars[j]['date'], exit_price, j - k + 1, reason,
                            hi / entry - 1, lo / entry - 1, cost_per_side(bars, i)))
        i = j + 1
    return trades


def run_all(series, short_volume=None, short_interest=None):
    """{variant: [Trade]} over the universe. Variants without their data are skipped (not faked)."""
    out = {'breakout_control': []}
    ratios = {s: short_ratio_series(r) for s, r in (short_volume or {}).items()}
    crowded = top_decile_by_date(ratios) if ratios else {}
    if crowded:
        out['squeeze_short_volume'] = []
    if short_interest:
        out['squeeze_days_to_cover'] = []
    for symbol, bars in series.items():
        out['breakout_control'] += run(symbol, bars, lambda i: True, 'breakout_control')
        if crowded:
            out['squeeze_short_volume'] += run(symbol, bars, lambda i, s=symbol, b=bars: s in crowded.get(b[i]['date'], ()),
                                               'squeeze_short_volume')
        if short_interest:
            rows = sorted(tuple(r) for r in short_interest.get(symbol, []))
            out['squeeze_days_to_cover'] += run(
                symbol, bars, lambda i, r=rows, b=bars: (days_to_cover_known(r, b[i]['date']) or 0) >= DTC_MIN,
                'squeeze_days_to_cover')
    return out


def squeeze_profile(trades):
    """How often a trade saw a real squeeze-sized run (max gain within the hold) vs a bleed."""
    if not trades:
        return dict(trades=0)
    n = len(trades)
    return dict(trades=n, ran_25=sum(t.mfe >= 0.25 for t in trades) / n, ran_50=sum(t.mfe >= 0.50 for t in trades) / n,
                ran_100=sum(t.mfe >= 1.0 for t in trades) / n, fell_15=sum(t.mae <= -0.15 for t in trades) / n,
                best=max(t.ret for t in trades), worst=min(t.ret for t in trades))
