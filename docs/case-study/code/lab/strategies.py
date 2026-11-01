"""Rule-based strategies with fixed, literature-style parameters (no tuning on our data, to avoid overfitting).

Data window: Webull serves 250 daily bars per read, so "long-term trend" uses the 100-day average (a 200-day
filter would leave only ~50 testable sessions); the lab's saved history grows every run.

Each strategy decides on the close of day i using only bars up to i; the backtest enters at the next open.
Exits: hard stop (ATR based, checked intraday), optional target (intraday), a signal exit at the close, and a
maximum holding period. Long only, whole positions, one open trade per symbol per strategy."""
from dataclasses import dataclass
from typing import Callable

from .indicators import atr, highest, ibs, lowest, rsi, sma


@dataclass(frozen=True)
class Strategy:
    id: str
    card: str                      # matching strategy card / setup family for the decision models
    summary: str
    entry: Callable                # (ctx, i) -> bool
    stop_atr: float                # hard stop distance below entry, in ATR(14) at signal
    target_atr: float | None       # optional profit target above entry, in ATR(14)
    max_days: int                  # holding limit in sessions (exit at that close)
    exit_signal: Callable | None   # (ctx, i, entry_price) -> bool, evaluated at the close


def _spy_ok(ctx, i, n=50):
    """Market regime: SPY close above its n-day average on the same session (unknown = not ok)."""
    if not ctx.get('spy_series'):
        return True                # no market series supplied (unit tests); the lab always supplies SPY
    day = ctx['bars'][i]['date']
    series, index = ctx['spy_series'], ctx['spy_index'].get(day)
    if index is None:
        return False
    avg = sma(series, n, index)
    return avg is not None and series[index] > avg


def _liquid(ctx, i, min_dollar_volume=20e6):
    b = ctx['bars']
    if i < 20:
        return False
    dv = sorted(b[k]['close'] * b[k]['volume'] for k in range(i - 19, i + 1))[10]
    return dv >= min_dollar_volume


def rsi2_entry(ctx, i):
    c = ctx['closes']
    s200 = sma(c, 100, i)
    r = rsi(c, 2, i)
    return s200 is not None and r is not None and c[i] > s200 and r < 10 and _liquid(ctx, i)


def close_above_sma5(ctx, i, entry):
    s5 = sma(ctx['closes'], 5, i)
    return s5 is not None and ctx['closes'][i] > s5


def ibs_entry(ctx, i):
    c = ctx['closes']
    s200 = sma(c, 100, i)
    return s200 is not None and c[i] > s200 and ibs(ctx['bars'][i]) < 0.15 and _liquid(ctx, i)


def ibs_exit(ctx, i, entry):
    return ibs(ctx['bars'][i]) > 0.75 or ctx['closes'][i] > ctx['bars'][i - 1]['high']


def three_down_entry(ctx, i):
    c = ctx['closes']
    s200 = sma(c, 100, i)
    return (s200 is not None and i >= 3 and c[i] > s200 and c[i] < c[i - 1] < c[i - 2] < c[i - 3]
            and _spy_ok(ctx, i) and _liquid(ctx, i))


def first_up_close(ctx, i, entry):
    return ctx['closes'][i] > ctx['closes'][i - 1]


def reversal_entry(ctx, i):
    """Short-term reversal: bottom decile 5-day return across the universe that day, market not broken."""
    day = ctx['bars'][i]['date']
    return ctx['symbol'] in ctx['losers_by_date'].get(day, ()) and _spy_ok(ctx, i) and _liquid(ctx, i)


def breakout_entry(ctx, i):
    c = ctx['closes']
    hi = highest(c, 20, i - 1) if i >= 21 else None
    s50 = sma(c, 50, i)
    return hi is not None and s50 is not None and c[i] > hi and c[i] > s50 and _spy_ok(ctx, i) and _liquid(ctx, i)


