"""Idea 2 event study on the event-chosen universe (tickers with insider clusters), development window only.

Event: the filing that completes a cluster of open-market insider purchases (>= $10k each) by >= N different
insiders within 10 calendar days; entry at the open of the session after the filing date. Return measured vs SPY
and vs the stock's own average drift over the development window (removes stocks that simply rose all period).
Round-trip costs by 20-day median dollar volume: >=$200M 0.2%, $50-200M 0.4%, $5-50M 0.7%, $1-5M 1.5%; below $1M
the stock is treated as untradeable and skipped.
Variants (pre-registered 2026-10-23): 2a N>=2; 2b N>=3; 2c N>=2 with a CEO or CFO among them; 2d N>=2 and cluster
value >= $100k. Each reported overall and by liquidity bucket."""
import json
import sys
from bisect import bisect_left
from datetime import date, timedelta
from math import sqrt

DEV_END = '2025-10-21'
H = (5, 20, 60)
LAB = '/mnt/user-data/uploads/auto trader/.local/lab/bars.json'
INS_BARS = '/mnt/user-data/uploads/auto trader/.local/research/insider_bars.json'
INS = '/mnt/user-data/uploads/auto trader/.local/research/insider_purchases.json'


def load():
    series = json.load(open(INS_BARS))['series']
    lab = json.load(open(LAB))['series']
    for s, b in lab.items():
        series.setdefault(s, b)
    return series


def liquidity(bars, k):
    dv = sorted(b['close'] * b['volume'] for b in bars[max(0, k - 20):k])
    return dv[len(dv) // 2] if dv else 0


def cost_rt(dv):
    if dv >= 200e6:return 0.002, 'large'
    if dv >= 50e6:return 0.004, 'mid'
    if dv >= 5e6:return 0.007, 'small'
    if dv >= 1e6:return 0.015, 'micro'
    return None, 'untradeable'


def own_drift(bars, h, cache, key):
    if key in cache:
        return cache[key]
    dates = [b['date'] for b in bars]
    end = bisect_left(dates, DEV_END)
    rets = [bars[k + h - 1]['close'] / bars[k]['open'] - 1 for k in range(0, max(0, end - h), 5)]
    cache[key] = sum(rets) / len(rets) if len(rets) >= 10 else None
    return cache[key]


def clusters(rows, n_min=2, ceo_cfo=False, min_total=0):
    by = {}
    for r in rows:
        if r['value'] >= 10000 and r['filing_date'] and r['trans_date'] and r['filing_date'] <= DEV_END:
            by.setdefault(r['ticker'], []).append(r)
    events = []
    for t, rs in by.items():
        rs.sort(key=lambda r: r['filing_date'])
        cooldown = ''
        for i, r in enumerate(rs):
            if r['filing_date'] <= cooldown:
                continue
            lo = (date.fromisoformat(r['trans_date']) - timedelta(days=10)).isoformat()
            group = [x for x in rs[:i + 1] if lo <= x['trans_date'] <= r['trans_date']]
            owners = {x['owner'] for x in group}
            if len(owners) < n_min or sum(x['value'] for x in group) < min_total:
                continue
            if ceo_cfo and not any(any(k in (x.get('title') or '').upper() for k in ('CEO', 'CHIEF EXECUTIVE', 'CFO', 'CHIEF FINANCIAL')) for x in group):
                continue
            events.append((t, r['filing_date']))
            cooldown = (date.fromisoformat(r['filing_date']) + timedelta(days=30)).isoformat()
    return events


def study(series, events):
    spy = series['SPY']; spy_dates = [b['date'] for b in spy]
    idx = {}
    cache = {}
    out = {}
    for t, filed in events:
        bars = series.get(t)
        if not bars:
            continue
        if t not in idx:
            idx[t] = [b['date'] for b in bars]
        dates = idx[t]
        k = bisect_left(dates, filed)
        if k < len(dates) and dates[k] == filed:
            k += 1                                   # filings land after hours: enter the next session
        if k < 20 or k >= len(bars):
            continue
        c, bucket = cost_rt(liquidity(bars, k))
        if c is None:
            out.setdefault(('untradeable', None), []).append(0)
            continue
        j = bisect_left(spy_dates, bars[k]['date'])
        for h in H:
            if k + h - 1 >= len(bars) or bars[k + h - 1]['date'] > DEV_END or j + h - 1 >= len(spy):
                continue
            d_own = own_drift(bars, h, cache, (t, h)); d_spy = own_drift(spy, h, cache, ('SPY', h))
            if d_own is None:
                continue
            r = bars[k + h - 1]['close'] / bars[k]['open'] - 1 - c
            rs = spy[j + h - 1]['close'] / spy[j]['open'] - 1
            x = (r - rs) - (d_own - d_spy)
            out.setdefault(('all', h), []).append(x)
            out.setdefault((bucket, h), []).append(x)
    return out


def show(name, out):
    skipped = len(out.get(('untradeable', None), []))
    print('%s  (skipped as untradeable: %d)' % (name, skipped))
    for bucket in ('all', 'large', 'mid', 'small', 'micro'):
        cells = []
        for h in H:
            xs = out.get((bucket, h), [])
            if len(xs) < 20:
                cells.append('h%-2d n=%-4d    --      ' % (h, len(xs)));continue
            m = sum(xs) / len(xs); sd = sqrt(sum((v - m) ** 2 for v in xs) / len(xs))
            cells.append('h%-2d n=%-4d %+6.2f%% t=%5.2f' % (h, len(xs), 100 * m, m / sd * sqrt(len(xs))))
        print('   %-6s %s' % (bucket, ' | '.join(cells)), flush=True)


if __name__ == '__main__':
    series = load()
    rows = json.load(open(INS))
    print('bars for %d tickers' % len(series), flush=True)
    show('2a >=2 insiders', study(series, clusters(rows, 2)))
    show('2b >=3 insiders', study(series, clusters(rows, 3)))
    show('2c >=2 incl. CEO/CFO', study(series, clusters(rows, 2, ceo_cfo=True)))
    show('2d >=2, cluster >= $100k', study(series, clusters(rows, 2, min_total=100000)))
