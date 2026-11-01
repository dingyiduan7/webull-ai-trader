"""Strategy lab: fill model, causality (no look-ahead), promotion gates and an end-to-end synthetic run."""
import random
from datetime import date, timedelta

from autonomous_trader.lab.backtest import (COST_PER_SIDE, Trade, evaluate, run_symbol, stats, todays_signals,
                                            verdict, context)
from autonomous_trader.lab.strategies import STRATEGIES, Strategy


def bars_from(closes, start=date(2025, 1, 2), spread=0.01, volume=5e6):
    out, d = [], start
    for k, c in enumerate(closes):
        o = closes[k - 1] if k else c
        out.append(dict(date=d.isoformat(), open=o, high=max(o, c) * (1 + spread), low=min(o, c) * (1 - spread),
                        close=c, volume=volume))
        d += timedelta(days=1)
    return out


def walk(n=250, seed=1, drift=0.0005, vol=0.02, start=100.0):
    rng, c, out = random.Random(seed), start, []
    for _ in range(n):
        c *= 1 + drift + rng.gauss(0, vol)
        out.append(round(c, 4))
    return out


def always(ctx, i):
    return i == 20


def test_entry_is_next_open_and_costs_are_charged():
    bars = bars_from([100.0] * 40)
    s = Strategy('t', 'x', 's', always, stop_atr=50, target_atr=None, max_days=3, exit_signal=None)
    (t,) = run_symbol(s, context('X', bars))
    assert t.entry_date == bars[21]['date'] and t.entry == bars[21]['open']
    assert t.days == 3 and t.reason == 'TIME'
    assert abs(t.ret - ((1 - COST_PER_SIDE) / (1 + COST_PER_SIDE) - 1)) < 1e-12   # flat price: only costs


def test_gap_below_stop_fills_at_open_not_stop():
    closes = [100.0] * 25 + [60.0] * 10
    bars = bars_from(closes, spread=0.001)
    bars[25]['open'] = 60.0
    s = Strategy('t', 'x', 's', always, stop_atr=1.0, target_atr=None, max_days=10, exit_signal=None)
    (t,) = run_symbol(s, context('X', bars))
    assert t.reason == 'STOP' and t.exit == 60.0


def test_signal_never_uses_future_bars():
    closes = walk(250, seed=3)
    base = bars_from(closes)
    future = [dict(b) for b in base]
    for b in future[150:]:
        b['close'] *= 3;b['high'] *= 3;b['open'] *= 3;b['low'] *= 3
    for strategy in STRATEGIES:
        a = context('X', base);b = context('X', future)
        for i in range(0, 149):
            assert strategy.entry(a, i) == strategy.entry(b, i), (strategy.id, i)


def test_verdict_gates():
    good = dict(trades=100, avg_return=0.01, return_per_day=0.002, profit_factor=1.6, t_stat=2.5)
    half = dict(trades=50, avg_return=0.008)
    assert verdict(good, half, half, baseline_avg=0.001) == ('PAPER', [])
    stage, why = verdict(good, half, dict(trades=50, avg_return=-0.001), baseline_avg=0.001)
    assert stage == 'REJECTED' and 'SECOND_HALF_NOT_PROFITABLE' in why
    stage, why = verdict(dict(good, trades=10), half, half, baseline_avg=0.0)
    assert 'TOO_FEW_TRADES' in why
    stage, why = verdict(dict(good, return_per_day=0.0005), half, half, baseline_avg=0.001)
    assert 'DOES_NOT_BEAT_BASELINE' in why


def test_stats_handles_streaks_and_empty():
    assert stats([]) == dict(trades=0)
    t = lambda r, d: Trade('X', 's', d, 100, d, 100 * (1 + r), 1, 'TIME', 0, 0)
    s = stats([t(0.05, '2025-01-01'), t(-0.02, '2025-01-02'), t(-0.01, '2025-01-03'), t(0.03, '2025-01-04')])
    assert s['trades'] == 4 and s['worst_losing_streak'] == 2 and s['win_rate'] == 0.5


def test_end_to_end_synthetic_universe_produces_report_rows_and_signals():
    universe = {('S%02d' % k): bars_from(walk(250, seed=k, drift=0.0008 if k % 2 else -0.0002)) for k in range(30)}
    spy = bars_from(walk(250, seed=99, drift=0.0006, vol=0.01))
    results, trades = evaluate(STRATEGIES, universe, spy)
    assert set(results) == {s.id for s in STRATEGIES}
    assert results['baseline_every_5th_day']['stage'] == 'BASELINE'
    for sid, r in results.items():
        assert r['stage'] in ('PAPER', 'REJECTED', 'BASELINE')
        for t in trades[sid]:
            assert t.entry_date <= t.exit_date and t.days >= 1
    signals = todays_signals(STRATEGIES, universe, spy)
    assert 'baseline_every_5th_day' not in signals
    for hits in signals.values():
        for h in hits:
            assert h['stop'] < h['close']


def reverting(n=250, seed=1, start=100.0):
    """Uptrend with strong short-term mean reversion: dips get bought back (a real, built-in edge)."""
    rng, c, out, prev = random.Random(seed), start, [], 0.0
    for _ in range(n):
        shock = rng.gauss(0, 0.02)
        r = 0.001 + shock - 0.6 * prev
        prev = shock
        c *= 1 + r
        out.append(round(c, 4))
    return out


def test_positive_control_real_edge_is_promoted_to_paper():
    universe = {('R%02d' % k): bars_from(reverting(250, seed=k), spread=0.004) for k in range(60)}
    spy = bars_from(walk(250, seed=7, drift=0.001, vol=0.005))
    results, _ = evaluate(STRATEGIES, universe, spy)
    assert results['rsi2_pullback']['stage'] == 'PAPER', results['rsi2_pullback']['reasons']


def test_lessons_block_fits_the_context_text_limit_and_keeps_newest():
    import json
    from autonomous_trader.decision_api.lessons import render, LESSONS
    text = render()
    assert len(text) <= 2000
    ids = [row['id'] for row in json.loads(text)['lessons']]
    assert ids[:2] == [LESSONS[0]['id'], LESSONS[1]['id']]     # newest lessons always reach the models


def test_costs_rise_for_less_liquid_names():
    from autonomous_trader.lab.backtest import cost_per_side
    liquid = bars_from([100.0] * 30, volume=5e6)      # $500M/day
    mid = bars_from([10.0] * 30, volume=1e7)          # $100M/day
    thin = bars_from([5.0] * 30, volume=5e6)          # $25M/day
    assert cost_per_side(liquid, 25) == 0.001 and cost_per_side(mid, 25) == 0.002 and cost_per_side(thin, 25) == 0.0035
