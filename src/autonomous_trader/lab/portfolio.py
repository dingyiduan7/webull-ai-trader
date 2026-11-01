"""Simple Trader v3 (owner approval 2026-10-21): "lab rules pick trades with relative strength as the main rule,
AI only vetoes, 2% risk per position with fractional shares, max 3 positions, regime filter".

This module holds the v3 rules shared by the lab (portfolio backtest, picks file) and the trader (sizing):
- ranking: relative_strength_hold first (by 20-day excess return over SPY), then momentum_leaders_hold (by
  60-day return) at half size;
- sizing: risk RISK_PER_POSITION of equity from entry to stop, at most MAX_POSITION_FRACTION of equity in one
  stock, at most MAX_POSITIONS open;
- regime: no new entries while SPY closes below its 100-day average, or while the last PAPER_WINDOW completed
  paper trades (out-of-sample) average below zero once at least PAPER_MIN of them exist.
"""
from math import floor

from .indicators import atr, sma

PRIMARY = 'relative_strength_hold'
SECONDARY = 'momentum_leaders_hold'
RULES = (PRIMARY, SECONDARY)
SIZE_FACTOR = {PRIMARY: 1.0, SECONDARY: 0.5}
EXIT_SMA = {PRIMARY: 20, SECONDARY: 50}
RISK_PER_POSITION = 0.02
MAX_POSITION_FRACTION = 0.40
MAX_POSITIONS = 3
REGIME_SMA = 100
PAPER_WINDOW = 20
PAPER_MIN = 10


def rank_score(strategy, closes, i, spy_closes=None, k=None):
    """Higher is stronger. RS: 20-session return minus SPY's; momentum: 60-session return."""
    if strategy == PRIMARY:
        if spy_closes is None or k is None or i < 20 or k < 20:
            return None
        return (closes[i] / closes[i - 20] - 1) - (spy_closes[k] / spy_closes[k - 20] - 1)
    if strategy == SECONDARY:
        return None if i < 60 else closes[i] / closes[i - 60] - 1
    return None


def order_key(row):
    """Sort key for candidate rows {strategy, score}: primary rule first, then strongest score."""
    return (RULES.index(row['strategy']) if row['strategy'] in RULES else 9, -(row.get('score') or -9))


def size_fraction(strategy, stop_distance_pct):
    """Fraction of equity to put in one entry: 2% risk / stop distance, half for the secondary rule, capped."""
    if not stop_distance_pct or stop_distance_pct <= 0 or strategy not in SIZE_FACTOR:
        return 0.0
    return min(MAX_POSITION_FRACTION, RISK_PER_POSITION / stop_distance_pct) * SIZE_FACTOR[strategy]


def regime(spy_bars, paper_returns):
    """Market regime for new entries. paper_returns: completed paper trade returns, oldest first."""
    closes = [b['close'] for b in spy_bars]
    i = len(closes) - 1
    avg = sma(closes, REGIME_SMA, i) if i >= 0 else None
    spy_ok = avg is not None and closes[i] > avg
    recent = list(paper_returns)[-PAPER_WINDOW:]
    paper_avg = sum(recent) / len(recent) if recent else None
    paper_ok = len(recent) < PAPER_MIN or paper_avg >= 0
    reasons = ([] if spy_ok else ['SPY_BELOW_100_DAY']) + ([] if paper_ok else ['PAPER_RESULTS_NEGATIVE'])
    return dict(entries_allowed=spy_ok and paper_ok, reasons=reasons,
                spy_close=None if i < 0 else round(closes[i], 4), spy_sma100=None if avg is None else round(avg, 4),
                paper_trades=len(recent), paper_avg_return=None if paper_avg is None else round(paper_avg, 5))


def exit_levels(series):
    """Per symbol: sums of the last 19 and 49 closes, so the trader can test 'price below the 20/50-day average
    including today' intraday as (sum + price) / n, plus the last close for sanity."""
    out = {}
    for symbol, bars in series.items():
        c = [b['close'] for b in bars]
        if len(c) < 50:
            continue
        out[symbol] = dict(date=bars[-1]['date'], last_close=round(c[-1], 4),
                           sum19=round(sum(c[-19:]), 4), sum49=round(sum(c[-49:]), 4))
    return out


def below_exit_average(level, strategy, price):
    """True when price is below the strategy's exit average computed with price as today's close."""
    n = EXIT_SMA.get(strategy)
    if not level or n is None or price is None or price <= 0:
        return False
    total = level['sum19'] if n == 20 else level['sum49']
    return price < (total + price) / n


