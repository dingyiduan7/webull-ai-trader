r"""200-run portfolio simulation of the v3 rules (section 3). Usage from the repo root:
    .venv\Scripts\python.exe <this folder>\run_portfolio_sim.py
Run from the repo root (D:\\auto trader). Requires .local/lab/bars.json."""
import json, random, sys
from pathlib import Path
ROOT = Path.cwd(); sys.path[:0] = [str(Path(__file__).resolve().parent)]   # uses this folder's lab/ copy; never touches src/
from lab.backtest import evaluate
from lab.strategies import STRATEGIES
from lab import portfolio as P
series = json.loads((ROOT / '.local/lab/bars.json').read_text())['series']; spy = series['SPY']
stocks = {s: b for s, b in series.items() if s != 'SPY' and len(b) >= 60}
results, trades = evaluate([s for s in STRATEGIES if s.id in P.RULES], stocks, spy)
spy_final = 500 * spy[-1]['close'] / spy[0]['close']
finals = []
for seed in range(200):
    rng = random.Random(seed); P.order_key = lambda r: (P.RULES.index(r['strategy']), rng.random())
    finals.append(P.simulate(trades, stocks, spy)['final_equity'])
finals.sort()
print('SPY %.0f | median %.0f p10 %.0f p90 %.0f min %.0f max %.0f | beat SPY %d/200 | lost money %d/200' % (
    spy_final, finals[100], finals[20], finals[180], finals[0], finals[-1],
    sum(f > spy_final for f in finals), sum(f < 500 for f in finals)))
