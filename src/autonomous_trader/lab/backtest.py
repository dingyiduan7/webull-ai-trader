"""Daily-bar backtest with realistic frictions, stability split and staged promotion verdicts.

Fill model (conservative): signal at day i's close, entry at day i+1's open plus cost; the stop is checked
first each day (a gap below fills at the open, not the stop); a target fills at max(open, target); signal and
time exits fill at the close minus cost. Costs default to 0.10% per side (spread + slippage, liquid names).
"""
from dataclasses import dataclass, field
from math import sqrt
from statistics import mean, median, pstdev

from .indicators import atr

COST_PER_SIDE = 0.001           # liquid names (median dollar volume >= $200M/day)


def cost_per_side(bars, i):
    """Spread + slippage per side by liquidity (2026-10-20, smaller names cost more to trade): >= $200M median
    20-day dollar volume 0.10%, $50-200M 0.20%, below that 0.35%."""
    if i < 20:
        return 0.0035
    dv = sorted(bars[k]['close'] * bars[k]['volume'] for k in range(i - 19, i + 1))[10]
    return COST_PER_SIDE if dv >= 200e6 else 0.002 if dv >= 50e6 else 0.0035

# Promotion gates (staged: REJECTED -> PAPER -> SMALL_LIVE -> LIVE). Backtests can only earn PAPER; live
# stages need live results on this account (the scorecard), so a backtest alone never grants full size.
MIN_TRADES = 40
MIN_TRADES_PER_HALF = 15
MIN_PROFIT_FACTOR = 1.2
MIN_T_STAT = 1.5


@dataclass
class Trade:
    symbol: str
    strategy: str
    entry_date: str
    entry: float
    exit_date: str
    exit: float
    days: int
    reason: str
    mfe: float
    mae: float
    cost: float = COST_PER_SIDE

    @property
    def ret(self):
        return (self.exit * (1 - self.cost)) / (self.entry * (1 + self.cost)) - 1


def context(symbol, bars, spy_bars=None, losers_by_date=None, leaders_by_date=None):
    ctx = dict(symbol=symbol, bars=bars, closes=[b['close'] for b in bars], losers_by_date=losers_by_date or {},
               leaders_by_date=leaders_by_date or {})
    if spy_bars:
        ctx['spy_series'] = [b['close'] for b in spy_bars]
        ctx['spy_index'] = {b['date']: k for k, b in enumerate(spy_bars)}
    return ctx


def run_symbol(strategy, ctx):
    bars, trades, i = ctx['bars'], [], 0
    n = len(bars)
    while i < n - 1:
        if not strategy.entry(ctx, i):
            i += 1
            continue
        a = atr(bars, 14, i)
        if not a:
            i += 1
            continue
        k = i + 1
        entry = bars[k]['open']
        stop = entry - strategy.stop_atr * a
        target = entry + strategy.target_atr * a if strategy.target_atr else None
        hi, lo = entry, entry
        exit_price = reason = None
        last = min(n - 1, k + strategy.max_days - 1)
        j = k
        while j <= last:
            b = bars[j]
            hi, lo = max(hi, b['high']), min(lo, b['low'])
            if b['low'] <= stop:       # stop first (conservative); a gap below fills at the open
                exit_price, reason = (b['open'] if b['open'] <= stop else stop), 'STOP'
                break
            if target is not None and b['high'] >= target:
                exit_price, reason = (b['open'] if b['open'] >= target else target), 'TARGET'
                break
            if strategy.exit_signal and strategy.exit_signal(ctx, j, entry):
                exit_price, reason = b['close'], 'SIGNAL'
                break
            if j == last:
                exit_price, reason = b['close'], 'TIME' if j == k + strategy.max_days - 1 else 'END_OF_DATA'
                break
            j += 1
        if reason == 'END_OF_DATA':
            break                      # an open trade at the end of history is not a completed result
        trades.append(Trade(ctx['symbol'], strategy.id, bars[k]['date'], entry, bars[j]['date'], exit_price,
                            j - k + 1, reason, hi / entry - 1, lo / entry - 1, cost_per_side(bars, i)))
        i = j + 1                      # one open trade per symbol per strategy
    return trades


def weekly_losers(series_by_symbol, decile=0.10):
    """Per date, the symbols whose 5-session return is in the bottom decile of the universe that day."""
    by_date = {}
    for symbol, bars in series_by_symbol.items():
        for k in range(5, len(bars)):
            by_date.setdefault(bars[k]['date'], []).append((bars[k]['close'] / bars[k - 5]['close'] - 1, symbol))
    out = {}
    for day, rows in by_date.items():
        if len(rows) < 20:
            continue
        rows.sort()
        out[day] = frozenset(s for _, s in rows[:max(1, int(len(rows) * decile))])
    return out


def momentum_leaders(series_by_symbol, top=0.20, lookback=60):
    """Per date, the symbols whose 60-session return is in the top 20% of the universe that day."""
    by_date = {}
    for symbol, bars in series_by_symbol.items():
        for k in range(lookback, len(bars)):
            by_date.setdefault(bars[k]['date'], []).append((bars[k]['close'] / bars[k - lookback]['close'] - 1, symbol))
    out = {}
    for day, rows in by_date.items():
        if len(rows) < 20:
            continue
        rows.sort(reverse=True)
        out[day] = frozenset(s for _, s in rows[:max(1, int(len(rows) * top))])
    return out