def below_10d_low(ctx, i, entry):
    lo = lowest(ctx['closes'], 10, i - 1) if i >= 11 else None
    return lo is not None and ctx['closes'][i] < lo


def near_high_entry(ctx, i):
    c = ctx['closes']
    hi = highest(c, 100, i)
    s50 = sma(c, 50, i)
    return (hi is not None and s50 is not None and c[i] >= 0.98 * hi and c[i] > s50
            and _spy_ok(ctx, i) and _liquid(ctx, i))


def below_sma20(ctx, i, entry):
    s20 = sma(ctx['closes'], 20, i)
    return s20 is not None and ctx['closes'][i] < s20


def trend_pullback_entry(ctx, i):
    c, b = ctx['closes'], ctx['bars']
    s20, s50, s200 = sma(c, 20, i), sma(c, 50, i), sma(c, 100, i)
    if None in (s20, s50, s200) or i < 1:
        return False
    return (s50 > s200 and c[i] > s50 and b[i]['low'] <= s20 * 1.005 and c[i] > s20 and c[i] > c[i - 1]
            and _spy_ok(ctx, i) and _liquid(ctx, i))


def momentum_leader_entry(ctx, i):
    """Top 20% 60-day return across the universe that day, in an uptrend, market healthy (cross-sectional)."""
    c = ctx['closes']
    s50 = sma(c, 50, i)
    day = ctx['bars'][i]['date']
    return (s50 is not None and c[i] > s50 and ctx['symbol'] in ctx.get('leaders_by_date', {}).get(day, ())
            and _spy_ok(ctx, i, 100) and _liquid(ctx, i))


def below_sma50(ctx, i, entry):
    s50 = sma(ctx['closes'], 50, i)
    return s50 is not None and ctx['closes'][i] < s50


def sma50_reclaim_entry(ctx, i):
    """Trend start: close crosses back above the 50-day while the 50-day is above the 100-day."""
    c = ctx['closes']
    s50, s50p, s100 = sma(c, 50, i), sma(c, 50, i - 1), sma(c, 100, i)
    if None in (s50, s50p, s100):
        return False
    return c[i] > s50 and c[i - 1] <= s50p and s50 > s100 and _spy_ok(ctx, i) and _liquid(ctx, i)


def relative_strength_entry(ctx, i):
    """Beating SPY by 5%+ over 20 sessions, within 5% of the 50-day high, market healthy."""
    c = ctx['closes']
    if i < 50 or not ctx.get('spy_series'):
        return False
    k = ctx['spy_index'].get(ctx['bars'][i]['date'])
    if k is None or k < 20:
        return False
    spy = ctx['spy_series']
    rs = (c[i] / c[i - 20] - 1) - (spy[k] / spy[k - 20] - 1)
    hi = highest(c, 50, i)
    return rs >= 0.05 and c[i] >= 0.95 * hi and _spy_ok(ctx, i) and _liquid(ctx, i)


ETFS = frozenset('SPY QQQ IWM DIA XLF XLE XLK XLV XLI XLY XLP XLU SMH SOXX KRE XBI GLD SLV TLT'.split())


def etf_trend_entry(ctx, i):
    """Index/sector ETF in an uptrend (above its 100-day, 50-day above 100-day); re-entry after a break."""
    c = ctx['closes']
    s50, s100, s100p = sma(c, 50, i), sma(c, 100, i), sma(c, 100, i - 1)
    if ctx['symbol'] not in ETFS or None in (s50, s100, s100p):
        return False
    return c[i] > s100 and s50 > s100 and _liquid(ctx, i, 5e6)


def below_sma100(ctx, i, entry):
    s100 = sma(ctx['closes'], 100, i)
    return s100 is not None and ctx['closes'][i] < s100


def every_day_entry(ctx, i):
    """Baseline (not tradable advice): buy every liquid day; shows the drift a real edge must beat."""
    return _liquid(ctx, i) and i % 5 == 0


