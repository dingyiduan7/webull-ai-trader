"""Mode-bound gateway consumes an M2 permit before any SDK submission."""
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from hashlib import sha256
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
import time
from webull.trade.request.v3.place_order_request import PlaceOrderRequest
from webull.trade.request.v3.cancel_order_request import CancelOrderRequest
from ..sandbox.certification.first_order import FirstOrderHttp
from ..sandbox.certification.controls import MODE
from ..sandbox.quiet import quiet_sdk
from ..durable.models import OrderObservation, Execution, FinalizeOrder, Protection, Admit
from ..contracts import OrderIntent
from ..decisions import parse_trade_decision,validate_semantics
from .config import ExecutionMode
from .cycle import predicates_match


def sandbox_ledger_id(account_id):return 'sandbox-'+sha256(account_id.encode()).hexdigest()[:32]


class SandboxTransport(FirstOrderHttp):
    def __init__(self,credentials):
        super().__init__(credentials,mode=MODE,journal=SimpleNamespace(account_hash=sha256(credentials.account_id.encode()).hexdigest()))

    @quiet_sdk()
    def place(self,intent,symbol):
        if not isinstance(symbol,str) or not __import__('re').fullmatch(r'[A-Z][A-Z0-9.]{0,9}',symbol):raise ValueError('INVALID_ORDER_SYMBOL')
        order=dict(client_order_id=intent.client_order_id,combo_type='NORMAL',symbol=symbol,instrument_type='EQUITY',
            market='US',order_type='LIMIT',limit_price=str(intent.limit_price_usd),quantity=str(intent.quantity),
            side='BUY' if intent.side=='BUY' else 'SELL',time_in_force='DAY',support_trading_session='CORE',entrust_type='QTY')
        req=PlaceOrderRequest();req.set_account_id(self._account_id);req.set_new_orders([order]);req.add_custom_headers_from_order([order])
        return self._send_bounded(req,'/trading/orders/place')

    @quiet_sdk()
    def cancel(self,client_id):
        req=CancelOrderRequest();req.set_account_id(self._account_id);req.set_client_order_id(client_id)
        return self._send_bounded(req,'/trading/orders/cancel')


def validate_detail(body,intent,broker_id,symbol='AAPL'):
    if not isinstance(body,dict) or body.get('client_order_id')!=intent.client_order_id:
        raise ValueError('BROKER_IDENTITY_CONFLICT')
    rows=body.get('orders')
    if not isinstance(rows,list) or len(rows)!=1:raise ValueError('BROKER_DETAIL_SHAPE')
    row=rows[0]
    if (row.get('order_id')!=broker_id or row.get('client_order_id')!=intent.client_order_id
            or row.get('symbol')!=symbol or row.get('side')!=('BUY' if intent.side=='BUY' else 'SELL')
            or row.get('order_type')!='LIMIT' or row.get('time_in_force')!='DAY'
            or row.get('support_trading_session')!='CORE'
            or Decimal(row['total_quantity'])!=intent.quantity or Decimal(row['limit_price'])!=intent.limit_price_usd):
        raise ValueError('BROKER_DETAIL_BINDING_CONFLICT')
    filled=Decimal(row['filled_quantity'])
    if filled not in (Decimal(0),intent.quantity):raise ValueError('PARTIAL_FILL_RECONCILIATION_REQUIRED')
    if row['status']=='FILLED':
        price=executed_price(row)
        if filled!=intent.quantity or price<=0 or (intent.side=='BUY' and price>intent.limit_price_usd) or (intent.side=='SELL_OWNED' and price<intent.limit_price_usd):
            raise ValueError('BROKER_FILL_CONFLICT')
    return row


def reported_fees(row):
    rows=row.get('fees');commission=row.get('commission')
    if not isinstance(rows,list) or not isinstance(commission,dict):raise ValueError('UNKNOWN_FEE_SHAPE')
    total=Decimal(0)
    for fee in rows:
        amount=Decimal(fee['actual_value'])
        if amount<0 or amount!=Decimal(fee['receivable_value']):raise ValueError('UNRESOLVED_FEE_VALUES')
        total+=amount
    if commission:
        amount=Decimal(commission['actual_commission'])
        if amount<0 or amount!=Decimal(commission['receivable_commission']):raise ValueError('UNRESOLVED_COMMISSION')
        total+=amount
    return total


