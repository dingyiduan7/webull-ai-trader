"""REAL order dispatch consumes M2 authority before the sole broker POST.

This gateway is unrelated to the isolated external-holdings liquidation.
"""
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_HALF_UP
from uuid import uuid4

from .contracts import OrderIntent
from .durable.models import Execution, FinalizeOrder, Protection, OrderObservation
from .real_trading import CAPABILITY_VERSION,RealOwnershipPacket,RealAccountOrderPacket
from .reference import NEW_YORK
from .risk import POLICY

MIN_REAL_CLOSE_LIFECYCLE=timedelta(minutes=15)
QUOTE_FREE_EXIT_REASONS=frozenset(('MANDATORY_SAME_DAY_EXIT','SESSION_LIQUIDATION','SAFETY_LATCH'))
SAFETY_CANCEL_REASONS=QUOTE_FREE_EXIT_REASONS|{'PROTECTIVE_STOP'}
PLANNED_EXIT_TTL=timedelta(seconds=60)
PLANNED_EXIT_LIMIT_ATTEMPTS=3


def final_execution(body,intent,symbol,broker_id):
    if not isinstance(body,dict) or body.get('client_order_id')!=intent.client_order_id:
        raise ValueError('REAL_ORDER_IDENTITY')
    rows=body.get('orders')
    if not isinstance(rows,list) or len(rows)!=1:raise ValueError('REAL_ORDER_DETAIL_SHAPE')
    row=rows[0];side='BUY' if intent.side=='BUY' else 'SELL'
    if any(row.get(k)!=v for k,v in dict(client_order_id=intent.client_order_id,order_id=broker_id,
            symbol=symbol,side=side,order_type=intent.order_type,time_in_force='DAY',support_trading_session='CORE').items()):
        raise ValueError('REAL_ORDER_IDENTITY')
    qty=Decimal(row['total_quantity']);filled=Decimal(row['filled_quantity'])
    if qty!=intent.quantity or not 0<=filled<=qty:raise ValueError('REAL_FILL_QUANTITY')
    if row['status'] in ('PENDING','SUBMITTED'):
        if filled:raise ValueError('REAL_FILL_STATUS_MISMATCH')
        return None
    # A DAY order that is not filled by the close may be reported EXPIRED: final like a cancel.
    states={'FILLED':'FILLED','CANCELLED':'CANCELED','EXPIRED':'CANCELED','FAILED':'REJECTED'}
    if row['status']!='PARTIAL_FILLED' and (row['status'] not in states or (row['status']=='FILLED' and filled!=qty)):
        raise ValueError('REAL_ORDER_FINALITY')
    if row['status']=='PARTIAL_FILLED' and not 0<filled<qty:raise ValueError('REAL_ORDER_PARTIAL_STATE')
    from .mvp.gateway import reported_fees
    fee=reported_fees(row)
    price=Decimal(row['filled_price']) if filled else None
    if filled:
        if not price.is_finite() or price<=0:raise ValueError('REAL_FILL_PRICE')
        if intent.side=='BUY' and price>intent.limit_price_usd:raise ValueError('REAL_FILL_LIMIT_BREACH')
        if intent.side=='SELL_OWNED' and intent.limit_price_usd is not None and price<intent.limit_price_usd:
            raise ValueError('REAL_FILL_LIMIT_BREACH')
        executed=datetime.fromisoformat(row['filled_time_at'].replace('Z','+00:00'))
        if executed.tzinfo is None:raise ValueError('REAL_FILL_TIME')
    else:
        if fee:raise ValueError('REAL_UNFILLED_FEE_UNRESOLVED')
        executed=None
    return dict(status='PARTIAL' if row['status']=='PARTIAL_FILLED' else states[row['status']],
        terminal=row['status']!='PARTIAL_FILLED',quantity=filled,price=price,fee=fee,executed_at=executed)


