"""Compact Astra alpha orchestration. All order authority remains in M2 and the gateway."""
import json
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor

from ..contracts import (AlphaBar,AlphaContext,AlphaDecision,AlphaInstrumentContext,
    AlphaManagedPlan,AlphaPositionContext,OrderIntent,Protection)
from ..numeric import floor_quantity
from ..risk import POLICY
from ..economic_policy import effective_policy, policy_revision, assert_policy_current, IMMUTABLE_BOUNDARIES, aggregate_risk_limit
from ..durable.models import Protection as ProtectionCommand

SOURCE='WEBULL_SANDBOX_READ_ONLY'


def compact_market_context(observation,dossier,limit=1900):
    """Bound model-facing evidence while the durable dossier remains complete."""
    windows={}
    for seconds in ('1','5','10','60'):
        row=(observation.live_windows or {}).get(seconds)
        if isinstance(row,dict):
            windows[seconds]={k:row[k] for k in ('event_count','change_bps','tick_volume',
                'first_price','last_price','high_price','low_price','source_end_at') if k in row}
    feedback=(dossier or {}).get('execution_feedback')
    if feedback:
        feedback={k:feedback.get(k) for k in ('reason','bid','ask','quote_at','authority')} | {
            'prior_open':{k:(dossier['execution_feedback'].get('prior_open') or {}).get(k)
                for k in ('desired_quantity','desired_notional_usd','entry_limit_usd','hard_stop_usd','target_usd','entry_expires_at')}}
    detection=None
    if dossier:
        raw=dossier.get('latest_screen_evidence')
        if isinstance(raw,dict):
            detection={k:raw.get(k) for k in ('source','screen_at','rank','latest_price','window_minutes',
                'window_change_percent','percent_per_minute','relative_volume_10d','volume') if k in raw}
            detection['first_detected_at']=dossier.get('first_detected_at')
    base={'cheap_discovery_sources':list(observation.discovery_sources),
        'detection_evidence':detection,
        'execution_feedback':feedback,
        'market_value':None if observation.market_value is None else str(observation.market_value),
        'change_ratio':None if observation.change_ratio is None else str(observation.change_ratio),
        'amplitude':None if observation.amplitude is None else str(observation.amplitude),
        'market_provenance':observation.market_provenance,
        'live_windows':windows,
        'live_window_material_signature':list(observation.stream_material_signature)}
    encoded=json.dumps({**base,'session_opportunity_dossier':dossier},separators=(',',':'))
    if len(encoded)<=limit:return encoded
    compact=None
    if dossier:
        compact={k:dossier.get(k) for k in ('scope','symbol','instrument_id','as_of','attempts','first_detected_at',
            'current_status','current_reason')}
        compact['quote_sequence']=[]
        for row in dossier.get('quote_sequence',())[-4:]:
            compact['quote_sequence'].append({k:row.get(k) for k in ('at','quote_at','bid','ask','bid_size','ask_size',
                'volume','relative_volume_10d','short_window_change_ratio','short_window_minutes','bar_first_at',
                'bar_last_at','bar_return_bps','regime_label')})
        compact['prior_astra_decisions']=[]
        for row in dossier.get('prior_astra_decisions',())[-3:]:
            value={k:row.get(k) for k in ('at','action','ask','quote_at','later_observations',
                'best_executable_return_bps','worst_executable_return_bps')}
            value['reason']=str(row.get('reason',''))[:240]
            compact['prior_astra_decisions'].append(value)
    payload={**base,'session_opportunity_dossier':compact}
    encoded=json.dumps(payload,separators=(',',':'))
    while len(encoded)>limit and compact and (len(compact['quote_sequence'])>1 or len(compact['prior_astra_decisions'])>1):
        if len(compact['quote_sequence'])>1:compact['quote_sequence'].pop(0)
        elif len(compact['prior_astra_decisions'])>1:compact['prior_astra_decisions'].pop(0)
        encoded=json.dumps(payload,separators=(',',':'))
    if len(encoded)>limit and compact:
        for row in compact['prior_astra_decisions']:row['reason']=row['reason'][:80]
        encoded=json.dumps(payload,separators=(',',':'))
    if len(encoded)>limit and compact:
        # Preserve identity and the latest executable quote if one verbose
        # legacy decision still exceeds the contract. The full evidence stays durable.
        compact['prior_astra_decisions']=[]
        encoded=json.dumps(payload,separators=(',',':'))
    if len(encoded)>limit:
        # Quote identity/freshness, bars, and executable prices are separate
        # typed fields. Descriptive evidence must never suppress a candidate.
        payload['live_windows']={w:{k:v for k,v in row.items() if k in
            ('event_count','change_bps','tick_volume')} for w,row in windows.items()}
        payload['cheap_discovery_sources']=payload['cheap_discovery_sources'][:3]
        if compact:compact['quote_sequence']=[]
        encoded=json.dumps(payload,separators=(',',':'))
    if len(encoded)>limit:
        payload['session_opportunity_dossier']=None
        payload['live_window_material_signature']=[]
        encoded=json.dumps(payload,separators=(',',':'))
    if len(encoded)>limit:
        # Raw evidence is already durable. Never discard actionable typed
        # context because a broker/model description exhausts this text slot.
        encoded=json.dumps({'descriptive_evidence_truncated':True,'execution_feedback':feedback},separators=(',',':'))
    return encoded


