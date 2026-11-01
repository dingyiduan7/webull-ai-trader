"""Batch 5 (pre-registered 2026-10-23): cross-sectional rules on a BROAD universe that was not picked by later fame:
every stock with an insider-purchase cluster in 2021-2026 (2,399 with bars) plus the lab list. Still survivorship-
biased (delisted names missing), which flatters long rules. Liquidity filter: 20-day median dollar volume >= $5M
at formation; costs by liquidity tier. Development window only.
15 12-1 momentum, top 20, monthly
16 52-week-high proximity (close / 252-day high), top 20, monthly
17 short-term reversal: 20 worst 5-day performers, weekly (Monday-equivalent: every 5 sessions)
18 low volatility: 20 lowest 60-day volatility, monthly
19 insider portfolio: hold every stock for 60 sessions after a 2+ insider cluster filing, equal weight, max 20"""
import json
import sys
from bisect import bisect_left
from math import sqrt
sys.path.insert(0, '/home/claude/research')
import harness as H

INS_BARS = '/mnt/user-data/uploads/auto trader/.local/research/insider_bars.json'
D = H.load(INS_BARS)
days, close = D['days'], D['close']
ETFS = set('SPY QQQ IWM DIA XLF XLE XLK XLV XLI XLY XLP XLU SMH SOXX KRE XBI GLD SLV TLT'.split())
STOCKS = [s for s in D['series'] if s not in ETFS and len(D['series'][s]) >= 260]
dv = D['dollar_volume']
H.DEV = (days[253], H.DEV[1])
H.MID = days[(253 + max(i for i, d in enumerate(days) if d <= H.DEV[1])) // 2]
print('universe', len(STOCKS), 'window', H.DEV, 'split', H.MID, flush=True)


def liquid(s, i):
    vals = [dv[s].get(days[k]) for k in range(i - 19, i + 1)]
    vals = sorted(v for v in vals if v is not None)
    return len(vals) >= 15 and vals[len(vals) // 2] >= 5e6


def every(n, selector):
    cache = {}
    def w(i):
        key = i // n
        if key not in cache:
            cache.clear(); cache[key] = selector(i)
        return cache[key]
    return w


def monthly(selector):
    cache = {}
    def w(i):
        key = days[i][:7]
        if key not in cache:
            cache.clear(); cache[key] = selector(i)
        return cache[key]
    return w


def top(rows, n=20, reverse=True):
    rows = sorted(rows, reverse=reverse)[:n]
    return {s: 1 / n for _, s in rows}


def px(s, k):
    return close[s].get(days[k])


def mom(i):
    rows = []
    for s in STOCKS:
        a, b = px(s, i - 252), px(s, i - 21)
        if a and b and px(s, i) and liquid(s, i):
            rows.append((b / a - 1, s))
    return top(rows)


def near_high(i):
    rows = []
    for s in STOCKS:
        c = px(s, i)
        if not c or not liquid(s, i):
            continue
        seq = [px(s, k) for k in range(i - 251, i + 1)]
        seq = [x for x in seq if x]
        if len(seq) >= 240:
            rows.append((c / max(seq), s))
    return top(rows)


def reversal(i):
    rows = []
    for s in STOCKS:
        a, b = px(s, i - 5), px(s, i)
        if a and b and liquid(s, i):
            rows.append((b / a - 1, s))
    return top(rows, reverse=False)


def low_vol(i):
    rows = []
    for s in STOCKS:
        seq = [px(s, k) for k in range(i - 60, i + 1)]
        if None in seq or not liquid(s, i):
            continue
        r = [seq[k + 1] / seq[k] - 1 for k in range(60)]
        m = sum(r) / 60
        rows.append((sqrt(sum((x - m) ** 2 for x in r) / 60), s))
    return top(rows, reverse=False)


def insider_portfolio():
    sys.path.insert(0, '/home/claude/research')
    import insider_study as I
    rows = json.load(open(I.INS))
    events = I.clusters(rows, 2)
    starts = {}
    for t, filed in events:
        if t in close:
            k = bisect_left(days, filed)
            if k < len(days) and days[k] == filed:
                k += 1
            starts.setdefault(k, []).append(t)
    held = {}
    def w(i):
        for s in [s for s, e in held.items() if e <= i]:
            del held[s]
        for t in starts.get(i, []):
            if t not in held and len(held) < 20 and liquid(t, i):
                held[t] = i + 60
        return {s: 1 / 20 for s in held}
    return w


if __name__ == '__main__':
    H.report('15 momentum 12-1 top 20, broad', D, monthly(mom))
    H.report('16 52-week-high proximity top 20, broad', D, monthly(near_high))
    H.report('17 short-term reversal 20, weekly, broad', D, every(5, reversal))
    H.report('18 low volatility 20, broad', D, monthly(low_vol))
    H.report('19 insider portfolio, 60 sessions, max 20', D, insider_portfolio())
