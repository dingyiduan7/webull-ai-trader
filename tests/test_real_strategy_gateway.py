from datetime import datetime,timedelta
from decimal import Decimal as D
from types import SimpleNamespace
import pytest

from autonomous_trader.real_strategy_gateway import final_execution,RealExecutionGateway


def detail(intent, *, status, filled, price='100', fee='.02'):
    return {'client_order_id':intent.client_order_id,'orders':[{
        'client_order_id':intent.client_order_id,'order_id':'broker-1','symbol':'AAPL','side':'BUY',
        'order_type':'LIMIT','time_in_force':'DAY','support_trading_session':'CORE',
        'total_quantity':str(intent.quantity),'filled_quantity':str(filled),'status':status,
        'filled_price':price,'filled_time_at':'2026-09-11T14:00:01Z',
        'fees':[{'actual_value':fee,'receivable_value':fee}],
        'commission':{'actual_commission':'0','receivable_commission':'0'}}]}


def real_intent(intent):
    return intent(key='real-buy',symbol='stock-a',quantity='2',price='101',stop='95').model_copy(
        update={'environment':'REAL','account_id':'real-'+'a'*64})


def test_partial_fill_is_retained_as_cumulative_nonterminal_evidence(intent):
    order=real_intent(intent)
    value=final_execution(detail(order,status='PARTIAL_FILLED',filled='1'),order,'AAPL','broker-1')
    assert value['status']=='PARTIAL' and not value['terminal']
    assert value['quantity']==1 and value['price']==100 and value['fee']==D('.02')


def test_terminal_fill_and_cancel_with_fill_preserve_exact_cumulative_economics(intent):
    order=real_intent(intent)
    filled=final_execution(detail(order,status='FILLED',filled='2',price='100.50',fee='.04'),order,'AAPL','broker-1')
    assert filled['terminal'] and filled['status']=='FILLED' and filled['quantity']==2
    canceled=final_execution(detail(order,status='CANCELLED',filled='1',price='100.25'),order,'AAPL','broker-1')
    assert canceled['terminal'] and canceled['status']=='CANCELED' and canceled['quantity']==1


def test_impossible_partial_or_buy_limit_breach_is_rejected(intent):
    order=real_intent(intent)
    with pytest.raises(ValueError,match='PARTIAL_STATE'):
        final_execution(detail(order,status='PARTIAL_FILLED',filled='2'),order,'AAPL','broker-1')
    with pytest.raises(ValueError,match='LIMIT_BREACH'):
        final_execution(detail(order,status='FILLED',filled='2',price='101.01'),order,'AAPL','broker-1')


def test_real_entry_requires_time_for_full_same_day_close_lifecycle(intent,calendar):
    session=calendar.session(datetime(2026,9,11).date())
    now=session.liquidation_at-timedelta(minutes=10)
    order=intent(key='real-close-window',symbol='stock-a',quantity='1',price='99',stop='95').model_copy(
        update={'environment':'REAL','account_id':'real-'+'a'*64})
    packet=SimpleNamespace(open_client_ids=(),instruments=(SimpleNamespace(instrument_id='stock-a',ticker='AAA'),),
        validate=lambda *args,**kwargs:session)
    class Controller:
        account_id=order.account_id
        state={'cash':{'liquidation_equity_usd':'500'},'session':session.model_dump(mode='json')}
        ledger=SimpleNamespace(execution_scope='REAL')
        def refresh(self,value):pass
        def admit(self,*args):pytest.fail('entry must fail before authority admission')
    source=SimpleNamespace(refresh=lambda state:packet)
    # Current runtime reads entry evidence through gate.read_checked.
    gate=SimpleNamespace(check=lambda *args,**kwargs:None,
        read_checked=lambda fetch,clock,**kwargs:(fetch(),None))
    value=RealExecutionGateway(gate,Controller(),source,object(),object(),lambda:now)
    plan=SimpleNamespace(latest_exit_at=session.liquidation_at)
    with pytest.raises(ValueError,match='REAL_INSUFFICIENT_CLOSE_LIFECYCLE'):
        value.submit(order,plan)


@pytest.mark.parametrize('terminal',[True,False])
def test_mandatory_exit_buy_cancel_requires_terminal_reconciliation(intent,terminal):
    order=real_intent(intent);state={'orders':{order.intent_id:{'final':False,'phase':'ACKNOWLEDGED',
        'intent':order.model_dump(mode='json')}}}
    packet=SimpleNamespace(symbol='AAPL',instrument_id=order.instrument_id)
    details=iter(({'orders':[{'order_id':'broker-1'}]},{'orders':[{'order_id':'broker-1'}]}))
    transport=SimpleNamespace(detail=lambda client:next(details),
        cancel_owned_buy_for_exit=lambda *args:None)
    source=SimpleNamespace(account_order_packet=lambda *args:packet)
    gate=SimpleNamespace(check_buy_cancel=lambda *args:None)
    controller=SimpleNamespace(state=state,ledger=SimpleNamespace(execution_scope='REAL'))
    gateway=RealExecutionGateway(gate,controller,source,transport,SimpleNamespace(),lambda:datetime(2026,9,11,14))
    calls=[]
    def reconcile(*args,**kwargs):
        calls.append(1)
        if len(calls)==1:return None
        if terminal:state['orders'][order.intent_id]['final']=True;return {'terminal':True}
        return None
    gateway.reconcile_order=reconcile
    if terminal:
        assert gateway.cancel_owned_buys_for_exit(order.instrument_id,packet,'SAFETY_LATCH') is packet
    else:
        with pytest.raises(ValueError,match='REAL_EXIT_BUY_CANCEL_UNCERTAIN'):
            gateway.cancel_owned_buys_for_exit(order.instrument_id,packet,'SAFETY_LATCH')
