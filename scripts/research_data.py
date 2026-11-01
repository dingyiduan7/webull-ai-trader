"""Edge-search research data (owner request 2026-10-22; paper research only, never places orders).

    .venv\\Scripts\\python.exe scripts\\research_data.py --sec        # SEC insider purchases + earnings dates
    .venv\\Scripts\\python.exe scripts\\research_data.py --etf-bars --insider-bars   # after hours: ETF and insider-ticker bars (Webull read)

--sec uses only public SEC EDGAR files. The SEC asks every automated client to identify itself with a name and
contact email in the User-Agent; the script asks for it once (or reads SEC_USER_AGENT) and sends it only to sec.gov.
--etf-bars uses the same read-only Webull daily-bar read as the strategy lab and refuses to run during market hours,
so it never competes with the trader for the shared key.

Output: .local/research/insider_purchases.json, earnings_dates.json, etf_bars.json
"""
from datetime import date, datetime, timedelta, timezone
from io import BytesIO, TextIOWrapper
from pathlib import Path
import csv
import json
import os
import sys
import time
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT), str(ROOT / 'src')]
OUT = ROOT / '.local' / 'research'
FORM345 = ('https://www.sec.gov/files/structureddata/data/insider-transactions-data-sets/{y}q{q}_form345.zip',
           'https://www.sec.gov/files/datastandardsinnovation/data/insider-transactions-data-sets/{y}q{q}_form345.zip')
TICKERS = 'https://www.sec.gov/files/company_tickers.json'
SUBMISSIONS = 'https://data.sec.gov/submissions/CIK{cik:010d}.json'
ETFS = ('SPY', 'QQQ', 'SSO', 'UPRO', 'SPXL', 'QLD', 'TQQQ', 'BIL', 'SHV', 'SGOV', 'IEF', 'TLT', 'GLD',
        'XLK', 'XLF', 'XLE', 'XLV', 'XLI', 'XLY', 'XLP', 'XLU', 'XLB', 'XLRE', 'XLC', 'SMH', 'IWM', 'MDY')
CLUSTER_RELATIONS = ('director', 'officer', 'tenpercentowner', 'ten percent owner')


class Sec:
    def __init__(self, agent):
        import requests
        self.s = requests.Session()
        self.s.headers.update({'User-Agent': agent, 'Accept-Encoding': 'gzip, deflate'})
        self.last = 0.0

    def get(self, url, **kw):
        for attempt in range(4):
            wait = 0.15 - (time.monotonic() - self.last)        # SEC fair-access limit: under 10 requests/second
            if wait > 0:
                time.sleep(wait)
            self.last = time.monotonic()
            try:
                r = self.s.get(url, timeout=(10, 120), **kw)
            except Exception:
                time.sleep(3 * (attempt + 1));continue
            if r.status_code == 200:
                return r
            if r.status_code == 404:
                return None
            time.sleep(5 * (attempt + 1))
        return None


def table(z, name):
    """Rows of one TSV inside a Form 3/4/5 data set zip, with upper-cased headers."""
    member = next((n for n in z.namelist() if n.upper().endswith(name.upper())), None)
    if member is None:
        return []
    with z.open(member) as raw:
        reader = csv.DictReader(TextIOWrapper(raw, encoding='utf-8', errors='replace'), delimiter='\t')
        return [{(k or '').upper(): (v or '') for k, v in row.items()} for row in reader]


def parse_form345(blob):
    """Open-market purchases (transaction code P, acquired) with ticker, dates, owner and relationship."""
    z = zipfile.ZipFile(BytesIO(blob))
    subs = {r['ACCESSION_NUMBER']: r for r in table(z, 'SUBMISSION.tsv')}
    owners = {}
    for r in table(z, 'REPORTINGOWNER.tsv'):
        owners.setdefault(r['ACCESSION_NUMBER'], []).append(r)
    out = []
    for t in table(z, 'NONDERIV_TRANS.tsv'):
        if t.get('TRANS_CODE') != 'P' or t.get('TRANS_ACQUIRED_DISP_CD', 'A') != 'A':
            continue
        sub = subs.get(t['ACCESSION_NUMBER'])
        if not sub or not sub.get('ISSUERTRADINGSYMBOL'):
            continue
        try:
            shares = float(t.get('TRANS_SHARES') or 0); price = float(t.get('TRANS_PRICEPERSHARE') or 0)
        except ValueError:
            continue
        own = owners.get(t['ACCESSION_NUMBER'], [{}])[0]
        out.append(dict(ticker=sub['ISSUERTRADINGSYMBOL'].strip().upper(), filing_date=_iso(sub.get('FILING_DATE')),
                        trans_date=_iso(t.get('TRANS_DATE')), owner=own.get('RPTOWNERCIK', ''),
                        relationship=own.get('RPTOWNER_RELATIONSHIP', ''), title=own.get('RPTOWNER_TITLE', '')[:60],
                        shares=shares, price=price, value=round(shares * price, 2)))
    return out


def _iso(text):
    text = (text or '').strip()
    for fmt in ('%d-%b-%Y', '%Y-%m-%d', '%m/%d/%Y'):
        try:
            return datetime.strptime(text[:11], fmt).date().isoformat()
        except ValueError:
            continue
    return text[:10]


def quarters(start_year=2021):
    today = date.today()
    for y in range(start_year, today.year + 1):
        for q in range(1, 5):
            if date(y, 3 * q - 2, 1) <= today:
                yield y, q


