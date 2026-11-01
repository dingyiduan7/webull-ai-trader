"""Continuous scheduling around the existing cycle; never a second risk engine."""
import json, os, threading
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from dataclasses import replace
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from math import floor, log
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo
from ..contracts import AlphaDecision,DailyResearchContext,OrderIntent
from ..decisions import (parse_alpha_decision,parse_trade_decision,validate_alpha_semantics,
    DecisionError,validate_entry_window,alpha_semantic_failure_code)
from ..durable.ledger import LocalDatabase,Ledger,migrate,grant_writer
from ..durable.models import FinalizeOrder,RefreshPermit,Mark
from ..durable.projection import Rejected
from ..mvp.config import RuntimeConfig,ExecutionMode,reviewed_calendar
from ..mvp.context import (SandboxSource,UniverseObservation,make_context,make_position_review_context,
    INSTRUCTIONS,POSITION_REVIEW_INSTRUCTIONS,DISCOVERY_CACHE_SECONDS)
from ..mvp.alpha import (AlphaPortfolioManager,baseline_snapshot,build_alpha_context,evaluate_baseline,
    translate_open)
from ..mvp.controller import Controller
from ..mvp.bootstrap import prepare_controller,refresh_session
from ..mvp.cycle import run_trading_cycle
from ..mvp.gateway import ExecutionGateway,SandboxTransport,LocalPositionManager,sandbox_ledger_id
from ..execution import SimulatedBroker
from ..decision_api.astra_probe import stamp_alpha_decision
from ..decision_api.brain import load_model_key,make_brain,shadow_model
from ..risk import POLICY
from ..economic_policy import (effective_policy,captured_policy,policy_revision,assert_policy_current,
    SandboxEconomics,AuthorizeSandboxEconomics,ReviseSandboxEconomics,ReleaseSandboxPreferences,AuthorizeDailyDownsideBudget,
    AuthorizeModelBudgetCap,account_fingerprint,IMMUTABLE_BOUNDARIES)
from .model_budget import ModelBudget
from .decision_evidence import build_portfolio_decision_evidence,persist_portfolio_decision_evidence
from ..sandbox.secrets import load_credentials,PreflightError
from ..sandbox.live_stream import LiveQuoteStream
from ..sandbox.certification.sanitize import OrderSanitizer
from ..sandbox.evidence import save_json
from .store import provision
from .views import trades_from_state
from .opportunities import (HOT_REFRESH_SECONDS,HOT_WATCH_SLOTS,HOT_WATCH_TTL_SECONDS,HOT_PROMOTION_MAX_WAIT_SECONDS,
    OpportunityQueue,code as candidate_code)
from ..research.portfolio import PortfolioResearchError

NY=ZoneInfo('America/New_York')

def next_discovery_due(started_at,completed_at):
    """Earliest non-overlapping full scan under the shared endpoint budget."""
    anchor=started_at if started_at is not None else completed_at
    return max(completed_at,anchor+timedelta(seconds=DISCOVERY_CACHE_SECONDS))

def next_entry_time(calendar,now):
    for offset in range(370):
        session=calendar.session(now.astimezone(NY).date()+timedelta(days=offset))
        if session and now<session.liquidation_at:return max(now,session.opens_at)
    return None

def portfolio_call_slot(session,now):
    """Two deterministic research windows; each can reserve at most one call."""
    if session is None or not session.permits_entry(now):return None
    midpoint=session.opens_at+(session.liquidation_at-session.opens_at)/2
    return 0 if now<midpoint else 1

def portfolio_shortlist(jobs,session,limit=8):
    """Bound a current-session shortlist without granting execution authority."""
    label=session.label.isoformat()
    return tuple(job for job in jobs if job.get('current') and job.get('session')==label)[:limit]

def portfolio_research_maps(jobs):
    daily={};financial={}
    for job in jobs:
        if isinstance(job.get('daily_research'),dict):
            daily[job['instrument_id']]=DailyResearchContext.model_validate(job['daily_research'])
        if isinstance(job.get('financial_research'),str) and job['financial_research']:
            financial[job['instrument_id']]=job['financial_research']
    return daily,financial

def recover_plan_state(state,store):
    if len(state['positions'])!=1 or any(not o['final'] for o in state['orders'].values()):raise ValueError('RECOVERY_REQUIRES_RECONCILIATION')
    position=next(iter(state['positions'].values()))
    if len(position['originating_orders'])!=1:raise ValueError('RECOVERY_PLAN_AMBIGUOUS')
    order=state['orders'][position['originating_orders'][0]];intent=OrderIntent.model_validate(order['intent'])
    raw=store.plan(intent.plan_id)
    if not raw:raise ValueError('PERSISTED_VALIDATED_PLAN_MISSING')
    decision=parse_trade_decision(json.dumps(raw));plan=decision.entry_plan
    if (decision.action!='PROPOSE_ENTRY' or order['phase']!='FILLED' or intent.side!='BUY' or plan is None
        or decision.decision_id!=intent.plan_id or plan.instrument_id!=intent.instrument_id
        or plan.strategy_episode_id!=intent.episode_id or plan.protection!=intent.protection
        or plan.limit_price_usd!=intent.limit_price_usd or plan.maximum_entry_usd!=intent.maximum_entry_usd
        or Decimal(position['quantity'])!=intent.quantity):raise ValueError('PERSISTED_PLAN_BINDING_MISMATCH')
    original=plan;version=1
    amended=store.amended_plan(intent.plan_id) if hasattr(store,'amended_plan') else None
    if amended:
        from ..contracts import EntryPlan
        active=EntryPlan.model_validate(amended['plan']);version=int(amended['plan_version'])
        if (amended.get('instrument_id')!=intent.instrument_id or active.instrument_id!=original.instrument_id
                or active.strategy_episode_id!=original.strategy_episode_id or version<2
                or active.protection.initial_stop_usd<original.protection.initial_stop_usd
                or active.latest_exit_at>original.latest_exit_at or active.sizing!=original.sizing):
            raise ValueError('PERSISTED_AMENDMENT_BINDING_MISMATCH')
        plan=active
    return intent,plan,version,original

def recover_plan(state,store):
    intent,plan,_,_=recover_plan_state(state,store)
    return intent,plan

def deep_plan_due(plan,planned_at,now):
    if plan is None or planned_at is None:return True
    age=now-planned_at
    return age>=timedelta(minutes=30) or (not plan.get('focus') and age>=timedelta(minutes=10))


def learning_signature(memory):
    """Billing counters alone are not new trading evidence requiring another call."""
    rows=[]
    for raw in memory:
        try:value=json.loads(raw)
        except (ValueError,TypeError):rows.append(raw);continue
        if isinstance(value,dict) and value.get('memory_type')=='portfolio_economics':
            value={k:v for k,v in value.items() if k not in ('all_known_api_model_cost_usd',
                'economic_net_after_all_known_model_cost_usd','costed_model_calls')}
        rows.append(value)
    return rows