def execution_cost_evidence(state):
    sides={}
    for x in state.get('executions',{}).values():
        q=Decimal(x['quantity'])
        if q<=0:continue
        side=state['orders'][x['intent_id']]['intent']['side']
        row=sides.setdefault(side,{'fills':0,'shares':Decimal(0),'fees':Decimal(0)})
        row['fills']+=1;row['shares']+=q;row['fees']+=Decimal(x['fee_usd'])
    return json.dumps({'scope':'observed_effective_sandbox_fills_not_a_universal_fee_schedule',
        'broker_fee_history':{side:{'fills':v['fills'],'shares':str(v['shares']),'fees_usd':str(v['fees']),
            'mean_fee_per_share_usd':str((v['fees']/v['shares']).quantize(Decimal('.00000001')))} for side,v in sides.items()},
        'slippage':'unmeasured; quote spread is observed separately; avoid double counting',
        'model_cost':'recorded in performance memory; current call is sunk for OPEN and ABSTAIN'},separators=(',',':'))


def build_alpha_context(universe,state,now,*,instrument_ids=None,recent_outcomes=(),position_plans=None,
        original_position_plans=None,
        observed_top_runners=(),opportunity_dossiers=None,include_market_context=True,daily_research=None,
        financial_research=None):
    opportunity_dossiers=opportunity_dossiers or {}
    daily_research=daily_research or {}
    financial_research=financial_research or {}
    wanted=set(instrument_ids) if instrument_ids is not None else None
    candidates=[]
    for o in universe.candidates:
        held=o.instrument_id in state['positions']
        if wanted is not None and o.instrument_id not in wanted:continue
        if wanted is None and not (o.entry_eligible or held):continue
        if not held:
            try:o.require_fresh(now)
            except ValueError:continue
        bars=[]
        for row in o.bars:
            bars.append(AlphaBar(at=row['time'],open_usd=row['open'],high_usd=row['high'],
                low_usd=row['low'],close_usd=row['close'],volume=row['volume']))
        closes=[x.close_usd for x in sorted(bars,key=lambda x:x.at)]
        movement=((closes[-1]-closes[0])/closes[0]).quantize(Decimal('0.00000001')) if len(closes)>1 else None
        volatility=((max(closes)-min(closes))/closes[0]).quantize(Decimal('0.00000001')) if len(closes)>1 else None
        candidates.append(AlphaInstrumentContext(instrument_id=o.instrument_id,symbol=o.ticker,quote_at=o.quote_at,
            bid_usd=o.bid,ask_usd=o.ask,price_increment_usd=o.instrument.price_increment_usd,
            quantity_increment=o.instrument.quantity_increment,reference_price_usd=(o.bid+o.ask)/2,
            spread_bps=o.features()['spread_bps'],bid_size=o.bid_size,ask_size=o.ask_size,
            relative_volume_10d=o.relative_volume_10d,bars=tuple(bars),volume=o.volume,
            volatility=volatility,price_movement=movement,
            market_context=(compact_market_context(o,opportunity_dossiers.get(o.instrument_id))
                if include_market_context else None),sector_context=financial_research.get(o.instrument_id),
            daily_research=daily_research.get(o.instrument_id)))
    if not candidates:raise ValueError('ALPHA_CONTEXT_REQUIRES_INSTRUMENT')
    observed={x.instrument_id:x for x in candidates};positions=[];position_plans=position_plans or {}
    original_position_plans=original_position_plans or {}
    liquidation=datetime.fromisoformat(state['session']['closes_at'])-timedelta(minutes=5)
    for iid,p in state['positions'].items():
        o=observed.get(iid)
        if o is None:continue
        q=Decimal(p['quantity']);basis=Decimal(p['basis_usd']);mark=Decimal(p['mark_usd'])
        plan=position_plans.get(iid)
        original=original_position_plans.get(iid)
        positions.append(AlphaPositionContext(position_id='position-'+iid,instrument_id=iid,symbol=o.symbol,
            quantity=q,average_entry_usd=basis/q,mark_usd=mark,active_stop_usd=p['stop_usd'],target_usd=plan.target_usd if plan else None,
            latest_exit_at=plan.latest_exit_at if plan else liquidation,
            unrealized_pnl_usd=mark*q-basis,
            strategy_label=original.strategy_label if original else (plan.strategy_label if plan else None),
            original_thesis=original.thesis if original else (plan.thesis if plan else None),
            entry_limit_usd=original.entry_limit_usd if original else (plan.entry_limit_usd if plan else None),
            original_stop_usd=original.original_stop_usd if original else (plan.original_stop_usd if plan else None),
            original_target_usd=original.target_usd if original else None,
            next_review_at=plan.next_review_at if plan else None,
            plan_version=plan.version if plan else None))
    equity=Decimal(state['cash']['liquidation_equity_usd']);basis=min(equity,Decimal(state['day_start_equity']))
    used=sum((Decimal(p['open_risk_usd']) for p in state['positions'].values()),Decimal(0))
    used+=sum((Decimal(r['risk_usd']) for r in state['reservations'].values()),Decimal(0))
    session=state['session'];expires=min(now+timedelta(seconds=90),datetime.fromisoformat(session['closes_at']))
    return AlphaContext(as_of=now,expires_at=expires,session_open=session['opens_at'],session_close=session['closes_at'],
        liquidation_at=liquidation,instruments=tuple(candidates),positions=tuple(positions),
        settled_cash_usd=state['cash']['settled_usd'],remaining_risk_usd=max(Decimal(0),aggregate_risk_limit(state)-used),
        reconciliation_ready=state['reconciliation']['ready'],settlement_known=state['reconciliation']['settlement_known'],
        recent_outcomes=tuple(recent_outcomes),observed_top_runners=tuple(observed_top_runners),
        execution_cost_evidence=execution_cost_evidence(state),economic_policy_hash=state.get('policy_hash'),
        economic_policy_revision=policy_revision(state),economic_policy=(effective_policy(state).model_dump_json() if 'economic_policy' in state else None),
        immutable_boundaries=IMMUTABLE_BOUNDARIES if 'economic_policy' in state else None)