STRATEGIES = (
    Strategy('rsi2_pullback', 'oversold_bounce_in_uptrend',
             'Above 100-day average, 2-day RSI under 10; exit on a close above the 5-day average or 10 days.',
             rsi2_entry, stop_atr=3.0, target_atr=None, max_days=10, exit_signal=close_above_sma5),
    Strategy('ibs_reversal', 'oversold_bounce_in_uptrend',
             'Above 100-day average, closed in the bottom 15% of the day range; exit on a strong close or 5 days.',
             ibs_entry, stop_atr=3.0, target_atr=None, max_days=5, exit_signal=ibs_exit),
    Strategy('three_down_days', 'oversold_bounce_in_uptrend',
             'Three lower closes in an uptrend, SPY above its 50-day; exit on the first up close or 7 days.',
             three_down_entry, stop_atr=3.0, target_atr=None, max_days=7, exit_signal=first_up_close),
    Strategy('weekly_loser_reversal', 'short_term_reversal',
             'Bottom 10% 5-day return across the universe, SPY above its 50-day; hold 5 days.',
             reversal_entry, stop_atr=3.0, target_atr=None, max_days=5, exit_signal=None),
    Strategy('donchian_breakout', 'near_52_week_high',
             '20-day closing high above the 50-day average, SPY healthy; exit below the 10-day low or 30 days.',
             breakout_entry, stop_atr=2.0, target_atr=None, max_days=30, exit_signal=below_10d_low),
    Strategy('near_high_momentum', 'trend_momentum',
             'Within 2% of the 100-day high, above the 50-day; exit on a close under the 20-day or 30 days.',
             near_high_entry, stop_atr=2.5, target_atr=None, max_days=30, exit_signal=below_sma20),
    Strategy('trend_pullback_sma20', 'trend_momentum',
             'Uptrend (50 > 100), dips to the 20-day and closes up; target 2 ATR, stop 1.5 ATR, 10 days.',
             trend_pullback_entry, stop_atr=1.5, target_atr=2.0, max_days=10, exit_signal=None),
    Strategy('momentum_leaders_hold', 'trend_momentum',
             'Top 20% 60-day performers above their 50-day, SPY above its 100-day; hold until a close under the '
             '50-day or 60 days; stop 3 ATR.',
             momentum_leader_entry, stop_atr=3.0, target_atr=None, max_days=60, exit_signal=below_sma50),
    Strategy('sma50_reclaim_trend', 'trend_momentum',
             'Close back above the 50-day while the 50-day is above the 100-day; hold until a close under the '
             '50-day or 60 days; stop 3 ATR.',
             sma50_reclaim_entry, stop_atr=3.0, target_atr=None, max_days=60, exit_signal=below_sma50),
    Strategy('relative_strength_hold', 'trend_momentum',
             'Beating SPY by 5%+ over 20 days and near its 50-day high; hold until a close under the 20-day '
             'or 40 days; stop 3 ATR.',
             relative_strength_entry, stop_atr=3.0, target_atr=None, max_days=40, exit_signal=below_sma20),
    Strategy('etf_trend_hold', 'trend_momentum',
             'Index/sector ETF above its 100-day with the 50-day above the 100-day; hold until a close under the '
             '100-day or 120 days; stop 4 ATR.',
             etf_trend_entry, stop_atr=4.0, target_atr=None, max_days=120, exit_signal=below_sma100),
    Strategy('baseline_hold_20d', 'research_hygiene',
             'Baseline only: buy every 5th liquid day, hold 20 days. Long holds must beat this per day held.',
             every_day_entry, stop_atr=5.0, target_atr=None, max_days=20, exit_signal=None),
    Strategy('baseline_every_5th_day', 'research_hygiene',
             'Baseline only: buy every 5th liquid day, hold 5 days. A strategy must beat this.',
             every_day_entry, stop_atr=3.0, target_atr=None, max_days=5, exit_signal=None),
)