def simulate(trades_by, series, spy_bars, start_equity=500.0):
    """Portfolio backtest of the v3 rules over the lab's per-strategy trades (entry next open, same exits).

    Each day: free slots (max 3) are filled from that day's entries, ranked by order_key; size by size_fraction,
    limited by available cash. Equity is marked at realized exits (positions at cost while open). Compared with
    holding SPY over the same dates."""
    spy_index = {b['date']: k for k, b in enumerate(spy_bars)}
    spy_closes = [b['close'] for b in spy_bars]
    idx = {s: {b['date']: k for k, b in enumerate(bars)} for s, bars in series.items()}
    rows = []
    for sid in RULES:
        strat_stop = 3.0
        for t in trades_by.get(sid, []):
            bars = series.get(t.symbol)
            k = idx.get(t.symbol, {}).get(t.entry_date)
            if not bars or k is None or k < 1:
                continue
            i = k - 1                                          # signal day
            a = atr(bars, 14, i)
            closes = [b['close'] for b in bars]
            if not a:
                continue
            score = rank_score(sid, closes, i, spy_closes, spy_index.get(bars[i]['date']))
            rows.append(dict(strategy=sid, score=score, trade=t, stop_pct=strat_stop * a / t.entry))
    by_day = {}
    for r in rows:
        by_day.setdefault(r['trade'].entry_date, []).append(r)
    days = sorted({b['date'] for b in spy_bars})
    cash, open_pos, closed = start_equity, [], []
    curve = []
    for day in days:
        for p in [p for p in open_pos if p['trade'].exit_date <= day]:
            open_pos.remove(p)
            cash += p['amount'] * (1 + p['trade'].ret)
            closed.append(dict(strategy=p['strategy'], ret=p['trade'].ret, pnl=p['amount'] * p['trade'].ret,
                               entry=p['trade'].entry_date, exit=p['trade'].exit_date, symbol=p['trade'].symbol))
        equity = cash + sum(p['amount'] for p in open_pos)
        held = {p['trade'].symbol for p in open_pos}
        for r in sorted(by_day.get(day, []), key=order_key):
            if len(open_pos) >= MAX_POSITIONS:
                break
            if r['trade'].symbol in held:
                continue
            amount = min(equity * size_fraction(r['strategy'], r['stop_pct']), cash)
            if amount < 5:                                      # below a sensible fractional order
                continue
            cash -= amount
            open_pos.append(dict(r, amount=amount))
            held.add(r['trade'].symbol)
        curve.append((day, cash + sum(p['amount'] for p in open_pos)))
    final = curve[-1][1] if curve else start_equity
    first, last = spy_index.get(days[0]), spy_index.get(days[-1])
    spy_ret = spy_closes[last] / spy_closes[first] - 1 if first is not None and last is not None else None
    peak, max_dd = start_equity, 0.0
    for _, e in curve:
        peak = max(peak, e)
        max_dd = max(max_dd, 1 - e / peak)
    half = days[len(days) // 2] if days else ''
    def part(lo, hi):
        sel = [c for c in closed if lo <= c['exit'] < hi]
        return dict(trades=len(sel), pnl=round(sum(c['pnl'] for c in sel), 2),
                    avg_return=round(sum(c['ret'] for c in sel) / len(sel), 5) if sel else None)
    years = max(len(days) / 252, 1e-9)
    return dict(start_equity=start_equity, final_equity=round(final, 2), total_return=round(final / start_equity - 1, 4),
                cagr=round((final / start_equity) ** (1 / years) - 1, 4), spy_return=None if spy_ret is None else round(spy_ret, 4),
                spy_cagr=None if spy_ret is None else round((1 + spy_ret) ** (1 / years) - 1, 4),
                max_drawdown=round(max_dd, 4), trades=len(closed),
                win_rate=round(sum(1 for c in closed if c['ret'] > 0) / len(closed), 3) if closed else None,
                by_rule={sid: part_rule for sid, part_rule in ((s, dict(trades=sum(1 for c in closed if c['strategy'] == s),
                         pnl=round(sum(c['pnl'] for c in closed if c['strategy'] == s), 2))) for s in RULES)},
                first_half=part('', half), second_half=part(half, '9999'), first_date=days[0] if days else None,
                last_date=days[-1] if days else None, curve=[(d, round(e, 2)) for d, e in curve[::5]])