def translate_open(decision: AlphaDecision,context: AlphaContext,account_id,now,decision_id,*,capability_version='mvp-local-monitor-v1',
        market_regime='UNCLASSIFIED',model_cost_usd=Decimal(0),shadow_audit_id=None,episode_id=None):
    p=decision.open
    if decision.action!='OPEN' or p is None:raise ValueError('OPEN_REQUIRED')
    instrument=next((x for x in context.instruments if x.instrument_id==p.instrument_id),None)
    if instrument is None:raise ValueError('ALPHA_INSTRUMENT_MISSING')
    quantity=p.desired_quantity if p.desired_quantity is not None else floor_quantity(
        p.desired_notional_usd,p.entry_limit_usd,instrument.quantity_increment)
    if quantity<=0:raise ValueError('ALPHA_SIZE_BELOW_INCREMENT')
    episode=episode_id or 'alpha-'+p.instrument_id
    protection=Protection(initial_stop_usd=p.hard_stop_usd,trigger_reference='BID',trigger_source_id=SOURCE,
        execution_policy_id='local-exit-v1',mechanism='LOCAL_MONITORED_STOP',stop_limit_usd=None)
    intent=OrderIntent(intent_id=decision_id,client_order_id=uuid4().hex,account_id=account_id,
        environment='OFFLINE',instrument_id=p.instrument_id,side='BUY',quantity=quantity,order_type='LIMIT',
        session='CORE',time_in_force='DAY',limit_price_usd=p.entry_limit_usd,trigger_price_usd=None,
        maximum_entry_usd=p.entry_limit_usd,protection=protection,plan_id=decision_id,episode_id=episode,
        deadline=p.entry_expires_at,capability_version=capability_version)
    plan=AlphaManagedPlan(plan_id=decision_id,instrument_id=p.instrument_id,symbol=p.symbol,episode_id=episode,
        strategy_label=p.strategy_label,thesis=p.thesis,quantity=quantity,entry_limit_usd=p.entry_limit_usd,
        original_stop_usd=p.hard_stop_usd,active_stop_usd=p.hard_stop_usd,target_usd=p.target_usd,
        entry_expires_at=p.entry_expires_at,next_review_at=p.next_review_at,
        original_latest_exit_at=context.exit_horizon,latest_exit_at=p.latest_exit_at,
        market_regime=market_regime,initial_model_cost_usd=model_cost_usd,shadow_audit_id=shadow_audit_id)
    return intent,plan


