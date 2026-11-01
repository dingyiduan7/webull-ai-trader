"""Batch 1 (pre-registered 2026-10-22): ideas testable on existing lab bars, development window only.
7  cross-sectional 12-1 momentum, top 20 equal weight, monthly
8  low volatility, 20 lowest 60-day vol, monthly
10 overnight hold of SPY (close -> next open), cash intraday
11 turn of month: SPY from the last session of a month through the 3rd session of the next, cash otherwise
1s synthetic 2x SPY with 200-day filter (real fund bars pending; the earlier full-period look is disclosed)"""
import sys
from math import sqrt
sys.path.insert(0, '/home/claude/research')
import harness as H

D = H.load()
days, close = D['days'], D['close']
STOCKS = [s for s in D['series'] if s not in H.__dict__.get('ETF', ()) and s not in
          'SPY QQQ IWM DIA XLF XLE XLK XLV XLI XLY XLP XLU SMH SOXX KRE XBI GLD SLV TLT'.split()]


def monthly(selector):
    cache = {}
    def w(i):
        d = days[i]; key = d[:7]
        if i + 1 < len(days) and days[i + 1][:7] == key and key in cache:
            return cache[key]
        if key not in cache or (i + 1 < len(days) and days[i + 1][:7] != key):
            pass
        if key not in cache:
            cache[key] = selector(i)
        return cache[key]
    return w


def ret(s, i, back):
    d0, d1 = days[i - back], days[i]
    c = close[s]
    return c[d1] / c[d0] - 1 if d0 in c and d1 in c else None


def mom_12_1(i, top=20):
    if i < 253:
        return {}
    rows = []
    for s in STOCKS:
        r = ret(s, i - 21, 231)
        if r is not None:
            rows.append((r, s))
    rows = sorted(rows, reverse=True)[:top]
    return {s: 1 / top for _, s in rows}


def low_vol(i, top=20):
    if i < 61:
        return {}
    rows = []
    for s in STOCKS:
        c = close[s]; seq = [c.get(days[k]) for k in range(i - 60, i + 1)]
        if None in seq:
            continue
        r = [seq[k + 1] / seq[k] - 1 for k in range(60)]
        m = sum(r) / 60
        rows.append((sqrt(sum((x - m) ** 2 for x in r) / 60), s))
    rows = sorted(rows)[:top]
    return {s: 1 / top for _, s in rows}


def turn_of_month(i):
    if i + 1 >= len(days):
        return {}
    d, n = days[i], days[i + 1]
    first_sessions = [k for k in range(i + 1, min(i + 5, len(days))) if days[k][:7] == n[:7]]
    # hold from the last session of the month (decided at its close) through the 3rd session of the next
    last_of_month = n[:7] != d[:7]
    k = i
    while k > 0 and days[k - 1][:7] == d[:7]:
        k -= 1
    session_no = i - k + 1          # 1-based session number of day i within its month
    return {'SPY': 1.0} if last_of_month or session_no <= 2 else {}


def trend_2x(i, n=200):
    if i < n:
        return {}
    c = [close['SPY'][days[k]] for k in range(i - n + 1, i + 1)]
    return {'SPY': 2.0} if c[-1] > sum(c) / n else {}


START = days[253]
H.DEV = (START, H.DEV[1]); H.MID = days[(253 + max(i for i, d in enumerate(days) if d <= '2025-10-21')) // 2]
print('development window', H.DEV, 'split', H.MID)

if __name__ == '__main__':
    out = [H.report('7 momentum 12-1 top20 monthly', D, monthly(mom_12_1)),
           H.report('8 low-vol 20 monthly', D, monthly(low_vol)),
           H.report('11 turn of month SPY', D, turn_of_month),
           H.report('1s synthetic 2x SPY, 200-day filter*', D, trend_2x)]
    print('* 2x via weight 2.0, ignores fund fees/borrow; real SSO bars pending')
