"""Strategy lab run (owner request 2026-10-19). READ ONLY: fetches Webull daily bars (the same read the trader
already makes), backtests every rule-based strategy with costs, checks stability across two halves, applies
the promotion gates and lists today's signals. Never places, cancels or modifies orders.

Usage:  .venv\\Scripts\\python.exe scripts\\strategy_lab.py            (fetch latest + backfill older + run)
        .venv\\Scripts\\python.exe scripts\\strategy_lab.py --cached   (re-run on the saved bars)
Writes: .local/lab/bars.json, .local/lab/report.json, .local/lab/report.md
"""
from datetime import datetime, timezone
from pathlib import Path
import json
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'src')]

from autonomous_trader.lab.backtest import COST_PER_SIDE, evaluate, stats, todays_signals   # noqa: E402
from autonomous_trader.lab.strategies import STRATEGIES                              # noqa: E402

OUT = ROOT / '.local' / 'lab'
MARKET = 'SPY'
UNIVERSE = (
    # Liquid US large/mid caps and the names this account has traded; ETFs for sector breadth.
    'AAPL MSFT NVDA AMZN GOOGL META TSLA AVGO AMD INTC MU QCOM TXN AMAT LRCX KLAC MRVL ARM SMCI DELL '
    'ORCL CRM ADBE NOW INTU PANW CRWD ZS NET DDOG SNOW PLTR SHOP UBER ABNB DASH COIN HOOD SOFI PYPL '
    'XYZ AFRM RBLX U ROKU NFLX DIS CMCSA T VZ TMUS JPM BAC WFC C GS MS SCHW AXP V MA BLK '
    'XOM CVX COP OXY SLB HAL DVN EOG MPC VLO PSX KMI WMB LNG FCX NEM AA X CLF NUE '
    'BA LMT RTX GE CAT DE HON UPS FDX UNP CSX DAL UAL AAL LUV CCL RCL NCLH MAR HLT '
    'WMT COST TGT HD LOW NKE SBUX MCD CMG LULU KO PEP PG CL KHC MO PM '
    'JNJ PFE MRK ABBV LLY BMY AMGN GILD MRNA BIIB UNH CVS CI HUM ISRG MDT '
    'F GM RIVN LCID NIO XPEV LI BABA JD PDD BIDU TSM ASML SONY '
    'CRWV IONQ RGTI SOUN MSTR MARA RIOT CLSK APP RDDT '
    'QQQ IWM DIA XLF XLE XLK XLV XLI XLY XLP XLU SMH SOXX KRE XBI GLD SLV TLT '
    # Affordable liquid mid/small caps (owner 2026-10-20: "we don't need to look at those [expensive] stocks").
    # Invalid or renamed tickers are retried alone and listed as failed; nothing else depends on this list.
    'SNAP PINS LYFT DKNG CHWY ETSY W CVNA PTON GME AMC NOK SIRI WBD PARA PLUG FCEL BE ENPH SEDG RUN FSLR '
    'CHPT QS LAZR JOBY ACHR RKLB ASTS LUNR PL BBAI AI PATH S OKTA TWLO DOCU ZM PD ESTC GTLB CFLT IOT FROG '
    'CELH ELF HIMS TOST UPST LC NU STNE PAGS XP BEKE TME BILI IQ FUTU TIGR GRAB SE CPNG '
    'KEY RF HBAN FITB CFG ZION ALLY COF SYF '
    'AR RRC EQT CTRA APA OVV SM MTDR NOG BTU CNX VALE RIO MP ALB SQM CC CF MOS NTR '
    'GAP ANF AEO URBN M KSS DG DLTR FIVE BURL ROST TJX BBWI VFC HBI UAA LEVI CROX SKX ONON BIRK '
    'EXPE TRIP JBLU ALK TEVA VTRS BMRN EXAS NVAX CRSP NTLA BEAM RXRX ILMN DXCM '
    'ON MCHP NXPI SWKS QRVO WOLF TER ENTG AMKR SITM ALGM ACLS FORM COHR LITE CIEN VRT SMTC CRUS SYNA '
    'DBX BOX FIVN RNG NCNO ZI KTOS HUT BITF CIFR WULF IREN CORZ BTBT NBIS APLD OPEN RDFN Z RKT UWMC '
    'HOOD NIO GRAB WBA KVUE KMB HPQ HPE INTC CSCO GLW DOW LYB DD EMR WDC STX NTAP PSTG'
).split()
UNIVERSE = tuple(dict.fromkeys(UNIVERSE))   # de-duplicate, keep order


