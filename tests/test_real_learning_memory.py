"""Learning memory: per-strategy scoreboard from realized trades."""
import json
from decimal import Decimal as D

from autonomous_trader.real_feedback import strategy_scoreboard


def trade(decision, strategy, net, qty='10', price='10', exit_time='2026-10-13T15:00:00Z'):
    return dict(decision_id=decision, strategy=strategy, net=D(net), quantity=D(qty), entry_price=D(price),
                ticker='ABC', entry_time='2026-10-13T14:00:00Z', exit_time=exit_time)


def test_fifo_pieces_merge_per_entry_and_group_by_label():
    value = json.loads(strategy_scoreboard([
        trade('p1', 'VWAP Reclaim', '1.50', qty='5'), trade('p1', 'vwap reclaim', '0.50', qty='5'),
        trade('p2', 'vwap reclaim', '-1.00'), trade('p3', 'ORB', '3.00'), trade('p4', None, '-0.25')]))
    rows = {r['strategy']: r for r in value['strategies']}
    assert rows['vwap reclaim']['trades'] == 2 and rows['vwap reclaim']['wins'] == 1
    assert rows['vwap reclaim']['net_after_broker_fees'] == '1.00'
    assert rows['vwap reclaim']['return_on_capital_bps'] == 50
    assert rows['orb']['confidence'] == 'anecdotal' and rows['unlabeled']['trades'] == 1


def test_confidence_grows_with_sample_size_and_output_is_compact():
    trades = [trade('p%d' % i, 'orb', '1') for i in range(25)]
    value = json.loads(strategy_scoreboard(trades))
    assert value['strategies'][0]['confidence'] == 'established'
    assert len(strategy_scoreboard([trade('p%d' % i, 's%d' % i, '1') for i in range(40)])) < 2000
    assert json.loads(strategy_scoreboard([]))['strategies'] == []
