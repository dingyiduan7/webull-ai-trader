r"""Squeeze tables (section 4). Usage from the repo root:
    .venv\Scripts\python.exe <this folder>\run_squeeze_sim.py
Requires .local/lab/bars.json and .local/lab/short_data.json."""
import json, random, sys
from pathlib import Path
ROOT = Path.cwd(); sys.path[:0] = [str(Path(__file__).resolve().parent)]   # uses this folder's lab/ copy; never touches src/
from lab.backtest import stats
from lab import squeeze as Q, portfolio as P
series = json.loads((ROOT / '.local/lab/bars.json').read_text())['series']; spy = series['SPY']
stocks = {s: b for s, b in series.items() if s != 'SPY' and len(b) >= 60}
sd = json.loads((ROOT / '.local/lab/short_data.json').read_text()); si = sd['short_interest']
START = '2023-12-06'
def show(name, tr):
    tr = [t for t in tr if t.entry_date >= START]; s = stats(tr); p = Q.squeeze_profile(tr)
    print('%-24s n=%4d avg %+.2f%% doubled %.1f%% ran50 %.1f%% fell15 %.1f%%' % (
        name, s['trades'], 100 * s['avg_return'], 100 * p['ran_100'], 100 * p['ran_50'], 100 * p['fell_15']))
show('control', Q.run_all(stocks)['breakout_control'])
for dtc in (3, 5, 8, 10):
    Q.DTC_MIN = dtc; show('days_to_cover>=%d' % dtc, Q.run_all(stocks, None, si)['squeeze_days_to_cover'])
Q.DTC_MIN = 5
show('short_volume_top_decile', Q.run_all(stocks, sd['short_volume'])['squeeze_short_volume'])
o = Q.run_all(stocks, None, si); spy2 = [b for b in spy if b['date'] >= START]
for name in ('breakout_control', 'squeeze_days_to_cover'):
    P.RULES = (name,); P.size_fraction = lambda s, x: 0.33
    tb = {name: [t for t in o[name] if t.entry_date >= START]}; fin = []
    for seed in range(100):
        rng = random.Random(seed); P.order_key = lambda r: (0, rng.random())
        fin.append(P.simulate(tb, stocks, spy2)['final_equity'])
    fin.sort()
    print('%s 3 slots: median %.0f p10 %.0f p90 %.0f | SPY %.0f' % (name, fin[50], fin[10], fin[90], 500 * spy2[-1]['close'] / spy2[0]['close']))