def baseline_snapshot(context):
    """Cheap counterfactual benchmarks only; output has no execution authority."""
    rows=[]
    for item in context.instruments:
        bars=sorted(item.bars,key=lambda x:x.at)
        move=(bars[-1].close_usd-bars[0].open_usd)/bars[0].open_usd if bars else Decimal(0)
        rows.append((item.symbol,item.instrument_id,move,item.ask_usd))
    strongest=max(rows,key=lambda x:x[2]);weakest=min(rows,key=lambda x:x[2])
    spy=next((x for x in rows if x[0]=='SPY'),None)
    specs=[('simple_momentum',strongest,strongest[2]),('strongest_relative_strength',strongest,strongest[2]),
        ('simple_mean_reversion',weakest,-weakest[2]),('largest_drop_rebound',weakest,-weakest[2]),
        ('top_mover_fixed_horizon',strongest,strongest[2])]
    if spy:specs.append(('intraday_spy_hold',spy,spy[2]))
    return {'at':context.as_of.isoformat(),'due_at':(context.as_of+timedelta(minutes=5)).isoformat(),'benchmarks':[
        {'name':name,'symbol':item[0],'instrument_id':item[1],'signal':str(signal),'entry_usd':str(item[3]),
            'entry_reference':'ASK'}
        for name,item,signal in specs]}


def evaluate_baseline(snapshot,context):
    if context.as_of<datetime.fromisoformat(snapshot['due_at']):return None
    marks={x.instrument_id:x.bid_usd for x in context.instruments};rows=[]
    for item in snapshot['benchmarks']:
        mark=marks.get(item['instrument_id']);entry=Decimal(item['entry_usd'])
        rows.append({**item,'exit_usd':str(mark) if mark is not None else None,
            'gross_return_bps':str((mark-entry)/entry*10000) if mark is not None else None})
    return {'started_at':snapshot['at'],'evaluated_at':context.as_of.isoformat(),'benchmarks':rows}


def recover_alpha_binding(state,store,instrument_id):
    position=state['positions'].get(instrument_id)
    if position is None or len(position['originating_orders'])!=1:raise ValueError('ALPHA_POSITION_ORIGIN_AMBIGUOUS')
    order=state['orders'][position['originating_orders'][0]];intent=OrderIntent.model_validate(order['intent'])
    raw=store.alpha_plan(intent.plan_id)
    if (not isinstance(raw,dict) or order['phase']!='FILLED' or intent.side!='BUY'
            or raw.get('plan_id')!=intent.plan_id or raw.get('instrument_id')!=instrument_id
            or raw.get('episode_id')!=intent.episode_id
            or Decimal(str(raw.get('quantity','0')))!=Decimal(position['quantity'])):
        raise ValueError('PERSISTED_ALPHA_PLAN_BINDING_MISMATCH')
    return intent,raw


def recover_alpha_plan(state,store,instrument_id):
    intent,raw=recover_alpha_binding(state,store,instrument_id)
    plan=AlphaManagedPlan.model_validate(raw)
    return intent,plan


def completed_alpha_trade(state,plan_id,instrument_id,episode_id):
    """Effective fills for one completed plan, including partial fills/exit attempts.

    An episode is a risk-accounting identity and may contain sequential plans.
    Only executions bound to this plan determine its realized outcome.
    """
    buys=[];sells=[]
    for x in state['executions'].values():
        order=state['orders'][x['intent_id']];intent=order['intent']
        if intent['plan_id']!=plan_id:continue
        if intent['instrument_id']!=instrument_id or intent['episode_id']!=episode_id:
            raise ValueError('ALPHA_TRADE_BINDING_MISMATCH')
        q=Decimal(x['quantity'])
        if q<=0:continue
        if intent['side']=='BUY':buys.append(x)
        elif intent['side']=='SELL_OWNED':sells.append(x)
        else:raise ValueError('ALPHA_TRADE_SIDE_UNKNOWN')
    buy_q=sum((Decimal(x['quantity']) for x in buys),Decimal(0))
    sell_q=sum((Decimal(x['quantity']) for x in sells),Decimal(0))
    if buy_q<=0 or sell_q!=buy_q:
        raise ValueError('ALPHA_TRADE_NOT_CLOSED')
    buy_value=sum((Decimal(x['quantity'])*Decimal(x['price_usd']) for x in buys),Decimal(0))
    sell_value=sum((Decimal(x['quantity'])*Decimal(x['price_usd']) for x in sells),Decimal(0))
    fees=sum((Decimal(x['fee_usd']) for x in buys+sells),Decimal(0))
    return {'quantity':buy_q,'entry_usd':buy_value/buy_q,'exit_usd':sell_value/sell_q,
        'capital_deployed_usd':buy_value,'realized_gross_usd':sell_value-buy_value,
        'broker_fees_usd':fees,'opened_at':min(datetime.fromisoformat(x['executed_at']) for x in buys)}


