"""Squeeze research data (paper only; no Webull key, no orders). Downloads free public FINRA data for the lab's
universe and saves .local/lab/short_data.json:

- daily short sale volume (Reg SHO, consolidated NMS file per trading day from cdn.finra.org);
- consolidated short interest (twice monthly, api.finra.org public dataset), when reachable without credentials.

Usage (after hours is fine; nothing here touches Webull):
    .venv\\Scripts\\python.exe scripts\\squeeze_data.py            # about 3 years of daily files
    .venv\\Scripts\\python.exe scripts\\squeeze_data.py --days 250
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
import json
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'src')]
OUT = ROOT / '.local' / 'lab' / 'short_data.json'
DAILY_URL = 'https://cdn.finra.org/equity/regsho/daily/CNMSshvol{:%Y%m%d}.txt'
SI_URL = 'https://api.finra.org/data/group/otcMarket/name/consolidatedShortInterest'
SI_FIELDS = ('symbolCode', 'settlementDate', 'currentShortPositionQuantity', 'averageDailyVolumeQuantity',
             'daysToCoverQuantity')


def parse_daily(text, universe):
    """Reg SHO daily file: Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market -> {symbol: (d, short, total)}."""
    out = {}
    for line in text.splitlines()[1:]:
        parts = line.strip().split('|')
        if len(parts) < 5 or parts[1] not in universe:
            continue
        try:
            d = '%s-%s-%s' % (parts[0][:4], parts[0][4:6], parts[0][6:8])
            out[parts[1]] = (d, int(float(parts[2])), int(float(parts[4])))
        except ValueError:
            continue
    return out


def weekdays_back(days, today=None):
    d, out = (today or date.today()) - timedelta(days=1), []
    while len(out) < days:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return sorted(out)


CACHE = ROOT / '.local' / 'lab' / 'regsho'


def fetch_daily(session, day, universe):
    """One day's rows for the universe; cached per day so an interrupted run resumes where it stopped."""
    cached = CACHE / ('%s.json' % day.isoformat())
    if cached.exists():
        try:
            return {k: tuple(v) for k, v in json.loads(cached.read_text(encoding='utf-8')).items()}
        except ValueError:
            pass
    for attempt in range(3):
        try:
            r = session.get(DAILY_URL.format(day), timeout=(10, 20), stream=True)
            if r.status_code in (403, 404):
                r.close()
                got = {}                        # holiday / not published
            elif r.status_code == 200:
                started, chunks = time.monotonic(), []
                for chunk in r.iter_content(262144):
                    chunks.append(chunk)
                    if time.monotonic() - started > 60:
                        raise TimeoutError('slow download')
                got = parse_daily(b''.join(chunks).decode('utf-8', 'replace'), universe)
            else:
                r.close()
                raise ValueError('HTTP %s' % r.status_code)
            CACHE.mkdir(parents=True, exist_ok=True)
            cached.write_text(json.dumps(got), encoding='utf-8')
            return got
        except Exception:
            time.sleep(2 * (attempt + 1))
    return None


def fetch_short_interest(session, universe, start):
    """Public consolidated short interest for the universe since start; None if the dataset needs credentials."""
    rows, symbols = [], sorted(universe)
    for k in range(0, len(symbols), 100):
        offset = 0
        while True:
            body = dict(fields=list(SI_FIELDS), limit=5000, offset=offset,
                        domainFilters=[dict(fieldName='symbolCode', values=symbols[k:k + 100])],
                        dateRangeFilters=[dict(fieldName='settlementDate', startDate=start.isoformat(),
                                               endDate=date.today().isoformat())])
            r = session.post(SI_URL, json=body, headers={'Accept': 'application/json'}, timeout=60)
            if r.status_code in (401, 403):
                print('short interest: FINRA API needs credentials (HTTP %s); continuing with short volume only'
                      % r.status_code)
                return None
            r.raise_for_status()
            page = r.json()
            rows += page
            if len(page) < 5000:
                break
            offset += 5000
    out = {}
    for row in rows:
        try:
            out.setdefault(row['symbolCode'], []).append(
                (row['settlementDate'][:10], int(float(row['currentShortPositionQuantity'] or 0)),
                 int(float(row.get('averageDailyVolumeQuantity') or 0)), float(row.get('daysToCoverQuantity') or 0)))
        except (KeyError, TypeError, ValueError):
            continue
    for v in out.values():
        v.sort()
    return out


def main(argv):
    import requests
    from strategy_lab import UNIVERSE
    days = int(argv[argv.index('--days') + 1]) if '--days' in argv else 750
    universe = set(UNIVERSE)
    session = requests.Session()
    session.headers['User-Agent'] = 'research script (personal, non-commercial)'
    session.mount('https://', requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=8))
    volume, missing = {}, []
    todo = weekdays_back(days)
    with ThreadPoolExecutor(max_workers=4) as pool:
        for k, (day, got) in enumerate(zip(todo, pool.map(lambda d: fetch_daily(session, d, universe), todo))):
            if got is None:
                missing.append(day.isoformat())
            for symbol, row in (got or {}).items():
                volume.setdefault(symbol, []).append(row)
            if k % 25 == 0 or k == len(todo) - 1:
                print('daily short volume: %d/%d days' % (k + 1, len(todo)), flush=True)
    try:
        interest = fetch_short_interest(session, universe, todo[0] - timedelta(days=30))
    except Exception as exc:
        print('short interest unavailable:', type(exc).__name__, str(exc)[:120])
        interest = None
    payload = dict(generated=date.today().isoformat(), days=days, short_volume=volume, short_interest=interest,
                   missing_days=missing)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload), encoding='utf-8')
    print('saved %s: short volume for %d symbols, short interest for %s symbols, %d days missing'
          % (OUT, len(volume), 'no' if interest is None else len(interest), len(missing)))


if __name__ == '__main__':
    main(sys.argv[1:])