def sec_insiders(sec):
    rows = []
    for y, q in quarters():
        r = next((x for x in (sec.get(u.format(y=y, q=q)) for u in FORM345) if x is not None), None)
        if r is None:
            print('insiders %dQ%d: not published' % (y, q), flush=True);continue
        got = parse_form345(r.content)
        rows += got
        print('insiders %dQ%d: %d open-market purchases' % (y, q, len(got)), flush=True)
    return rows


def sec_earnings(sec, symbols):
    """8-K filings with Item 2.02 (results of operations) per symbol: the public earnings-release dates."""
    r = sec.get(TICKERS)
    ciks = {v['ticker'].upper(): int(v['cik_str']) for v in r.json().values()} if r else {}
    out, missing = {}, []
    for k, symbol in enumerate(symbols):
        cik = ciks.get(symbol)
        if cik is None:
            missing.append(symbol);continue
        r = sec.get(SUBMISSIONS.format(cik=cik))
        if r is None:
            missing.append(symbol);continue
        recent = r.json().get('filings', {}).get('recent', {})
        dates = []
        for form, filed, items, accepted in zip(recent.get('form', []), recent.get('filingDate', []),
                                                recent.get('items', []), recent.get('acceptanceDateTime', [])):
            if form == '8-K' and '2.02' in (items or ''):
                dates.append(dict(filed=filed, accepted=accepted))
        out[symbol] = sorted(dates, key=lambda d: d['accepted'])
        if k % 50 == 0:
            print('earnings dates: %d/%d symbols' % (k + 1, len(symbols)), flush=True)
    return out, missing


def _plus(day, n):
    return (date.fromisoformat(day) + timedelta(days=n)).isoformat()


def market_hours(now=None):
    from zoneinfo import ZoneInfo
    t = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo('America/New_York'))
    return t.weekday() < 5 and (8, 45) <= (t.hour, t.minute) < (16, 30)


def etf_bars():
    if market_hours():
        raise SystemExit('Market hours: this read shares the trader\'s Webull key. Run it after 2:30 PM MT.')
    import strategy_lab as L
    series, failed = L.fetch(list(ETFS))
    history = L.backfill(series)
    return dict(series=series, failed=failed, history=history)


def main(argv):
    OUT.mkdir(parents=True, exist_ok=True)
    if '--sec' in argv:
        agent = os.environ.get('SEC_USER_AGENT') or input('SEC asks for "Name email" to identify this script: ').strip()
        if '@' not in agent:
            raise SystemExit('SEC requires a contact email in the User-Agent.')
        sec = Sec(agent)
        from strategy_lab import UNIVERSE
        rows = sec_insiders(sec)
        if not rows:
            raise SystemExit('No insider data downloaded; the SEC file location may have changed.')
        (OUT / 'insider_purchases.json').write_text(json.dumps(rows), encoding='utf-8')
        print('saved %d insider purchases' % len(rows))
        if (OUT / 'earnings_dates.json').exists():
            return main([a for a in argv if a != '--sec'])     # earnings dates already saved
        dates, missing = sec_earnings(sec, list(UNIVERSE))
        (OUT / 'earnings_dates.json').write_text(json.dumps(dict(dates=dates, missing=missing)), encoding='utf-8')
        print('saved earnings dates for %d symbols (%d without SEC filings: ETFs/foreign)' % (len(dates), len(missing)))
    if '--insider-bars' in argv:
        # Daily bars for tickers with a real insider cluster (2+ different insiders buying $10k+ within 10 days), a
        # universe picked by events, not by later performance. Same read-only Webull read as the lab. Works in
        # chunks, saves after each, resumes where it left off, and stops by itself before the market opens.
        rows = json.loads((OUT / 'insider_purchases.json').read_text(encoding='utf-8'))
        by = {}
        for r in rows:
            if r.get('value', 0) >= 10000 and r.get('trans_date'):
                by.setdefault(r['ticker'], []).append((r['trans_date'], r['owner']))
        tickers = []
        for t, xs in by.items():
            if not (t.replace('.', '').isalnum() and len(t) <= 5):
                continue
            xs.sort()
            if any(len({o for d, o in xs if xs[i][0] <= d <= _plus(xs[i][0], 10)}) >= 2 for i in range(len(xs))):
                tickers.append(t)
        tickers.sort()
        path = OUT / 'insider_bars.json'
        saved = json.loads(path.read_text(encoding='utf-8')) if path.exists() else dict(series={}, failed=[])
        done = set(saved['series']) | {f[0] for f in saved.get('failed', [])}
        todo = [t for t in tickers if t not in done]
        print('insider-cluster tickers: %d (%d already saved, %d to fetch)' % (len(tickers), len(done), len(todo)), flush=True)
        import strategy_lab as L
        for k in range(0, len(todo), 100):
            if market_hours():
                print('Stopping before the market opens; run the same command again tonight to finish.')
                break
            series, failed = L.fetch(todo[k:k + 100])
            L.backfill(series)
            saved['series'].update(series); saved['failed'] = saved.get('failed', []) + [list(f) for f in failed]
            path.write_text(json.dumps(saved), encoding='utf-8')
            print('insider bars: %d of %d tickers done' % (min(k + 100, len(todo)), len(todo)), flush=True)
        print('saved insider bars for %d tickers' % len(saved['series']))
    if '--etf-bars' in argv:
        payload = etf_bars()
        (OUT / 'etf_bars.json').write_text(json.dumps(payload), encoding='utf-8')
        print('saved ETF bars for %d symbols; failed %s' % (len(payload['series']), payload['failed']))


if __name__ == '__main__':
    main(sys.argv[1:])
