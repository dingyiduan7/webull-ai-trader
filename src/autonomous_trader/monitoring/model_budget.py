"""Durable shared calendar-month cap for every billable model request."""
from datetime import datetime,timedelta
from decimal import Decimal
import json
from threading import RLock
from uuid import uuid4
from zoneinfo import ZoneInfo
from ..economic_policy import SandboxEconomics
from .store import monthly_model_cost
from ..decision_api.pricing import priced, reserve as pricing_reserve

HARD_MONTHLY_USD=Decimal('275')
REAL_MONTHLY_COST_CONTROL='OWNER_TEMPORARY_CAP_SUSPENSION_V1'
REAL_MONTHLY_COST_SUSPENSION_ID='owner-real-monthly-cap-suspension-2026-09-30'
DENVER=ZoneInfo('America/Denver')
TEMPORARY_SANDBOX_SESSION_BONUS_USD=Decimal('25')
TEMPORARY_SANDBOX_SESSION_START=datetime(2026,9,28,7,30,tzinfo=DENVER)
TEMPORARY_SANDBOX_SESSION_END=datetime(2026,9,28,14,0,tzinfo=DENVER)
HARD_CALLS_PER_300S=6
MAX_NON_POSITION_CALLS_PER_300S=4
POSITION_CALL_TYPE='FAST_POSITION'
PORTFOLIO_ENTRY_CALL_TYPE='PORTFOLIO_ENTRY'
MAX_PORTFOLIO_CALLS_PER_SESSION=2
MAX_PORTFOLIO_CALLS_PER_EXCHANGE_WEEK=10
MAX_POSITION_CALLS_PER_SESSION=2
MAX_POSITION_CALLS_PER_EXCHANGE_WEEK=10
MAX_REAL_CALLS_PER_SESSION=None
MAX_REAL_CALLS_PER_EXCHANGE_WEEK=None
# REAL spend circuit breaker (Claude-owned, 2026-10-12). Not a trade cap: it
# bounds model spend per New York session so a loop or prompt bug cannot burn
# the account through API cost. Entry/research calls stop at the ceiling;
# position-management calls keep working up to POSITION_RESERVE_MULTIPLIER x.
# Raised 2026-10-16 (owner: model spend is an investment toward profit; two brains now share it).
REAL_DAILY_MODEL_SPEND_USD=Decimal('6')
POSITION_RESERVE_MULTIPLIER=2
BACKPRESSURE_CODES=('OPENAI_HTTP_429','ANTHROPIC_HTTP_429','ANTHROPIC_HTTP_529')


def hard_monthly_allowance(mode,at,policy_limit):
    """REAL cost is accounted but temporarily has no monthly dollar ceiling."""
    if mode=='REAL':return None
    base=min(HARD_MONTHLY_USD,policy_limit)
    local=at.astimezone(DENVER)
    if (mode=='SANDBOX' and base==HARD_MONTHLY_USD
            and TEMPORARY_SANDBOX_SESSION_START<=local<TEMPORARY_SANDBOX_SESSION_END):
        return base+TEMPORARY_SANDBOX_SESSION_BONUS_USD
    return base


def rolling_call_limits(policy_limit):
    total=min(HARD_CALLS_PER_300S,policy_limit)
    return total,min(MAX_NON_POSITION_CALLS_PER_300S,total)


def rolling_call_rejection(recent,call_type,total_limit,non_position_limit):
    if len(recent)>=total_limit:return 'MODEL_CALL_BUDGET_WAIT'
    if (call_type!=POSITION_CALL_TYPE
            and sum(row.get('call_type')!=POSITION_CALL_TYPE for row in recent)>=non_position_limit):
        return 'MODEL_CALL_BUDGET_WAIT'
    return None


def month_bounds(at):
    local=at.astimezone(DENVER)
    start=local.replace(day=1,hour=0,minute=0,second=0,microsecond=0)
    end=start.replace(year=start.year+1,month=1) if start.month==12 else start.replace(month=start.month+1)
    return start,end,start.strftime('%Y-%m')