def fetch(symbols):
    from autonomous_trader.real_secrets import load_real_credentials
    from autonomous_trader.real_strategy_broker import RealStrategyHttp
    from autonomous_trader.sandbox.http import build_request, Operation
    http = RealStrategyHttp(load_real_credentials(ROOT))
    series, failed = {}, []
    for start in range(0, len(symbols), 10):
        group = tuple(symbols[start:start + 10])
        for attempt in range(3):
            try:
                request = build_request(Operation.HISTORY_DAILY_BATCH, symbols=group)
                request.set_count('250')
                body = http.read(request)
                break
            except Exception as exc:          # rate limit or transport: wait and retry, never block the run
                body = None
                time.sleep(5 * (attempt + 1))
                last = type(exc).__name__ + ':' + str(exc)[:60]
        if body is None and len(group) > 1:
            # One invalid/renamed ticker (e.g. SQ -> XYZ) rejects the whole batch: retry each symbol alone.
            for single in group:
                part, bad = fetch_one(http, build_request, Operation, single)
                if part:
                    series.update(part)
                else:
                    failed.append((single, bad))
            continue
        if body is None:
            failed += [(s, last) for s in group]
            continue
        series.update(parse(body))
        print('fetched', start + len(group), 'of', len(symbols), flush=True)
    return series, failed


TARGET_BARS = 1250            # about five years of sessions
BACKFILL_PAGES = 4            # older 250-bar pages per run (the saved history keeps growing across runs)


def backfill(series, pages=BACKFILL_PAGES, http=None, sleep=time.sleep):
    """Read older daily bars (lab-only read-only window, owner-approved 2026-10-19) page by page backwards
    from each symbol's earliest saved bar. Stops for a symbol when no older bars come back."""
    from datetime import datetime as dt, timezone as tz
    from autonomous_trader.sandbox.http import build_request, Operation
    if http is None:
        from autonomous_trader.real_secrets import load_real_credentials
        from autonomous_trader.real_strategy_broker import RealStrategyHttp
        http = RealStrategyHttp(load_real_credentials(ROOT))
    status = dict(pages=0, added_bars=0, unsupported=False)
    done = set()
    for page in range(pages):
        need = [s for s in series if s not in done and len(series[s]) < TARGET_BARS]
        if not need:
            break
        for start in range(0, len(need), 10):
            group = tuple(need[start:start + 10])
            earliest = max(series[s][0]['date'] for s in group)
            end_ms = int(dt.fromisoformat(earliest).replace(tzinfo=tz.utc).timestamp() * 1000) - 1
            body = None
            for attempt in range(3):
                try:
                    request = build_request(Operation.HISTORY_DAILY_BATCH, symbols=group)
                    request.set_count('250')
                    request.set_end_time(end_ms)
                    body = http.read_lab_history(request)
                    break
                except Exception as exc:
                    last = type(exc).__name__ + ':' + str(exc)[:60]
                    sleep(5 * (attempt + 1))
            if body is None:
                print('backfill failed', group[0], last, flush=True)
                continue
            older = parse(body)
            for symbol in group:
                before = len(series[symbol]); first = series[symbol][0]['date']
                merged = {b['date']: b for b in older.get(symbol, []) if b['date'] < first}
                if not merged:
                    done.add(symbol);continue
                merged.update({b['date']: b for b in series[symbol]})
                series[symbol] = sorted(merged.values(), key=lambda b: b['date'])
                status['added_bars'] += len(series[symbol]) - before
        status['pages'] = page + 1
        print('backfill page', page + 1, 'bars added so far', status['added_bars'], flush=True)
        if page == 0 and status['added_bars'] == 0:
            status['unsupported'] = True       # Webull ignored the window: say so, do not pretend
            break
    return status


def parse(body):
    out = {}
    for row in body.get('result', []) if isinstance(body, dict) else []:
        bars = []
        for raw in row.get('result') or []:
            try:
                bars.append(dict(date=str(raw['time'])[:10], open=float(raw['open']), high=float(raw['high']),
                                 low=float(raw['low']), close=float(raw['close']), volume=float(raw['volume'])))
            except (KeyError, TypeError, ValueError):
                continue
        bars = sorted({b['date']: b for b in bars if b['open'] > 0 and b['close'] > 0}.values(), key=lambda b: b['date'])
        if bars:
            out[row.get('symbol')] = bars
    return out


