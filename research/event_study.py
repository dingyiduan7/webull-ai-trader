"""Event studies for ideas 2 (clustered insider buying) and 3 (post-earnings drift), built to resist the lab
universe's hindsight bias: each event's return is measured against SPY AND against the same stock's own average
return over the development window, so a stock that simply rose all period earns no credit.

Entry: next session's open after the information was public (insider: SEC filing date; earnings: 8-K acceptance
time, after-close filings count from the next session). Horizons 5/20/60 sessions. Costs 0.20% round trip added.
Development window only (filing dates <= 2025-10-21 and exits inside it).

Usage: python event_study.py insiders .local/research/insider_purchases.json
       python event_study.py earnings .local/research/earnings_dates.json
"""
import json
import sys
from bisect import bisect_left
from math import sqrt

sys.path.insert(0, '/home/claude/research')
import harness as H

DEV_END = '2025-10-21'
HORIZONS = (5, 20, 60)
COST = 0.002


def series_index(data):
    out = {}
    for s, bars in data['series'].items():
        out[s] = ([b['date'] for b in bars], bars)
    return out


def own_drift(bars, h):
    """Average h-session open-to-close-h return of this stock over the development window (its own baseline)."""
    rets = []
    dates = [b['date'] for b in bars]
    end = bisect_left(dates, DEV_END)
    for k in range(0, max(0, end - h), 5):
        rets.append(bars[k + h - 1]['close'] / bars[k]['open'] - 1)
    return sum(rets) / len(rets) if rets else None


def event_returns(data, events):
    """events: [(symbol, first_tradable_date)] -> per horizon list of (excess vs SPY minus own drift)."""
    idx = series_index(data)
    spy_dates, spy = idx['SPY']
    drift_cache, out = {}, {h: [] for h in HORIZONS}
    for symbol, day in events:
        if symbol not in idx or day > DEV_END:
            continue
        dates, bars = idx[symbol]
        k = bisect_left(dates, day)
        j = bisect_left(spy_dates, day)
        for h in HORIZONS:
            if k + h - 1 >= len(bars) or bars[k + h - 1]['date'] > DEV_END or j + h - 1 >= len(spy):
                continue
            r = bars[k + h - 1]['close'] / bars[k]['open'] - 1 - COST
            rs = spy[j + h - 1]['close'] / spy[j]['open'] - 1
            key = (symbol, h)
            if key not in drift_cache:
                drift_cache[key] = own_drift(bars, h)
            spy_drift = drift_cache.setdefault(('SPY', h), own_drift(spy, h))
            if drift_cache[key] is None:
                continue
            out[h].append((r - rs) - (drift_cache[key] - spy_drift))
    return out


def summarize(name, out):
    for h, xs in out.items():
        if len(xs) < 20:
            print('%-40s h=%2d n=%d (too few)' % (name, h, len(xs)));continue
        m = sum(xs) / len(xs); sd = sqrt(sum((x - m) ** 2 for x in xs) / len(xs))
        print('%-40s h=%2d n=%4d mean %+.2f%%  t=%.2f  win %.0f%%' % (
            name, h, len(xs), 100 * m, m / sd * sqrt(len(xs)) if sd else 0, 100 * sum(x > 0 for x in xs) / len(xs)))


def next_session(dates, day, after_close=False):
    k = bisect_left(dates, day)
    if k < len(dates) and dates[k] == day and after_close:
        k += 1
    return dates[k] if k < len(dates) else None


def insider_events(rows, spy_dates, min_insiders=2, window=10, min_value=10000):
    """Clusters: >= min_insiders distinct insiders buying in the open market within `window` calendar days;
    the event is public on the filing date of the purchase that completes the cluster."""
    from datetime import date, timedelta
    by = {}
    for r in rows:
        if r['value'] >= min_value and r['filing_date'] and r['trans_date']:
            by.setdefault(r['ticker'], []).append(r)
    events = []
    for ticker, rs in by.items():
        rs.sort(key=lambda r: r['filing_date'])
        last_event = None
        for i, r in enumerate(rs):
            lo = (date.fromisoformat(r['trans_date']) - timedelta(days=window)).isoformat()
            owners = {x['owner'] for x in rs[:i + 1] if lo <= x['trans_date'] <= r['trans_date']}
            if len(owners) >= min_insiders and (last_event is None or r['filing_date'] > last_event):
                day = next_session(spy_dates, r['filing_date'], after_close=True)   # filings often land after hours
                if day:
                    events.append((ticker, day))
                    last_event = (date.fromisoformat(r['filing_date']) + timedelta(days=30)).isoformat()
    return events


if __name__ == '__main__':
    import os
    extra = sys.argv[3] if len(sys.argv) > 3 and os.path.exists(sys.argv[3]) else None
    data = H.load(extra)   # insider runs: pass insider_bars.json to cover tickers outside the lab list
    spy_dates = [b['date'] for b in data['series']['SPY']]
    kind, path = sys.argv[1], sys.argv[2]
    payload = json.load(open(path))
    if kind == 'insiders':
        ev = insider_events(payload, spy_dates)
        print('insider clusters in universe:', sum(1 for s, _ in ev if s in data['series']))
        summarize('2a insider cluster >=2, $10k+', event_returns(data, ev))
    elif kind == 'earnings':
        from datetime import datetime, timezone
        from zoneinfo import ZoneInfo
        ny = ZoneInfo('America/New_York')
        idx = series_index(data)
        up, down, allr = [], [], []
        for s, rows in payload['dates'].items():
            if s not in idx:
                continue
            dates, bars = idx[s]
            for r in rows:
                acc = r.get('accepted') or ''
                try:
                    t = datetime.fromisoformat(acc.replace('Z', '+00:00')).astimezone(ny)
                except ValueError:
                    continue
                # Reaction session: same day if released before 9:30 ET, else the next session.
                day = t.date().isoformat()
                before_open = (t.hour, t.minute) < (9, 30)
                k = bisect_left(dates, day)
                if k < len(dates) and dates[k] == day and not before_open:
                    k += 1
                if k < 21 or k + 1 >= len(bars):
                    continue
                gap = bars[k]['open'] / bars[k - 1]['close'] - 1
                avgv = sum(b['volume'] for b in bars[k - 20:k]) / 20
                entry = bars[k + 1]['date']                      # buy the session after the reaction day
                allr.append((s, entry))
                if gap >= 0.05 and bars[k]['volume'] >= 2 * avgv:
                    up.append((s, entry))
                elif gap <= -0.05 and bars[k]['volume'] >= 2 * avgv:
                    down.append((s, entry))
        summarize('3x all earnings releases (control)', event_returns(data, allr))
        summarize('3a gap up >=5% on >=2x volume', event_returns(data, up))
        summarize('3b gap down <=-5% on >=2x volume', event_returns(data, down))
