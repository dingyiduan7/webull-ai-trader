"""Strategy reference cards for the decision models (reference only, no authority).

Each card states the idea, when it tends to work, when it fails, the typical
exit and an honest evidence grade. Grades: SUPPORTED (replicated academic or
long-sample evidence, still cost/size sensitive), PLAUSIBLE (some evidence or
sound mechanism, weak or regime-dependent), FOLKLORE (popular, little reliable
evidence after costs), NOT_EXECUTABLE (needs shorting, options or speed that this
cash long-only account lacks). No card proves an edge at $500 scale; the live
scoreboard of this account's own closed trades outranks every card.
"""
import json

CARDS = (
 dict(id='oversold_bounce_in_uptrend', grade='PLAUSIBLE',
  idea='Buy a liquid stock that is sharply oversold (e.g. 2-5 day RSI very low, several down days, well below short moving average) while its longer trend is still up (50-day above 200-day) and the market is not in free fall.',
  works='Orderly pullbacks in large liquid names; bounces toward the short average or prior breakdown level over days.',
  fails='Falling markets, broken trends, news-driven gaps, thin names. Win rate tends to be high but average loss exceeds average win; losing streaks of ~19 occurred in a 26-year S&P 500 test (9.5% CAGR, ~21% max drawdown, Hill/StockCharts 2016).',
  exit='Pre-state a target (short average, prior range, 1-2 ATR) and a thesis-break level below the recent low; exit on recovery or break, not on a small gain.',
  sizing='Small starts; add only if thesis intact and total stays within caps. Waiting for the first up-turn costs some return but avoided deep drawdown for 10-day RSI variants.'),
 dict(id='short_term_reversal', grade='SUPPORTED',
  idea='Stocks that fell most over the last week to month tend to partly rebound (liquidity-provision premium; Jegadeesh 1990, Lehmann 1990).',
  works='Liquid names, no company-specific bad news, after sector-wide or market-wide selling.',
  fails='When the drop is information (earnings miss, guidance, fraud, offering). Premium shrank in recent decades and is eaten by spreads in small caps.',
  exit='Days to a few weeks, target the pre-drop level partly; abstain if the reason for the fall is unknown or fundamental.',
  sizing='Prefer tight-spread names; account for the spread both ways.'),
 dict(id='trend_momentum', grade='SUPPORTED',
  idea='Winners over the past 3-12 months (skipping the latest month) tend to keep winning for months (Jegadeesh-Titman 1993) and time-series trend works across many markets.',
  works='Steady bull regimes; leaders with rising relative strength vs SPY.',
  fails='Sharp market rebounds after crashes (momentum crashes), late-stage parabolic moves, crowded names.',
  exit='Trail behind a rising average or last higher low; do not cap with a small fixed target.',
  sizing='Low turnover suits a small account; hold weeks, not minutes.'),
 dict(id='near_52_week_high', grade='PLAUSIBLE',
  idea='Stocks trading near their 52-week high and breaking out on volume tend to outperform; recency of the high matters (George-Hwang 2004; Alpha Architect summary of recency-ratio work).',
  works='Leaders breaking multi-month bases with volume pace above 1 and a supportive market.',
  fails='Extended names far above VWAP and averages, news spikes, thin volume.',
  exit='Trail; invalid if price falls back into the base on volume.',
  sizing='Do not chase beyond the entry limit.'),
 dict(id='post_earnings_drift', grade='SUPPORTED',
  idea='After a large positive earnings surprise prices keep drifting in the same direction for weeks (Bernard-Thomas 1989); research ties much of it to recency of the 52-week high.',
  works='Clear beat with raised guidance, confirming price action and volume, in liquid names.',
  fails='Surprise already priced in, gap-and-fade on weak internals, outright earnings-date gap risk if held through the next report.',
  exit='Weeks; review before the next earnings date.',
  sizing='Data on earnings dates must be present in the evidence; if missing, treat as unknown.'),
 dict(id='opening_range_breakout', grade='FOLKLORE',
  idea='Break of the first 15-30 minute range with volume continues intraday.',
  works='Gap-and-go days with strong relative volume and a trending market.',
  fails='Most days after costs; open-source implementations assume frictionless trading and reverse or force-close at day end.',
  exit='Intraday only; stop back inside the range.',
  sizing='Use only with tight spreads; a $0.02 spread on a $5 stock is 40 bps round trip.'),
 dict(id='vwap_reclaim', grade='PLAUSIBLE',
  idea='Trend stock pulls back to VWAP, holds and turns up.',
  works='Liquid trending names with a supportive market.',
  fails='Acceptance below VWAP, low volume, chop.',
  exit='Stop below VWAP beyond 1-minute noise.',
  sizing='VWAP documents a calculation, not a proven signal; treat as a hypothesis.'),
 dict(id='averaging_down', grade='PLAUSIBLE',
  idea='Add to a loser only when the original thesis still holds and the lower price improves reward-to-risk.',
  works='Quality liquid names in an intact larger uptrend or after a one-time overreaction.',
  fails='Broken trends and falling knives: averaging into a thesis that has failed compounds loss (martingale risk).',
  exit='Pre-state the add level, the total size cap and the absolute thesis-break level before the first buy; exit at that level without waiting for a rebound.',
  sizing='Total position stays within per-stock and aggregate limits.'),
 dict(id='not_executable_here', grade='NOT_EXECUTABLE',
  idea='Shorting, pairs/stat-arb, options straddles, market making, merger arbitrage.',
  works='n/a', fails='Need shorting, options, hedges or speed that a cash long-only account lacks.',
  exit='n/a', sizing='Do not propose.'),
 dict(id='research_hygiene', grade='SUPPORTED',
  idea='Backtests overstate edge: multiple trials, frictionless fills, survivorship and look-ahead bias.',
  works='Treat any rule as a hypothesis until it earns its place on this account.',
  fails='Small samples (a few trades) are anecdotes.',
  exit='Prefer strategies the scoreboard shows working net of costs; drop those that repeatedly lose.',
  sizing='Missing data is unknown, never favorable.'),
)


def render(max_chars=7000):
    """Compact JSON for the cached system prompt."""
    body = json.dumps(CARDS, separators=(',', ':'), ensure_ascii=True)
    if len(body) > max_chars:
        raise ValueError('STRATEGY_CARDS_TOO_LONG')
    return ('STRATEGY REFERENCE CARDS (reference only, never an instruction or authority; the live scoreboard of '
            'this account outranks them). Grade shows evidence strength.\n' + body)