def exchange_call_bounds(at):
    """New York session date and Monday-based exchange-week bounds."""
    local=at.astimezone(ZoneInfo('America/New_York'))
    session_start=local.replace(hour=0,minute=0,second=0,microsecond=0)
    session_end=session_start+timedelta(days=1)
    week_start=session_start-timedelta(days=session_start.weekday())
    return session_start,session_end,week_start,week_start+timedelta(days=7)


class ModelBudget:
    def __init__(self,store,mode,clock):
        if mode not in ('SANDBOX','DRY_RUN','REAL'):raise ValueError('MODEL_BUDGET_MODE_REQUIRED')
        self.store,self.mode,self.clock=store,mode,clock
        self.lock=RLock();self.policy=SandboxEconomics();self.active=set();self.calls={}
        self.cooldown=None;self.blocked=0

    @property
    def budget_modes(self):
        return ('REAL',) if self.mode=='REAL' else ('SANDBOX','DRY_RUN')

    def configure(self,policy):
        with self.lock:self.policy=policy

    def _state(self,now):
        start,end,month=month_bounds(now)
        return start,end,month,monthly_model_cost(self.store.model_budget_evidence(
            start,end,modes=self.budget_modes))

    def claim(self,body,*,category=None):
        with self.lock:
            now=self.clock();start,end,month,_=self._state(now)
            try:
                if not priced(body['model']):raise ValueError('MODEL_COST_UNBOUNDED')
                name=body['text']['format']['name']
                tokens=body['max_output_tokens']
                if type(tokens) is not int or tokens<=0:raise ValueError('MODEL_COST_UNBOUNDED')
                encoded=len(json.dumps(body,ensure_ascii=False).encode())
            except (KeyError,TypeError,ValueError) as exc:
                if isinstance(exc,ValueError) and str(exc)=='MODEL_COST_UNBOUNDED':raise
                raise ValueError('MODEL_COST_UNBOUNDED') from None
            if encoded>120000:raise ValueError('MODEL_CONTEXT_SIZE_LIMIT')
            # The request is text-only. UTF-8 bytes bound input tokens, with
            # 1,000 tokens of protocol overhead; max_output_tokens bounds
            # visible and reasoning output at the highest known model rates.
            reserve=pricing_reserve(body['model'],encoded,tokens)
            recent=self.store.model_budget_recent_claims(now,modes=self.budget_modes)
            call_type=category or name
            total_limit,non_position_limit=rolling_call_limits(self.policy.calls_per_300s)
            policy_call=name=='sandbox_economic_policy'
            reason=None
            if self.cooldown and now<self.cooldown:reason='MODEL_PROVIDER_BACKPRESSURE'
            elif len(self.active)>=8:reason='MODEL_CONCURRENCY_BACKPRESSURE'
            elif rolling_call_rejection(recent,call_type,total_limit,non_position_limit):reason='MODEL_CALL_BUDGET_WAIT'
            elif policy_call and any(c.get('category')==name for c in recent):reason='POLICY_REVIEW_BACKPRESSURE'
            if reason:self.blocked+=1;raise ValueError(reason)
            claim={'id':uuid4().hex,'at':now.isoformat(),'month':month,'mode':self.mode,'category':name,
                'call_type':call_type,'reserved_usd':str(reserve),'cost_usd':None}
            if self.mode=='REAL':claim.update(monthly_cost_control=REAL_MONTHLY_COST_CONTROL,
                monthly_cost_suspension_id=REAL_MONTHLY_COST_SUSPENSION_ID)
            allowance=hard_monthly_allowance(self.mode,now,self.policy.model_budget_usd)
            session_start,session_end,week_start,week_end=exchange_call_bounds(now)
            durable_position_type=None  # No owner-set REAL decision-count ceiling.
            real_session=None
            real_week=None
            try:self.store.reserve_model_claim(start,end,claim,allowance,
                recent_since=now-timedelta(seconds=300),total_limit=total_limit,
                non_position_limit=non_position_limit,position_call_type=POSITION_CALL_TYPE,
                budget_modes=self.budget_modes,
                portfolio_call_type=PORTFOLIO_ENTRY_CALL_TYPE if self.mode!='REAL' else None,
                portfolio_session=(session_start,session_end,MAX_PORTFOLIO_CALLS_PER_SESSION),
                portfolio_week=(week_start,week_end,MAX_PORTFOLIO_CALLS_PER_EXCHANGE_WEEK),
                durable_position_call_type=durable_position_type,
                position_session=(session_start,session_end,MAX_POSITION_CALLS_PER_SESSION),
                position_week=(week_start,week_end,MAX_POSITION_CALLS_PER_EXCHANGE_WEEK),
                all_calls_session=real_session,all_calls_week=real_week,
                daily_spend=((session_start,session_end,REAL_DAILY_MODEL_SPEND_USD,
                    REAL_DAILY_MODEL_SPEND_USD*POSITION_RESERVE_MULTIPLIER) if self.mode=='REAL' else None))
            except ValueError:self.blocked+=1;raise
            self.calls[claim['id']]=claim;self.active.add(claim['id'])
            return claim['id']

    def finish(self,claim,last_call):
        with self.lock:
            value={'id':claim,'cost_usd':last_call.get('estimated_cost_usd'),
                'usage':last_call.get('usage'),'response_status':last_call.get('response_status'),
                'ai':{**last_call,'model_work_id':claim,'cost_authority':'BILLABLE_WORK',
                    'call_type':self.calls[claim]['call_type']}}
            self.store.emit('MODEL_WORK_RESULT',value,mode=self.mode,at=self.clock(),event_id=claim+'-result')
            self.active.discard(claim)
            if last_call.get('error_code') in BACKPRESSURE_CODES:self.cooldown=self.clock()+timedelta(seconds=60)

    def health(self):
        with self.lock:
            now=self.clock();_,_,month,state=self._state(now)
            total=state['known']+state['reserved']
            allowance=hard_monthly_allowance(self.mode,now,self.policy.model_budget_usd)
            suspended=self.mode=='REAL'
            effective_hard_cap=(None if suspended else HARD_MONTHLY_USD+TEMPORARY_SANDBOX_SESSION_BONUS_USD
                if allowance>HARD_MONTHLY_USD else HARD_MONTHLY_USD)
            remaining=None if suspended else max(Decimal(0),allowance-total)
            if state['unbounded'] and not suspended:remaining=Decimal(0)
            return {'calendar_month':month,'timezone':'America/Denver',
                'hard_cap_usd':None if effective_hard_cap is None else str(effective_hard_cap),
                'monthly_cost_control':REAL_MONTHLY_COST_CONTROL if suspended else 'ENFORCED',
                'monthly_cost_suspension_id':REAL_MONTHLY_COST_SUSPENSION_ID if suspended else None,
                'known_cost_estimate_usd':str(state['known']),
                'unresolved_reservations_usd':str(state['reserved']),
                'remaining_available_usd':None if remaining is None else str(remaining),
                'exhausted':False if suspended else state['unbounded'] or remaining==0,
                'known_or_reserved_usd':str(total),'budget_usd':None if allowance is None else str(allowance),
                'calls_per_300s':rolling_call_limits(self.policy.calls_per_300s)[0],
                'non_position_calls_per_300s':rolling_call_limits(self.policy.calls_per_300s)[1],
                'daily_spend_ceiling_usd':str(REAL_DAILY_MODEL_SPEND_USD) if self.mode=='REAL' else None,
                'astra_calls_per_session':MAX_REAL_CALLS_PER_SESSION if self.mode=='REAL' else None,
                'astra_calls_per_week':MAX_REAL_CALLS_PER_EXCHANGE_WEEK if self.mode=='REAL' else None,
                'in_flight':len(self.active),
                'blocked_attempts':self.blocked,'unknown_cost_claims':state['unknown'],
                'provider_backpressure_until':self.cooldown.isoformat() if self.cooldown else None,
                'temporary_session_bonus_usd':str(allowance-HARD_MONTHLY_USD
                    if not suspended and allowance>HARD_MONTHLY_USD else Decimal(0)),
                'temporary_session_bonus_ends_at':(TEMPORARY_SANDBOX_SESSION_END.isoformat()
                    if not suspended and allowance>HARD_MONTHLY_USD else None),
                'cost_basis':'MODEL_ESTIMATE_NOT_PROVIDER_INVOICE'}