class RealExecutionGateway:
    def __init__(self,gate,controller,source,transport,store,clock):
        if controller.ledger.execution_scope!='REAL':raise ValueError('REAL_LEDGER_REQUIRED')
        self.gate,self.controller,self.source,self.transport,self.store,self.clock=gate,controller,source,transport,store,clock

    def submit(self,intent,plan,*,packet=None,exit_reason=None):
        if intent.side=='BUY':
            packet,_=self.gate.read_checked(
                lambda:(self.source.refresh_instrument(self.controller.state,intent.instrument_id)
                    if hasattr(self.source,'refresh_instrument') else self.source.refresh(self.controller.state)),self.clock)
            now=self.clock();packet.validate(now,entry=True);self.controller.refresh(packet)
        else:
            packet=packet or self.source.exit_packet(self.controller.state,intent.instrument_id);now=self.clock()
            if isinstance(packet,(RealOwnershipPacket,RealAccountOrderPacket)) and exit_reason not in QUOTE_FREE_EXIT_REASONS:
                raise ValueError('REAL_QUOTE_REQUIRED_FOR_EXIT')
            self.gate.check_exit(packet,now,intent,self.controller.state)
            refresh_for_exit=getattr(self.controller,'refresh_for_exit',None)
            if refresh_for_exit:refresh_for_exit(packet)
        if intent.environment!='REAL' or intent.account_id!=self.controller.account_id:raise ValueError('REAL_INTENT_REQUIRED')
        symbol=(next(i.ticker for i in packet.instruments if i.instrument_id==intent.instrument_id)
            if intent.side=='BUY' else packet.symbol)
        if intent.side=='BUY' and packet.open_client_ids:raise ValueError('REAL_OPEN_ORDER_RECONCILIATION_REQUIRED')
        symbol_cap=Decimal(self.controller.state['cash']['liquidation_equity_usd'])*POLICY.stock_exposure
        if intent.side=='BUY' and (intent.quantity%1 or intent.quantity*intent.maximum_entry_usd>symbol_cap):
            raise ValueError('REAL_ENTRY_SCOPE')
        if intent.side=='BUY':
            session=__import__('autonomous_trader.reference',fromlist=['Session']).Session.model_validate(
                self.controller.state['session'])
            close_deadline=min(plan.latest_exit_at,session.liquidation_at)
            if now+MIN_REAL_CLOSE_LIFECYCLE>=close_deadline:
                raise ValueError('REAL_INSUFFICIENT_CLOSE_LIFECYCLE')
        nonce=self.controller.admit(intent,plan)
        # The transport validates and consumes this exact nonce immediately before POST.
        response=self.transport.submit(intent,symbol,self.controller,nonce,packet,self.gate,exit_reason=exit_reason)
        if not isinstance(response,dict) or response.get('client_order_id')!=intent.client_order_id or not response.get('order_id'):
            self.controller.incident('REAL_SUBMISSION_UNCERTAIN');raise ValueError('REAL_SUBMISSION_UNCERTAIN')
        self.store.emit_idempotent('REAL_BROKER_ACK',dict(intent_id=intent.intent_id,
            client_order_id=intent.client_order_id,broker_order_id=response['order_id']),mode='REAL',at=self.clock(),
            event_id='real-ack-'+intent.intent_id)
        self.controller.send(OrderObservation(intent_id=intent.intent_id,status='ACKNOWLEDGED',
            evidence_id='real-ack-'+intent.intent_id))
        return response['order_id']

    def reconcile_order(self,intent,symbol,broker_id,*,refresh=True,body=None):
        value=final_execution(self.transport.detail(intent.client_order_id) if body is None else body,
            intent,symbol,broker_id)
        if value is None:return None
        state=self.controller.state
        if state['orders'][intent.intent_id]['final']:
            order=state['orders'][intent.intent_id]
            executions=[x for x in state['executions'].values() if x['intent_id']==intent.intent_id]
            quantity=sum((Decimal(x['quantity']) for x in executions),Decimal(0))
            notional=sum((Decimal(x['quantity'])*Decimal(x['price_usd']) for x in executions),Decimal(0))
            fees=sum((Decimal(x['fee_usd']) for x in executions),Decimal(0))
            if (not value['terminal'] or value['status']!=order['phase']
                    or value['quantity']!=quantity or value['fee']!=fees
                    or (quantity and value['price']*quantity!=notional)):
                raise ValueError('REAL_FINAL_ORDER_HISTORY_MISMATCH')
            return value
        prior=[x for x in state['executions'].values() if x['intent_id']==intent.intent_id]
        prior_qty=sum((Decimal(x['quantity']) for x in prior),Decimal(0))
        prior_notional=sum((Decimal(x['quantity'])*Decimal(x['price_usd']) for x in prior),Decimal(0))
        prior_fee=sum((Decimal(x['fee_usd']) for x in prior),Decimal(0))
        if value['quantity']<prior_qty or value['fee']<prior_fee:raise ValueError('REAL_CUMULATIVE_FILL_REGRESSION')
        ids=[x['execution_id'] for x in prior]
        delta=value['quantity']-prior_qty
        if delta:
            if value['executed_at']>self.clock():raise ValueError('REAL_FUTURE_FILL')
            day=value['executed_at'].astimezone(NEW_YORK).date().isoformat()
            due=next((d for d in state['config']['settlement_days'] if d>day),None) if intent.side=='SELL_OWNED' else None
            cumulative_notional=value['quantity']*value['price']
            if cumulative_notional<=prior_notional:raise ValueError('REAL_CUMULATIVE_FILL_VALUE')
            delta_price=(cumulative_notional-prior_notional)/delta;delta_fee=value['fee']-prior_fee
            xid='real-fill-'+intent.client_order_id+'-'+str(value['quantity']).replace('.','_')
            execution=Execution(execution_id=xid,intent_id=intent.intent_id,quantity=delta,
                price_usd=delta_price,fee_usd=delta_fee,executed_at=value['executed_at'],settlement_date=due,evidence_id=xid)
            self.controller.send(execution)
            ids.append(xid)
        if value['status']=='PARTIAL':
            self.controller.send(OrderObservation(intent_id=intent.intent_id,status='PARTIALLY_FILLED',
                evidence_id='real-partial-'+intent.intent_id+'-'+str(value['quantity']).replace('.','_')))
        else:
            self.controller.send(FinalizeOrder(intent_id=intent.intent_id,status=value['status'],execution_ids=tuple(ids),
                total_quantity=value['quantity'],total_fees_usd=value['fee'],evidence_id='real-final-'+intent.intent_id))
        if refresh:
            packet=self.source.refresh(self.controller.state);self.controller.refresh(packet)
        if delta and intent.side=='BUY' and refresh:
            position=self.controller.state['positions'].get(intent.instrument_id)
            if position is None:raise ValueError('REAL_PARTIAL_POSITION_MISSING')
            stop=intent.protection.initial_stop_usd
            if Decimal(position['mark_usd'])<=stop:
                self.controller.incident('REAL_FILL_BELOW_PROTECTION_REQUIRES_RECOVERY')
                raise ValueError('REAL_FILL_BELOW_PROTECTION_REQUIRES_RECOVERY')
            self.controller.send(Protection(instrument_id=intent.instrument_id,quantity=position['quantity'],
                stop_usd=stop,evidence_id='real-protection-'+intent.intent_id+'-'+str(value['quantity']).replace('.','_')))
        self.store.emit_idempotent('REAL_FINAL_ORDER',dict(intent_id=intent.intent_id,side=intent.side,
            quantity=str(value['quantity']),price=None if value['price'] is None else str(value['price']),
            broker_fees_usd=str(value['fee']),status=value['status']),mode='REAL',at=self.clock(),
            event_id=('real-final-' if value['terminal'] else 'real-partial-')+intent.intent_id+'-'+str(value['quantity']).replace('.','_'))
        return value

    def cancel_owned_buys_for_exit(self,instrument_id,packet,reason):
        if reason not in QUOTE_FREE_EXIT_REASONS:raise ValueError('REAL_EXIT_CANCEL_AUTHORITY_REQUIRED')
        for oid,row in tuple(self.controller.state['orders'].items()):
            intent=OrderIntent.model_validate(row['intent'])
            if row['final'] or intent.side!='BUY' or intent.instrument_id!=instrument_id:continue
            body=self.transport.detail(intent.client_order_id);rows=body.get('orders',[]) if isinstance(body,dict) else []
            if len(rows)!=1 or not rows[0].get('order_id'):raise ValueError('REAL_EXIT_BUY_CANCEL_UNCERTAIN')
            result=self.reconcile_order(intent,packet.symbol,rows[0]['order_id'],refresh=False,body=body)
            if result and result.get('terminal'):continue
            current=self.controller.state['orders'][oid]
            if current['phase']=='CANCEL_PENDING':raise ValueError('REAL_EXIT_BUY_CANCEL_UNCERTAIN')
            packet=self.source.account_order_packet(self.controller.state,instrument_id)
            self.gate.check_buy_cancel(packet,self.clock(),self.controller.state)
            self.transport.cancel_owned_buy_for_exit(intent,self.controller,self.gate,packet,reason)
            body=self.transport.detail(intent.client_order_id);rows=body.get('orders',[]) if isinstance(body,dict) else []
            if len(rows)!=1 or not rows[0].get('order_id'):raise ValueError('REAL_EXIT_BUY_CANCEL_UNCERTAIN')
            result=self.reconcile_order(intent,packet.symbol,rows[0]['order_id'],refresh=False,body=body)
            if not result or not result.get('terminal'):raise ValueError('REAL_EXIT_BUY_CANCEL_UNCERTAIN')
        packet=self.source.account_order_packet(self.controller.state,instrument_id)
        self.gate.check_buy_cancel(packet,self.clock(),self.controller.state)
        return packet

    def cancel_planned_sells_for_safety(self,instrument_id,reason):
        """A safety exit (stop, deadline, latch) must not wait for a resting planned-exit LIMIT to expire:
        cancel it now and confirm it is final, so its reserved shares are free for the MARKET sell."""
        if reason not in SAFETY_CANCEL_REASONS:raise ValueError('REAL_EXIT_CANCEL_AUTHORITY_REQUIRED')
        pending=False
        for oid,row in tuple(self.controller.state['orders'].items()):
            intent=OrderIntent.model_validate(row['intent'])
            if row['final'] or intent.side!='SELL_OWNED' or intent.order_type!='LIMIT' or intent.instrument_id!=instrument_id:continue
            symbol=self.controller.state['instruments'][instrument_id]['ticker']
            body=self.transport.detail(intent.client_order_id);rows=body.get('orders',[]) if isinstance(body,dict) else []
            if len(rows)!=1 or not rows[0].get('order_id'):raise ValueError('REAL_EXIT_SELL_CANCEL_UNCERTAIN')
            result=self.reconcile_order(intent,symbol,rows[0]['order_id'],refresh=False,body=body)
            if result and result.get('terminal'):continue
            # Broker cancels are asynchronous: a cancel already requested is waited for, never repeated.
            if self.controller.state['orders'][oid]['phase'] not in ('CANCEL_PENDING',):
                packet=self.source.account_order_packet(self.controller.state,instrument_id)
                self.gate.check_buy_cancel(packet,self.clock(),self.controller.state)
                self.transport.cancel_planned_sell_for_safety(intent,self.controller,self.gate,packet,reason)
                body=self.transport.detail(intent.client_order_id);rows=body.get('orders',[]) if isinstance(body,dict) else []
                if len(rows)!=1 or not rows[0].get('order_id'):raise ValueError('REAL_EXIT_SELL_CANCEL_UNCERTAIN')
                result=self.reconcile_order(intent,symbol,rows[0]['order_id'],refresh=False,body=body)
            if not result or not result.get('terminal'):pending=True
        return pending

    def protective_exit(self,plan,reason,*,quantity=None,packet=None,limit_usd=None):
        """MARKET for safety exits (stop, deadline, latch). With `limit_usd`, a planned exit: a LIMIT sell at
        that price (the current bid, never below the market) that is cancelled after PLANNED_EXIT_TTL."""
        state=self.controller.state;p=state['positions'][plan.instrument_id]
        session=__import__('autonomous_trader.reference',fromlist=['Session']).Session.model_validate(state['session'])
        limit=limit_usd is not None
        intent=OrderIntent(intent_id=uuid4().hex,client_order_id=uuid4().hex,account_id=self.controller.account_id,
            environment='REAL',instrument_id=plan.instrument_id,side='SELL_OWNED',quantity=quantity or p['quantity'],
            order_type='LIMIT' if limit else 'MARKET',session='CORE',time_in_force='DAY',
            limit_price_usd=limit_usd if limit else None,trigger_price_usd=None,
            maximum_entry_usd=None,protection=None,plan_id=plan.plan_id,episode_id=plan.episode_id,
            deadline=min(session.closes_at-timedelta(minutes=2),self.clock()+PLANNED_EXIT_TTL) if limit else session.closes_at,
            capability_version=CAPABILITY_VERSION)
        self.store.emit_idempotent('REAL_EXIT_TRIGGER',dict(intent_id=intent.intent_id,reason=reason,plan_id=plan.plan_id,
            quantity=p['quantity']),mode='REAL',at=self.clock(),event_id='real-exit-'+intent.intent_id)
        return intent,self.submit(intent,plan,packet=packet,exit_reason=reason)
