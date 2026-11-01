"""Sequential positions sharing an episode must have independent economics."""
from datetime import datetime,timezone
from decimal import Decimal as D
from types import SimpleNamespace
import json

from autonomous_trader.mvp.alpha import AlphaPortfolioManager,completed_alpha_trade
from autonomous_trader.monitoring.store import corrected_outcomes,compact_performance_memory
from autonomous_trader.monitoring.views import trades_from_state
from scripts.correct_alpha_outcome_fees import corrected_payload
from scripts.correct_alpha_outcome_metrics import corrected_metrics


NOW=datetime(2026,9,21,18,tzinfo=timezone.utc)


def _state():
    state={'positions':{},'instruments':{'stock':{'ticker':'AAA'}},'orders':{},'executions':{}}
    def order(oid,plan,side):
        state['orders'][oid]={'intent':{'plan_id':plan,'episode_id':'alpha-stock',
            'instrument_id':'stock','side':side}}
    def fill(xid,oid,quantity,price,fee,sequence):
        state['executions'][xid]={'intent_id':oid,'quantity':str(quantity),'price_usd':str(price),
            'fee_usd':str(fee),'executed_at':NOW.isoformat(),'sequence':sequence}
    order('buy-1','plan-1','BUY');fill('b1','buy-1',1,10,0,1)
    order('sell-1','plan-1','SELL_OWNED');fill('s1','sell-1',1,'9.9','.1',2)
    order('cancel-2','plan-2','BUY')  # Unfilled attempt has no realized fee.
    order('buy-2','plan-2','BUY');fill('b2a','buy-2',1,10,0,3);fill('b2b','buy-2',1,10,0,4)
    order('sell-2a','plan-2','SELL_OWNED');fill('s2a','sell-2a',1,'9.9','.2',5)
    order('sell-2b','plan-2','SELL_OWNED');fill('s2b','sell-2b',1,'9.8','.3',6)
    return state


def _plan(plan_id):
    return SimpleNamespace(plan_id=plan_id,instrument_id='stock',episode_id='alpha-stock',symbol='AAA',
        strategy_label='exploratory',thesis='Supplied evidence',market_regime='TEST',
        initial_model_cost_usd=D('.05'),shadow_audit_id=None)


def test_sequential_episode_positions_have_independent_fees_and_partial_fills():
    state=_state();events=[]
    manager=AlphaPortfolioManager.__new__(AlphaPortfolioManager)
    manager.metrics={};manager.store=SimpleNamespace();manager.emit=lambda k,p:events.append((k,p))
    for pid in ('plan-1','plan-2'):
        manager._outcome(_plan(pid),{}, {},NOW,state)
    a,b=[p for k,p in events if k=='ALPHA_OUTCOME']
    assert [D(x['broker_fees_usd']) for x in (a,b)]==[D('.1'),D('.5')]
    assert [D(x['realized_net_after_broker_fees_usd']) for x in (a,b)]==[D('-.2'),D('-.8')]
    assert b['quantity']=='2' and D(b['realized_gross_usd'])==D('-.3')
    fifo=trades_from_state(state,{})
    assert sum((x['net'] for x in fifo),D(0))==sum((D(x['realized_net_after_broker_fees_usd']) for x in (a,b)),D(0))
    assert completed_alpha_trade(state,'plan-2','stock','alpha-stock')['broker_fees_usd']==D('.5')


def test_append_only_correction_replaces_learning_projection_once():
    state=_state();events=[]
    manager=AlphaPortfolioManager.__new__(AlphaPortfolioManager)
    manager.metrics={};manager.store=SimpleNamespace();manager.emit=lambda k,p:events.append(p)
    manager._outcome(_plan('plan-1'),{}, {},NOW,state)
    manager._outcome(_plan('plan-2'),{}, {},NOW,state)
    first,correct=events;wrong=dict(correct)
    wrong.update(broker_fees_usd='.6',realized_net_after_broker_fees_usd='-.9',
        realized_net_after_all_known_costs_usd='-.95')
    assert corrected_payload(wrong,state)==correct
    source=[('event-1',first),('event-2',wrong)]
    correction={'original_event_id':'event-2','replacement':correct}
    effective=corrected_outcomes(source,[correction])
    assert len(effective)==2 and effective[-1]==correct and source[-1][1]==wrong
    legacy=[{'plan_id':p['plan_id'],'closed_at':(NOW.replace(second=59)).isoformat(),
        'strategy_label':p['strategy_label'],'market_regime':p['market_regime'],
        'realized_net_after_broker_fees_usd':p['realized_net_after_broker_fees_usd'],
        'realized_net_after_all_known_costs_usd':p['realized_net_after_broker_fees_usd'],
        'evidence_quality':'LEDGER_LEGACY_NO_EXCURSION'} for p in effective]
    memory=compact_performance_memory(legacy+list(effective))
    assert json.loads(memory[0])['realized_net_after_broker_fees_usd']=='-1'
    assert json.loads(memory[0])['completed_trades']==2
    assert sum(json.loads(chunk)['groups'][0]['samples'] for chunk in memory[1:]
        if json.loads(chunk)['memory_type']=='strategy_regime_evidence')==2
    recent=next(json.loads(chunk) for chunk in memory if json.loads(chunk)['memory_type']=='recent_completed_trade_evidence')
    assert len(recent['trades'])==2 and all(x['realized_net_after_all_known_costs_usd'] for x in recent['trades'])


def test_new_same_symbol_plan_starts_fresh_excursion_exit_reason_and_model_cost():
    manager=AlphaPortfolioManager.__new__(AlphaPortfolioManager)
    manager.metrics={};manager.store=SimpleNamespace()
    first=manager._metrics(_plan('plan-1'),D('10'))
    first.update(max_bid=D('12'),min_bid=D('8'),mfe=D('2'),mae=D('2'),
        model_cost=D('.25'),exit_reason='STOP')
    second=manager._metrics(_plan('plan-2'),D('9.8'))
    assert second is not first
    assert second['max_bid']==second['min_bid']==D('9.8')
    assert second['mfe']==second['mae']==second['model_cost']==0
    assert 'exit_reason' not in second
    assert manager._metrics(_plan('plan-1'),D('10')) is first


def test_journal_bound_metric_rebuild_removes_inherited_cost_and_mfe():
    outcome={'quantity':'300','capital_deployed_usd':'36399',
        'realized_net_after_broker_fees_usd':'-24.81',
        'model_cost_usd':'.6081555','maximum_favorable_excursion_usd':'186',
        'maximum_adverse_excursion_usd':'60'}
    result=corrected_metrics(outcome,['121.36','121.42','121.13'],'.096055')
    assert D(result['maximum_favorable_excursion_usd'])==D('27')
    assert D(result['maximum_adverse_excursion_usd'])==D('60')
    assert D(result['model_cost_usd'])==D('.5121005')
    assert D(result['realized_net_after_all_known_costs_usd'])==D('-25.3221005')
    assert outcome['maximum_favorable_excursion_usd']=='186'