class AlphaPortfolioManager:
    """Nonblocking deterministic protection for up to the M2 hard position limit."""
    ready=True
    def __init__(self,store,*,review,emit,exit_manager,prepare_review=None):
        self.store,self.review,self.emit,self.exit_manager=store,review,emit,exit_manager
        self.prepare_review=prepare_review
        self.executor=ThreadPoolExecutor(max_workers=4,thread_name_prefix='astra-fast-position')
        self.pending={};self.metrics={}

    def _submit_review(self,observation,plan):
        # Snapshot construction stays on the writer thread. Workers receive
        # immutable model context and cannot read or mutate the ledger.
        if self.prepare_review is not None:
            work=self.prepare_review(observation,plan)
            return self.executor.submit(work)
        return self.executor.submit(self.review,observation,plan)

    def plans(self,state):
        return {iid:recover_alpha_plan(state,self.store,iid)[1] for iid in state['positions']}

    def _metrics(self,plan,mark):
        current=self.metrics.get(plan.plan_id)
        if current is not None:return current
        saved=self.store.alpha_metrics(plan.plan_id,plan.instrument_id) if hasattr(self.store,'alpha_metrics') else None
        if saved:
            current={'max_bid':Decimal(saved['max_bid_usd']),'min_bid':Decimal(saved['min_bid_usd']),
                'mfe':Decimal(saved['maximum_favorable_excursion_usd']),
                'mae':Decimal(saved['maximum_adverse_excursion_usd']),
                'model_cost':Decimal(saved.get('position_model_cost_usd','0')),'_checkpointed':True}
            if saved.get('exit_reason'):current['exit_reason']=saved['exit_reason']
        else:current={'max_bid':mark,'min_bid':mark,'mfe':Decimal(0),'mae':Decimal(0),'model_cost':Decimal(0),'_checkpointed':False}
        self.metrics[plan.plan_id]=current
        return current

    def _persist_metrics(self,plan,m,summary='Durable position excursion checkpoint'):
        self.emit('ALPHA_POSITION_METRICS',{'summary':summary,'plan_id':plan.plan_id,
            'instrument_id':plan.instrument_id,'max_bid_usd':str(m['max_bid']),'min_bid_usd':str(m['min_bid']),
            'maximum_favorable_excursion_usd':str(m.get('mfe',Decimal(0))),
            'maximum_adverse_excursion_usd':str(m.get('mae',Decimal(0))),
            'position_model_cost_usd':str(m.get('model_cost',Decimal(0))),'exit_reason':m.get('exit_reason')})
        m['_checkpointed']=True

    def _outcome(self,plan,before,result,at,state):
        trade=completed_alpha_trade(state,plan.plan_id,plan.instrument_id,plan.episode_id)
        q=trade['quantity'];entry=trade['entry_usd'];exit_price=trade['exit_usd']
        m=self.metrics.get(plan.plan_id,{})
        broker_fees=trade['broker_fees_usd']
        model_cost=plan.initial_model_cost_usd+Decimal(m.get('model_cost',0))
        gross=trade['realized_gross_usd'];net_after_broker=gross-broker_fees;economic_net=net_after_broker-model_cost
        opened_at=trade['opened_at'];holding_seconds=max(0,int((at-opened_at).total_seconds()))
        capital=trade['capital_deployed_usd']
        shadow=self.store.shadow_audit(plan.shadow_audit_id) if plan.shadow_audit_id and hasattr(self.store,'shadow_audit') else None
        self.emit('ALPHA_OUTCOME',{'summary':'Closed alpha position '+plan.symbol,'plan_id':plan.plan_id,
            'instrument_id':plan.instrument_id,'symbol':plan.symbol,'strategy_label':plan.strategy_label,'thesis':plan.thesis,
            'entry_usd':str(entry),'exit_usd':str(exit_price),'quantity':str(q),
            'realized_gross_usd':str(gross),'broker_fees_usd':str(broker_fees),
            'realized_net_after_broker_fees_usd':str(net_after_broker),
            'realized_net_after_all_known_costs_usd':str(economic_net),
            'return_on_capital':str(economic_net/capital),'holding_seconds':holding_seconds,
            'capital_deployed_usd':str(capital),'capital_time_usd_seconds':str(capital*holding_seconds),
            'maximum_favorable_excursion_usd':str(m.get('mfe',Decimal(0))),
            'maximum_adverse_excursion_usd':str(m.get('mae',Decimal(0))),
            'win_loss':'WIN' if economic_net>0 else ('LOSS' if economic_net<0 else 'FLAT'),
            'market_regime':plan.market_regime,'exit_reason':m.get('exit_reason'),
            'failure_origin':('INFRASTRUCTURE_OR_SAFETY' if m.get('exit_reason') in
                ('SAFETY_LATCH','OPEN_POSITION_OUTSIDE_SESSION','RECOVERY','FORCED_LIQUIDATION') else 'ASTRA_DECISION'),
            'outcome_reason':m.get('last_decision_reason') or m.get('exit_reason'),
            'model_cost_usd':str(model_cost),'evidence_quality':'FULL_RUNTIME',
            'shadow_audit_id':plan.shadow_audit_id,'shadow_decision':shadow,
            'opened_at':opened_at.isoformat(),'closed_at':at.isoformat()})
        self.metrics.pop(plan.plan_id,None)

    def _exit(self,intent,plan,o,gateway,controller,session,clock,reason,conditions):
        before=dict(controller.state['positions'][intent.instrument_id]);m=self._metrics(plan,Decimal(before['mark_usd'])) if plan is not None else self.metrics.setdefault(intent.instrument_id,{})
        m['exit_reason']=reason
        if plan is not None:self._persist_metrics(plan,m,'Durable position exit checkpoint')
        result=self.exit_manager._exit_owned(intent,o,gateway,controller,session,clock,reason,conditions)
        if plan is not None:self._outcome(plan,before,result,clock(),controller.state)
        else:self.emit('ALPHA_OUTCOME_DEFERRED',{'summary':'Safety exit completed; invalid alpha plan retained for ledger-derived outcome rebuild',
            'instrument_id':intent.instrument_id,'plan_id':intent.plan_id,'exit_reason':reason})
        self.pending.pop(intent.instrument_id,None)
        return result

    def _persist(self,plan,summary):
        self.emit('ALPHA_PLAN_MODIFIED',{'summary':summary,'plan_id':plan.plan_id,
            'instrument_id':plan.instrument_id,'plan':plan.model_dump(mode='json')})

    @staticmethod
    def _cross_session_plan(plan,session,now):
        """Expose a carried position to Astra without reviving stale session authority."""
        review_at=min(now+timedelta(minutes=1),session.liquidation_at)
        return plan.model_copy(update={'original_latest_exit_at':session.liquidation_at,
            'latest_exit_at':session.liquidation_at,'next_review_at':review_at,'version':plan.version+1})

    def tick(self,universe,controller,gateway,session,clock):
        now=clock();state=controller.state
        for iid in list(state['positions']):
            intent,raw_plan=recover_alpha_binding(state,self.store,iid);o=universe.held_for_exit(iid,now)
            p=state['positions'][iid];q=Decimal(p['quantity']);basis=Decimal(p['basis_usd']);mark=o.bid
            plan_for_metrics=None
            try:plan_for_metrics=AlphaManagedPlan.model_validate(raw_plan)
            except Exception:pass
            m=self._metrics(plan_for_metrics,mark) if plan_for_metrics is not None else self.metrics.setdefault(iid,{'max_bid':mark,'min_bid':mark,'model_cost':Decimal(0)})
            old=(m['max_bid'],m['min_bid'])
            m['max_bid']=max(m['max_bid'],mark);m['min_bid']=min(m['min_bid'],mark)
            m['mfe']=max(Decimal(0),q*m['max_bid']-basis);m['mae']=max(Decimal(0),basis-q*m['min_bid'])
            if plan_for_metrics is not None and (old!=(m['max_bid'],m['min_bid']) or not m.get('_checkpointed')):
                self._persist_metrics(plan_for_metrics,m)
            # Durable owned quantity, durable protection, the current session
            # boundary, and latches are sufficient to execute a due safety exit.
            # Full alpha-plan/model validation must never starve this path.
            try:raw_deadline=datetime.fromisoformat(str(raw_plan['latest_exit_at']))
            except (KeyError,TypeError,ValueError):raw_deadline=session.liquidation_at
            carried_raw=raw_deadline<session.opens_at
            safety_conditions={'STOP':not carried_raw and mark<=Decimal(p['stop_usd']),
                'ABSOLUTE_LATEST_EXIT':not carried_raw and now>=min(raw_deadline,session.liquidation_at),
                'SAFETY_LATCH':any(state['latches'].values())}
            due=[name for name,value in safety_conditions.items() if value]
            if due:
                try:validated_plan=AlphaManagedPlan.model_validate(raw_plan)
                except Exception:validated_plan=None
                self._exit(intent,validated_plan,o,gateway,controller,session,clock,due[0],due)
                return {'exited':iid,'reason':due[0]}
            plan=AlphaManagedPlan.model_validate(raw_plan)
            active=max(plan.active_stop_usd,Decimal(p['stop_usd']))
            if Decimal(p['protected_quantity'])<q and active<mark:
                controller.send(ProtectionCommand(instrument_id=iid,quantity=q,stop_usd=active,evidence_id='alpha-manager-armed'))
                state=controller.state;p=state['positions'][iid]
            carried=plan.latest_exit_at<session.opens_at
            if carried:
                # A prior-session alpha plan cannot sell or manage the position in a
                # new session. Only a hard safety latch retains pre-review authority.
                if any(state['latches'].values()):
                    self._exit(intent,plan,o,gateway,controller,session,clock,'SAFETY_LATCH',['SAFETY_LATCH'])
                    return {'exited':iid,'reason':'SAFETY_LATCH'}
                try:o.require_monitor_fresh(now)
                except ValueError as exc:
                    if str(exc)!='STALE_MARKET_OR_ACCOUNT':raise
                    self.emit('POSITION_MARKET_DATA_FAILURE',{'summary':'Fresh held quote unavailable; Astra review withheld',
                        'instrument_id':iid,'reason':str(exc),'quote_at':o.quote_at.isoformat(),'checked_at':now.isoformat()})
                    continue
                pending=self.pending.get(iid)
                if pending and pending['future'].done():
                    review_plan=pending['review_plan']
                    try:
                        ai,context=pending['future'].result();d=AlphaDecision.model_validate(ai['decision'])
                        assert_policy_current(context,controller.state)
                        current=controller.state['positions'].get(iid)
                        if current is None or Decimal(current['quantity'])!=plan.quantity:raise ValueError('POSITION_CHANGED_DURING_ALPHA_REVIEW')
                        cost=ai.get('estimated_cost_usd');m['model_cost']+=Decimal(cost or 0);self._persist_metrics(plan,m,'Durable position model-cost checkpoint')
                        if d.action=='CLOSE':
                            self._exit(intent,plan,o,gateway,controller,session,clock,'ASTRA_CLOSE',['ASTRA_CLOSE'])
                            self.emit('ALPHA_POSITION_REVIEW',{'summary':'Astra priority carried-position action CLOSE',
                                'action':'CLOSE','cross_session_reassessment':True,'ai':ai})
                            return {'exited':iid,'reason':'ASTRA_CLOSE'}
                        if d.action=='HOLD':
                            review_plan=review_plan.model_copy(update={'next_review_at':d.hold.next_review_at})
                        elif d.action=='MODIFY':
                            change=d.modify;stop=change.requested_stop_usd or review_plan.active_stop_usd
                            if not review_plan.active_stop_usd<=stop<o.bid:raise ValueError('ALPHA_MODIFY_STOP_STATE_CHANGED')
                            target=review_plan.target_usd
                            if change.target_action=='SET':target=change.requested_target_usd
                            if change.target_action=='REMOVE':target=None
                            review_plan=review_plan.model_copy(update={'active_stop_usd':stop,'target_usd':target,
                                'next_review_at':change.requested_next_review_at or review_plan.next_review_at,
                                'latest_exit_at':change.requested_latest_exit_at or review_plan.latest_exit_at})
                            if stop>Decimal(current['stop_usd']):controller.send(ProtectionCommand(
                                instrument_id=iid,quantity=q,stop_usd=stop,evidence_id='alpha-cross-session-'+plan.plan_id))
                        else:raise ValueError('ALPHA_POSITION_ACTION_INVALID')
                        self._persist(review_plan,'Validated Astra cross-session position reassessment')
                        self.emit('ALPHA_POSITION_REVIEW',{'summary':'Astra priority carried-position action '+d.action,
                            'action':d.action,'cross_session_reassessment':True,'ai':ai})
                    except Exception:
                        m['cross_session_retry_at']=now+timedelta(minutes=1)
                        self.emit('ALPHA_POSITION_REVIEW',{'summary':'Astra priority carried-position review rejected; no prior-session alpha authority applied',
                            'action':'INVALID','cross_session_reassessment':True})
                    finally:self.pending.pop(iid,None)
                    continue
                retry_at=m.get('cross_session_retry_at')
                if pending is None and (retry_at is None or now>=retry_at):
                    review_plan=self._cross_session_plan(plan,session,now)
                    self.pending[iid]={'future':self._submit_review(o,review_plan),
                        'requested_at':now,'review_plan':review_plan}
                    self.emit('ALPHA_POSITION_REVIEW_REQUESTED',{'summary':'Priority Astra reassessment for position carried across sessions',
                        'plan_id':plan.plan_id,'instrument_id':iid,'cross_session_reassessment':True})
                continue
            target_review_due=(plan.target_usd is not None and mark>=plan.target_usd
                and m.get('target_reviewed_version')!=plan.version)
            try:o.require_monitor_fresh(now)
            except ValueError as exc:
                if str(exc)!='STALE_MARKET_OR_ACCOUNT':raise
                self.emit('POSITION_MARKET_DATA_FAILURE',{'summary':'Fresh held quote unavailable; Astra review withheld',
                    'instrument_id':iid,'reason':str(exc),'quote_at':o.quote_at.isoformat(),'checked_at':now.isoformat()})
                continue
            pending=self.pending.get(iid)
            if pending and pending['future'].done():
                try:
                    ai,context=pending['future'].result();raw=ai['decision'];d=AlphaDecision.model_validate(raw)
                    assert_policy_current(context,controller.state)
                    current=controller.state['positions'].get(iid)
                    if current is None or Decimal(current['quantity'])!=plan.quantity:raise ValueError('POSITION_CHANGED_DURING_ALPHA_REVIEW')
                    cost=ai.get('estimated_cost_usd');m['model_cost']+=Decimal(cost or 0);self._persist_metrics(plan,m,'Durable position model-cost checkpoint')
                    if d.action=='CLOSE':
                        m['last_decision_reason']=d.reason
                        self._exit(intent,plan,o,gateway,controller,session,clock,'ASTRA_CLOSE',['ASTRA_CLOSE'])
                        self.emit('ALPHA_POSITION_REVIEW',{'summary':'Astra fast position action CLOSE','action':'CLOSE','ai':ai});return {'exited':iid,'reason':'ASTRA_CLOSE'}
                    if d.action=='HOLD':
                        plan=plan.model_copy(update={'next_review_at':d.hold.next_review_at,'version':plan.version+1})
                    elif d.action=='MODIFY':
                        change=d.modify;stop=change.requested_stop_usd or plan.active_stop_usd
                        if not plan.active_stop_usd<=stop<o.bid:raise ValueError('ALPHA_MODIFY_STOP_STATE_CHANGED')
                        target=plan.target_usd
                        if change.target_action=='SET':target=change.requested_target_usd
                        if change.target_action=='REMOVE':target=None
                        plan=plan.model_copy(update={'active_stop_usd':stop,'target_usd':target,
                            'next_review_at':change.requested_next_review_at or plan.next_review_at,
                            'latest_exit_at':change.requested_latest_exit_at or plan.latest_exit_at,'version':plan.version+1})
                        if stop>Decimal(current['stop_usd']):controller.send(ProtectionCommand(instrument_id=iid,quantity=q,stop_usd=stop,evidence_id='alpha-modify-'+plan.plan_id))
                    else:raise ValueError('ALPHA_POSITION_ACTION_INVALID')
                    self._persist(plan,'Validated Astra '+d.action+' position plan')
                    m['target_reviewed_version']=plan.version
                    # The decision is applied against the writer thread's
                    # latest observation. Evidence accumulated during the call
                    # is consumed instead of triggering an immediate duplicate.
                    m['review_material_signature']=o.stream_material_signature
                    self.emit('ALPHA_POSITION_REVIEW',{'summary':'Astra fast position action '+d.action,'action':d.action,'ai':ai})
                except Exception:
                    retry=min(now+timedelta(minutes=1),plan.latest_exit_at)
                    plan=plan.model_copy(update={'next_review_at':retry,'version':plan.version+1});self._persist(plan,'Invalid fast review; deterministic protection retained')
                    m['target_reviewed_version']=plan.version
                    self.emit('ALPHA_POSITION_REVIEW',{'summary':'Astra fast position review rejected; protection retained','action':'INVALID'})
                finally:self.pending.pop(iid,None)
            # Astra owns review timing. Ordinary stream wiggles remain in the
            # next fresh context but cannot replace the horizon it selected.
            # A model-owned target still wakes an immediate reassessment; hard
            # protection and the session boundary are handled above.
            if iid not in self.pending and (now>=plan.next_review_at or target_review_due):
                self.pending[iid]={'future':self._submit_review(o,plan),'requested_at':now}
                self.emit('ALPHA_POSITION_REVIEW_REQUESTED',{'summary':'Astra priority target reassessment started' if target_review_due else 'Astra fast position review started',
                    'plan_id':plan.plan_id,'instrument_id':iid,'target_reassessment':target_review_due,
                    'scheduled_reassessment':now>=plan.next_review_at})
        return {'positions_monitored':len(state['positions'])}