def executed_price(row):
    """Prefer explicit broker executed notional without trusting a rounded average."""
    reported=Decimal(row['filled_price'])
    value=row.get('executed_notional_usd')
    if value is None:return reported
    quantity=Decimal(row['filled_quantity']);notional=Decimal(value)
    if quantity<=0 or notional<=0:raise ValueError('EXECUTED_NOTIONAL_INVALID')
    exact=notional/quantity
    if exact.quantize(Decimal('.01'),rounding=ROUND_HALF_UP)!=reported:
        raise ValueError('EXECUTED_NOTIONAL_CONFLICT')
    return exact


class ExecutionGateway:
    def __init__(self,config,*,simulation=None,transport=None,sanitizer=None,evidence_sink=lambda value:None):
        config.validate()
        if config.mode==ExecutionMode.DRY_RUN and transport is not None:raise ValueError('DRY_RUN_EXTERNAL_TRANSPORT_FORBIDDEN')
        self.config,self.simulation,self.transport=config,simulation,transport
        self.sanitizer,self.evidence_sink=sanitizer,evidence_sink
        self.submission_attempted=False

    def recover_terminal_cancellations(self,controller):
        """Finalize only broker-proven, zero-fill terminal cancel outcomes.

        A cancel acknowledgement can precede the broker's terminal order state.
        The durable CANCEL_PENDING state must therefore be resolved by a later
        order-detail read before account reconciliation can become ready again.
        """
        if self.config.mode!=ExecutionMode.SANDBOX:return ()
        recovered=[]
        for intent_id,order in tuple(controller.state['orders'].items()):
            if order['final'] or order['phase']!='CANCEL_PENDING':continue
            intent=OrderIntent.model_validate(order['intent'])
            response=self.transport.detail(intent.client_order_id);self._capture(response)
            if response.status_code!=200:raise ValueError('CANCEL_RECOVERY_DETAIL_UNCERTAIN')
            rows=response.body.get('orders') if isinstance(response.body,dict) else None
            broker_id=rows[0].get('order_id') if isinstance(rows,list) and len(rows)==1 else None
            symbol=controller.state['instruments'][intent.instrument_id]['ticker']
            row=validate_detail(response.body,intent,broker_id,symbol)
            if row['status'] in ('SUBMITTED','PENDING'):continue
            if row['status']=='FILLED':raise ValueError('CANCEL_RECOVERY_FILL_REQUIRES_RECONCILIATION')
            status={'CANCELLED':'CANCELED','FAILED':'REJECTED','EXPIRED':'EXPIRED'}.get(row['status'])
            if status is None:raise ValueError('UNKNOWN_BROKER_STATE')
            fee=reported_fees(row)
            if Decimal(row['filled_quantity'])!=0 or fee!=0:raise ValueError('TERMINAL_TOTALS_UNCERTAIN')
            controller.lease()
            controller.send(FinalizeOrder(intent_id=intent_id,status=status,execution_ids=(),total_quantity='0',
                total_fees_usd='0',evidence_id='rest-recovery-'+uuid4().hex))
            recovered.append(intent_id)
        return tuple(recovered)

    def dispatch(self,intent,nonce,observation,controller,session,clock):
        self.config.validate()
        if intent.side=='BUY':
            try:observation.require_fresh(clock())
            except ValueError as exc:
                if str(exc)!='STALE_MARKET_OR_ACCOUNT' or not getattr(self,'refresh_entry',None):raise
                # No write/permit consumption occurred. Refresh once and mint
                # a permit bound to the newly reconciled state before trying.
                observation,nonce=self.refresh_entry(intent)
                observation.require_fresh(clock())
        else:observation.require_exit_valid(clock())
        if not session.contains(clock()) or (intent.side=='BUY' and not session.permits_entry(clock())):raise ValueError('SESSION_CLOSED')
        if (observation.instrument_id!=intent.instrument_id or observation.instrument.ticker!=observation.ticker
                or observation.instrument.quantity_increment!=1 or observation.instrument.price_increment_usd!=Decimal('.01')):
            raise ValueError('INSTRUMENT_EXECUTION_BINDING')
        if intent.quantity%1 or intent.limit_price_usd%Decimal('.01') or intent.order_type!='LIMIT':raise ValueError('WHOLE_SHARE_CENT_LIMIT_REQUIRED')
        if self.config.mode==ExecutionMode.SANDBOX:
            if self.transport is None or controller.account_id!=sandbox_ledger_id(self.transport._account_id):raise ValueError('SANDBOX_ACCOUNT_BINDING')
        elif self.simulation is None:raise ValueError('DRY_RUN_BROKER_REQUIRED')
        controller.consume(intent,nonce)  # Persist CLAIMED_UNCERTAIN, never reset after timeout.
        if self.config.mode==ExecutionMode.DRY_RUN:
            broker_id=self.simulation.submit(intent,clock())
            row={'status':'FILLED','filled_quantity':str(intent.quantity),'filled_price':str(intent.limit_price_usd),
                'fees':[],'commission':{},'filled_time_at':clock().isoformat()}
        else:
            self.submission_attempted=True
            response=self.transport.place(intent,observation.ticker)
            self._capture(response)
            if response.status_code!=200 or response.body.get('client_order_id')!=intent.client_order_id or not response.body.get('order_id'):
                raise ValueError('SUBMISSION_UNCERTAIN_NO_RETRY')
            broker_id=response.body['order_id']
            row=None
            # Yield known working entries to the runner when holdings need
            # supervision; yielding must never cancel Astra's valid order.
            checks=6
            if intent.side=='BUY' and not controller.state['positions']:
                remaining=max(0,(intent.deadline-clock()).total_seconds())
                checks=max(checks,int(remaining/3.2)+2)
            renew_at=clock()+timedelta(seconds=35)
            for _ in range(checks):
                if clock()>=renew_at:
                    controller.lease()
                    renew_at=clock()+timedelta(seconds=35)
                detail=self.transport.detail(intent.client_order_id);self._capture(detail)
                if detail.status_code!=200:raise ValueError('DETAIL_UNCERTAIN_NO_RETRY')
                row=validate_detail(detail.body,intent,broker_id,observation.ticker)
                if row['status'] in ('FILLED','CANCELLED','FAILED','EXPIRED'):break
                if row['status'] not in ('SUBMITTED','PENDING'):raise ValueError('UNKNOWN_BROKER_STATE')
                if intent.side=='BUY' and (clock()>=intent.deadline or any(controller.state['latches'].values())):break
            if row['status'] in ('SUBMITTED','PENDING'):
                if Decimal(row['filled_quantity'])!=0:raise ValueError('WORKING_ORDER_NEEDS_RECONCILIATION')
                if intent.side=='BUY' and clock()<intent.deadline and not any(controller.state['latches'].values()):
                    controller.send(OrderObservation(intent_id=intent.intent_id,status='ACKNOWLEDGED',evidence_id=uuid4().hex))
                    return {'status':'WORKING','filled_quantity':row['filled_quantity'],
                        'filled_price':None,'fees_usd':'0','origin':'WEBULL_SANDBOX'}
                # Only known unfilled owned intent is canceled; journal precedes write.
                if Decimal(row['filled_quantity'])!=0 or not session.contains(clock()):raise ValueError('WORKING_ORDER_NEEDS_RECONCILIATION')
                controller.send(OrderObservation(intent_id=intent.intent_id,status='CANCEL_PENDING',evidence_id=uuid4().hex))
                response=self.transport.cancel(intent.client_order_id);self._capture(response)
                detail=self.transport.detail(intent.client_order_id);self._capture(detail)
                if detail.status_code!=200:
                    raise ValueError('CANCEL_UNCERTAIN' if response.status_code!=200 else 'CANCEL_DETAIL_UNCERTAIN')
                row=validate_detail(detail.body,intent,broker_id,observation.ticker)
                if row['status'] not in ('FILLED','CANCELLED'):
                    raise ValueError('CANCEL_UNCERTAIN' if response.status_code!=200 else 'CANCEL_NOT_TERMINAL')
        return self._finalize(intent,row,controller,session,clock)

    def service_working_entries(self,controller,session,clock):
        """Resume durable accepted BUYs without resubmission or early timeout cancellation."""
        if self.config.mode!=ExecutionMode.SANDBOX:return ()
        results=[]
        for oid,order in tuple(controller.state['orders'].items()):
            if order['final'] or order['phase']!='ACKNOWLEDGED' or order['intent']['side']!='BUY':continue
            controller.lease()
            intent=OrderIntent.model_validate(order['intent'])
            detail=self.transport.detail(intent.client_order_id);self._capture(detail)
            if detail.status_code!=200:raise ValueError('DETAIL_UNCERTAIN_NO_RETRY')
            rows=detail.body.get('orders') if isinstance(detail.body,dict) else None
            broker_id=rows[0].get('order_id') if isinstance(rows,list) and len(rows)==1 else None
            symbol=controller.state['instruments'][intent.instrument_id]['ticker']
            row=validate_detail(detail.body,intent,broker_id,symbol)
            if row['status'] in ('SUBMITTED','PENDING'):
                if Decimal(row['filled_quantity'])!=0:raise ValueError('WORKING_ORDER_NEEDS_RECONCILIATION')
                if clock()<intent.deadline and not any(controller.state['latches'].values()):continue
                if Decimal(row['filled_quantity'])!=0 or session is None or not session.contains(clock()):
                    raise ValueError('WORKING_ORDER_NEEDS_RECONCILIATION')
                controller.send(OrderObservation(intent_id=oid,status='CANCEL_PENDING',evidence_id=uuid4().hex))
                response=self.transport.cancel(intent.client_order_id);self._capture(response)
                detail=self.transport.detail(intent.client_order_id);self._capture(detail)
                if detail.status_code!=200:raise ValueError('CANCEL_DETAIL_UNCERTAIN')
                row=validate_detail(detail.body,intent,broker_id,symbol)
                if row['status'] not in ('FILLED','CANCELLED'):raise ValueError('CANCEL_NOT_TERMINAL')
            result=self._finalize(intent,row,controller,session,clock)
            results.append({'intent_id':oid,'plan_id':intent.plan_id,'broker':result})
        return tuple(results)

    def _finalize(self,intent,row,controller,session,clock):
        controller.send(OrderObservation(intent_id=intent.intent_id,status='ACKNOWLEDGED',evidence_id=uuid4().hex))
        fee=reported_fees(row)
        if row['status']=='FILLED':
            fill_at=datetime.fromisoformat(row['filled_time_at'].replace('Z','+00:00'))
            if fill_at>clock():
                # Never substitute a receipt time for an economic timestamp.
                time.sleep(min(1,max(0,(fill_at-clock()).total_seconds())))
            state=controller.state
            settlement=next((d for d in state['config']['settlement_days'] if d>session.label.isoformat()),None) if intent.side=='SELL_OWNED' else None
            xid=uuid4().hex
            controller.send(Execution(execution_id=xid,intent_id=intent.intent_id,quantity=intent.quantity,
                price_usd=executed_price(row),fee_usd=fee,executed_at=fill_at,settlement_date=settlement,evidence_id='rest-'+uuid4().hex))
            controller.send(FinalizeOrder(intent_id=intent.intent_id,status='FILLED',execution_ids=(xid,),
                total_quantity=intent.quantity,total_fees_usd=fee,evidence_id='rest-final-'+uuid4().hex))
        else:
            if Decimal(row['filled_quantity'])!=0 or fee!=0:raise ValueError('TERMINAL_TOTALS_UNCERTAIN')
            status={'CANCELLED':'CANCELED','FAILED':'REJECTED','EXPIRED':'EXPIRED'}.get(row['status'])
            if status is None:raise ValueError('UNKNOWN_TERMINAL_STATUS')
            controller.send(FinalizeOrder(intent_id=intent.intent_id,status=status,execution_ids=(),total_quantity='0',total_fees_usd='0',evidence_id=uuid4().hex))
        return {'status':row['status'],'filled_quantity':row['filled_quantity'],'filled_price':row.get('filled_price'),
            'fees_usd':str(fee),'origin':'SIMULATED_DRY_RUN' if self.config.mode==ExecutionMode.DRY_RUN else 'WEBULL_SANDBOX'}

    def _capture(self,response):
        from ..sandbox.evidence import fixture
        self.evidence_sink(fixture(response,self.sanitizer))


