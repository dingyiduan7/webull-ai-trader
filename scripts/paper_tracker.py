"""Forward paper tracker (owner request 2026-10-23: "sure" to a nightly paper tracker). Paper only: no orders, no
writes anywhere except .local/paper/. Every run replays the FROZEN rules below over sessions from START onward, so
each new day is data none of these rules was tuned on. Missed nights are caught up automatically.

Candidates (frozen 2026-10-24, from the edge-search near misses; see claude/edge-search-protocol-2026-10-22.md):
  A  TQQQ while QQQ closes above its 200-day average, else BIL
  B  QLD, same filter
  C  Insider clusters with a CEO or CFO (2+ insiders buying $10k+ within 10 days), stocks trading $1M-$50M a day:
     buy the session after the filing, hold 60 sessions, up to 20 equal slots
  D  Insider clusters (2+ insiders), stocks trading >= $200M a day: hold 5 sessions, up to 20 equal slots
  E  The trader's AI models: every OPEN proposal (Opus and Astra, placed or not), scored over 20 sessions vs SPY
Benchmark: SPY. Costs: round trip 0.2% (>= $200M a day), 0.4%, 0.7% (>= $5M), 1.5% (>= $1M); ETFs 0.1%.

Set up once:  .venv\\Scripts\\python.exe scripts\\paper_tracker.py --install   (then it runs daily at 4:30 PM)
Webull reads are refused during market hours (they share the trader's key). SEC reads identify themselves with the
contact saved in .local/paper/sec_agent.txt (asked once).
Output: .local/paper/report.md and report.json
"""
from bisect import bisect_left
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import json
import re
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT), str(ROOT / 'src')]
OUT = ROOT / '.local' / 'paper'
START = '2026-10-26'
FORM4_FROM = '2026-10-12'          # clusters need the 10 days before START too
ETFS = ('SPY', 'QQQ', 'TQQQ', 'QLD', 'BIL')
DAILY_INDEX = 'https://www.sec.gov/Archives/edgar/daily-index/{y}/QTR{q}/form.{d}.idx'
ARCHIVE = 'https://www.sec.gov/Archives/'


# ---------- SEC Form 4 (daily) ----------

def sec_agent():
    import os
    path = OUT / 'sec_agent.txt'
    agent = os.environ.get('SEC_USER_AGENT') or (path.read_text(encoding='utf-8').strip() if path.exists() else '')
    if '@' not in agent:
        agent = input('SEC asks for "Name email" to identify this script (saved locally): ').strip()
        if '@' not in agent:
            raise SystemExit('SEC requires a contact email.')
        path.write_text(agent, encoding='utf-8')
    return agent


class Sec:
    def __init__(self, agent):
        import requests
        self.s = requests.Session()
        self.s.headers.update({'User-Agent': agent, 'Accept-Encoding': 'gzip, deflate'})
        self.last = 0.0

    def get(self, url):
        for attempt in range(4):
            wait = 0.12 - (time.monotonic() - self.last)
            if wait > 0:
                time.sleep(wait)
            self.last = time.monotonic()
            try:
                r = self.s.get(url, timeout=(10, 60))
            except Exception:
                time.sleep(3 * (attempt + 1));continue
            if r.status_code == 200:
                return r.text
            if r.status_code in (403, 404):
                return None
            time.sleep(5 * (attempt + 1))
        return None


def _tag(xml, name):
    m = re.search(r'<%s>\s*(?:<value>)?\s*([^<]*?)\s*(?:</value>)?\s*</%s>' % (name, name), xml, re.S)
    return m.group(1).strip() if m else ''


def parse_form4(text):
    """Open-market purchases in one Form 4 submission: [{ticker, owner, title, trans_date, value}]."""
    m = re.search(r'<ownershipDocument>.*?</ownershipDocument>', text or '', re.S)
    if not m:
        return []
    doc = m.group(0)
    ticker = _tag(doc, 'issuerTradingSymbol').upper()
    owner = _tag(doc, 'rptOwnerCik')
    title = _tag(doc, 'officerTitle')
    out = []
    for t in re.findall(r'<nonDerivativeTransaction>.*?</nonDerivativeTransaction>', doc, re.S):
        if _tag(t, 'transactionCode') != 'P' or _tag(t, 'transactionAcquiredDisposedCode') not in ('A', ''):
            continue
        try:
            shares = float(_tag(t, 'transactionShares') or 0); price = float(_tag(t, 'transactionPricePerShare') or 0)
        except ValueError:
            continue
        out.append(dict(ticker=ticker, owner=owner, title=title[:60], trans_date=_tag(t, 'transactionDate')[:10],
                        value=round(shares * price, 2)))
    return out