def deep_context_signature(context):
    """Cost-control fingerprint; excludes clocks and insignificant price noise."""
    def bucket(value,step):
        if value is None:return None
        return str((Decimal(value)/Decimal(step)).quantize(Decimal('1')))
    payload={'instruments':[{'id':x.instrument_id,'spread':bucket(x.spread_bps,'.5'),
        'relative_volume':bucket(x.relative_volume_10d,'.1'),'volatility':bucket(x.volatility,'.001'),
        'movement':bucket(x.price_movement,'.001'),'volume':bucket(x.volume,'10000')} for x in context.instruments],
        'positions':[x.instrument_id for x in context.positions],
        'settled_cash':bucket(context.settled_cash_usd,'1000'),'risk':bucket(context.remaining_risk_usd,'100'),
        'performance_memory':learning_signature(context.recent_outcomes),
        'top_runners':[(json.loads(x)['instrument_id'],json.loads(x)['percent_per_minute']) for x in context.observed_top_runners]}
    return sha256(json.dumps(payload,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def fast_context_signature(context):
    """Economic-change fingerprint for a non-hot FAST_ENTRY candidate.

    Exact top-of-book churn is not new opportunity evidence. Hot/streaming
    runners use ``hot_context_signature`` and remain responsive; this path
    buys another model call only after a material change in price, spread,
    liquidity, volume, velocity/rank, or other decision evidence.
    """
    def log_bucket(value,buckets_per_log_unit):
        if value is None:return None
        number=float(value)
        return None if number<=0 else floor(log(number)*buckets_per_log_unit)
    def market_evidence(raw):
        try:value=json.loads(raw or '{}')
        except (TypeError,ValueError):return raw
        windows=value.get('live_windows') or {}
        detection=value.get('detection_evidence') or {}
        return {'execution_feedback':value.get('execution_feedback'),
            'detection':None if not detection else {
                'source':detection.get('source'),'window_minutes':detection.get('window_minutes'),
                'move':floor(float(detection.get('window_change_percent') or 0)/2),
                'speed':floor(float(detection.get('percent_per_minute') or 0)),
                'relative_volume':log_bucket(detection.get('relative_volume_10d'),10)},
            'live_windows':{seconds:{
                'change':floor(float(row.get('change_bps') or 0)/(25 if seconds in ('1','5') else 50)),
                'volume':log_bucket(row.get('tick_volume'),4)}
                for seconds,row in windows.items() if isinstance(row,dict)}}
    focal_id=context.focal_instrument_id or context.instruments[0].instrument_id
    runners=list(map(json.loads,context.observed_top_runners))
    focal_runner=next(((rank,row) for rank,row in enumerate(runners,1)
        if row.get('instrument_id')==focal_id),None)
    runner_evidence=None if focal_runner is None else {'rank':focal_runner[0],
        'speed':floor(float(focal_runner[1].get('percent_per_minute') or 0))}
    payload={'instruments':[{'id':x.instrument_id,
        'bid':log_bucket(x.bid_usd,200),'ask':log_bucket(x.ask_usd,200),
        'spread':floor(float(x.spread_bps)/10),
        'bars':[(log_bucket(b.close_usd,200),log_bucket(b.volume,10)) for b in x.bars[-3:]],
        'volume':log_bucket(x.volume,20),'relative_volume':log_bucket(x.relative_volume_10d,10),
        'bid_size':log_bucket(x.bid_size,10),'ask_size':log_bucket(x.ask_size,10),
        'market_context':market_evidence(x.market_context)} for x in context.instruments],
        'positions':[x.model_dump(mode='json') for x in context.positions],
        'settled_cash':str(context.settled_cash_usd),'risk':str(context.remaining_risk_usd),
        'performance_memory':learning_signature(context.recent_outcomes),
        'focal_runner':runner_evidence}
    return sha256(json.dumps(payload,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def evidence_signature(raw):
    """Keep economic evidence; receipt clocks and the previous call are not new evidence."""
    try:value=json.loads(raw or '{}')
    except (TypeError,ValueError):return raw
    def stable(value):
        if isinstance(value,dict):return {k:stable(v) for k,v in value.items()
            if not k.endswith('_at') and k not in ('at','as_of','session_opportunity_dossier')}
        if isinstance(value,list):return [stable(v) for v in value]
        return value
    return stable(value)


def hot_context_signature(context):
    """Economic-change fingerprint for a focused runner.

    Discovery still wakes a newly promoted runner immediately. Subsequent
    calls require a meaningful change in that runner's own evidence; unrelated
    leaderboard churn and ordinary top-of-book noise do not buy another model
    call.
    """
    def log_bucket(value,buckets_per_log_unit):
        if value is None:return None
        number=float(value)
        return None if number<=0 else floor(log(number)*buckets_per_log_unit)
    focal=next((x for x in context.instruments if x.instrument_id==context.focal_instrument_id),context.instruments[0])
    try:market=json.loads(focal.market_context or '{}')
    except (TypeError,ValueError):market={}
    windows=market.get('live_windows') or {}
    raw_detection=market.get('detection_evidence') or {}
    detection_evidence={'source':raw_detection.get('source'),
        'window_minutes':raw_detection.get('window_minutes'),
        'move':floor(float(raw_detection.get('window_change_percent') or 0)/2),
        'speed':floor(float(raw_detection.get('percent_per_minute') or 0)),
        'relative_volume':log_bucket(raw_detection.get('relative_volume_10d'),10)} if raw_detection else None
    window_evidence={seconds:{
        'change':floor(float(row.get('change_bps') or 0)/(25 if seconds in ('1','5') else 50)),
        'volume':log_bucket(row.get('tick_volume'),4)}
        for seconds,row in windows.items() if isinstance(row,dict)}
    payload={'instrument_id':focal.instrument_id,'bid':log_bucket(focal.bid_usd,200),
        'ask':log_bucket(focal.ask_usd,200),'spread':floor(float(focal.spread_bps)/10),
        'bar_close':log_bucket(focal.bars[-1].close_usd if focal.bars else None,200),
        'volume':log_bucket(focal.volume,20),'relative_volume':log_bucket(focal.relative_volume_10d,10),
        'positions':[x.model_dump(mode='json') for x in context.positions],
        'settled_cash':str(context.settled_cash_usd),'risk':str(context.remaining_risk_usd),
        'performance_memory':learning_signature(context.recent_outcomes),
        'execution_feedback':market.get('execution_feedback'),
        'detection_evidence':detection_evidence,
        'live_windows':window_evidence}
    return sha256(json.dumps(payload,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def runner_evidence(rows,now):
    """Timestamped observation only; a screen never grants entry authority."""
    evidence=[]
    for row in rows[:10]:
        try:
            screen_age=(now-datetime.fromisoformat(row['screen_at'])).total_seconds()
            quote_age=(now-datetime.fromisoformat(row['quote_at'])).total_seconds()
            if screen_age<0 or quote_age<0 or screen_age>300:continue
            value={**row,'screen_age_seconds':round(screen_age,3),'quote_age_seconds':round(quote_age,3),
                'current':screen_age<=120 and quote_age<=2,
                'authority':'DISCOVERY_ONLY_REQUIRES_OWN_FRESH_EVALUATION'}
            evidence.append(json.dumps(value,separators=(',',':')))
        except (KeyError,TypeError,ValueError):continue
    return tuple(evidence)


def comparative_opportunities(universe,focal_id,jobs,deep_plan,now,held=()):
    """Fresh decision-support rows, excluded from executable context.instruments."""
    current={j['instrument_id']:j for j in jobs if j.get('current')}
    ranks={x['instrument_id']:x for x in (deep_plan or {}).get('focus',())}
    rows=[]
    for o in sorted(universe.eligible(now),key=lambda x:current.get(x.instrument_id,{}).get('rank',999)):
        j=current.get(o.instrument_id)
        if not j or o.instrument_id==focal_id or o.instrument_id in held or j['symbol']!=o.ticker:continue
        deep=ranks.get(o.instrument_id,{})
        closes=[Decimal(b['close']) for b in sorted(o.bars,key=lambda b:b['time'])]
        row={'instrument_id':o.instrument_id,'symbol':o.ticker,'quote_at':o.quote_at.isoformat(),
            'quote_age_ms':int((now-o.quote_at).total_seconds()*1000),'fresh_at':now.isoformat(),
            'bid':str(o.bid),'ask':str(o.ask),'spread_bps':str(o.features()['spread_bps']),
            'bid_size':None if o.bid_size is None else str(o.bid_size),'ask_size':None if o.ask_size is None else str(o.ask_size),
            'volume':None if o.volume is None else str(o.volume),
            'relative_volume':None if o.relative_volume_10d is None else str(o.relative_volume_10d),
            'change_ratio':None if o.change_ratio is None else str(o.change_ratio),
            'bar_move':str((closes[-1]-closes[0])/closes[0]) if closes else None,
            'bar_range':str((max(closes)-min(closes))/closes[0]) if closes else None,
            'deep_rank':deep.get('rank'),'deep_thesis':deep.get('thesis','')[:200],
            'pipeline_status':j['status'],'pipeline_reason':j.get('reason'),
            'authority':'COMPARISON_ONLY_REQUIRES_OWN_FRESH_EVALUATION'}
        value=json.dumps(row,separators=(',',':'))
        if len(value)>1900 or sum(map(len,rows))+len(value)>8000:continue
        rows.append(value)
        if len(rows)==8:break
    return tuple(rows)


def shadow_snapshot(audit,context,deep_plan,audit_id):
    instruments={x.instrument_id:x for x in context.instruments};preferred=audit['audit'].get('preferred')
    shadow_iid=preferred.get('instrument_id') if preferred else None
    top=next(iter(sorted(deep_plan.get('focus',()),key=lambda x:x['rank'])),None)
    return {'audit_id':audit_id,'started_at':context.as_of.isoformat(),
        'due_at':(context.as_of+timedelta(minutes=5)).isoformat(),'shadow_action':audit['audit']['action'],
        'shadow_instrument_id':shadow_iid,'shadow_symbol':preferred.get('symbol') if preferred else None,
        'shadow_entry_ask_usd':str(instruments[shadow_iid].ask_usd) if shadow_iid in instruments else None,
        'astra_deep_instrument_id':top.get('instrument_id') if top else None,
        'astra_deep_symbol':top.get('symbol') if top else None,
        'astra_action':None,'astra_instrument_id':None,'astra_symbol':None,'astra_entry_ask_usd':None}


def evaluate_shadow(snapshot,context):
    if context.as_of<datetime.fromisoformat(snapshot['due_at']):return None
    marks={x.instrument_id:x.bid_usd for x in context.instruments}
    def score(action,iid,entry):
        if action=='ABSTAIN':return Decimal(0)
        mark=marks.get(iid)
        if action!='OPEN' or mark is None or entry is None:return None
        entry=Decimal(entry);return (mark-entry)/entry*10000
    shadow_score=score(snapshot['shadow_action'],snapshot['shadow_instrument_id'],snapshot['shadow_entry_ask_usd'])
    astra_score=score(snapshot['astra_action'],snapshot['astra_instrument_id'],snapshot['astra_entry_ask_usd'])
    winner=None
    if shadow_score is not None and astra_score is not None:
        winner='TIE' if shadow_score==astra_score else ('SHADOW' if shadow_score>astra_score else 'ASTRA')
    return {**snapshot,'evaluated_at':context.as_of.isoformat(),
        'shadow_observed_return_bps':None if shadow_score is None else str(shadow_score),
        'astra_observed_return_bps':None if astra_score is None else str(astra_score),'winner':winner,
        'score_note':'Five-minute bid versus starting ask counterfactual; includes spread, excludes fees, slippage and fill probability.'}

class ObservedSource:
    def __init__(self,base,engine):self.base,self.engine,self._reported_discovery=base,engine,0
    def _report_discovery(self):
        stats=getattr(self.base,'discovery_stats',{})
        generation=stats.get('generation',0)
        if generation>self._reported_discovery:
            self._reported_discovery=generation
            payload={k:v for k,v in stats.items() if k!='generation'}
            self.engine.emit('UNIVERSE_DISCOVERED',{'summary':'Cheap broad scan shortlisted detailed candidates',**payload})
    def refresh(self,state=None):
        try:
            o=self.base.refresh(state)
            self.engine.update(webull='CONNECTED',webull_at=self.engine.clock().isoformat())
            if self.engine.config.mode==ExecutionMode.DRY_RUN and state:
                # Deliberately simulated account, real market. Never mix its fills into the sandbox account.
                o=replace(o,settled=Decimal(state['cash']['settled_usd']),unsettled=Decimal(state['cash']['unsettled_usd']),
                    equity=Decimal(state['cash']['liquidation_equity_usd']),positions={k:v['quantity'] for k,v in state['positions'].items()},
                    open_intents=tuple(k for k,v in state['orders'].items() if not v['final']))
            return o
        except Exception:
            self.engine.update(webull='ERROR');raise

    def refresh_portfolio(self,state=None):
        try:
            o=self.base.refresh(state,portfolio=True)
            self._report_discovery()
            self.engine.update(webull='CONNECTED',webull_at=self.engine.clock().isoformat())
            if self.engine.config.mode==ExecutionMode.DRY_RUN and state:
                o=replace(o,settled=Decimal(state['cash']['settled_usd']),unsettled=Decimal(state['cash']['unsettled_usd']),
                    equity=Decimal(state['cash']['liquidation_equity_usd']),positions={k:v['quantity'] for k,v in state['positions'].items()},
                    open_intents=tuple(k for k,v in state['orders'].items() if not v['final']))
            return o
        except Exception:
            self.engine.update(webull='ERROR');raise

    def refresh_focus(self,state,symbol):
        try:
            o=self.base.refresh_focus(state,symbol)
            self.engine.update(webull='CONNECTED',webull_at=self.engine.clock().isoformat())
            return o
        except Exception:
            self.engine.update(webull='ERROR');raise

    def refresh_comparative_focus(self,state,symbol,alternatives):
        try:
            o=self.base.refresh_comparative_focus(state,symbol,alternatives)
            self.engine.update(webull='CONNECTED',webull_at=self.engine.clock().isoformat());return o
        except Exception:
            self.engine.update(webull='ERROR');raise

    def refresh_held(self,state):
        try:
            o=self.base.refresh_held(state)
            self.engine.update(webull='CONNECTED',webull_at=self.engine.clock().isoformat())
            return o
        except Exception:
            self.engine.update(webull='ERROR');raise

    def refresh_account(self,state):
        try:
            value=self.base.refresh_account(state)
            self.engine.update(webull='CONNECTED',webull_at=self.engine.clock().isoformat());return value
        except Exception:
            self.engine.update(webull='ERROR');raise

class ObservedController(Controller):
    def __init__(self,original,engine):
        super().__init__(original.ledger,original.account_id,original.clock);self.holder=original.holder;self.token=original.token;self.engine=engine
    def refresh(self,o):
        state=super().refresh(o)
        if not state['reconciliation']['ready']:raise ValueError('ACCOUNT_RECONCILIATION_MISMATCH')
        exposure=sum((Decimal(p['exposure_usd']) for p in state['positions'].values()),Decimal(0))
        self.engine.emit('EQUITY_SAMPLE',{'equity':state['cash']['liquidation_equity_usd'],
            'pnl':str(Decimal(state['cash']['liquidation_equity_usd'])-Decimal(state['segments'][0]['reference_usd'])),
            'daily_pnl':str(Decimal(state['cash']['liquidation_equity_usd'])-Decimal(state['day_start_equity'])),
            'gross_exposure_usd':str(exposure),'position_count':len(state['positions'])})
        self.engine.emit('CONTEXT_REFRESHED',{'summary':'Market/account context refreshed and reconciled'})
        self.engine.update(last_account_refresh=self.engine.clock().isoformat())
        return state

class ObservedBrain:
    _NO_DISPATCH_CODES={
        'MODEL_MONTHLY_BUDGET_EXHAUSTED','MODEL_COST_UNBOUNDED','MODEL_BUDGET_UNAVAILABLE',
        'MODEL_PROVIDER_BACKPRESSURE','MODEL_CONCURRENCY_BACKPRESSURE','MODEL_CALL_BUDGET_WAIT',
        'POLICY_REVIEW_BACKPRESSURE','MODEL_CONTEXT_SIZE_LIMIT'}
    def __init__(self,client,engine):
        self.client,self.engine=client,engine
        self.client.budget=getattr(engine,'model_budget',None)
    @staticmethod
    def _failure_code(exc,fallback):
        value=str(exc)
        budget_code=value in {'MODEL_PROVIDER_BACKPRESSURE','MODEL_CONCURRENCY_BACKPRESSURE','MODEL_CALL_BUDGET_WAIT',
            'MODEL_MONTHLY_BUDGET_EXHAUSTED','MODEL_COST_UNBOUNDED','MODEL_BUDGET_UNAVAILABLE',
            'POLICY_REVIEW_BACKPRESSURE','MODEL_CONTEXT_SIZE_LIMIT'}
        return value if (isinstance(exc,PreflightError) or budget_code) and __import__('re').fullmatch(r'[A-Z_0-9]{1,120}',value) else fallback
    def _record_failed_call(self,call_type,failure_code):
        if failure_code in self._NO_DISPATCH_CODES:
            return  # No provider dispatch; do not create an unknown-cost legacy event.
        value=getattr(self.client,'last_call',None)
        if value is not None:
            self.engine.emit('MODEL_CALL_FAILED',{'summary':'Rejected model response; billing evidence retained',
                'call_type':call_type,'failure_code':failure_code,'ai':value})
    def decide(self,context,registry):
        self.engine.update(activity='Calling Astra',openai='CONNECTING')
        try:
            self.client.verify_model()
            ai=self.client.decide(context,registry,clock=self.engine.clock)
            self.engine.update(openai='CONNECTED' if ai.get('schema_valid') and ai.get('semantics_valid') else 'ERROR',openai_at=self.engine.clock().isoformat())
            return ai
        except Exception:
            self.engine.update(openai='ERROR')
            return {'schema_valid':False,'semantics_valid':False,'decision':None,'result':'OPENAI_CONNECTION_OR_MODEL_CHECK_FAILED','model':self.engine.config.model,'estimated_cost_usd':None}

    def select_candidate(self,universe,state,*,clock):
        self.engine.update(activity='Calling Astra for candidate selection',openai='CONNECTING')
        try:
            selected=self.client.select_candidate(universe,state,clock=clock)
            self.engine.update(openai='CONNECTED',openai_at=self.engine.clock().isoformat())
            return selected
        except Exception:
            self.engine.update(openai='ERROR')
            raise ValueError('ASTRA_CANDIDATE_SELECTION_FAILED') from None

    def deep_plan(self,context):
        self.engine.update(activity='Calling Astra deep planner',openai='CONNECTING')
        try:
            self.client.call_category='DEEP'
            value=self.client.deep_plan(context,captured_policy(context))
            value['model_work_id']=getattr(self.client,'last_call',{}).get('model_work_id')
            self.engine.update(openai='CONNECTED',openai_at=self.engine.clock().isoformat());return value
        except Exception as exc:
            # AstraClient emits fixed, credential-free error codes. Preserve
            # those codes so an invalid output, timeout, or HTTP failure can be
            # diagnosed without recording response bodies or secrets.
            code=self._failure_code(exc,'ASTRA_DEEP_PLAN_FAILED')
            self._record_failed_call('DEEP',code)
            self.engine.update(openai='AVAILABLE' if code in self._NO_DISPATCH_CODES else 'ERROR');raise ValueError(code) from None

    def fast_decide(self,context,mode):
        self.engine.update(activity='Calling Astra fast '+mode.lower(),openai='CONNECTING')
        try:
            self.client.call_category='FAST_'+mode
            value=self.client.fast_decide(context,mode=mode,hard_policy=captured_policy(context),clock=self.engine.clock)
            value['model_work_id']=getattr(self.client,'last_call',{}).get('model_work_id')
            self.engine.update(openai='CONNECTED',openai_at=self.engine.clock().isoformat());return value
        except Exception as exc:
            code=self._failure_code(exc,'ASTRA_FAST_DECISION_FAILED')
            self._record_failed_call('FAST_'+mode,code)
            self.engine.update(openai='AVAILABLE' if code in self._NO_DISPATCH_CODES else 'ERROR');raise ValueError(code) from None

    def portfolio_decide(self,context):
        self.engine.update(activity='Calling Astra portfolio entry research',openai='CONNECTING')
        try:
            self.client.call_category='PORTFOLIO_ENTRY'
            value=self.client.fast_decide(context,mode='ENTRY',hard_policy=captured_policy(context),clock=self.engine.clock)
            value['model_work_id']=getattr(self.client,'last_call',{}).get('model_work_id')
            self.engine.update(openai='CONNECTED',openai_at=self.engine.clock().isoformat());return value
        except Exception as exc:
            code=self._failure_code(exc,'ASTRA_PORTFOLIO_DECISION_FAILED')
            self._record_failed_call('PORTFOLIO_ENTRY',code)
            self.engine.update(openai='AVAILABLE' if code in self._NO_DISPATCH_CODES else 'ERROR');raise ValueError(code) from None

    def shadow_audit(self,context):
        self.engine.update(activity='Calling low-reasoning shadow auditor',openai='CONNECTING')
        try:
            self.client.call_category='SHADOW'
            value=self.client.shadow_audit(context,captured_policy(context))
            value['model_work_id']=getattr(self.client,'last_call',{}).get('model_work_id')
            self.engine.update(openai='CONNECTED',openai_at=self.engine.clock().isoformat());return value
        except Exception as exc:
            code=self._failure_code(exc,'SHADOW_AUDIT_FAILED')
            self._record_failed_call('SHADOW',code)
            self.engine.update(openai='AVAILABLE' if code in self._NO_DISPATCH_CODES else 'ERROR');raise ValueError(code) from None

    def policy_decide(self,context):
        try:
            self.client.call_category='ECONOMIC_POLICY'
            value=self.client.policy_decide(context)
            value['model_work_id']=getattr(self.client,'last_call',{}).get('model_work_id')
            value['response_at']=self.engine.clock().isoformat()
            return value
        except Exception as exc:
            self._record_failed_call('ECONOMIC_POLICY',self._failure_code(exc,'ECONOMIC_POLICY_FAILED'))
            raise

class Engine:
    def __init__(self,root,config,store,*,clock=lambda:datetime.now(timezone.utc),interval=300,poll=30,
            portfolio_research=None,approved_sme_advisory=None):
        config.validate()
        if not 30<=interval<=86400 or not 5<=poll<=300:raise ValueError('INVALID_RUNNER_INTERVAL')
        self.root=Path(root);self.config=config;self.store=store;self.clock=clock;self.interval=interval;self.poll=poll
        # Disabled unless the owner-controlled composition root injects a
        # bounded provider. Default launches perform no added research reads.
        self.portfolio_research=portfolio_research
        self.approved_sme_advisory=approved_sme_advisory
        self.calendar=reviewed_calendar();self.initialized=False
        self.controller=None;self.source=None;self.gateway=None;self.brain_factory=None;self.shadow_factory=None;self.alpha_manager=None;self.monitor=None
        self.deep_plan=None;self.deep_plan_at=None;self.deep_context_signature=None;self.unassigned_deep_cost=Decimal(0)
        self.focus_cursor=0;self.baseline_pending=None;self.consecutive_focus_invalidations=0
        self.shadow_pending=None;self.current_shadow_audit_id=None;self.fast_signatures={}
        self.opportunities=None;self.next_discovery=clock();self.background={};self.live_stream=None
        self.discovery_executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='market-discovery')
        self.discovery_pending=None;self.discovery_started_at=None;self.discovery_binding=None
        self.planning_executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='alpha-planning')
        self.shadow_executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='alpha-shadow')
        self.background_retry={};self.pipeline_outcomes=()
        self.policy_pending=None;self.policy_retry_at=None;self.model_budget=None
        self.policy_executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='sandbox-economic-policy')
        self.consecutive_universe_insufficiencies=0;self.universe_retry_at=None
        self.next_evaluation=clock();self.next_poll=clock();self.lock=threading.RLock();self.wake_event=threading.Event()
        previous=store.runtime();self.previous=previous
        self.state=dict(status='RUNNING',mode=config.mode.value,model=config.model,reasoning=config.reasoning,
            ledger_schema='mvp_sandbox' if config.mode==ExecutionMode.SANDBOX else 'mvp_dry_run',account_id=None,
            activity='Initializing',webull='NOT_CHECKED',openai='NOT_CHECKED',last_successful_cycle=previous.get('last_successful_cycle') if previous.get('mode')==config.mode.value else None,next_evaluation=None,
            next_account_poll=None,last_error=None,started_at=clock().isoformat())
    def update(self,**kwargs):
        with self.lock:self.state.update(kwargs)
    def heartbeat(self):
        if self.model_budget is not None:self.update(model_budget=self.model_budget.health())
        with self.lock:payload=dict(self.state)
        self.store.heartbeat(payload)

    @staticmethod
    def _flat_local_evidence_trustworthy(state,session):
        orders=state.get('orders',{});incidents=state.get('incidents',{})
        reconciliation=state.get('reconciliation',{})
        return (not state.get('positions')
            and all(order.get('final') and order.get('phase') not in ('UNKNOWN','CANCEL_PENDING','REPLACE_PENDING')
                for order in orders.values())
            and not any(value.get('pending_correction') for value in incidents.values())
            and not any(state.get('latches',{}).values())
            and not state.get('risk_breaches')
            and reconciliation.get('ready') is True and reconciliation.get('settlement_known') is True
            and state.get('session',{}).get('label')==session.label.isoformat())

    def _error_requires_immediate_account_recovery(self):
        code=str(self.state.get('last_error') or '')
        return any(token in code for token in ('ORDER','RECONCILIATION','SETTLEMENT','ACCOUNT_NOT_READY','RECOVERY','CORRECTION'))

    def _account_refresh_completed(self):
        self.next_poll=self.clock()+timedelta(seconds=self.poll)
        self.update(next_account_poll=self.next_poll.isoformat())

    def backfill_performance_memory(self):
        """Expose pre-loop ledger trades without rewriting accounting history."""
        state=self.controller.state;plans={};episodes={}
        for order in state['orders'].values():
            intent=order['intent']
            if intent['side']!='BUY':continue
            plan=self.store.alpha_plan(intent['plan_id'])
            if plan:plans[intent['plan_id']]=plan
            episodes[intent['plan_id']]=intent['episode_id']
        for trade in trades_from_state(state,{},plans):
            plan=plans.get(trade['decision_id'],{});capital=trade['quantity']*trade['entry_price']
            opened=datetime.fromisoformat(trade['entry_time']);closed=datetime.fromisoformat(trade['exit_time'])
            payload={'summary':'Ledger-derived historical alpha outcome '+trade['ticker'],'plan_id':trade['decision_id'] or 'UNATTRIBUTED',
                'instrument_id':next((o['intent']['instrument_id'] for o in state['orders'].values()
                    if o['intent']['plan_id']==trade['decision_id']),trade['ticker']),'symbol':trade['ticker'],
                'strategy_label':plan.get('strategy_label') or episodes.get(trade['decision_id'],'LEGACY_UNCLASSIFIED'),
                'thesis':plan.get('thesis') or 'Historical ledger outcome; original thesis unavailable.',
                'entry_usd':str(trade['entry_price']),'exit_usd':str(trade['exit_price']),'quantity':str(trade['quantity']),
                'realized_gross_usd':str(trade['gross']),'broker_fees_usd':str(trade['fees']),
                'realized_net_after_broker_fees_usd':str(trade['net']),
                'realized_net_after_all_known_costs_usd':str(trade['net']),
                'return_on_capital':str(trade['net']/capital),'holding_seconds':max(0,int((closed-opened).total_seconds())),
                'capital_deployed_usd':str(capital),'capital_time_usd_seconds':str(capital*max(0,int((closed-opened).total_seconds()))),
                'maximum_favorable_excursion_usd':'0','maximum_adverse_excursion_usd':'0',
                'win_loss':'WIN' if trade['net']>0 else ('LOSS' if trade['net']<0 else 'FLAT'),
                'market_regime':plan.get('market_regime','LEGACY_UNRECORDED'),'exit_reason':'LEGACY_UNRECORDED',
                'model_cost_usd':'0','model_cost_attribution':'GLOBAL_ONLY_UNATTRIBUTED',
                'evidence_quality':'LEDGER_LEGACY_NO_EXCURSION','opened_at':opened.isoformat(),'closed_at':closed.isoformat()}
            eid='ledger-outcome-'+sha256((str(trade['decision_id'])+'|'+closed.isoformat()).encode()).hexdigest()
            self.store.emit('ALPHA_OUTCOME',payload,mode=self.config.mode.value,at=closed,event_id=eid)
    def emit(self,kind,payload):
        self.store.emit(kind,payload,mode=self.config.mode.value,at=self.clock(),
            event_id='decision-'+payload['ai']['decision']['decision_id'] if kind=='ASTRA_DECISION' and payload['ai'].get('decision') else None)
        if kind=='ACTIVITY':self.update(activity=payload['summary'])
        # Codes and selected summaries only; no raw exceptions, requests or configuration.
        log=self.root/'.local/mvp/runner.log';log.parent.mkdir(parents=True,exist_ok=True)
        with log.open('a',encoding='utf-8') as f:f.write(json.dumps({'at':self.clock().isoformat(),'kind':kind,'summary':payload.get('summary',kind)})+'\n')
    def _reset_partial_initialization(self):
        stream=self.live_stream
        self.initialized=False;self.controller=None;self.source=None;self.gateway=None
        self.brain_factory=None;self.shadow_factory=None;self.alpha_manager=None;self.monitor=None
        self.model_budget=None;self.opportunities=None;self.live_stream=None
        if stream is not None:
            try:stream.close()
            except Exception:pass

    def initialize(self):
        """Publish runtime dependencies only after the full startup succeeds."""
        if self.initialized:return
        self._reset_partial_initialization()
        try:self._initialize_once()
        except Exception:
            self._reset_partial_initialization()
            raise
        self.initialized=True

    def _initialize_once(self):
        credentials=load_credentials(self.root);key=load_model_key(self.config.model)
        sanitizer=OrderSanitizer((*credentials.sensitive_values(),key));self.store.check=sanitizer.assert_clean
        self.store.import_existing(self.root)
        self.live_stream=LiveQuoteStream(credentials,clock=self.clock,on_material=self.wake_event.set);self.live_stream.start()
        self.source=ObservedSource(SandboxSource(credentials,self.live_stream),self)
        schema=self.state['ledger_schema'];ledger=Ledger(LocalDatabase('postgres','m2_writer',schema=schema),
            execution_scope='SANDBOX' if self.config.mode==ExecutionMode.SANDBOX else 'OFFLINE')
        aid=sandbox_ledger_id(credentials.account_id) if self.config.mode==ExecutionMode.SANDBOX else (
            self.previous.get('account_id') if self.previous.get('mode')=='DRY_RUN' else None)
        original=None
        if aid:
            try:ledger.snapshot(aid);original=Controller(ledger,aid,self.clock)
            except Rejected as exc:
                if str(exc)!='UNKNOWN_ACCOUNT':raise
        if original is None:
            observation=self.source.refresh()
            original=prepare_controller(ledger,self.config,observation,credentials.account_id,self.clock)
        self.update(account_id=original.account_id)
        self.gateway=ExecutionGateway(self.config,simulation=SimulatedBroker(self.calendar) if self.config.mode==ExecutionMode.DRY_RUN else None,
            transport=SandboxTransport(credentials) if self.config.mode==ExecutionMode.SANDBOX else None,sanitizer=sanitizer,
            evidence_sink=lambda v:save_json(self.root/'.local/mvp/evidence'/(uuid4().hex+'.json'),v,sanitizer))
        self.brain_factory=lambda:ObservedBrain(make_brain(key,model=self.config.model,reasoning=self.config.reasoning,instructions=INSTRUCTIONS,approved_sme_advisory=self.approved_sme_advisory),self)
        self.shadow_factory=lambda:ObservedBrain(make_brain(key,model=shadow_model(self.config.model),reasoning='low'),self)
        # Read-only model availability check; never a decision/order during closed sessions.
        self.brain_factory().client.verify_model()
        self.update(openai='AVAILABLE',openai_at=self.clock().isoformat())
        self.monitor=LocalPositionManager(monitor_dry_run=True,review=self.review_position,emit=self.emit)
        self.alpha_manager=AlphaPortfolioManager(self.store,review=self.review_alpha_position,emit=self.emit,
            exit_manager=LocalPositionManager(monitor_dry_run=True,emit=self.emit),prepare_review=self.prepare_alpha_position_review)
        self.controller=ObservedController(original,self)
        if self.config.mode==ExecutionMode.SANDBOX:
            if 'economic_policy' not in self.controller.state:
                self.controller.lease();self.controller.send(AuthorizeSandboxEconomics())
            if not self.controller.state['economic_policy'].get('preferences_released'):
                state=self.controller.state
                # Owner-authorized once, through the normal writer and fresh
                # broker accounting; never claim it is an Astra policy decision.
                if (not state['positions'] and all(o['final'] for o in state['orders'].values())
                        and not any(state['latches'].values())):
                    account=self.source.refresh_account(state)
                    self.controller.refresh(account)
                    self.controller.send(ReleaseSandboxPreferences())
            if not self.controller.state['economic_policy'].get('daily_downside_budget'):
                state=self.controller.state
                if (not state['positions'] and all(o['final'] for o in state['orders'].values())
                        and not any(state['latches'].values())):
                    account=self.source.refresh_account(state)
                    self.controller.refresh(account)
                    self.controller.send(AuthorizeDailyDownsideBudget())
            if self.controller.state['economic_policy'].get('model_budget_cap_authorization_id') != AuthorizeModelBudgetCap().authorization_id:
                state=self.controller.state
                if (not state['positions'] and all(o['final'] for o in state['orders'].values())
                        and not any(state['latches'].values())):
                    account=self.source.refresh_account(state)
                    self.controller.refresh(account)
                    self.controller.send(AuthorizeModelBudgetCap())
        self.model_budget=ModelBudget(self.store,self.config.mode.value,self.clock)
        self.model_budget.configure(effective_policy(self.controller.state) if 'economic_policy' in self.controller.state
            else SandboxEconomics())
        self.backfill_performance_memory()
        self.update(pipeline=self._queue().health())
        self.emit('RUNNER_STARTED',{'summary':'Runner initialized; '+self.config.mode.value+'; production disabled'})
        self.heartbeat()
    def review_position(self,observation,intent,plan,plan_version,original_plan,metrics):
        original=self.store.plan(intent.plan_id)
        if not original:raise ValueError('PERSISTED_VALIDATED_PLAN_MISSING')
        context,registry=make_position_review_context(observation,self.controller.state,self.clock(),intent=intent,plan=plan,
            plan_version=plan_version,original_decision=original,
            maximum_favorable_excursion_usd=metrics['maximum_favorable_excursion_usd'],
            maximum_adverse_excursion_usd=metrics['maximum_adverse_excursion_usd'])
        brain=ObservedBrain(make_brain(load_model_key(self.config.model),model=self.config.model,
            reasoning=self.config.reasoning,instructions=POSITION_REVIEW_INSTRUCTIONS),self)
        return brain.decide(context,registry),context,registry

    def review_alpha_position(self,observation,plan):
        return self.prepare_alpha_position_review(observation,plan)()

    def prepare_alpha_position_review(self,observation,plan):
        state=self.controller.state
        raw_original=self.store.original_alpha_plan(plan.plan_id)
        original=AlphaManagedPlan.model_validate(raw_original)
        if (original.plan_id!=plan.plan_id or original.instrument_id!=plan.instrument_id
                or original.episode_id!=plan.episode_id):
            raise ValueError('PERSISTED_ALPHA_PLAN_BINDING_MISMATCH')
        universe=UniverseObservation((observation,),observation.received_at,observation.settled,observation.unsettled,
            observation.equity,observation.positions,observation.open_intents,observation.evidence_id)
        context=build_alpha_context(universe,state,self.clock(),instrument_ids=(plan.instrument_id,),
            position_plans={plan.instrument_id:plan},original_position_plans={plan.instrument_id:original},
            recent_outcomes=self.store.recent_outcomes())
        brain=ObservedBrain(make_brain(load_model_key(self.config.model),model=self.config.model,
            reasoning=self.config.reasoning),self)
        return lambda:(brain.fast_decide(context,'POSITION'),context)

    def _prioritize_candidate_result(self,session):
        """Keep completed FAST_ENTRY work ahead of avoidable blocking reads."""
        queue=self.opportunities
        if queue is None or not hasattr(queue,'completed'):return False
        completed=queue.completed(limit=1)
        if completed:
            job,context,ai=completed[0]
            self._candidate_result(job,context,ai,session)
            self.update(pipeline=queue.health())
            return True
        # A flat-account broad/account refresh can take materially longer than
        # the five-second writer cadence.  Do not start one while a focused
        # answer is imminent.  OPEN still performs its mandatory post-model
        # exact-symbol/account refresh inside _candidate_result before admission.
        active=any(queue.jobs[key].get('status')=='RUNNING'
            for _,key,_ in getattr(queue,'futures',{}).values())
        if active:
            next_at=self.clock()+timedelta(seconds=5)
            self.update(status='RUNNING',last_error=None,
                activity='Focused Astra decision in flight; preserving immediate execution review',
                next_evaluation=next_at.isoformat(),pipeline=queue.health())
            if self.live_stream is not None and queue.capacity()>0:
                # A fresh event can use an existing free model slot. Never
                # queue slow comparative reads behind a perishable answer.
                self._run_candidates(session,stream_only=True)
            return True
        return False

    def _alpha_step(self,now,session):
        state=self.controller.state
        self._sync_stream_watch()
        if hasattr(self.gateway,'service_working_entries') and session and session.contains(now):
            for result in self.gateway.service_working_entries(self.controller,session,self.clock):
                self.emit('BROKER_RESULT',{'summary':'Working entry reached REST terminal state',**result})
            state=self.controller.state
        # Resolve an asynchronously completed broker cancellation before the
        # open-order set is compared during account reconciliation. This is a
        # read-only broker check and grants no new order authority.
        recovered=()
        if any(not o['final'] and o['phase']=='CANCEL_PENDING' for o in state.get('orders',{}).values()):
            recovered=self.gateway.recover_terminal_cancellations(self.controller);state=self.controller.state
        if state['positions'] and (session is None or not session.contains(now)):
            self.update(status='ERROR',last_error='OPEN_POSITION_OUTSIDE_SESSION',activity='Open position: waiting for regular session; no outside-session order');return
        if session is None or not session.contains(now):
            self._drain_background()
            if self.opportunities is not None:
                for job,_,ai in self.opportunities.completed():
                    self.emit('ALPHA_DECISION',{'summary':'Closed-session portfolio result recorded without authority','ai':ai,'call_type':'PORTFOLIO_ENTRY'})
                    self.opportunities.finish(job,'DROPPED','SESSION_CLOSED')
                self.update(pipeline=self.opportunities.health())
            next_at=next_entry_time(self.calendar,now)
            self.update(status='RUNNING',activity='Waiting for regular entry session',last_error=None,
                next_evaluation=next_at.isoformat() if next_at else None);return
        # Owned-position safety is independent of the candidate universe and
        # always runs before discovery, planning, or new-entry work.
        if state['positions']:
            try:held=self.source.refresh_held(state)
            except ValueError as exc:
                # An accepted entry may fill between order detail and the
                # account read. Resolve only that known fill, then read again.
                pending=any(not o['final'] and o['phase']=='ACKNOWLEDGED' and o['intent']['side']=='BUY'
                    for o in state['orders'].values())
                if str(exc)!='HELD_INSTRUMENT_BROKER_BINDING_MISMATCH' or not pending:raise
                completed=self.gateway.service_working_entries(self.controller,session,self.clock)
                if not any(x['broker']['status']=='FILLED' for x in completed):raise
                for result in completed:self.emit('BROKER_RESULT',{'summary':'Known entry fill reconciled before held monitoring',**result})
                state=self.controller.state;held=self.source.refresh_held(state)
            self.controller.lease();refresh_session(self.controller,held,self.clock,exit_observation=True);state=self.controller.state
            self._account_refresh_completed()
            if recovered:
                state=self.controller.resolve_recovered_cancellation_incidents(held,recovered)
            if any(not o['final'] and not (o['phase']=='ACKNOWLEDGED' and o['intent']['side']=='BUY'
                    and o['intent']['instrument_id'] not in state['positions'])
                    for o in state['orders'].values()) or not state['reconciliation']['ready']:
                self.update(status='ERROR',activity='Held position requires reconciliation',last_error='ACCOUNT_NOT_READY',next_evaluation=None);return
            result=self.alpha_manager.tick(held,self.controller,self.gateway,session,self.clock)
            state=self.controller.state
            if result.get('exited'):
                self.next_evaluation=self.clock()+timedelta(seconds=self.interval)
                self.update(status='RUNNING',activity='Mandatory held-position exit completed',last_successful_cycle=self.clock().isoformat(),
                    last_error=None,next_evaluation=self.next_evaluation.isoformat());return
        if any(not o['final'] for o in state['orders'].values()):
            # Accepted entries remain reserved and supervised across turns.
            # Do not reconcile a flat pre-fill snapshot through a racing fill.
            known=all(o['final'] or (o['phase']=='ACKNOWLEDGED' and o['intent']['side']=='BUY')
                for o in state['orders'].values())
            self.update(status='RUNNING' if known else 'ERROR',last_error=None if known else 'ACCOUNT_NOT_READY',
                activity='Supervising accepted entry through its Astra deadline' if known else 'Order state requires reconciliation')
            return
        # Flat-account authority is independent of market discovery. An empty,
        # stale, or temporarily unavailable candidate universe must not prevent
        # a fresh account-only reconciliation from restoring readiness.
        rec=state['reconciliation'];valid_until=rec.get('valid_until')
        reconciliation_due=(not rec.get('ready') or not rec.get('settlement_known')
            or state['session'].get('label')!=session.label.isoformat()
            or valid_until is None or now>datetime.fromisoformat(valid_until)
            or self._error_requires_immediate_account_recovery())
        if not state['positions'] and reconciliation_due:
            account=self.source.refresh_account(state)
            self.controller.lease();refresh_session(self.controller,account,self.clock)
            self._account_refresh_completed();state=self.controller.state
            rec=state['reconciliation']
            if (any(not o['final'] for o in state['orders'].values()) or not rec['ready']
                    or not rec['settlement_known'] or any(state['latches'].values())):
                self.update(status='ERROR',last_error='ACCOUNT_NOT_READY',
                    activity='Flat account requires reconciliation; no entry authority');return
            self.update(status='RUNNING',last_error=None,
                activity='Flat account reconciliation refreshed; market discovery remains independent',
                next_evaluation=now.isoformat())
            return
        # Held supervision and uncertain-order recovery have already run. Model
        # futures never hold up this thread, nor own any account authority.
        self._drain_background()
        # A completed focused answer is economically perishable.  Handle it
        # before another slow broad/account read; held-position safety above
        # remains first, and an OPEN must still pass its own fresh broker read.
        discovery_ready=(self.discovery_pending is not None and self.discovery_pending.done())
        if (self.config.mode==ExecutionMode.SANDBOX and not state['positions'] and now>=self.next_discovery
                and self.discovery_pending is None):
            # Read-only market discovery owns no controller/gateway handle and
            # cannot block completed model results or held supervision.
            self.discovery_started_at=now
            self.discovery_binding=account_fingerprint(state)
            self.discovery_pending=self.discovery_executor.submit(self.source.refresh_portfolio,state)
            discovery_ready=False
            self.update(activity='Broad Webull discovery running independently',next_evaluation=now.isoformat())
        if (discovery_ready and self.opportunities is not None and any(
                self.opportunities.jobs[key].get('status')=='RUNNING'
                for _,key,_ in self.opportunities.futures.values())):
            # Discovery must keep finding new names while Astra thinks. Consume
            # metadata for the live watch now, not its perishable account/mark
            # authority later. Every candidate still gets exact fresh reads.
            future=self.discovery_pending;self.discovery_pending=None;self.discovery_binding=None
            scanned=future.result()
            self.opportunities.refill(scanned.candidates,session.label,(self.deep_plan or {}).get('focus',()))
            stats=getattr(getattr(self.source,'base',self.source),'discovery_stats',{})
            self.latest_top_runners=tuple(stats.get('top_runners',()))
            self._promote_hot(self.opportunities,stats)
            self.next_discovery=next_discovery_due(self.discovery_started_at,self.clock())
            discovery_ready=False
            self.emit('DISCOVERY_MARKET_ONLY',{'summary':'Live watch refreshed while Astra reasons; no account or order authority',
                'candidate_count':len(scanned.candidates),'observed_at':scanned.received_at.isoformat()})
        if self._prioritize_candidate_result(session):return
        # A promoted exact-symbol retry is more perishable than another broad
        # discovery pass. Keep it ahead of that blocking scan until it either
        # reaches Astra or expires under the existing bounded stale-data rule.
        if self.opportunities is not None and hasattr(self.opportunities,'expire_overdue_hot'):
            queue=self.opportunities;queue.expire_overdue_hot();self._sync_stream_watch();at=self.clock()
            active_hot=any(job.get('current') and job['instrument_id'] not in state['positions']
                and queue._hot(job,at) and job['status'] not in ('RUNNING','RESULT_READY','EXECUTING')
                for job in queue.jobs.values())
            if active_hot and not discovery_ready:
                if (not state['positions'] and (self.clock()>=self.next_poll or not state['reconciliation']['ready']
                        or state['session'].get('label')!=session.label.isoformat()
                        or self._error_requires_immediate_account_recovery())):
                    account=self.source.refresh_account(state)
                    self.controller.lease();refresh_session(self.controller,account,self.clock)
                    self._account_refresh_completed();state=self.controller.state
                if (any(not o['final'] for o in state['orders'].values()) or not state['reconciliation']['ready']
                        or any(state['latches'].values())):
                    self.update(status='ERROR',last_error='ACCOUNT_NOT_READY',
                        activity='Hot opportunity blocked by account authority');return
                self._run_candidates(session);return
        # Economic-policy model proposals are not part of the active trading
        # loop. The durable owner-controlled policy remains authoritative;
        # Astra owns trading choices inside it without spending budget trying
        # to mutate safety or cost settings.
        if self.opportunities is not None and self.clock()<self.next_discovery and not discovery_ready:
            if not state['positions'] and (self.clock()>=self.next_poll or not state['reconciliation']['ready']
                    or state['session'].get('label')!=session.label.isoformat()
                    or self._error_requires_immediate_account_recovery()):
                account=self.source.refresh_account(state)
                self.controller.lease();refresh_session(self.controller,account,self.clock)
                self._account_refresh_completed();state=self.controller.state
            if any(not o['final'] for o in state['orders'].values()) or not state['reconciliation']['ready'] or any(state['latches'].values()):
                self.update(status='ERROR',last_error='ACCOUNT_NOT_READY',activity='Portfolio entries blocked by account authority');return
            self._run_candidates(session);return
        flat_trusted=self._flat_local_evidence_trustworthy(state,session)
        if (self.state.get('status')=='ERROR' and flat_trusted and not self._error_requires_immediate_account_recovery()
                and self.clock()<self.next_poll):
            self.update(activity='Error; bounded flat broker retry pending; no order authority',
                next_evaluation=self.next_poll.isoformat(),next_account_poll=self.next_poll.isoformat());return
        if self.universe_retry_at is not None and self.clock()<self.universe_retry_at:
            self.update(status='RUNNING',activity='Market universe temporarily insufficient; bounded retry pending',last_error=None,
                next_evaluation=self.universe_retry_at.isoformat());return
        if (flat_trusted and self.state.get('status')=='RUNNING' and self.clock()<self.next_evaluation
                and self.clock()<self.next_poll and self.clock()<self.next_discovery):
            self.update(activity='Cached flat account state; waiting for bounded broker check; no order authority',last_error=None,
                next_evaluation=self.next_evaluation.isoformat(),next_account_poll=self.next_poll.isoformat());return
        # Bind validation and the broad planning context to one completed
        # broker-snapshot instant. Durable reconciliation may consume the
        # remainder of the strict two-second quote window; re-reading the clock
        # afterward must not retroactively invalidate evidence that was fresh
        # when this snapshot completed. Focused data is independently refreshed
        # before the fast call and again before admission.
        universe=None
        try:
            if self.config.mode==ExecutionMode.SANDBOX:
                if self.discovery_pending is None:
                    self.discovery_started_at=self.clock()
                    self.discovery_binding=account_fingerprint(state)
                    self.discovery_pending=self.discovery_executor.submit(self.source.refresh_portfolio,state)
                    self.update(activity='Broad Webull discovery running independently');return
                if not self.discovery_pending.done():
                    self.update(activity='Broad Webull discovery running independently');return
                future=self.discovery_pending;self.discovery_pending=None
                if self.discovery_binding!=account_fingerprint(self.controller.state):
                    future.result()  # Observe any worker failure; result has no account authority.
                    self.discovery_binding=None;self.next_discovery=self.clock()
                    self.emit('DISCOVERY_DISCARDED',{'summary':'Read-only discovery snapshot discarded after account state changed'})
                    return
                self.discovery_binding=None
                universe=future.result()
            else:universe=self.source.refresh_portfolio(state)
            snapshot_at=self.clock();universe.require_fresh(snapshot_at)
        except ValueError as exc:
            discovery_local=(str(exc)=='INSUFFICIENT_ELIGIBLE_UNIVERSE' or str(exc).startswith((
                'BROKER_READ_FAILED TOP_ACTIVE ','BROKER_READ_FAILED GAINERS_LOSERS ')))
            if discovery_local and self.opportunities is not None:
                if universe is not None:
                    # Metadata identifies watch candidates, not order authority.
                    # A stale broad snapshot must not prevent exact stream refresh.
                    self.opportunities.refill(universe.candidates,session.label,(self.deep_plan or {}).get('focus',()))
                self.next_discovery=self.clock()+timedelta(seconds=self.poll)
                self.emit('DISCOVERY_RETRY',{'summary':'Discovery unavailable; exact-symbol queue refresh continues',
                    'reason':candidate_code(exc),'retry_at':self.next_discovery.isoformat()})
                discovery_stats=getattr(getattr(self.source,'base',self.source),'discovery_stats',{})
                self.latest_top_runners=tuple(discovery_stats.get('top_runners',()))
                self._promote_hot(self.opportunities,discovery_stats)
                # Do not confer account authority from an incomplete scan.
                # The next turn refreshes account/held state before queue work.
                self.next_poll=self.clock()
                self.update(activity='Discovery retry scheduled; candidates retained for direct refresh');return
            if str(exc)!='INSUFFICIENT_ELIGIBLE_UNIVERSE':raise
            self.consecutive_universe_insufficiencies+=1
            self.universe_retry_at=self.clock()+timedelta(seconds=self.poll)
            self.emit('UNIVERSE_RETRY',{'summary':'Fresh bounded universe temporarily insufficient; no model or order authority',
                'consecutive_insufficiencies':self.consecutive_universe_insufficiencies,
                'retry_at':self.universe_retry_at.isoformat()})
            if self.consecutive_universe_insufficiencies>=3:raise
            self.update(status='RUNNING',activity='Market universe temporarily insufficient; bounded retry scheduled',last_error=None,
                next_evaluation=self.universe_retry_at.isoformat());return
        self.consecutive_universe_insufficiencies=0;self.universe_retry_at=None
        self.controller.lease();refresh_session(self.controller,universe,self.clock);state=self.controller.state
        self._account_refresh_completed()
        if recovered:
            state=self.controller.resolve_recovered_cancellation_incidents(universe,recovered)
        if any(not o['final'] for o in state['orders'].values()) or not state['reconciliation']['ready']:
            self.update(status='ERROR',activity='Entries halted; reconciliation requires attention',last_error='ACCOUNT_NOT_READY',next_evaluation=None);return
        if state['positions']:
            self.update(activity='Alpha portfolio monitored; evaluating additional capacity')
        if any(state['latches'].values()):
            self.update(status='ERROR',activity='Safety latch active; entries disabled',last_error='ACCOUNT_NOT_READY',next_evaluation=None);return
        # A completed broker refresh and reconciliation is the recovery
        # boundary for ordinary flat-account read failures.  Clear the stale
        # error before returning to a cached wait or an unavailable entry
        # window; no order authority is granted by this state transition.
        self.update(status='RUNNING',last_error=None)
        if not session.permits_entry(self.clock()) or len(state['positions'])>=effective_policy(state).maximum_positions:
            self.update(status='RUNNING',activity='Portfolio monitored; entry unavailable',next_evaluation=None);return
        discovery_stats=getattr(getattr(self.source,'base',self.source),'discovery_stats',{})
        observed_runners=discovery_stats.get('top_runners',())
        self.latest_top_runners=tuple(observed_runners)
        queue=self._queue()
        queue.refill(universe.candidates,session.label,(self.deep_plan or {}).get('focus',()))
        # A fresh short-window screen may wake a bounded hot candidate even
        # while the ordinary evaluation timer is in cooldown. It still needs
        # its own exact quote, model decision, fresh revalidation and admission.
        promotions=self._promote_hot(queue,discovery_stats)
        self.next_discovery=next_discovery_due(self.discovery_started_at,self.clock())
        dossiers={j['instrument_id']:queue.dossier(j,snapshot_at) for j in queue.jobs.values() if j.get('current')}
        outcomes=self.store.recent_outcomes();broad=build_alpha_context(universe,state,snapshot_at,recent_outcomes=outcomes,
            observed_top_runners=runner_evidence(self.latest_top_runners,snapshot_at),opportunity_dossiers=dossiers)
        if self.baseline_pending:
            result=evaluate_baseline(self.baseline_pending,broad)
            if result:self.emit('BASELINE_RESULT',{'summary':'Five-minute benchmark outcomes recorded',**result});self.baseline_pending=None
        if self.shadow_pending:
            result=evaluate_shadow(self.shadow_pending,broad)
            if result:self.emit('SHADOW_RESULT',{'summary':'Shadow and Astra counterfactual scored',**result});self.shadow_pending=None
        measured=(self.store.measured_portfolio_feedback(snapshot_at)
            if hasattr(self.store,'measured_portfolio_feedback') else None)
        # Portfolio Astra sees one bounded, causal summary. The wider dashboard
        # memory remains available elsewhere but is not mixed into this input.
        self.pipeline_outcomes=(measured,) if measured else ()
        # Entry research is performed by the two scheduled portfolio calls.
        # Do not buy a separate DEEP planning call that cannot itself OPEN.
        if self.clock()<self.next_evaluation and not promotions:
            self.update(status='RUNNING',activity='Portfolio monitored; waiting for next alpha evaluation',
                next_evaluation=self.next_evaluation.isoformat());return
        self._run_candidates(session)

    def _economic_step(self,session):
        """Only this writer may apply proposals; economic halts do not hide review."""
        state=self.controller.state
        if self.policy_pending is not None:
            future,context=self.policy_pending
            if not future.done():return
            self.policy_pending=None
            try:
                value=future.result()
                self.emit('POLICY_DECISION',{'summary':'Astra SANDBOX economic-policy proposal','ai':value})
                state=self.controller.state
                observation=(self.source.refresh_held(state) if state['positions'] else self.source.refresh_account(state))
                self.controller.lease();refresh_session(self.controller,observation,self.clock,exit_observation=bool(state['positions']))
                self._account_refresh_completed()
                command=ReviseSandboxEconomics(proposal=value['proposal'],context_hash=context['context_hash'],
                    context_at=context['as_of'],response_at=value['response_at'],account_fingerprint=context['account_fingerprint'],
                    model=value['model'],reasoning=value['reasoning'],decision_id=uuid4().hex)
                state=self.controller.send(command)
                self._queue().aux['economic_policy']['signature']=context['material_signature'];self._queue().save()
                if self.model_budget:self.model_budget.configure(effective_policy(state))
                self.emit('POLICY_APPLIED',{'summary':'Versioned Astra SANDBOX economic policy applied',
                    'revision':policy_revision(state),'policy_hash':state['policy_hash'],
                    'release_halts':list(command.proposal.release_halts)})
            except Exception as exc:
                self.policy_retry_at=self.clock()+timedelta(seconds=300)
                self.emit('POLICY_REJECTED',{'summary':'Economic-policy proposal has no authority',
                    'reason':candidate_code(exc)})
            return
        state=self.controller.state
        rec=state['reconciliation']
        if (not session.contains(self.clock()) or not rec['ready'] or not rec['settlement_known']
                or self.clock()>datetime.fromisoformat(rec['valid_until']) or state['latches']['incident']
                or any(not x['final'] for x in state['orders'].values())):return
        if self.model_budget and self.model_budget.health()['exhausted']:return
        if self.policy_retry_at is not None and self.clock()<self.policy_retry_at:return
        memory=self.store.recent_outcomes()
        fingerprint=account_fingerprint(state)
        # Current holdings/cash, new outcome evidence or a changed deep thesis
        # justify reassessment. Wall-clock expiry alone does not.
        outcome_evidence=tuple(row for row in memory if json.loads(row).get('memory_type')!='portfolio_economics')
        budget_health=self.model_budget.health() if self.model_budget else None
        spent=Decimal(budget_health['known_or_reserved_usd']) if budget_health else Decimal(0)
        budget=Decimal(budget_health['hard_cap_usd']) if budget_health else Decimal('125')
        budget_band='EXHAUSTED' if spent>=budget else ('LOW' if spent>=budget*Decimal('0.8') else 'AVAILABLE')
        material={'account':fingerprint,'outcomes':outcome_evidence,'deep':self.deep_context_signature,'model_budget_band':budget_band}
        signature=sha256(json.dumps(material,sort_keys=True).encode()).hexdigest()
        material_signature=signature
        if self.policy_retry_at is not None:
            signature+='-retry-'+self.policy_retry_at.isoformat();self.policy_retry_at=None
        if not self._queue().claim_aux('economic_policy',signature):return
        context={'as_of':self.clock().isoformat(),'account_fingerprint':fingerprint,'material_signature':material_signature,
            'based_revision':policy_revision(state),'based_policy_hash':state['policy_hash'],
            'current_policy':state['economic_policy'],'cash':state['cash'],'positions':state['positions'],
            'session':state['session'],'latches':state['latches'],'day_start_equity':state['day_start_equity'],
            'historic_high_water':state['historic_high_water'],'segment_high_water':state['segments'][-1]['high_water_usd'],
            'daily_realized':state['daily_realized'].get(state['session']['label'],'0'),
            'performance_memory':memory,'deep_plan':self.deep_plan,
            'immutable_boundaries':IMMUTABLE_BOUNDARIES,
            'model_budget':budget_health}
        context['context_hash']=sha256(json.dumps(context,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        self.policy_pending=(self.policy_executor.submit(self.brain_factory().policy_decide,context),context)

    def _queue(self):
        if self.opportunities is None:
            self.opportunities=OpportunityQueue(self.store,self.config.mode.value,self.clock,
                workers=self.config.opportunity_workers,calls_per_window=self.config.opportunity_calls_per_window,
                window=self.interval)
        if self.controller is not None and 'economic_policy' in self.controller.state:
            self.opportunities.calls_per_window=effective_policy(self.controller.state).calls_per_300s
            self.opportunities.window=300
        if self.model_budget is not None:
            self.opportunities.calls_per_window=min(self.opportunities.calls_per_window,
                int(self.model_budget.health()['non_position_calls_per_300s']))
        return self.opportunities

    def _promote_hot(self,queue,discovery_stats):
        promotions=queue.promote_hot(discovery_stats.get('screen_runners',()))
        for promotion in promotions:
            self.emit('HOT_WATCH_PROMOTED',{'summary':'Broad-screen runner entered bounded hot watch; no order authority',
                **promotion,'hot_slots':HOT_WATCH_SLOTS,'refresh_seconds':HOT_REFRESH_SECONDS,
                'ttl_seconds':HOT_WATCH_TTL_SECONDS})
        self._sync_stream_watch()
        return promotions

    def _sync_stream_watch(self):
        if self.live_stream is None or self.opportunities is None:return
        now=self.clock()
        state=self.controller.state if getattr(self,'controller',None) is not None else {}
        held=[state['instruments'][iid]['ticker'] for iid in state.get('positions',{})]
        candidates=[j['symbol'] for j in sorted(self.opportunities.jobs.values(),key=lambda x:(
            0 if x['status'] in ('RUNNING','RESULT_READY','EXECUTING') else 1,x.get('hot_rank',999),x.get('rank',999)))
            if j.get('current') or j['status'] in ('RUNNING','RESULT_READY','EXECUTING')]
        symbols=list(dict.fromkeys(held+candidates))[:35]
        self.live_stream.watch(symbols)
        changed=False
        for job in self.opportunities.jobs.values():
            if job.get('symbol') not in symbols or job['status'] in ('RUNNING','RESULT_READY','EXECUTING'):continue
            revision=self.live_stream.revision(job['symbol'])
            if revision<=job.get('stream_signaled_revision',0):continue
            if self.live_stream.fresh_quote(job['symbol']) is None:continue
            job['stream_signaled_revision']=revision
            if revision>job.get('stream_evaluated_revision',0):
                job.pop('hot_epoch_blocked_signal_at',None)
                job.update(status='PENDING',reason='STREAM_MATERIAL_CHANGE',not_before=now.isoformat(),updated_at=now.isoformat())
                changed=True
        if changed:self.opportunities.save()
        self.update(live_market_stream=self.live_stream.health())

    def _start_planning(self,broad):
        if self.model_budget and self.model_budget.health()['exhausted']:return
        signature=deep_context_signature(broad)
        now=self.clock()
        policy=captured_policy(broad)
        due=(self.deep_plan is None or self.deep_plan_at is None or
            (now-self.deep_plan_at).total_seconds()>=policy.deep_min_interval_seconds) if isinstance(policy,SandboxEconomics) else deep_plan_due(self.deep_plan,self.deep_plan_at,now)
        if ('deep' not in self.background and due
                and (self.deep_plan is None or signature!=self.deep_context_signature)
                and now>=self.background_retry.get('deep',now)
                and self._queue().claim_aux('deep',signature)):
            future=self.planning_executor.submit(self.brain_factory().deep_plan,broad)
            self.background['deep']=(future,broad,signature,self.clock())

    def _drain_background(self):
        for kind,(future,broad,signature,started) in list(self.background.items()):
            if not future.done():
                if self.clock()-started>timedelta(seconds=90):
                    self.update(planning_health=kind.upper()+'_TIMEOUT_SLOT_RETAINED')
                continue
            del self.background[kind]
            try:
                value=future.result()
                if self.clock()-started>timedelta(seconds=90):
                    self.emit('ALPHA_DEEP_PLAN' if kind=='deep' else 'SHADOW_AUDIT',{
                        'summary':'Late '+kind+' result recorded without planning authority',
                        'deep' if kind=='deep' else 'shadow':value,'disposition':'LATE_RESULT_NO_AUTHORITY'})
                    continue
                if kind=='deep':
                    assert_policy_current(broad,self.controller.state)
                    self.deep_plan=value['plan'];self.deep_plan_at=self.clock();self.deep_context_signature=signature
                    self.unassigned_deep_cost+=Decimal(value.get('estimated_cost_usd') or 0)
                    self.emit('ALPHA_DEEP_PLAN',{'summary':'Astra deep market plan refreshed','deep':value})
                    self.baseline_pending=baseline_snapshot(broad)
                    self.emit('BASELINE_SNAPSHOT',{'summary':'Cheap benchmark signals captured',**self.baseline_pending})
                    if self.opportunities is not None:
                        ranks={x['instrument_id']:x['rank'] for x in self.deep_plan.get('focus',())}
                        for j in self.opportunities.jobs.values():j['rank']=ranks.get(j['instrument_id'],100)
                        self.opportunities.save()
                    if (self.shadow_factory is not None and 'shadow' not in self.background
                            and self._queue().claim_aux('shadow',signature)):
                        self.background['shadow']=(self.shadow_executor.submit(self.shadow_factory().shadow_audit,broad),broad,signature,self.clock())
                else:
                    audit_id=uuid4().hex;self.current_shadow_audit_id=audit_id
                    self.shadow_pending=shadow_snapshot(value,broad,self.deep_plan or {},audit_id)
                    self.emit('SHADOW_AUDIT',{'summary':'Non-authoritative asynchronous shadow audit recorded',
                        'audit_id':audit_id,'shadow':value,'snapshot':self.shadow_pending})
                self.update(planning_health='OK')
            except Exception as exc:
                self.background_retry[kind]=self.clock()+timedelta(seconds=self.interval)
                self.emit('ALPHA_DEEP_PLAN_FAILED' if kind=='deep' else 'SHADOW_AUDIT_FAILED',
                    {'summary':kind+' observer failed; independent candidates continue','reason':candidate_code(exc)})

    @staticmethod
    def _candidate_read_failure(exc):
        # refresh_focus includes account reads. Never classify an unknown
        # account/source failure as local just because it happened during focus.
        value=str(exc)
        return value in {'SELECTED_SYMBOL_NOT_ELIGIBLE','STALE_MARKET_OR_ACCOUNT','STALE_BARS',
            'INELIGIBLE_OR_ILLIQUID_STOCK','FOCUSED_INSTRUMENT_IDENTITY_MISMATCH',
            'FOCUSED_INSTRUMENT_PAYLOAD_INCOMPLETE','FOCUSED_INSTRUMENT_PAYLOAD_INVALID'} or value.startswith((
            'BROKER_READ_FAILED SNAPSHOT ','BROKER_READ_FAILED INSTRUMENTS ','BROKER_READ_FAILED HISTORY_BATCH '))

    def _run_candidates(self,session,*,stream_only=False):
        queue=self._queue()
        try:
            # One admission per runner turn gives held supervision priority
            # before each subsequent result, including after a new fill.
            while True:
                completed=queue.completed(limit=1)
                if not completed:break
                job,context,ai=completed[0]
                previous_positions=set(self.controller.state['positions'])
                self._candidate_result(job,context,ai,session)
                # Drain completed no-trade results immediately, but process at
                # most one OPEN result per writer turn regardless of its broker
                # outcome. Held supervision still precedes every turn.
                if ((ai.get('decision') or {}).get('action')=='OPEN'
                        or set(self.controller.state['positions'])!=previous_positions
                        or any(not o['final'] for o in self.controller.state['orders'].values())):return
            if not session.permits_entry(self.clock()):
                self.update(activity='Portfolio supervised; session closed to entries');return
            if self.model_budget and self.model_budget.health()['exhausted']:
                self.next_evaluation=self.clock()+timedelta(seconds=self.interval)
                self.update(status='RUNNING',last_error=None,activity='Monthly model-cost cap reached; deterministic supervision continues',
                    next_evaluation=self.next_evaluation.isoformat());return
            if len(self.controller.state['positions'])>=effective_policy(self.controller.state).maximum_positions:return
            queue.expire_overdue_hot()
            held=self.controller.state['positions']
            ready=queue.eligible_jobs(held)
            if stream_only:
                ready=[job for job in ready if self.live_stream.fresh_quote(job['symbol']) is not None
                    and self.live_stream.revision(job['symbol'])>job.get('stream_evaluated_revision',0)]
            shortlist=portfolio_shortlist(ready,session)
            slot=portfolio_call_slot(session,self.clock())
            slot_key='portfolio_entry|'+session.label.isoformat()+'|'+str(slot)
            if slot is None or slot_key in queue.aux:
                self.update(activity='Portfolio supervised; scheduled entry research already used or not due')
                return
            # Bounded batch, reads/reconciliation serialized here. Workers see
            # immutable contexts only and have no gateway/controller handles.
            for job in shortlist[:1]:
                if queue.capacity()<=0:break
                prior_future_ids=set(queue.futures)
                queue.prepare(job)
                refreshed=None
                try:
                    state=self.controller.state
                    hot=queue._hot(job,self.clock())
                    streamed=(self.live_stream is not None and self.live_stream.revision(job['symbol'])>job.get('stream_evaluated_revision',0))
                    focused=hot or streamed
                    alternatives=[j['symbol'] for j in shortlist[1:]]
                    job['exact_refresh_started_at']=self.clock().isoformat();queue.save()
                    try:
                        refreshed=(self.source.refresh_comparative_focus(state,job['symbol'],alternatives)
                            if alternatives and hasattr(self.source,'refresh_comparative_focus')
                            else self.source.refresh_focus(state,job['symbol']))
                    finally:
                        at=self.clock();job['exact_refresh_completed_at']=at.isoformat()
                        job.setdefault('first_exact_refresh_completed_at',at.isoformat());queue.save()
                    item=refreshed.select(job['symbol'],at)
                    job['market_source_at']=item.quote_at.isoformat()
                    job['market_received_at']=(item.quote_received_at or item.received_at).isoformat()
                    job['market_window_updated_at']=(item.stream_window_updated_at or at).isoformat()
                    if item.instrument_id!=job['instrument_id']:raise ValueError('FOCUSED_INSTRUMENT_IDENTITY_MISMATCH')
                    refreshed_by_id={x.instrument_id:x for x in refreshed.candidates}
                    for shortlisted in shortlist:
                        observed=refreshed_by_id.get(shortlisted['instrument_id'])
                        if observed is None or observed.ticker!=shortlisted['symbol']:continue
                        observed.require_fresh(at)
                        screen=next((x for x in getattr(self,'latest_top_runners',()) if x.get('symbol')==shortlisted['symbol']),None)
                        queue.record_observation(shortlisted,observed,at,
                            regime_label=(self.deep_plan or {}).get('regime_label'),screen_evidence=screen)
                except Exception as exc:
                    if not self._candidate_read_failure(exc):raise
                    self.emit('CANDIDATE_MARKET_DATA_FAILURE',{'summary':'Candidate market refresh failed; no model or order authority',
                        'symbol':job['symbol'],'reason':candidate_code(exc),'checked_at':self.clock().isoformat()})
                    if hot and str(exc)=='STALE_MARKET_OR_ACCOUNT':
                        item=next((x for x in getattr(refreshed,'candidates',())
                            if x.instrument_id==job['instrument_id']),None)
                        checked_at=self.clock()
                        self.emit('HOT_EXACT_REFRESH_FAILED',{'summary':'Hot exact-symbol quote remained stale; bounded retry only',
                            'symbol':job['symbol'],'instrument_id':job['instrument_id'],'reason':candidate_code(exc),
                            'checked_at':checked_at.isoformat(),
                            'quote_at':item.quote_at.isoformat() if item else None,
                            'quote_received_at':item.quote_received_at.isoformat() if item and item.quote_received_at else None,
                            'source_age_ms':None if item is None else int((checked_at-item.quote_at).total_seconds()*1000),
                            'refresh_elapsed_ms':int((checked_at-datetime.fromisoformat(job['exact_refresh_started_at'])).total_seconds()*1000)})
                        if queue.retry_hot_refresh(job,candidate_code(exc)):continue
                        continue
                    queue.finish(job,'DROPPED',candidate_code(exc));continue
                if self.portfolio_research is not None:
                    observed_shortlist=tuple(refreshed_by_id[j['instrument_id']] for j in shortlist
                        if j['instrument_id'] in refreshed_by_id)
                    if hasattr(self.portfolio_research,'prepare'):
                        self.portfolio_research.prepare(observed_shortlist,at,session.label)
                    retained=[]
                    for shortlisted in shortlist:
                        observed=refreshed_by_id.get(shortlisted['instrument_id'])
                        try:
                            if observed is None or observed.ticker!=shortlisted['symbol']:
                                raise PortfolioResearchError('RESEARCH_EXACT_QUOTE_MISSING')
                            research=self.portfolio_research.context_for(observed,at)
                        except PortfolioResearchError as exc:
                            reason=str(exc)
                            self.emit('PORTFOLIO_RESEARCH_EXCLUDED',{
                                'summary':'Candidate lacks mandatory broker identity or fresh exact quote',
                                'symbol':shortlisted['symbol'],'instrument_id':shortlisted['instrument_id'],
                                'reason':reason})
                            queue.finish(shortlisted,'DROPPED',reason);continue
                        shortlisted['daily_research']=(research.daily.model_dump(mode='json') if research.daily else None)
                        shortlisted['financial_research']=research.financial_context
                        retained.append(shortlisted)
                    shortlist=tuple(retained);queue.save()
                    if job not in shortlist:
                        if not shortlist:self.emit('PORTFOLIO_RESEARCH_NO_CALL',{
                            'summary':'No candidate retained mandatory broker identity and fresh exact quote; no paid Astra call'})
                        continue
                if hot and self.clock()-datetime.fromisoformat(job['hot_promoted_at'])>=timedelta(seconds=HOT_PROMOTION_MAX_WAIT_SECONDS):
                    queue.expire_hot(job,'HOT_PROMOTION_EXPIRED');continue
                # No broad candidate eligibility is used for held positions.
                shortlist_ids={j['instrument_id'] for j in shortlist}
                authority_ids=set(state['positions'])|shortlist_ids
                authority=replace(refreshed,candidates=tuple(x for x in refreshed.candidates if x.instrument_id in authority_ids))
                self.controller.refresh(authority);self._account_refresh_completed()
                state=self.controller.state
                if not state['reconciliation']['ready'] or any(state['latches'].values()) or any(not o['final'] for o in state['orders'].values()):
                    queue.finish(job,'QUARANTINED','ACCOUNT_NOT_READY');raise ValueError('ACCOUNT_NOT_READY')
                if not session.permits_entry(self.clock()):
                    queue.finish(job,'DROPPED','SESSION_CLOSED');break
                try:
                    ids=tuple(set(state['positions'])|shortlist_ids)
                    daily_research,financial_research=portfolio_research_maps(shortlist)
                    context=build_alpha_context(authority,state,at,instrument_ids=ids,recent_outcomes=self.pipeline_outcomes,
                        position_plans=self.alpha_manager.plans(state) if state['positions'] else {},
                        observed_top_runners=runner_evidence(getattr(self,'latest_top_runners',()),at),
                        opportunity_dossiers={j['instrument_id']:queue.dossier(j,at) for j in shortlist},
                        daily_research=daily_research,financial_research=financial_research)
                    context=context.model_copy(update={'focal_instrument_id':None,'comparative_opportunities':()})
                    if {x.instrument_id for x in context.instruments}!=shortlist_ids:raise ValueError('MISSING_INSTRUMENT_CONTEXT')
                    signature=fast_context_signature(context)
                except Exception as exc:
                    self.emit('CANDIDATE_CONTEXT_FAILURE',{'summary':'Candidate context preparation failed before Astra',
                        'symbol':job['symbol'],'reason':candidate_code(exc)})
                    queue.finish(job,'QUARANTINED',candidate_code(exc));continue
                # Reconciliation and context assembly can outlive the strict
                # entry-quote window. Never buy model reasoning against a mark
                # that already lacks entry authority at provider dispatch.
                try:item.require_fresh(self.clock())
                except ValueError as exc:
                    if str(exc)!='STALE_MARKET_OR_ACCOUNT':raise
                    checked_at=self.clock()
                    self.emit('CANDIDATE_MARKET_DATA_FAILURE',{
                        'summary':'Candidate quote expired before Astra dispatch; no paid call or order authority',
                        'symbol':job['symbol'],'reason':'STALE_MARKET_OR_ACCOUNT',
                        'stage':'PRE_MODEL_DISPATCH','checked_at':checked_at.isoformat(),
                        'quote_at':item.quote_at.isoformat(),
                        'quote_received_at':(item.quote_received_at or item.received_at).isoformat(),
                        'source_age_ms':int((checked_at-item.quote_at).total_seconds()*1000)})
                    if hot and queue.retry_hot_refresh(job,'STALE_MARKET_OR_ACCOUNT'):continue
                    queue.finish(job,'DROPPED','STALE_MARKET_OR_ACCOUNT');continue
                # Construct client on owner thread; it receives only this context.
                if not queue.claim_once(slot_key,signature):
                    queue.finish(job,'COMPLETED','PORTFOLIO_CALL_SLOT_ALREADY_RESERVED');break
                brain=self.brain_factory()
                if queue.submit(job,context,signature,lambda b=brain,c=context:b.portfolio_decide(c)):
                    if self.live_stream is not None:
                        job['stream_evaluated_revision']=self.live_stream.revision(job['symbol']);queue.save()
                    if hasattr(self,'wake_event'):
                        for future,_,_ in queue.futures.values():future.add_done_callback(lambda _:self.wake_event.set())
                    self.emit('ALPHA_FOCUS_SELECTED',{'summary':'Scheduled bounded portfolio analysis started',
                        'symbol':job['symbol'],'instrument_id':job['instrument_id'],'job_id':job.get('job_id'),
                        'portfolio_slot':slot,'shortlist':[{'symbol':j['symbol'],'instrument_id':j['instrument_id']} for j in shortlist],
                        'hot_watch':hot,'hot_epoch_id':job.get('hot_epoch_id'),
                        'signal_at':job.get('hot_signal_at'),'promoted_at':job.get('hot_promoted_at'),
                        'exact_refresh_started_at':job.get('exact_refresh_started_at'),
                        'exact_refresh_completed_at':job.get('exact_refresh_completed_at'),'model_started_at':job.get('model_started_at'),
                        'market_source_at':job.get('market_source_at'),'market_received_at':job.get('market_received_at'),
                        'market_window_updated_at':job.get('market_window_updated_at'),
                        'comparison':list(context.comparative_opportunities),'execution_cost_evidence':context.execution_cost_evidence})
                # A model that completed while this blocking broker refresh ran
                # is handled before another refresh can add avoidable queue lag.
                prior_completed=any(job_id in queue.futures and queue.futures[job_id][0].done()
                    for job_id in prior_future_ids)
                ready_result=queue.completed(limit=1) if prior_completed else []
                if ready_result:
                    completed_job,completed_context,completed_ai=ready_result[0]
                    self._candidate_result(completed_job,completed_context,completed_ai,session)
                    return
            self.next_evaluation=self.clock()+timedelta(seconds=5)
            self.update(status='RUNNING',last_error=None,last_successful_cycle=self.clock().isoformat(),
                activity='Portfolio opportunity queue active; held positions have priority',next_evaluation=self.next_evaluation.isoformat())
        finally:self.update(pipeline=queue.health())

    def _candidate_result(self,job,context,ai,session):
        queue=self.opportunities
        if job['status']!='RESULT_READY':return
        call_job=job
        job['model_handled_at']=self.clock().isoformat()
        trace={k:job.get(k) for k in ('hot_epoch_id','hot_signal_at','hot_promoted_at','exact_refresh_started_at',
            'exact_refresh_completed_at','first_exact_refresh_completed_at','market_source_at','market_received_at',
            'market_window_updated_at','model_started_at','model_handled_at')}
        def finish_open(status,reason,summary='Astra OPEN stopped before broker submission'):
            decisions=(job.get('dossier') or {}).get('decisions',[])
            if decisions and decisions[-1]['action']=='OPEN':decisions[-1]['execution_block']=reason
            job['terminal_at']=self.clock().isoformat();trace.update({k:job.get(k) for k in (
                'post_model_refresh_started_at','post_model_refresh_completed_at','post_model_market_source_at',
                'post_model_market_received_at','post_model_window_updated_at','pre_order_validated_at',
                'post_model_source_age_ms','post_model_account_age_ms','risk_admitted_at','order_submit_started_at','terminal_at')})
            self.emit('ALPHA_ADMISSION_DROP',{'summary':summary,'symbol':job['symbol'],
                'instrument_id':job['instrument_id'],'reason':reason,'latency_trace':trace})
            queue.finish(job,status,reason)
        # Account reads never occur inside the candidate-local exception scope.
        binding_state=self.controller.state
        try:
            assert_policy_current(context,binding_state)
            decision=parse_alpha_decision(json.dumps(ai['decision']))
            if decision.action not in ('OPEN','ABSTAIN'):raise ValueError('ALPHA_ACTION_NOT_ALLOWED')
            routing=ai.get('defer_to')
            if routing is not None:
                known={(x['instrument_id'],x['symbol']) for x in map(json.loads,context.comparative_opportunities)}
                if (decision.action!='ABSTAIN' or not isinstance(routing,dict) or set(routing)!={'instrument_id','symbol'}
                        or (routing['instrument_id'],routing['symbol']) not in known):raise ValueError('INVALID_COMPARISON_ROUTING')
            if decision.action=='OPEN':
                selected=next((candidate for candidate in queue.jobs.values()
                    if candidate.get('current') and candidate.get('session')==session.label.isoformat()
                    and candidate['instrument_id']==decision.open.instrument_id
                    and candidate['symbol']==decision.open.symbol),None)
                known={(item.instrument_id,item.symbol) for item in context.instruments}
                if selected is None or (decision.open.instrument_id,decision.open.symbol) not in known:
                    raise ValueError('ALPHA_PORTFOLIO_SELECTION_MISMATCH')
                if selected is not call_job:
                    queue.finish(call_job,'COMPLETED','PORTFOLIO_SELECTED_OTHER')
                    selected.update(status='RESULT_READY',reason='PORTFOLIO_SELECTED',job_id=call_job.get('job_id'),
                        model_started_at=call_job.get('model_started_at'),updated_at=self.clock().isoformat())
                    queue.save();job=selected
            envelope=stamp_alpha_decision(decision,context,created_at=self.clock(),policy_version=binding_state['policy_hash'],
                capability_version=binding_state['config']['capabilities']['version'],model=ai['model'],reasoning=ai['reasoning'],usage=ai['usage'],
                estimated_cost_usd=ai.get('estimated_cost_usd') or '0')
        except Exception as exc:
            self.emit('ALPHA_DECISION',{'summary':'Portfolio model result rejected','ai':ai,'call_type':'PORTFOLIO_ENTRY',
                'job_id':call_job.get('job_id'),'latency_trace':trace})
            queue.finish(job,'DROPPED','ALPHA_CANONICAL_INVALID' if isinstance(exc,DecisionError) else candidate_code(exc));return
        try:
            evidence=build_portfolio_decision_evidence(context,decision,envelope,ai,
                regime_label=(self.deep_plan or {}).get('regime_label') or 'UNCLASSIFIED')
            persist_portfolio_decision_evidence(self.store,evidence,mode=self.config.mode.value,at=envelope.created_at)
        except Exception:
            queue.finish(job,'QUARANTINED','PORTFOLIO_DECISION_EVIDENCE_FAILED');return
        self.emit('ALPHA_DECISION',{'summary':'Astra portfolio entry action '+decision.action,'ai':ai,
            'envelope':envelope.model_dump(mode='json'),'call_type':'PORTFOLIO_ENTRY','job_id':call_job.get('job_id'),'latency_trace':trace,
            'symbol':job['symbol'],'instrument_id':job['instrument_id'],'opportunity_dossier':queue.dossier(job,self.clock())})
        queue.record_decision(job,decision.action,decision.reason,self.clock(),details={
            'decision_id':envelope.decision_id,'strategy':decision.open.strategy_label if decision.open else None,
            'market_context':{'regime':(self.deep_plan or {}).get('regime_label'),
                'source_quote_at':job.get('market_source_at')},'model_cost_usd':ai.get('estimated_cost_usd')})
        if self.shadow_pending and self.shadow_pending.get('astra_action') is None:
            item=next((x for x in context.instruments if x.instrument_id==job['instrument_id']),None)
            self.shadow_pending.update(astra_action=decision.action,astra_instrument_id=job['instrument_id'],
                astra_symbol=job['symbol'],astra_entry_ask_usd=str(item.ask_usd) if item else None)
        if decision.action=='ABSTAIN':
            routing=ai.get('defer_to')
            if routing:
                known={(x['instrument_id'],x['symbol']) for x in map(json.loads,context.comparative_opportunities)}
                identity=(routing.get('instrument_id'),routing.get('symbol'))
                if identity in known and identity[0]!=job['instrument_id']:
                    preferred=queue.prefer(*identity,held=binding_state['positions'])
                    self.emit('ALPHA_CANDIDATE_DEFERRED',{'summary':'Astra preferred an existing comparison candidate; no order authority',
                        'from_symbol':job['symbol'],'to_symbol':identity[1],'prioritized':preferred})
            queue.finish(job,'COMPLETED','DEFERRED' if routing else 'ABSTAIN',
                cooldown=HOT_REFRESH_SECONDS if queue._hot(job,self.clock()) else self.interval);return
        if not session.permits_entry(self.clock()):finish_open('DROPPED','SESSION_CLOSED');return
        try:validate_entry_window(decision,context,self.clock())
        except DecisionError as exc:
            finish_open('DROPPED',alpha_semantic_failure_code(exc));return
        state=self.controller.state
        if job['instrument_id'] in state['positions']:
            finish_open('DROPPED','ALREADY_OWNED');return
        if any(not o['final'] for o in state['orders'].values()) or any(state['latches'].values()):
            finish_open('QUARANTINED','ACCOUNT_NOT_READY');raise ValueError('ACCOUNT_NOT_READY')
        job['post_model_refresh_started_at']=self.clock().isoformat()
        try:
            latest=self.source.refresh_focus(state,job['symbol']);at=self.clock()
            job['post_model_refresh_completed_at']=at.isoformat()
            observed=next((x for x in latest.candidates if x.ticker==job['symbol']),None)
            if observed is not None:
                job['post_model_market_source_at']=observed.quote_at.isoformat()
                job['post_model_market_received_at']=(observed.quote_received_at or observed.received_at).isoformat()
                job['post_model_window_updated_at']=(observed.stream_window_updated_at or at).isoformat()
                job['post_model_source_age_ms']=int((at-observed.quote_at).total_seconds()*1000)
                job['post_model_account_age_ms']=int((at-observed.received_at).total_seconds()*1000)
            observation=latest.select(job['symbol'],at)
            if observation.instrument_id!=job['instrument_id']:raise ValueError('FOCUSED_INSTRUMENT_IDENTITY_MISMATCH')
        except Exception as exc:
            if not self._candidate_read_failure(exc):raise
            job.setdefault('post_model_refresh_completed_at',self.clock().isoformat())
            finish_open('DROPPED',candidate_code(exc));return
        self.controller.refresh(latest);self._account_refresh_completed()
        state=self.controller.state
        if not state['reconciliation']['ready']:raise ValueError('ACCOUNT_NOT_READY')
        original=next(x for x in context.instruments if x.instrument_id==job['instrument_id'])
        def reassess(reason):
            queue.record_observation(job,observation,at)
            job['execution_feedback']={'reason':reason,'prior_open':decision.open.model_dump(mode='json'),
                'bid':str(observation.bid),'ask':str(observation.ask),'quote_at':observation.quote_at.isoformat(),
                'authority':'NO_ORDER_SUBMITTED_REQUIRES_NEW_ASTRA_DECISION'}
            finish_open('PENDING',reason)
            job.update(not_before=self.clock().isoformat(),current=True)
            queue.save()
        if original.ask_usd<=decision.open.entry_limit_usd<observation.ask:
            # Do not turn a marketable Astra entry into an obsolete passive bid,
            # or spend beyond its price authority. Ask Astra against the new quote.
            reassess('ENTRY_PRICE_MOVED_REASSESS')
            return
        if decision.open.hard_stop_usd>=observation.bid:
            # A changed quote invalidates protection, not Astra's authority to
            # reconsider. No entry/stop/size is changed by deterministic code.
            reassess('STOP_GEOMETRY_REASSESS')
            return
        failure_stage='ALPHA_CONTEXT_INVALID'
        try:
            validate_entry_window(decision,context,self.clock())
            ids=tuple(set(state['positions'])|{job['instrument_id']})
            daily_research,financial_research=portfolio_research_maps((job,))
            fresh=build_alpha_context(latest,state,at,instrument_ids=ids,recent_outcomes=self.pipeline_outcomes,
                position_plans=self.alpha_manager.plans(state) if state['positions'] else {},
                opportunity_dossiers={job['instrument_id']:queue.dossier(job,at)},
                include_market_context=False,daily_research=daily_research,
                financial_research=financial_research)
            failure_stage='ALPHA_SEMANTIC_INVALID'
            validate_alpha_semantics(decision,fresh,self.clock(),maximum_quote_age_ms=5000)
            observation=latest.select(job['symbol'],self.clock())
            failure_stage='ALPHA_TRANSLATION_INVALID'
            intent,plan=translate_open(decision,fresh,self.controller.account_id,self.clock(),envelope.decision_id,
                capability_version=state['config']['capabilities']['version'],market_regime=(self.deep_plan or {}).get('regime_label','UNCLASSIFIED'),
                model_cost_usd=self.unassigned_deep_cost+Decimal(ai.get('estimated_cost_usd') or 0),shadow_audit_id=self.current_shadow_audit_id)
        except DecisionError as exc:
            finish_open('DROPPED',alpha_semantic_failure_code(exc));return
        except Exception as exc:
            reason=candidate_code(exc)
            finish_open('DROPPED',failure_stage if reason.startswith('CANDIDATE_FAILURE_') else reason);return
        if self.clock()>=intent.deadline:
            finish_open('DROPPED','ENTRY_EXPIRED_BEFORE_ADMISSION');return
        job['pre_order_validated_at']=self.clock().isoformat()
        self.emit('ALPHA_PLAN_CREATED',{'summary':'Validated alpha plan persisted before serialized admission',
            'plan_id':plan.plan_id,'instrument_id':plan.instrument_id,'plan':plan.model_dump(mode='json'),
            'latency_trace':{**trace,**{k:job.get(k) for k in ('post_model_refresh_started_at',
                'post_model_refresh_completed_at','post_model_market_source_at','post_model_market_received_at',
                'post_model_window_updated_at','pre_order_validated_at')}}})
        # This claim is durable before admission. A crash never replays the
        # result; M2 remains authoritative for its reservation/permit/order.
        queue.finish(job,'EXECUTING','RISK_ADMISSION')
        try:
            nonce=(self.controller.admit(intent,plan,policy_hash=envelope.policy_version)
                if 'economic_policy' in state else self.controller.admit(intent,plan))
        except Rejected as exc:
            if str(exc)=='STOP_GEOMETRY':
                reassess('STOP_GEOMETRY_REASSESS');return
            reasons=str(exc).split(',')
            local={'INSUFFICIENT_SETTLED_CASH','POSITION_LIMIT','ACCOUNT_EXPOSURE_LIMIT','STOCK_EXPOSURE_LIMIT',
                'OPEN_RISK','DAILY_DOWNSIDE_BUDGET','STALE_MARKET','STOP_GEOMETRY','SCHEDULED_LIQUIDATION',
                'EPISODE_FRAGMENTATION'}
            if not all(x in local or x.startswith('EPISODE_RISK:') for x in reasons):raise
            finish_open('DROPPED',str(exc));return
        job['risk_admitted_at']=self.clock().isoformat()
        self.unassigned_deep_cost=Decimal(0)
        self.emit('INTENT_APPROVED',{'summary':'M2 risk approved portfolio intent','decision_id':plan.plan_id})
        tracks_submission=hasattr(self.gateway,'submission_attempted')
        if tracks_submission:self.gateway.submission_attempted=False
        job['order_submit_started_at']=self.clock().isoformat()
        def refresh_entry(reserved):
            nonlocal observation,at
            renewed=self.source.refresh_focus(self.controller.state,job['symbol']);at=self.clock()
            observation=renewed.select(job['symbol'],at)
            if observation.instrument_id!=reserved.instrument_id:raise ValueError('FOCUSED_INSTRUMENT_IDENTITY_MISMATCH')
            # RESERVED is internal and has never reached Webull. Do not feed
            # its absence back as a broker-order mismatch. Verify the read
            # agrees with existing reconciled accounting, update only marks,
            # and retain the original reconciliation expiry.
            checked=self.controller.state
            expected_open={k for k,o in checked['orders'].items() if not o['final'] and o['phase']!='RESERVED'}
            if (set(renewed.open_intents)!=expected_open
                    or {k:Decimal(v) for k,v in renewed.positions.items()}!={k:Decimal(v['quantity']) for k,v in checked['positions'].items()}
                    or renewed.settled!=Decimal(checked['cash']['settled_usd'])
                    or renewed.unsettled!=Decimal(checked['cash']['unsettled_usd'])):
                raise ValueError('ACCOUNT_CHANGED_BEFORE_DISPATCH')
            for item in renewed.candidates:
                old=checked['marks'].get(item.instrument_id)
                if old and item.quote_at<datetime.fromisoformat(old['observed_at']):continue
                self.controller.send(Mark(instrument_id=item.instrument_id,bid_usd=item.bid,
                    observed_at=item.quote_at,source_id='WEBULL_SANDBOX'))
            job['dispatch_refresh_source_at']=observation.quote_at.isoformat()
            if reserved.protection.initial_stop_usd>=observation.bid:raise ValueError('STOP_GEOMETRY_REASSESS')
            if original.ask_usd<=reserved.limit_price_usd<observation.ask:raise ValueError('ENTRY_PRICE_MOVED_REASSESS')
            refreshed=self.controller.send(RefreshPermit(intent_id=reserved.intent_id))
            return observation,refreshed['outbox'][reserved.intent_id]['nonce']
        if tracks_submission:self.gateway.refresh_entry=refresh_entry
        try:broker=self.gateway.dispatch(intent,nonce,observation,self.controller,session,self.clock)
        except Exception as exc:
            if tracks_submission and not self.gateway.submission_attempted:
                # The gateway proves no external write began. Finalize the
                # durable reservation normally; never retain false uncertainty.
                self.controller.send(FinalizeOrder(intent_id=intent.intent_id,status='REJECTED',execution_ids=(),
                    total_quantity='0',total_fees_usd='0',evidence_id='prewrite-'+uuid4().hex))
                if str(exc) in ('STOP_GEOMETRY_REASSESS','ENTRY_PRICE_MOVED_REASSESS'):
                    reassess(str(exc));return
                finish_open('DROPPED',candidate_code(exc),'Broker submission did not begin');return
            finish_open('QUARANTINED','BROKER_WRITE_UNCERTAIN_NO_REPLAY','Broker write result uncertain');raise
        finally:
            if tracks_submission:self.gateway.refresh_entry=None
        job['terminal_at']=self.clock().isoformat();trace.update({k:job.get(k) for k in (
            'post_model_refresh_started_at','post_model_refresh_completed_at','post_model_market_source_at',
            'post_model_market_received_at','post_model_window_updated_at','pre_order_validated_at',
            'risk_admitted_at','order_submit_started_at','terminal_at')})
        self.emit('BROKER_RESULT',{'summary':'Portfolio entry REST result: '+broker['status'],'broker':broker,
            'latency_trace':trace})
        if broker['status'] not in ('FILLED','WORKING'):
            decisions=(job.get('dossier') or {}).get('decisions',[])
            if decisions:decisions[-1]['execution_block']='BROKER_'+broker['status']
        queue.finish(job,'COMPLETED','BROKER_'+broker['status'],cooldown=self.interval)

    def step(self):
        now=self.clock();session=self.calendar.session(now.astimezone(NY).date())
        if not self.initialized:self.initialize()
        if self.alpha_manager is not None:
            return self._alpha_step(now,session)
        state=self.controller.state
        # Holdings are handled before scheduling or entry checks, including after entry cutoff.
        if state['positions']:
            self.update(activity='Restoring/monitoring existing position',next_evaluation=None)
            if session is None or not session.contains(now):
                self.update(status='ERROR',last_error='OPEN_POSITION_OUTSIDE_SESSION',activity='Open position: waiting for regular session; no outside-session order')
                return
            o=self.source.refresh(state);o.require_monitor_fresh(self.clock());self.controller.lease();refresh_session(self.controller,o,self.clock)
            intent,plan,plan_version,original_plan=recover_plan_state(self.controller.state,self.store)
            self.emit('MONITOR_RESUMED',{'summary':'Deterministic position monitor restored from persisted plan'})
            self.monitor.manage(intent,plan,self.source,self.gateway,self.controller,session,self.clock,plan_version,original_plan)
            self.next_evaluation=self.clock()+timedelta(seconds=self.interval)
            return
        if now>=self.next_poll or (session and session.contains(now) and state['session']['label']!=session.label.isoformat()):
            o=self.source.refresh(state)
            if session and session.contains(now):o.require_fresh(self.clock())
            self.controller.lease();refresh_session(self.controller,o,self.clock)
            self.next_poll=self.clock()+timedelta(seconds=self.poll)
        state=self.controller.state
        if any(not o['final'] for o in state['orders'].values()) or any(state['latches'].values()) or not state['reconciliation']['ready']:
            self.update(status='ERROR',activity='Entries halted; reconciliation/safety latch requires attention',last_error='ACCOUNT_NOT_READY',next_evaluation=None)
            return
        if not session or not session.permits_entry(self.clock()):
            next_at=next_entry_time(self.calendar,self.clock())
            self.update(status='RUNNING',activity='Waiting for regular entry session',next_evaluation=next_at.isoformat() if next_at else None)
            return
        if self.clock()<self.next_evaluation:
            self.update(status='RUNNING',activity='Waiting for next evaluation',next_evaluation=self.next_evaluation.isoformat());return
        self.update(status='RUNNING',activity='Running trading cycle',next_evaluation=None)
        self.gateway.submission_attempted=False
        report=run_trading_cycle(config=self.config,calendar=self.calendar,clock=self.clock,source=self.source,
            brain=self.brain_factory(),controller=self.controller,gateway=self.gateway,context_factory=make_context,monitor=self.monitor,emit=self.emit)
        self.emit('CYCLE_COMPLETED',{'summary':'Cycle complete: '+str(report.get('astra_action'))+'; '+str(report.get('blocker') or 'OK'),'report':report})
        self.next_evaluation=self.clock()+timedelta(seconds=self.interval)
        if not report.get('blocker'):
            self.update(status='RUNNING',last_successful_cycle=self.clock().isoformat(),last_error=None)
        else:self.update(status='ERROR',last_error=report['blocker'])
        self.update(next_evaluation=self.next_evaluation.isoformat(),activity='Cycle complete; waiting')
    def record_error(self,exc=None):
        import re
        code=str(exc) if exc is not None and re.fullmatch(r'[A-Z_ 0-9:]{1,120}',str(exc)) else (
            'RUNNER_STEP_FAILED '+type(exc).__name__.upper() if exc is not None else 'RUNNER_STEP_FAILED')
        retry_at=self.clock()+timedelta(seconds=self.poll)
        self.next_poll=retry_at
        self.next_evaluation=max(self.next_evaluation,retry_at)
        self.update(status='ERROR',last_error=code,activity='Error; entries blocked until a fresh successful reconciliation',
            next_evaluation=self.next_evaluation.isoformat(),next_account_poll=self.next_poll.isoformat())
        try:self.emit('ERROR',{'summary':code+'; retrying state checks, no new order authority'})
        except Exception:pass
    def _writer_wait_seconds(self):
        queue=self.opportunities
        if queue is not None and any(queue.jobs[key].get('status')=='RUNNING'
                for _,key,_ in getattr(queue,'futures',{}).values()):return 1
        if self.live_stream is not None and self.live_stream.active():return 1
        if self.discovery_pending is not None:return 1
        return 5
    def run(self,stop):
        try:
            while not stop.is_set():
                self.wake_event.clear()
                try:
                    if os.name=='nt':
                        import ctypes
                        session=self.calendar.session(self.clock().astimezone(NY).date())
                        ctypes.windll.kernel32.SetThreadExecutionState(0x80000001 if session and session.contains(self.clock()) else 0x80000000)
                    self.step()
                except Exception as exc:self.record_error(exc)
                try:self.heartbeat()
                except Exception:pass
                self.wake_event.wait(self._writer_wait_seconds())
        finally:
            if self.live_stream is not None:self.live_stream.close()
