"""Edge-search harness: daily-rebalanced bookkeeping for rule portfolios vs SPY on the development window.

Every candidate is a function weights(day_index) -> {symbol: weight} decided with data up to the previous close
and applied from that close to the next close (so a signal at close t earns t -> t+1). Costs are charged on
turnover. The holdout window is refused unless explicitly unlocked.
"""
import json
from math import sqrt

BARS = '/mnt/user-data/uploads/auto trader/.local/lab/bars.json'
DEV = ('2021-10-26', '2025-10-21')
HOLDOUT = ('2025-10-22', '2026-10-21')
MID = '2023-10-23'


def load(extra=None):
    series = json.load(open(BARS))['series']
    if extra:
        series.update(json.load(open(extra))['series'])
    days = sorted({b['date'] for b in series['SPY']})
    close = {s: {b['date']: b['close'] for b in bars} for s, bars in series.items()}
    opens = {s: {b['date']: b['open'] for b in bars} for s, bars in series.items()}
    vol = {s: {b['date']: b['close'] * b['volume'] for b in bars} for s, bars in series.items()}
    return dict(series=series, days=days, close=close, open=opens, dollar_volume=vol)


def cost_for(data, symbol, day):
    """Per-side cost by liquidity, same tiers as the lab (ETFs and mega caps 0.10%)."""
    dv = data['dollar_volume'][symbol].get(day)
    if dv is None:
        return 0.0035
    return 0.001 if dv >= 200e6 else 0.002 if dv >= 50e6 else 0.0035


def run(data, weights, window=None, allow_holdout=False, cash_rate=0.03):
    window = window or DEV
    if window[1] >= HOLDOUT[0] and not allow_holdout:
        raise ValueError('HOLDOUT_LOCKED')
    days, close = data['days'], data['close']
    idx = [i for i, d in enumerate(days) if window[0] <= d <= window[1]]
    w_prev, eq, spy_eq, ex = {}, 1.0, 1.0, []
    rows = []
    for i in idx[:-1]:
        d, nxt = days[i], days[i + 1]
        w = weights(i) or {}
        turn = sum(abs(w.get(s, 0) - w_prev.get(s, 0)) * cost_for(data, s, d) for s in set(w) | set(w_prev))
        r = sum(wt * (close[s][nxt] / close[s][d] - 1) for s, wt in w.items() if d in close[s] and nxt in close[s])
        r += max(0.0, 1 - sum(w.values())) * cash_rate / 252
        r -= turn
        rs = close['SPY'][nxt] / close['SPY'][d] - 1
        eq *= 1 + r; spy_eq *= 1 + rs
        ex.append(r - rs); rows.append((nxt, eq, spy_eq))
        w_prev = w
    n = len(ex); mu = sum(ex) / n; sd = sqrt(sum((x - mu) ** 2 for x in ex) / n)
    peak, dd = 1.0, 0.0
    for _, e, _ in rows:
        peak = max(peak, e); dd = max(dd, 1 - e / peak)
    return dict(start=days[idx[0]], end=days[idx[-1]], growth=round(eq, 3), spy=round(spy_eq, 3),
                t=round(mu / sd * sqrt(n), 2) if sd else None, max_dd=round(dd, 3), days=n)


def halves(data, weights):
    return [run(data, weights, (DEV[0], MID)), run(data, weights, (MID, DEV[1]))]


def report(name, data, weights):
    a = run(data, weights); h1, h2 = halves(data, weights)
    print('%-44s x%.2f vs SPY x%.2f  t=%5s  dd %2.0f%% | halves t %s / %s' % (
        name, a['growth'], a['spy'], a['t'], 100 * a['max_dd'], h1['t'], h2['t']), flush=True)
    return dict(name=name, all=a, halves=[h1, h2])
