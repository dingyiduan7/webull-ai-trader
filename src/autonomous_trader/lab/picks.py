"""Lab picks for the trader (owner approval 2026-10-20): "lab-tested momentum picks join the brains' candidate
list". Read-only evidence: a pick only adds a symbol to the candidates the decision models see, with its tested
record; every entry rule, risk limit and the two-model consensus still apply unchanged.

The lab (scripts/strategy_lab.py, run after each close) writes .local/lab/picks.json with today's signals of
strategies at stage PAPER or better. A stale or malformed file yields no picks (never blocks trading)."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import re

ROOT = Path(__file__).resolve().parents[3]
PICKS_FILE = ROOT / '.local' / 'lab' / 'picks.json'
MAX_PICKS = 8                           # symbols checked; at most MAX_IN_SHORTLIST reach the models
MAX_IN_SHORTLIST = 4
MAX_AGE = timedelta(hours=96)          # a weekend plus a holiday; older picks are ignored
SYMBOL = re.compile(r'[A-Z][A-Z0-9.]{0,9}')


def load(now=None, path=None, max_price=None):
    """{symbol: compact note} for at most MAX_PICKS picks, strongest strategy first; {} when unavailable.
    max_price: the account's per-stock affordability ceiling from the last refresh (whole shares); picks whose
    signal close is above it (with 5% room) are skipped so they do not take quote/profile reads."""
    now = now or datetime.now(timezone.utc)
    try:
        payload = json.loads(Path(path or PICKS_FILE).read_text(encoding='utf-8'))
        generated = datetime.fromisoformat(payload['generated_at'])
        if generated.tzinfo is None or not timedelta(0) <= now - generated <= MAX_AGE:
            return {}
        out = {}
        for row in payload['picks']:
            symbol = row.get('symbol')
            if not isinstance(symbol, str) or not SYMBOL.fullmatch(symbol) or symbol in out:
                continue
            if max_price is not None and float(row.get('close') or 0) * 1.05 > float(max_price):
                continue
            out[symbol] = dict(strategy=str(row['strategy'])[:40], stage=str(row['stage'])[:12],
                               tested=str(row['tested'])[:160], exit=str(row['exit'])[:160],
                               signal_close=row.get('close'), suggested_stop=row.get('stop'))
            if len(out) >= MAX_PICKS:
                break
        return out
    except Exception:
        return {}