def stats(trades):
    if not trades:
        return dict(trades=0)
    rets = [t.ret for t in trades]
    wins, losses = [r for r in rets if r > 0], [r for r in rets if r <= 0]
    sd = pstdev(rets) if len(rets) > 1 else 0.0
    streak = worst = 0
    for r in sorted(trades, key=lambda t: t.exit_date):
        streak = streak + 1 if r.ret <= 0 else 0
        worst = max(worst, streak)
    gross_loss = -sum(losses)
    days = mean(t.days for t in trades)
    return dict(trades=len(rets), win_rate=len(wins) / len(rets), avg_return=mean(rets),
                avg_days=days, return_per_day=mean(rets) / days if days else None,
                avg_win=mean(wins) if wins else 0.0, avg_loss=mean(losses) if losses else 0.0,
                profit_factor=(sum(wins) / gross_loss) if gross_loss > 0 else None,
                t_stat=(mean(rets) / (sd / sqrt(len(rets)))) if sd > 0 else None,
                median_days=median(t.days for t in trades), worst_losing_streak=worst,
                exits={r: sum(1 for t in trades if t.reason == r) for r in ('STOP', 'TARGET', 'SIGNAL', 'TIME')},
                avg_mfe=mean(t.mfe for t in trades), avg_mae=mean(t.mae for t in trades))


def verdict(all_stats, first, second, baseline_avg=None):
    # baseline_avg: buy-anything return PER DAY HELD; the strategy's return per day must exceed it.
    """PAPER only when the edge is positive after costs in BOTH halves, large enough and better than baseline."""
    reasons = []
    if all_stats.get('trades', 0) < MIN_TRADES:
        reasons.append('TOO_FEW_TRADES')
    for name, half in (('FIRST_HALF', first), ('SECOND_HALF', second)):
        if half.get('trades', 0) < MIN_TRADES_PER_HALF:
            reasons.append(name + '_TOO_FEW_TRADES')
        elif half['avg_return'] <= 0:
            reasons.append(name + '_NOT_PROFITABLE')
    if all_stats.get('trades'):
        if (all_stats['profit_factor'] or 0) < MIN_PROFIT_FACTOR:
            reasons.append('PROFIT_FACTOR_BELOW_1_2')
        if (all_stats['t_stat'] or 0) < MIN_T_STAT:
            reasons.append('NOT_STATISTICALLY_CLEAR')
        # Compared per day held, so a 40-day hold is not credited for simply being exposed longer.
        if baseline_avg is not None and (all_stats.get('return_per_day') or 0) <= baseline_avg:
            reasons.append('DOES_NOT_BEAT_BASELINE')
    return ('PAPER' if not reasons else 'REJECTED'), reasons


def evaluate(strategies, series_by_symbol, spy_bars):
    """Run every strategy over every symbol; stability = earlier vs later half of each strategy's trades."""
    losers, leaders = weekly_losers(series_by_symbol), momentum_leaders(series_by_symbol)
    results, trades_by = {}, {}
    for strategy in strategies:
        trades = []
        for symbol, bars in series_by_symbol.items():
            trades += run_symbol(strategy, context(symbol, bars, spy_bars, losers, leaders))
        trades_by[strategy.id] = trades
    # Baseline per day held: the better of the 5-day and 20-day buy-anything holds (the bar to beat).
    baseline = max((stats(trades_by.get(b, [])).get('return_per_day') or 0)
                   for b in ('baseline_every_5th_day', 'baseline_hold_20d'))
    for strategy in strategies:
        trades = trades_by[strategy.id]
        # Stability: earlier half of this strategy's trades vs the later half (by entry date).
        entries = sorted(t.entry_date for t in trades)
        split = entries[len(entries) // 2] if entries else ''
        s = stats(trades)
        first = stats([t for t in trades if t.entry_date < split])
        second = stats([t for t in trades if t.entry_date >= split])
        stage, why = verdict(s, first, second, None if strategy.id.startswith('baseline') else baseline)
        if strategy.id.startswith('baseline'):
            stage, why = 'BASELINE', []
        results[strategy.id] = dict(card=strategy.card, summary=strategy.summary, stage=stage, reasons=why,
                                    all=s, first_half=first, second_half=second, split_date=split)
    return results, trades_by


def todays_signals(strategies, series_by_symbol, spy_bars):
    """Symbols whose entry rule fires on the latest bar (for tomorrow's open), per strategy."""
    losers, leaders = weekly_losers(series_by_symbol), momentum_leaders(series_by_symbol)
    out = {}
    for strategy in strategies:
        if strategy.id.startswith('baseline'):
            continue
        hits = []
        for symbol, bars in series_by_symbol.items():
            if len(bars) < 2:
                continue
            ctx = context(symbol, bars, spy_bars, losers, leaders)
            i = len(bars) - 1
            if strategy.entry(ctx, i):
                a = atr(bars, 14, i)
                if a:
                    close = bars[i]['close']
                    hits.append(dict(symbol=symbol, date=bars[i]['date'], close=round(close, 4),
                                     stop=round(close - strategy.stop_atr * a, 4),
                                     target=round(close + strategy.target_atr * a, 4) if strategy.target_atr else None,
                                     max_days=strategy.max_days))
        out[strategy.id] = hits
    return out