def form4_day(sec, day):
    """Purchases filed on `day` (cached per day). None if the index is not published (weekend/holiday/today)."""
    cache = OUT / 'form4' / ('%s.json' % day)
    if cache.exists():
        return json.loads(cache.read_text(encoding='utf-8'))
    d = date.fromisoformat(day)
    idx = sec.get(DAILY_INDEX.format(y=d.year, q=(d.month - 1) // 3 + 1, d=d.strftime('%Y%m%d')))
    if idx is None:
        return None
    rows = []
    for line in idx.splitlines():
        if not line.startswith('4 ') or 'edgar/data/' not in line:
            continue
        path = line.split()[-1]
        for p in parse_form4(sec.get(ARCHIVE + path)):
            rows.append(dict(p, filing_date=day))
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(rows), encoding='utf-8')
    return rows


def all_form4(sec, until):
    rows, d = [], date.fromisoformat(FORM4_FROM)
    while d.isoformat() < until:
        if d.weekday() < 5:
            got = form4_day(sec, d.isoformat())
            if got:
                rows += got
            print('form 4 %s: %s purchases' % (d, 'not published' if got is None else len(got)), flush=True)
        d += timedelta(days=1)
    return rows


def clusters(rows, n_min=2, ceo_cfo=False):
    by, events = {}, []
    for r in rows:
        if r['value'] >= 10000 and r['ticker'] and r['trans_date']:
            by.setdefault(r['ticker'], []).append(r)
    for t, rs in by.items():
        rs.sort(key=lambda r: r['filing_date'])
        cooldown = ''
        for i, r in enumerate(rs):
            if r['filing_date'] <= cooldown or r['filing_date'] < START:
                continue
            lo = (date.fromisoformat(r['trans_date']) - timedelta(days=10)).isoformat()
            group = [x for x in rs[:i + 1] if lo <= x['trans_date'] <= r['trans_date']]
            if len({x['owner'] for x in group}) < n_min:
                continue
            if ceo_cfo and not any(any(k in (x['title'] or '').upper() for k in ('CEO', 'CHIEF EXECUTIVE', 'CFO', 'CHIEF FINANCIAL'))
                                   for x in group):
                continue
            events.append((t, r['filing_date']))
            cooldown = (date.fromisoformat(r['filing_date']) + timedelta(days=30)).isoformat()
    return events


# ---------- model decisions (read-only ledger) ----------

def model_proposals():
    """OPEN proposals since START from the trader's evidence: (model, symbol, proposed_at)."""
    try:
        from autonomous_trader.durable.ledger import LocalDatabase
        db = LocalDatabase('postgres', 'mvp_reader', schema='mvp_monitor')
        out, ids = [], {}
        with db.connect() as c:
            c.execute('SET TRANSACTION READ ONLY')
            for at, kind, p in c.execute(
                    "SELECT at,kind,payload FROM events WHERE mode='REAL' AND at >= %s AND kind IN "
                    "('REAL_PAID_DECISION_EVIDENCE','REAL_DUAL_BRAIN_DECISION') ORDER BY at", (START,)).fetchall():
                for q in p.get('context_quotes') or []:
                    ids[q.get('instrument_id')] = q.get('symbol')
                if kind == 'REAL_PAID_DECISION_EVIDENCE' and p.get('mode') == 'ENTRY':
                    d = p.get('decision') or {}
                    if d.get('action') == 'OPEN' and (d.get('open') or {}).get('symbol'):
                        out.append(('opus', d['open']['symbol'], at.isoformat()))
                if kind == 'REAL_DUAL_BRAIN_DECISION' and p.get('mode') == 'ENTRY' and p.get('second_action') == 'OPEN':
                    out.append(('astra', ids.get(p.get('second_instrument')), at.isoformat()))
        return [x for x in out if x[1]], None
    except Exception as exc:
        return [], type(exc).__name__ + ':' + str(exc)[:120]


# ---------- prices ----------

def market_hours(now=None):
    from zoneinfo import ZoneInfo
    t = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo('America/New_York'))
    return t.weekday() < 5 and (8, 45) <= (t.hour, t.minute) < (16, 30)


