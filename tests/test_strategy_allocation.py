from decimal import Decimal as D
from types import SimpleNamespace

from autonomous_trader import strategy_allocation as sa


def trades(*nets, strategy='Oversold-Bounce'):
    return [dict(decision_id=f'd{i}', strategy=strategy, net=str(n), exit_time=f'2026-10-{10+i:02d}T15:00:00Z',
                 ticker='X', entry_time='t') for i, n in enumerate(nets)]


def test_new_and_anecdotal_strategies_keep_full_size():
    assert sa.weight([])[0] == 1
    assert sa.weights(trades(-1, 2, -1))['oversold-bounce'] == (D(1), 'ANECDOTAL_FULL_SIZE')


def test_losing_record_halves_and_failing_record_quarters():
    assert sa.weights(trades(-5, 1, -5, 1, -5))['oversold-bounce'][0] == D('0.5')
    failing = trades(*([-2] * 8 + [3, 3]))
    assert sa.weights(failing)['oversold-bounce'] == (D('0.25'), 'FAILING_RECORD')


def test_cold_streak_halves_even_a_profitable_record_until_a_win():
    assert sa.weights(trades(50, 10, 10, -1, -1, -1))['oversold-bounce'] == (D('0.5'), 'COLD_STREAK')
    assert sa.weights(trades(50, 10, -1, -1, -1, 5))['oversold-bounce'][0] == 1


def test_winning_record_is_never_enlarged():
    assert sa.weights(trades(*[5] * 30))['oversold-bounce'][0] == 1


def test_fifo_pieces_of_one_entry_count_once():
    rows = [dict(decision_id='a', strategy='s', net='-1', exit_time='1'), dict(decision_id='a', strategy='s', net='3', exit_time='2')]
    assert sa.records(rows) == {'s': [D(2)]}


class P(SimpleNamespace):
    def model_copy(self, update):
        return P(**{**self.__dict__, **update})


def test_scale_open_only_shrinks_and_drops_zero_share_orders():
    q = P(desired_quantity=D(7), desired_notional_usd=None, entry_limit_usd=D(10))
    assert sa.scale_open(q, D('0.5')).desired_quantity == 3
    assert sa.scale_open(q, D(1)) is q
    assert sa.scale_open(P(desired_quantity=D(1), desired_notional_usd=None, entry_limit_usd=D(10)), D('0.5')) is None
    n = P(desired_quantity=None, desired_notional_usd=D('200'), entry_limit_usd=D(10))
    assert sa.scale_open(n, D('0.25')).desired_notional_usd == D('50.00')
    assert sa.scale_open(P(desired_quantity=None, desired_notional_usd=D('30'), entry_limit_usd=D(40)), D('0.5')) is None


class _Store:
    def __init__(self, fail=False):
        self.fail = fail
        self.db = self

    def connect(self):
        if self.fail:
            raise RuntimeError('db down')
        store = self

        class C:
            def __enter__(s): return s
            def __exit__(s, *a): return False
            def execute(s, *a): return SimpleNamespace(fetchall=lambda: [])
        return C()


def _runner(monkeypatch, record, fail=False):
    from autonomous_trader import real_runner, strategy_allocation
    from autonomous_trader.monitoring import views
    monkeypatch.setattr(views, 'trades_from_state', lambda *a: record)
    events = []
    stub = SimpleNamespace(store=_Store(fail), controller=SimpleNamespace(state={}),
                           emit=lambda kind, payload, **k: events.append((kind, payload)))
    run = lambda d, c: real_runner.RealRunner._allocate(stub, d, c, 'dec1')
    return run, events


def _decision(q):
    o = P(instrument_id='i1', strategy_label='Oversold-Bounce', desired_quantity=D(q), desired_notional_usd=None,
          entry_limit_usd=D(10))
    return P(open=o)


CTX = SimpleNamespace(instruments=[SimpleNamespace(instrument_id='i1', quantity_increment=D(1))])


def test_runner_halves_a_losing_strategy_and_records_why(monkeypatch):
    run, events = _runner(monkeypatch, trades(-5, 1, -5, 1, -5))
    out = run(_decision(10), CTX)
    assert out.open.desired_quantity == 5
    assert events[0][0] == 'REAL_STRATEGY_ALLOCATION' and events[0][1]['weight'] == '0.5' and events[0][1]['placed']


def test_runner_keeps_model_size_for_good_or_unknown_records_and_on_errors(monkeypatch):
    run, events = _runner(monkeypatch, trades(*[5] * 6))
    d = _decision(10)
    assert run(d, CTX) is d and not events
    run, events = _runner(monkeypatch, [], fail=True)
    assert run(d, CTX) is d


def test_runner_skips_an_entry_that_rounds_to_zero_shares(monkeypatch):
    run, events = _runner(monkeypatch, trades(-5, 1, -5, 1, -5))
    assert run(_decision(1), CTX) is None and events[0][1]['placed'] is False