def fetch_one(http, build_request, Operation, symbol):
    try:
        request = build_request(Operation.HISTORY_DAILY_BATCH, symbols=(symbol,))
        request.set_count('250')
        return parse(http.read(request)), None
    except Exception as exc:
        return None, type(exc).__name__ + ':' + str(exc)[:60]


def pct(x):
    return '—' if x is None else '%+.2f%%' % (100 * x)


PAPER_START = '2026-10-20'     # strategies were fixed on 2026-10-19: trades entered from here on are out-of-sample


def paper_tracking(results, trades_by):
    """Forward (paper) record: trades each PAPER strategy would have entered from PAPER_START, no money."""
    out = {}
    for sid, r in results.items():
        if r['stage'] != 'PAPER':
            continue
        trades = [t for t in trades_by.get(sid, []) if t.entry_date >= PAPER_START]
        rows = [dict(symbol=t.symbol, entry_date=t.entry_date, entry=round(t.entry, 4), exit_date=t.exit_date,
                     exit=round(t.exit, 4), reason=t.reason, ret=round(t.ret, 5)) for t in trades]
        out[sid] = dict(stats=stats(trades), trades=rows[-50:])
    return out


def write_picks(results, signals, first_bar_date, last_bar_date):
    """Today's signals of PAPER-or-better strategies for the trader's candidate list (lab/picks.py)."""
    order = sorted((sid for sid, r in results.items() if r['stage'] in ('PAPER', 'SMALL_LIVE', 'LIVE')),
                   key=lambda sid: -(results[sid]['all'].get('t_stat') or 0))
    picks, seen = [], set()
    for sid in order:
        a, strat = results[sid]['all'], next(x for x in STRATEGIES if x.id == sid)
        tested = '%d trades %s..%s, %+.2f%%/trade after costs, median %s days, both halves positive' % (
            a['trades'], first_bar_date, last_bar_date, 100 * a['avg_return'], a['median_days'])
        for hit in signals.get(sid, []):
            if hit['symbol'] in seen:
                continue
            seen.add(hit['symbol'])
            picks.append(dict(symbol=hit['symbol'], strategy=sid, stage=results[sid]['stage'], tested=tested,
                              exit=strat.summary[:160], close=hit['close'], stop=hit['stop']))
    payload = dict(generated_at=datetime.now(timezone.utc).isoformat(), last_bar_date=last_bar_date, picks=picks)
    (OUT / 'picks.json').write_text(json.dumps(payload, indent=1), encoding='utf-8')
    with open(OUT / 'paper-signals.jsonl', 'a', encoding='utf-8') as log:   # daily paper ledger (append-only)
        for pick in picks:
            log.write(json.dumps(dict(pick, logged_at=payload['generated_at'], signal_date=last_bar_date)) + '\n')
    return payload



