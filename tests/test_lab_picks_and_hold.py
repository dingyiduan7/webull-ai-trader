"""Owner approvals 2026-10-20:
- "lab-tested momentum picks join the brains' candidate list, and the lab runs automatically after each close
  with daily paper tracking"
- "extending the maximum hold to 60 trading days for positions that meet the high-upside rule"."""
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT)]


# ---- 60-session hold horizon ----

def test_hold_horizon_is_60_sessions_and_ledger_limits_allow_it(monkeypatch):
    from autonomous_trader import carry
    from autonomous_trader.mvp.config import reviewed_calendar
    from autonomous_trader.durable.models import RealConfiguration
    from autonomous_trader.real_configuration import settlement_days_from
    assert carry.MAX_HOLD_SESSIONS == 60
    monkeypatch.setattr(carry, 'carry_enabled', lambda: True)
    session = reviewed_calendar().session(date(2026, 10, 20))
    horizon = carry.exit_horizon(session)
    later = carry.future_sessions(session.label, 60)
    assert len(later) == 60 and horizon == later[-1].liquidation_at
    days = settlement_days_from(date(2026, 10, 20))
    assert len(days) > 61                              # 60 sessions + settlement are known to the ledger
    RealConfiguration(charter_id='c', policy_hash='p', carry_sessions=60, settlement_days=days, evidence_id='e')
    with pytest.raises(Exception):
        RealConfiguration(charter_id='c', policy_hash='p', carry_sessions=61, settlement_days=days, evidence_id='e')


def test_hold_horizon_is_still_same_day_without_overnight_authority(monkeypatch):
    from autonomous_trader import carry
    from autonomous_trader.mvp.config import reviewed_calendar
    monkeypatch.setattr(carry, 'carry_enabled', lambda: False)
    session = reviewed_calendar().session(date(2026, 10, 20))
    assert carry.exit_horizon(session) == session.liquidation_at


def test_high_upside_rule_unchanged():
    from decimal import Decimal as D
    from autonomous_trader.carry import qualifies_for_carry
    assert qualifies_for_carry(D('100'), D('95'), D('110'))          # +10%, 2x the 5 risk
    assert not qualifies_for_carry(D('100'), D('95'), D('104'))      # under 5% upside
    assert not qualifies_for_carry(D('100'), D('90'), D('115'))      # under 2x reward-to-risk


def test_ledger_horizon_uses_carry_sessions_from_configuration():
    from datetime import time
    from autonomous_trader.durable.projection import exit_horizon
    from autonomous_trader.reference import NEW_YORK
    from autonomous_trader.real_configuration import settlement_days_from
    days = [d.isoformat() for d in settlement_days_from(date(2026, 10, 20))]
    from autonomous_trader.mvp.config import reviewed_calendar
    sess = reviewed_calendar().session(date(2026, 10, 20))
    s = dict(session=dict(label='2026-10-20', opens_at=sess.opens_at.isoformat(), closes_at=sess.closes_at.isoformat()),
             config=dict(carry_sessions=60, settlement_days=days))
    later = [d for d in days if d > '2026-10-20']
    expected = datetime.combine(date.fromisoformat(later[59]), time(16), NEW_YORK)
    assert exit_horizon(s) == expected


# ---- lab picks ----

def write(tmp, picks, at=None):
    path = tmp / 'picks.json'
    path.write_text(json.dumps(dict(generated_at=(at or datetime.now(timezone.utc)).isoformat(), picks=picks)))
    return path


def pick(symbol, close=50.0, strategy='momentum_leaders_hold'):
    return dict(symbol=symbol, strategy=strategy, stage='PAPER', tested='t', exit='e', close=close, stop=close * 0.9)


def test_picks_load_fresh_capped_affordable_and_ignore_bad_rows(tmp_path):
    from autonomous_trader.lab.picks import load, MAX_PICKS
    rows = [pick('BIG', close=400.0), pick('bad symbol'), pick('AAA'), pick('AAA')] + [pick('S%02d' % k) for k in range(20)]
    out = load(path=write(tmp_path, rows), max_price=200)
    assert 'BIG' not in out and 'bad symbol' not in out and list(out)[0] == 'AAA'
    assert len(out) == MAX_PICKS


def test_stale_or_broken_picks_never_block(tmp_path):
    from autonomous_trader.lab.picks import load
    assert load(path=write(tmp_path, [pick('AAA')], at=datetime.now(timezone.utc) - timedelta(days=5))) == {}
    (tmp_path / 'x.json').write_text('{not json')
    assert load(path=tmp_path / 'x.json') == {} and load(path=tmp_path / 'missing.json') == {}