class LocalPositionManager:
    """Synchronous stop/target/deadline monitor, never broker-native protection.

    A lost process/network cannot guarantee a stop fill. Persisted positions and
    incidents block further entries; never report a local stop as broker-held.
    """
    ready=True

    def __init__(self,*,sleep=time.sleep,monitor_dry_run=False,review=None,emit=lambda kind,payload:None):
        self.sleep=sleep;self.monitor_dry_run=monitor_dry_run;self.review=review;self.emit=emit

    def _exit_owned(self,intent,observation,gateway,controller,session,clock,reason,conditions):
        if any(not order['final'] and not (order.get('phase')=='ACKNOWLEDGED'
                and order.get('intent',{}).get('side')=='BUY'
                and order['intent']['instrument_id']!=intent.instrument_id)
                for order in controller.state['orders'].values()):raise ValueError('EXIT_ALREADY_PENDING')
        q=Decimal(controller.state['positions'][intent.instrument_id]['quantity'])
        self.emit('POSITION_EXIT_TRIGGER',{'summary':'Deterministic owned-position exit: '+reason,'reason':reason,
            'simultaneously_true_conditions':conditions,'instrument_id':intent.instrument_id,'quantity':str(q),
            'bid':str(observation.bid),'ask':str(observation.ask),'spread_bps':str(observation.features()['spread_bps']),
            'timestamp':clock().isoformat()})
        price=(observation.bid*Decimal('.998')).quantize(Decimal('.01'),rounding=ROUND_DOWN)
        exit_intent=OrderIntent(intent_id=uuid4().hex,client_order_id=uuid4().hex,account_id=controller.account_id,
            environment='OFFLINE',instrument_id=intent.instrument_id,side='SELL_OWNED',quantity=q,order_type='LIMIT',
            session='CORE',time_in_force='DAY',limit_price_usd=price,trigger_price_usd=None,maximum_entry_usd=None,
            protection=None,plan_id=intent.plan_id,episode_id=intent.episode_id,deadline=min(clock()+timedelta(seconds=60),session.closes_at),
            capability_version=intent.capability_version)
        state=controller.send(Admit(intent=exit_intent,fee_allowance_usd=str(1+q*Decimal('.02')),exit_cost_usd='0',
            stress_allowance_usd='0',latest_exit_at=session.liquidation_at,max_holding_seconds=60))
        result=gateway.dispatch(exit_intent,state['outbox'][exit_intent.intent_id]['nonce'],observation,controller,session,clock)
        if result['status']!='FILLED':raise ValueError('EXIT_NOT_FILLED_RECONCILE')
        return result

    def _apply_review(self,bundle,intent,plan,plan_version,original_plan,observation,gateway,controller,session,clock):
        ai,context,registry=bundle
        if not ai.get('schema_valid') or not ai.get('semantics_valid') or not ai.get('decision'):
            raise ValueError(ai.get('result') or 'INVALID_POSITION_REVIEW')
        decision=parse_trade_decision(__import__('json').dumps(ai['decision']))
        validate_semantics(decision,context,registry,clock())
        if decision.action not in ('HOLD','PROPOSE_EXIT','PROPOSE_AMENDMENT'):raise ValueError('POSITION_REVIEW_ACTION_NOT_ALLOWED')
        state=controller.state;position=state['positions'].get(intent.instrument_id)
        if (position is None or Decimal(position['quantity'])!=intent.quantity or any(not x['final'] for x in state['orders'].values())
                or not state['reconciliation']['ready']):raise ValueError('POSITION_STATE_CHANGED_DURING_REVIEW')
        if any(state['latches'].values()):
            return plan,plan_version,'SAFETY_LATCH'
        if clock()>=min(plan.latest_exit_at,original_plan.latest_exit_at,session.liquidation_at):
            return plan,plan_version,'ABSOLUTE_LATEST_EXIT'
        if decision.action=='HOLD':return plan,plan_version,None
        if decision.action=='PROPOSE_EXIT':
            e=decision.exit_request
            if e.mode!='CLOSE' or e.requested_quantity is not None:raise ValueError('MVP_POSITION_EXIT_MUST_CLOSE')
            return plan,plan_version,'ASTRA_PROPOSE_EXIT'
        a=decision.amendment
        if a.requested_trailing is not None:raise ValueError('MVP_TRAILING_AMENDMENT_UNSUPPORTED')
        if a.requested_targets is not None:
            if len(a.requested_targets)>1 or any(t.fraction_of_initial_quantity!=1 or t.price_usd<=observation.ask for t in a.requested_targets):
                raise ValueError('MVP_TARGET_AMENDMENT_UNSAFE')
        protection=plan.protection.model_copy(update={'initial_stop_usd':a.requested_stop_usd}) if a.requested_stop_usd is not None else plan.protection
        updated=plan.model_copy(update={
            'protection':protection,
            'targets':a.requested_targets if a.requested_targets is not None else plan.targets,
            'latest_exit_at':a.requested_latest_exit_at if a.requested_latest_exit_at is not None else plan.latest_exit_at,
            'invalidation':a.requested_invalidation if a.requested_invalidation is not None else plan.invalidation})
        next_version=plan_version+1
        self.emit('POSITION_PLAN_AMENDED',{'summary':'Validated Astra position-plan amendment applied','plan_id':intent.plan_id,
            'instrument_id':intent.instrument_id,'plan_version':str(next_version),'prior_plan_version':str(plan_version),
            'absolute_latest_exit_at':original_plan.latest_exit_at.isoformat(),'plan':updated.model_dump(mode='json'),
            'decision_id':decision.decision_id})
        if a.requested_stop_usd is not None:
            controller.send(Protection(instrument_id=intent.instrument_id,quantity=Decimal(position['quantity']),
                stop_usd=a.requested_stop_usd,evidence_id='astra-amendment-'+decision.decision_id))
        return updated,next_version,None

    def manage(self,intent,plan,source,gateway,controller,session,clock,plan_version=1,original_plan=None):
        if gateway.config.mode==ExecutionMode.DRY_RUN and not self.monitor_dry_run:
            # Simulation tests can inspect the owned simulated position without waiting.
            return
        original_plan=original_plan or plan
        position=controller.state['positions'][intent.instrument_id]
        active_stop=max(plan.protection.initial_stop_usd,Decimal(position['stop_usd']))
        # A stop crossed during downtime is already due: sell owned quantity rather
        # than trying to register a protection level above the current bid.
        if active_stop<Decimal(position['mark_usd']):
            controller.send(Protection(instrument_id=intent.instrument_id,quantity=intent.quantity,
                stop_usd=active_stop,evidence_id='local-manager-armed'))
        fills=[datetime.fromisoformat(x['executed_at']) for x in controller.state['executions'].values() if x['intent_id']==intent.intent_id and Decimal(x['quantity'])>0]
        if not fills:raise ValueError('MONITOR_FILL_EVIDENCE_MISSING')
        first=min(fills)
        interval=timedelta(seconds=original_plan.max_holding_seconds)
        next_review=first+interval
        maximum_bid=minimum_bid=Decimal(position['mark_usd'])
        executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='astra-position-review') if self.review else None
        pending=None;request_at=None;request_snapshot=None
        try:
            while True:
                observation=source.refresh(controller.state);observation.require_exit_valid(clock());controller.refresh_for_exit(observation)
                if not controller.state['positions']:return
                maximum_bid=max(maximum_bid,observation.bid);minimum_bid=min(minimum_bid,observation.bid)
                features=observation.features()
                active_stop=max(plan.protection.initial_stop_usd,Decimal(controller.state['positions'][intent.instrument_id]['stop_usd']))
                conditions={'STOP':observation.bid<=active_stop,'INVALIDATION':predicates_match(plan.invalidation,features),
                    'TARGET':any(observation.bid>=t.price_usd for t in plan.targets),
                    'ABSOLUTE_LATEST_EXIT':clock()>=min(plan.latest_exit_at,original_plan.latest_exit_at,session.liquidation_at),
                    'SAFETY_LATCH':any(controller.state['latches'].values())}
                true_conditions=[name for name,value in conditions.items() if value]
                if true_conditions:
                    self._exit_owned(intent,observation,gateway,controller,session,clock,true_conditions[0],true_conditions)
                    after=source.refresh(controller.state);controller.refresh_for_exit(after)
                    if after.positions:raise ValueError('EXIT_POSITION_NOT_FLAT')
                    return
                try:observation.require_monitor_fresh(clock())
                except ValueError as exc:
                    if str(exc)!='STALE_MARKET_OR_ACCOUNT':raise
                    self.sleep(3.2);continue
                if pending is not None and pending.done():
                    decision_at=clock();action=None;rationale=None;proposed=None;validator='REJECTED';rejection=None
                    try:
                        bundle=pending.result();raw=bundle[0].get('decision') or {};action=raw.get('action');rationale=raw.get('reasoning_summary')
                        proposed={'amendment':raw.get('amendment'),'exit_request':raw.get('exit_request')}
                        plan,plan_version,exit_reason=self._apply_review(bundle,intent,plan,plan_version,original_plan,
                            observation,gateway,controller,session,clock);validator='APPROVED'
                        if exit_reason:
                            self._exit_owned(intent,observation,gateway,controller,session,clock,exit_reason,[exit_reason])
                            after=source.refresh(controller.state);controller.refresh_for_exit(after)
                            if after.positions:raise ValueError('EXIT_POSITION_NOT_FLAT')
                            return
                    except Exception as exc:
                        rejection=str(exc) if __import__('re').fullmatch(r'[A-Za-z0-9_ :.-]{1,200}',str(exc)) else 'POSITION_REVIEW_INVALID'
                    finally:
                        self.emit('POSITION_REVIEW',{'summary':'Astra position review '+validator.lower(),'request_timestamp':request_at.isoformat(),
                            'decision_timestamp':decision_at.isoformat(),'latency_ms':round((decision_at-request_at).total_seconds()*1000),
                            'action':action,'rationale':rationale,'proposed_amendments':proposed,'validator_result':validator,
                            'rejection_reason':rejection,'position_quote_snapshot':request_snapshot})
                    pending=None;next_review=clock()+interval
                if self.review and pending is None and clock()>=next_review:
                    request_at=clock();request_snapshot={'instrument_id':intent.instrument_id,'quantity':controller.state['positions'][intent.instrument_id]['quantity'],
                        'bid':str(observation.bid),'ask':str(observation.ask),'spread_bps':str(features['spread_bps']),'timestamp':request_at.isoformat(),
                        'active_stop_usd':str(active_stop),'targets':[x.model_dump(mode='json') for x in plan.targets],
                        'plan_version':str(plan_version)}
                    basis=Decimal(controller.state['positions'][intent.instrument_id]['basis_usd']);q=Decimal(controller.state['positions'][intent.instrument_id]['quantity'])
                    metrics={'maximum_favorable_excursion_usd':max(Decimal(0),q*maximum_bid-basis),
                        'maximum_adverse_excursion_usd':max(Decimal(0),basis-q*minimum_bid)}
                    pending=executor.submit(self.review,observation,intent,plan,plan_version,original_plan,metrics)
                self.sleep(3.2)
        except Exception:
            controller.incident('LOCAL_MONITOR_INTERRUPTED_RECONCILE_REQUIRED')
            raise
        finally:
            if executor:executor.shutdown(wait=False,cancel_futures=True)