def report_md(results, signals, meta, paper=None):
    lines = ['# Strategy lab report', '',
             'Generated %s · %d symbols · %s to %s · cost %.2f%% per side · READ ONLY' % (
                 meta['generated_at'], meta['symbols'], meta['first_date'], meta['last_date'], 100 * COST_PER_SIDE), '',
             '| Strategy | Stage | Trades | Win rate | Avg / trade | Per day held | Profit factor | t | Median days | 1st half | 2nd half |',
             '|---|---|---|---|---|---|---|---|---|---|---|']
    order = sorted(results.items(), key=lambda kv: (kv[1]['stage'] != 'PAPER', -(kv[1]['all'].get('avg_return') or -9)))
    for sid, r in order:
        a, f, s = r['all'], r['first_half'], r['second_half']
        if not a.get('trades'):
            lines.append('| %s | %s | 0 | | | | | | | | |' % (sid, r['stage']));continue
        lines.append('| %s | %s | %d | %.0f%% | %s | %s | %s | %s | %s | %s (%d) | %s (%d) |' % (
            sid, r['stage'], a['trades'], 100 * a['win_rate'], pct(a['avg_return']), pct(a.get('return_per_day')),
            '—' if a['profit_factor'] is None else '%.2f' % a['profit_factor'],
            '—' if a['t_stat'] is None else '%.1f' % a['t_stat'], a['median_days'],
            pct(f.get('avg_return')), f.get('trades', 0), pct(s.get('avg_return')), s.get('trades', 0)))
    lines += ['', 'Rejection reasons:', '']
    for sid, r in order:
        if r['reasons']:
            lines.append('- %s: %s' % (sid, ', '.join(r['reasons'])))
    lines += ['', 'Paper tracking (entries since %s, out-of-sample, no money):' % PAPER_START, '']
    for sid, row in (paper or {}).items():
        st = row['stats']
        lines.append('- %s: %s' % (sid, 'no completed paper trades yet' if not st.get('trades') else
                     '%d trades, %.0f%% wins, %s per trade' % (st['trades'], 100 * st['win_rate'], pct(st['avg_return']))))
    lines += ['', "Today's signals (entry at the next open):", '']
    for sid, hits in signals.items():
        stage = results[sid]['stage']
        lines.append('- %s [%s]: %s' % (sid, stage, ', '.join(h['symbol'] for h in hits) or 'none'))
    lines.insert(3, 'History: %s SPY sessions; backfill %s' % (meta.get('spy_bars'), meta.get('history')))
    lines += ['', 'Caveat: limited daily history. PAPER means the evidence is worth '
              'tracking live without money, not that it will work. Live size needs live results on this account.']
    return '\n'.join(lines) + '\n'


def main(argv):
    OUT.mkdir(parents=True, exist_ok=True)
    if '--after-close' in argv:
        # Started by the trader after the close: once per day is enough (a restarted child may close twice).
        try:
            last = datetime.fromisoformat(json.loads((OUT / 'picks.json').read_text(encoding='utf-8'))['generated_at'])
            if (datetime.now(timezone.utc) - last).total_seconds() < 6 * 3600:
                return
        except Exception:
            pass
    cache = OUT / 'bars.json'
    if '--cached' in argv and cache.exists():
        payload = json.loads(cache.read_text(encoding='utf-8'));series, failed = payload['series'], payload['failed']
    else:
        series, failed = fetch([MARKET] + [s for s in UNIVERSE if s != MARKET])
        if cache.exists():             # history grows: keep older saved bars, newer reads win per date
            try:
                old = json.loads(cache.read_text(encoding='utf-8'))['series']
                for symbol, bars in old.items():
                    merged = {b['date']: b for b in bars}
                    merged.update({b['date']: b for b in series.get(symbol, [])})
                    series[symbol] = sorted(merged.values(), key=lambda b: b['date'])
            except Exception:
                pass
        cache.write_text(json.dumps(dict(series=series, failed=failed)), encoding='utf-8')
    history = dict(pages=0, added_bars=0, unsupported=None)
    if '--no-backfill' not in argv:
        try:
            history = backfill(series)
            cache.write_text(json.dumps(dict(series=series, failed=failed)), encoding='utf-8')
        except Exception as exc:
            history = dict(error=type(exc).__name__ + ':' + str(exc)[:80])
    spy = series.get(MARKET)
    if not spy:
        raise SystemExit('LAB_NEEDS_SPY_BARS (fetch failed: %s)' % failed[:3])
    stocks = {s: b for s, b in series.items() if s != MARKET and len(b) >= 60}
    results, trades_by = evaluate(STRATEGIES, stocks, spy)
    signals = todays_signals(STRATEGIES, stocks, spy)
    paper = paper_tracking(results, trades_by)
    write_picks(results, signals, spy[0]['date'], spy[-1]['date'])
    dates = sorted({b['date'] for b in spy})
    meta = dict(generated_at=datetime.now(timezone.utc).isoformat(timespec='seconds'), symbols=len(stocks),
                first_date=dates[0], last_date=dates[-1], failed=failed, cost_per_side=COST_PER_SIDE,
                history=history, spy_bars=len(spy))
    (OUT / 'report.json').write_text(json.dumps(dict(meta=meta, results=results, signals=signals, paper=paper), indent=1), encoding='utf-8')
    text = report_md(results, signals, meta, paper)
    (OUT / 'report.md').write_text(text, encoding='utf-8')
    print(text)
    print('saved', OUT / 'report.md')


if __name__ == '__main__':
    main(sys.argv[1:])