def test_shortlist_keeps_at_most_four_lab_picks_and_scanner_names():
    """Mirror of the RealMarketSource shortlist rule (held first, <=4 lab picks, then broker rank)."""
    from autonomous_trader.lab.picks import MAX_IN_SHORTLIST
    from autonomous_trader.real_triggers import MAX_SHORTLIST
    assert MAX_IN_SHORTLIST == 4 and MAX_SHORTLIST == 8
    src = (ROOT / 'src/autonomous_trader/real_market.py').read_text(encoding='utf-8')
    assert "lab_items=[item for item in items if item.symbol not in held and item.symbol in lab][:MAX_IN_SHORTLIST]" in src
    assert "self.lab_picks=load_lab_picks(now,max_price=getattr(self,'last_symbol_cap',None))" in src


# ---- lab after close, paper tracking ----

def test_lab_launches_only_after_a_closed_day():
    import run_real_trader as R
    calls = []
    assert R.launch_lab_after_close('PORTFOLIO_EVALUATION', spawn=calls.append) is False
    assert R.launch_lab_after_close('CLOSED_RECONCILED', spawn=calls.append) is True
    assert calls and calls[0][-2].endswith('strategy_lab.py') and calls[0][-1] == '--after-close'
    def boom(argv):raise OSError('no')
    assert R.launch_lab_after_close('CLOSED_UNRECONCILED', spawn=boom) is False      # never raises


def test_write_picks_and_paper_tracking(tmp_path, monkeypatch):
    import strategy_lab as L
    from autonomous_trader.lab.backtest import Trade
    monkeypatch.setattr(L, 'OUT', tmp_path)
    results = {'momentum_leaders_hold': dict(stage='PAPER', all=dict(trades=100, avg_return=0.02, median_days=19, t_stat=4.0)),
               'rsi2_pullback': dict(stage='REJECTED', all=dict(trades=100, avg_return=-0.01, median_days=3, t_stat=-1.0))}
    signals = {'momentum_leaders_hold': [dict(symbol='AAA', close=10.0, stop=9.0)],
               'rsi2_pullback': [dict(symbol='BBB', close=10.0, stop=9.0)]}
    payload = L.write_picks(results, signals, '2021-10-26', '2026-10-19')
    assert [p['symbol'] for p in payload['picks']] == ['AAA']                     # rejected strategies never feed picks
    assert (tmp_path / 'paper-signals.jsonl').read_text().count('AAA') == 1
    t = lambda d: Trade('AAA', 'momentum_leaders_hold', d, 10, d, 11, 3, 'SIGNAL', 0.1, 0)
    paper = L.paper_tracking(results, {'momentum_leaders_hold': [t('2026-10-19'), t('2026-10-21')]})
    assert paper['momentum_leaders_hold']['stats']['trades'] == 1                  # only entries from PAPER_START


def test_after_close_run_is_once_per_day(tmp_path, monkeypatch):
    import strategy_lab as L
    monkeypatch.setattr(L, 'OUT', tmp_path)
    (tmp_path / 'picks.json').write_text(json.dumps(dict(generated_at=datetime.now(timezone.utc).isoformat(), picks=[])))
    monkeypatch.setattr(L, 'fetch', lambda symbols: pytest.fail('must not fetch again the same day'))
    assert L.main(['--after-close']) is None


def test_discovery_puts_affordable_lab_picks_first_then_broker_rank(monkeypatch):
    from types import SimpleNamespace
    from autonomous_trader.real_market import RealMarketSource
    from autonomous_trader.lab import picks
    from test_real_daily import NOW
    seen = {}
    def fake_load(now, max_price=None):
        seen['max_price'] = max_price
        return {'LABA': {}, 'LABB': {}}
    monkeypatch.setattr(picks, 'load', fake_load)
    rows = [{'symbol': s, 'price': '1'} for s in ('SCNA', 'LABB', 'SCNB')]
    source = object.__new__(RealMarketSource)
    source.clock = lambda: NOW;source.bar_cache = {};source.last_symbol_cap = 199
    source.http = SimpleNamespace(read=lambda request: rows)
    assert source.discover() == ('LABA', 'LABB', 'SCNA', 'SCNB')
    assert seen['max_price'] == 199 and set(source.lab_picks) == {'LABA', 'LABB'}
