from datetime import date
from decimal import Decimal as D

from autonomous_trader import scorecard as sc


def T(i, net, exit, strategy='oversold-bounce'):
    return dict(decision_id=f'd{i}', ticker='X', entry_time='t', exit_time=exit, net=D(str(net)), strategy=strategy)


def build(monkeypatch, trades, daily, costs):
    monkeypatch.setattr(sc, 'trades_from_state', lambda *a: trades)
    return sc.weekly({'daily_realized': daily}, {}, costs)


def test_weekly_net_subtracts_model_cost_and_groups_by_strategy(monkeypatch):
    trades = [T(1, 12, '2026-10-13T15:00:00Z'), T(2, -4, '2026-10-14T15:00:00Z', 'breakout'),
              T(3, 6, '2026-10-20T15:00:00Z')]
    weeks, _ = build(monkeypatch, trades, {'2026-10-13': '12', '2026-10-14': '-4', '2026-10-20': '6'},
                     {date(2026, 10, 12): D('3'), date(2026, 10, 19): D('1')})
    assert [w['week'] for w in weeks] == ['2026-10-12', '2026-10-19']
    assert weeks[0]['realized'] == 8 and weeks[0]['net'] == 5 and weeks[0]['trades'] == 2 and weeks[0]['wins'] == 1
    assert weeks[0]['by_strategy']['breakout']['net'] == -4
    assert weeks[1]['net'] == 5


def test_week_with_only_model_cost_is_a_loss(monkeypatch):
    weeks, _ = build(monkeypatch, [], {}, {date(2026, 10, 12): D('2.5')})
    assert weeks[0]['net'] == D('-2.5') and weeks[0]['trades'] == 0


def test_risk_tiers_tighten_as_equity_grows():
    assert sc.tier_risk(D(500)) == D('0.10') and sc.tier_risk(D(2500)) == D('0.05')
    assert sc.tier_risk(D(20000)) == D('0.03') and sc.tier_risk(D(80000)) == D('0.02')
    notes = sc.recommendation([], [], D(2500), D('0.10'))
    assert any(n.startswith('TIGHTEN_RISK') for n in notes)
    assert not any(n.startswith('TIGHTEN_RISK') for n in sc.recommendation([], [], D(500), D('0.10')))


def test_add_capital_needs_four_profitable_weeks_and_ten_trades():
    week = lambda n, net: dict(week='w', trades=n, wins=n, realized=D(net), model_cost=D(0), net=D(net), by_strategy={})
    good = [week(3, 10)] * 4
    assert any(n.startswith('ADD_CAPITAL_CANDIDATE') for n in sc.recommendation(good, [], D(500), D('0.10')))
    assert any(n.startswith('KEEP_CAPITAL') for n in sc.recommendation(good[:3], [], D(500), D('0.10')))
    assert any(n.startswith('KEEP_CAPITAL') for n in sc.recommendation([week(2, 10)] * 4, [], D(500), D('0.10')))
    assert any(n.startswith('KEEP_CAPITAL') for n in sc.recommendation(good + [week(3, -1)], [], D(500), D('0.10')))


def test_render_mentions_reduced_sizes_and_total(monkeypatch):
    trades = [T(i, -3, f'2026-10-1{i}T15:00:00Z') for i in range(5)]
    weeks, trades = build(monkeypatch, trades, {f'2026-10-1{i}': '-3' for i in range(5)}, {})
    text = sc.render(weeks, sc.recommendation(weeks, trades, D(480), D('0.10')), D(480))
    assert 'SIZE_REDUCED: oversold-bounce at 50% size' in text and 'TOTAL NET AFTER MODEL COST: -15.00' in text


def test_script_reads_only_and_writes_latest(tmp_path, monkeypatch):
    import importlib.util, pathlib
    from datetime import datetime, timezone
    from autonomous_trader.durable.projection import digest
    spec = importlib.util.spec_from_file_location('weekly_scorecard', pathlib.Path(__file__).parents[1] / 'scripts' / 'weekly_scorecard.py')
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    state = {'daily_realized': {'2026-10-13': '7.50'}, 'cash': {'liquidation_equity_usd': '507.50'},
             'executions': {}, 'orders': {}, 'instruments': {}}
    seen = []

    class R:
        def __init__(self, rows): self.rows = rows
        def fetchone(self): return self.rows[0] if self.rows else None
        def fetchall(self): return self.rows

    class C:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, q, args=None):
            text = q.as_string(None) if hasattr(q, 'as_string') else q
            seen.append(text)
            assert not any(w in text.upper() for w in ('INSERT', 'UPDATE', 'DELETE'))
            if 'FROM runtime' in text: return R([({'ledger_schema': 'real_trader', 'account_id': 'a1'},)])
            if 'accounts' in text: return R([(state, digest(state))])
            return R([])

    class DB:
        def connect(self): return C()
    monkeypatch.setattr('autonomous_trader.economic_policy.effective_policy', lambda s: type('P', (), {'trade_risk': D('0.10')})())
    assert mod.main(db=DB(), now=datetime(2026, 10, 16, 20, tzinfo=timezone.utc), out_dir=tmp_path) == 0
    text = (tmp_path / 'latest.txt').read_text()
    assert '2026-10-12' in text and 'TOTAL NET AFTER MODEL COST: 7.50' in text
    assert seen[0] == 'SET TRANSACTION READ ONLY'


def test_decisions_show_reason_for_every_pass_and_astra_verdict():
    ev = [dict(decision_snapshot_at='2026-10-16T14:05:00+00:00', action='ABSTAIN', selected_symbol=None, model_cost_usd='0.12',
               candidates=[dict(symbol='AAA'), dict(symbol='BBB')], stated_reason='AAA spread too wide; BBB no base.'),
          dict(decision_snapshot_at='2026-10-16T15:00:00+00:00', action='OPEN', selected_symbol='BBB', model_cost_usd='0.15',
               candidates=[dict(symbol='BBB')], stated_reason='BBB reclaimed VWAP on volume.')]
    dual = [dict(primary_action='OPEN', primary_instrument='i2', second_action='ABSTAIN', second_instrument=None,
                 consensus_action='ABSTAIN', second_reason='Volume pace below 1.', second_error=None)]
    text = sc.render_decisions(ev, dual, [dict(strategy='vwap-reclaim', weight='0.5', reason='COLD_STREAK', placed=True)])
    assert 'ABSTAIN' in text and 'why: AAA spread too wide' in text and '[AAA,BBB]' in text
    assert 'Astra ABSTAIN' in text and 'Astra why: Volume pace below 1.' in text and 'SIZE vwap-reclaim: x0.5' in text
