"""One evaluation. Model has no gateway handle; only M2 grants dispatch authority."""
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4
from ..contracts import OrderIntent
from ..decisions import parse_trade_decision, validate_semantics
from ..numeric import floor_quantity
from .config import ExecutionMode


def predicates_match(group,features):
    values=[]
    for p in group.predicates:
        if p.persistence_ms or p.feature_id not in features or p.source_id!='WEBULL_SANDBOX_READ_ONLY':
            raise ValueError('UNSUPPORTED_MVP_PREDICATE')
        a,b=features[p.feature_id],Decimal(p.value)
        values.append({'LT':a<b,'LTE':a<=b,'GT':a>b,'GTE':a>=b,'EQ':a==b,'NEQ':a!=b}[p.operator])
    if not values:raise ValueError('EMPTY_PREDICATE')
    return all(values) if group.join=='ALL' else any(values)


def translate(decision,context,account_id,now):
    p=decision.entry_plan
    expected_episode='mvp-'+context.instrument.ticker.lower()
    if (p is None or p.entry_order_type!='LIMIT' or p.ticker!=context.instrument.ticker
            or p.strategy_episode_id!=expected_episode or p.protection.mechanism!='LOCAL_MONITORED_STOP'
            or p.trailing.mode!='NONE' or len(p.targets)>1
            or any(t.fraction_of_initial_quantity!=1 for t in p.targets)):
        raise ValueError('UNSUPPORTED_MVP_PLAN')
    if decision.forecast is None or decision.forecast.expected_gross_return_bps is None or decision.forecast.estimated_execution_cost_bps is None:
        raise ValueError('POSITIVE_NET_EXPECTATION_REQUIRED')
    if decision.forecast.expected_gross_return_bps<=decision.forecast.estimated_execution_cost_bps:
        raise ValueError('NONPOSITIVE_EXPECTED_NET_RETURN')
    q=p.sizing.amount if p.sizing.unit=='SHARES' else floor_quantity(p.sizing.amount,p.maximum_entry_usd,Decimal(1))
    if q<max(Decimal(1),p.sizing.minimum_acceptable_quantity) or q%1:
        raise ValueError('WHOLE_SHARES_REQUIRED')
    if now>=min(p.entry_expires_at,decision.valid_until,p.latest_exit_at):raise ValueError('PLAN_EXPIRED')
    return OrderIntent(intent_id=decision.decision_id,client_order_id=uuid4().hex,account_id=account_id,
        environment='OFFLINE',instrument_id=p.instrument_id,side='BUY',quantity=q,order_type='LIMIT',
        session='CORE',time_in_force='DAY',limit_price_usd=p.limit_price_usd,trigger_price_usd=None,
        maximum_entry_usd=p.maximum_entry_usd,protection=p.protection,plan_id=decision.decision_id,
        episode_id=p.strategy_episode_id,deadline=min(p.entry_expires_at,decision.valid_until),
        capability_version=context.capabilities.version)


def run_trading_cycle(*,config,calendar,clock,source,brain,controller,gateway,context_factory,monitor,emit=lambda kind,payload:None):
    """Injected I/O supports the exact same deterministic path in DRY_RUN/SANDBOX."""
    report={'astra_action':None,'risk_approved':False,'order_submitted':False,'generated_intent':None,
        'broker_status':None,'mode':config.mode.value,'session_gate':None,'blocker':None}
    try:
        config.validate()
        now=clock(); session=calendar.session(now.astimezone(__import__('zoneinfo').ZoneInfo('America/New_York')).date())
        if session is None or not session.permits_entry(now):
            report.update(session_gate='CLOSED_OR_ENTRY_CUTOFF',blocker='CLOSED_OR_ENTRY_CUTOFF')
            return report
        report['session_gate']='OPEN'
        observed=source.refresh(controller.state)
        observed.require_fresh(clock());controller.refresh(observed)
        if hasattr(observed,'candidates'):
            emit('ACTIVITY',{'summary':'Astra candidate selection in progress'})
            selection=brain.select_candidate(observed,controller.state,clock=clock)
            emit('ASTRA_SELECTION',{'summary':'Astra selected '+selection['selected_symbol']+' for detailed evaluation','selection':selection})
            report['selection']=selection
            observed=source.refresh(controller.state)
            observed.require_fresh(clock());observed=observed.select(selection['selected_symbol'],clock())
            controller.refresh(observed)
        context,registry=context_factory(observed,controller.state,clock())
        emit('ACTIVITY',{'summary':'Astra evaluation in progress'})
        ai=brain.decide(context,registry)
        emit('ASTRA_DECISION',{'ai':ai})
        report['ai']=ai
        if not ai.get('schema_valid') or not ai.get('semantics_valid') or not ai.get('decision'):
            raise ValueError('INVALID_AI_DECISION')
        decision=parse_trade_decision(__import__('json').dumps(ai['decision']))
        validate_semantics(decision,context,registry,clock())
        report['astra_action']=decision.action
        if decision.action in ('ABSTAIN','REQUEST_DATA'):return report
        if decision.action!='PROPOSE_ENTRY':raise ValueError('ACTION_NOT_ALLOWED')
        latest=source.refresh(controller.state)
        latest.require_fresh(clock())
        observed=latest.select(decision.entry_plan.ticker,clock()) if hasattr(latest,'candidates') else latest
        controller.refresh(observed)
        if not session.permits_entry(clock()):raise ValueError('CLOSED_OR_ENTRY_CUTOFF')
        if not monitor.ready:raise ValueError('LOCAL_MANAGER_UNAVAILABLE')
        if observed.positions or observed.open_intents:raise ValueError('EXISTING_POSITION_OR_OBLIGATION')
        p=decision.entry_plan
        features=observed.features()
        if not predicates_match(p.entry_condition,features):raise ValueError('ENTRY_CONDITION_FALSE')
        if predicates_match(p.invalidation,features):raise ValueError('PLAN_INVALIDATED')
        if features['spread_bps']>p.max_spread_bps:raise ValueError('SPREAD_LIMIT')
        intent=translate(decision,context,controller.account_id,clock())
        nonce=controller.admit(intent,p)  # All hard cash/exposure/risk/latch checks in M2.
        report['risk_approved']=True
        report['generated_intent']=intent.model_dump(mode='json')
        emit('INTENT_APPROVED',{'summary':'Risk approved; bounded intent ready','decision_id':decision.decision_id})
        broker=gateway.dispatch(intent,nonce,observed,controller,session,clock)
        report['broker_status']=broker['status']
        report['order_submitted']=config.mode==ExecutionMode.SANDBOX
        report['broker']=broker
        emit('BROKER_RESULT',{'summary':'Entry REST result: '+broker['status'],'broker':broker})
        if broker['status']=='FILLED':monitor.manage(intent,p,source,gateway,controller,session,clock)
        report['resulting_positions']=controller.state['positions']
    except Exception as exc:
        # Exceptions may contain broker/DB details; expose only registered local codes.
        reason=str(exc)
        report['blocker']=reason if __import__('re').fullmatch(r'[A-Z_ ,]{1,150}',reason) else 'CYCLE_FAILED_RECONCILE_REQUIRED'
    report['order_submitted']=bool(getattr(gateway,'submission_attempted',False))
    return report