def bars_for(symbols):
    path = OUT / 'bars.json'
    saved = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    if market_hours():
        print('Market hours: using saved prices only.')
        return saved
    import strategy_lab as L
    series, failed = L.fetch(sorted(set(symbols)))
    for s, b in series.items():
        merged = {x['date']: x for x in saved.get(s, [])}
        merged.update({x['date']: x for x in b})
        saved[s] = sorted(merged.values(), key=lambda x: x['date'])
    path.write_text(json.dumps(saved), encoding='utf-8')
    if failed:
        print('no prices for', [f[0] for f in failed][:20])
    return saved


# ---------- replay ----------

def tier(bars, k):
    dv = sorted(b['close'] * b['volume'] for b in bars[max(0, k - 20):k])
    dv = dv[len(dv) // 2] if dv else 0
    if dv >= 200e6:return 0.002, dv
    if dv >= 50e6:return 0.004, dv
    if dv >= 5e6:return 0.007, dv
    if dv >= 1e6:return 0.015, dv
    return None, dv


def replay_trend(series, fund, base):
    spy = {b['date']: b['close'] for b in series['SPY']}
    days = [d for d in sorted(spy) if d >= START]
    c = {s: {b['date']: b['close'] for b in series[s]} for s in (fund, base, 'BIL')}
    base_days = sorted(c[base])
    eq, held = 1.0, None
    for i in range(len(days) - 1):
        d, n = days[i], days[i + 1]
        k = bisect_left(base_days, d)
        closes = [c[base][x] for x in base_days[max(0, k - 199):k + 1]]
        want = fund if len(closes) == 200 and closes[-1] > sum(closes) / 200 else 'BIL'
        if want != held:
            eq *= 1 - 0.001; held = want
        if d in c[want] and n in c[want]:
            eq *= c[want][n] / c[want][d]
    return eq, (spy[days[-1]] / spy[days[0]] if len(days) > 1 else 1.0), len(days)


def replay_events(series, events, hold, lo, hi, slots=20):
    """Equal-slot portfolio: each event buys at the next session's open, sells at the close `hold` sessions later."""
    trades = []
    for t, filed in events:
        bars = series.get(t)
        if not bars:
            continue
        dates = [b['date'] for b in bars]
        k = bisect_left(dates, filed)
        if k < len(dates) and dates[k] == filed:
            k += 1
        if k >= len(bars) or k < 5:
            continue
        cost, dv = tier(bars, k)
        if cost is None or not lo <= dv < hi:
            continue
        j = min(k + hold - 1, len(bars) - 1)
        trades.append(dict(symbol=t, entry=bars[k]['date'], exit=bars[j]['date'], open=j < k + hold - 1,
                           ret=bars[j]['close'] / bars[k]['open'] - 1 - cost))
    trades.sort(key=lambda x: x['entry'])
    live, taken = [], []
    for tr in trades:
        live = [x for x in live if x['exit'] >= tr['entry']]
        if len(live) < slots:
            live.append(tr); taken.append(tr)
    gain = sum(x['ret'] for x in taken) / slots          # each slot is 1/slots of the account
    return dict(trades=len(taken), open=sum(x['open'] for x in taken), growth=1 + gain, rows=taken[-30:])


def score_models(series, proposals):
    spy = series['SPY']; spy_dates = [b['date'] for b in spy]
    rows = []
    for model, symbol, at in proposals:
        bars = series.get(symbol)
        if not bars:
            continue
        dates = [b['date'] for b in bars]
        k = bisect_left(dates, at[:10]) + 1                # next session's open
        if k >= len(bars):
            continue
        j = min(k + 19, len(bars) - 1)
        a = bisect_left(spy_dates, bars[k]['date']); b = bisect_left(spy_dates, bars[j]['date'])
        if b >= len(spy):
            continue
        r = bars[j]['close'] / bars[k]['open'] - 1 - 0.002
        rs = spy[b]['close'] / spy[a]['open'] - 1
        rows.append(dict(model=model, symbol=symbol, proposed=at[:16], entry=bars[k]['date'], through=bars[j]['date'],
                         complete=j == k + 19, ret=round(r, 4), vs_spy=round(r - rs, 4)))
    return rows


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    sec = Sec(sec_agent())
    today = date.today().isoformat()
    form4 = all_form4(sec, today)
    ev_c = clusters(form4, 2, ceo_cfo=True)
    ev_d = clusters(form4, 2)
    proposals, model_error = model_proposals()
    symbols = set(ETFS) | {t for t, _ in ev_c + ev_d} | {p[1] for p in proposals}
    series = bars_for(symbols)
    if 'SPY' not in series:
        raise SystemExit('No SPY prices yet; run after the close.')
    report = dict(generated_at=datetime.now(timezone.utc).isoformat(), start=START)
    for key, fund, base in (('A', 'TQQQ', 'QQQ'), ('B', 'QLD', 'QQQ')):
        g, s, n = replay_trend(series, fund, base)
        report[key] = dict(growth=round(g, 4), spy=round(s, 4), sessions=n)
    report['C'] = replay_events(series, ev_c, 60, 1e6, 50e6)
    report['D'] = replay_events(series, ev_d, 5, 200e6, float('inf'))
    report['E'] = dict(rows=score_models(series, proposals), error=model_error)
    (OUT / 'report.json').write_text(json.dumps(report, indent=1), encoding='utf-8')
    spy_g = report['A']['spy']
    lines = ['# Paper tracker', '', 'Since %s (%d sessions). Paper only. SPY: %+.2f%%.' % (
        START, report['A']['sessions'], 100 * (spy_g - 1)), '',
             '| Candidate | Result | vs SPY |', '|---|---|---|']
    names = dict(A='TQQQ above 200-day, else T-bills', B='QLD above 200-day, else T-bills',
                 C='Insider clusters with CEO/CFO, small stocks, 60 days', D='Insider clusters, large stocks, 5 days')
    for k in 'ABCD':
        g = report[k]['growth']
        extra = '' if k in 'AB' else ' (%d trades, %d still open)' % (report[k]['trades'], report[k]['open'])
        lines.append('| %s %s | %+.2f%%%s | %+.2f pts |' % (k, names[k], 100 * (g - 1), extra, 100 * (g - spy_g)))
    rows = report['E']['rows']
    done = [r for r in rows if r['complete']]
    lines += ['', '## AI model proposals (20 sessions, vs SPY)', '']
    for model in ('opus', 'astra'):
        mine = [r for r in rows if r['model'] == model]
        fin = [r for r in mine if r['complete']]
        avg = sum(r['vs_spy'] for r in fin) / len(fin) if fin else None
        lines.append('- %s: %d proposals, %d complete, average vs SPY %s' % (
            model, len(mine), len(fin), '—' if avg is None else '%+.2f%%' % (100 * avg)))
    if report['E']['error']:
        lines.append('- (ledger not readable: %s)' % report['E']['error'])
    lines += ['', 'A candidate earns live money only after beating SPY over at least 3 months of these paper days.']
    (OUT / 'report.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print('\n'.join(lines))


TASK = 'AutoTrader-PaperTracker'


def install():
    """Save the SEC contact, register a daily 4:30 PM run (this user, no admin needed), then run once now."""
    import subprocess
    OUT.mkdir(parents=True, exist_ok=True)
    sec_agent()
    wrapper = OUT / 'run_tracker.cmd'
    wrapper.write_text('@echo off\r\ncd /d "%s"\r\n"%s" scripts\\paper_tracker.py >> .local\\paper\\run.log 2>&1\r\n'
                       % (ROOT, sys.executable), encoding='utf-8')
    code = subprocess.call(['schtasks', '/Create', '/F', '/SC', 'DAILY', '/ST', '16:30', '/TN', TASK, '/TR', '"%s"' % wrapper])
    print('Scheduled task %s: %s' % (TASK, 'created (daily 4:30 PM)' if code == 0 else 'NOT created (exit %d)' % code))
    main()


if __name__ == '__main__':
    install() if '--install' in sys.argv else main()
