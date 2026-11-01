"""Versioned paper-account economics; operational authority is not model editable."""
from decimal import Decimal
from hashlib import sha256
import json
from typing import Annotated, Literal
from pydantic import Field, StrictInt, model_validator
from .contracts import Record, ID, UTC, Text, Positive, payload_hash
from .risk import POLICY


class SandboxEconomics(Record):
    account_exposure: Annotated[Positive, Field(le=1)] = Decimal('1')
    stock_exposure: Annotated[Positive, Field(le=1)] = Decimal('.40')
    trade_risk: Annotated[Positive, Field(le=1)] = Decimal('.015')
    open_risk: Annotated[Positive, Field(le=1)] = Decimal('.05')
    daily_loss: Annotated[Positive, Field(le=1)] = Decimal('.05')
    drawdown: Annotated[Positive, Field(le=1)] = Decimal('.20')
    experiment_death: Annotated[Positive, Field(le=1)] = Decimal('.90')
    # Nine held names plus a focal entry fit the certified ten-symbol refresh.
    maximum_positions: Annotated[StrictInt, Field(ge=1, le=9)] = 5
    # Astra may prefer a smaller spend; the independent monthly gate is fixed.
    model_budget_usd: Annotated[Positive, Field(le=275)] = Decimal('125')
    calls_per_300s: Annotated[StrictInt, Field(ge=1, le=120)] = 6
    deep_min_interval_seconds: Annotated[StrictInt, Field(ge=0, le=86400)] = 1800

    @model_validator(mode='after')
    def coherent(self):
        if not self.stock_exposure <= self.account_exposure:
            raise ValueError('stock exposure exceeds account exposure')
        if not self.trade_risk <= self.open_risk <= self.account_exposure:
            raise ValueError('incoherent risk geometry')
        return self


class PolicyProposal(Record):
    based_revision: Annotated[StrictInt, Field(ge=0)]
    based_policy_hash: ID
    policy: SandboxEconomics
    release_halts: tuple[Literal['daily', 'drawdown', 'terminated'], ...]
    reason: Text
    evidence: Text
    expected_incremental_value_usd: Annotated[Positive, Field(le=1000000000)]

    @model_validator(mode='after')
    def unique_releases(self):
        if len(set(self.release_halts)) != len(self.release_halts):
            raise ValueError('duplicate halt release')
        return self


class AuthorizeSandboxEconomics(Record):
    kind: Literal['AUTHORIZE_SANDBOX_ECONOMICS'] = 'AUTHORIZE_SANDBOX_ECONOMICS'
    authorization_id: Literal['owner-sandbox-economic-authority-20260921'] = 'owner-sandbox-economic-authority-20260921'


class ReleaseSandboxPreferences(Record):
    """One-time owner authorization; no broker action or model-made policy."""
    kind: Literal['RELEASE_SANDBOX_PREFERENCES'] = 'RELEASE_SANDBOX_PREFERENCES'
    authorization_id: Literal['owner-sandbox-preferences-20260922'] = 'owner-sandbox-preferences-20260922'


class AuthorizeDailyDownsideBudget(Record):
    kind: Literal['AUTHORIZE_DAILY_DOWNSIDE_BUDGET'] = 'AUTHORIZE_DAILY_DOWNSIDE_BUDGET'
    authorization_id: Literal['owner-catastrophic-downside-20260922'] = 'owner-catastrophic-downside-20260922'


class AuthorizeModelBudgetCap(Record):
    """Owner-authorized one-time migration of the shared paid-model ceiling."""
    kind: Literal['AUTHORIZE_MODEL_BUDGET_CAP'] = 'AUTHORIZE_MODEL_BUDGET_CAP'
    authorization_id: Literal['owner-model-budget-cap-125-20260923', 'owner-model-budget-cap-225-20260924',
        'owner-model-budget-cap-275-20260925'] = 'owner-model-budget-cap-275-20260925'


class ReviseSandboxEconomics(Record):
    kind: Literal['REVISE_SANDBOX_ECONOMICS'] = 'REVISE_SANDBOX_ECONOMICS'
    proposal: PolicyProposal
    context_hash: ID
    context_at: UTC
    response_at: UTC
    account_fingerprint: ID
    model: Literal['gpt-6-astra','claude-sonnet-5-5','claude-opus-5-5','claude-haiku-5-5','claude-fable-5-1']
    reasoning: ID
    decision_id: ID


# The REAL risk policy in force before the owner's 2026-10-16 revision. A REAL ledger replays every event
# under the policy recorded in its journal (CREATE / REAL_CONFIGURATION), never under whatever the running
# code says, so a rebuild reproduces history exactly across releases.
from decimal import Decimal as _D
from .risk import HardPolicy as _HardPolicy
# Frozen literal of every field (never read from risk.POLICY), so a future change of the code limits can never
# alter how an existing journal replays.
_REAL_BASE=dict(environment='OFFLINE',account_exposure=_D('1'),stock_exposure=_D('0.40'),maximum_positions=5,
    trade_risk=_D('0.015'),open_risk=_D('0.05'),daily_loss=_D('0.05'),drawdown=_D('0.20'),experiment_death=_D('0.90'),
    margin=False,borrowing=False,money_movement=False,direction='LONG_ONLY',universe='US_LISTED_LIQUID_STOCKS',
    session='CORE',drawdown_restart='AUTHENTICATED_OWNER_NEW_SEGMENT')
LEGACY_REAL_POLICY=_HardPolicy.model_construct(**_REAL_BASE)
_LIMIT_FIELDS=('account_exposure','stock_exposure','trade_risk','open_risk','daily_loss','drawdown','experiment_death')


def real_policy_from(values):
    return _HardPolicy.model_construct(**{**_REAL_BASE,**{k:_D(str(v)) for k,v in values.items() if k in _LIMIT_FIELDS}})


def effective_policy(state):
    value=state.get('economic_policy')
    if value:return SandboxEconomics.model_validate(value['policy'])
    if state.get('execution_scope')=='REAL':
        recorded=state.get('real_policy')
        return real_policy_from(recorded) if recorded else LEGACY_REAL_POLICY
    return POLICY


def daily_downside_budget(state):
    """Remaining loss capacity: realized loss and equity decline cannot be ignored.

    Gains do not enlarge the fixed start-of-day budget. Existing conservative
    stop risk (including exit costs/stress) and pending buys consume this amount.
    """
    day=Decimal(state['day_start_equity']);policy=effective_policy(state)
    realized=Decimal(state['daily_realized'].get(state['session']['label'],'0'))
    equity=Decimal(state['cash']['liquidation_equity_usd'])
    return max(Decimal(0),min(day*policy.daily_loss+min(Decimal(0),realized),
        equity-day*(1-policy.daily_loss)))


def aggregate_risk_limit(state):
    limit=min(Decimal(state['cash']['liquidation_equity_usd']),Decimal(state['day_start_equity']))*effective_policy(state).open_risk
    if state.get('economic_policy',{}).get('daily_downside_budget'):
        limit=min(limit,daily_downside_budget(state))
    return limit


def captured_policy(context):
    return SandboxEconomics.model_validate_json(context.economic_policy) if context.economic_policy else POLICY


def policy_revision(state):
    return state.get('economic_policy',{}).get('revision',0)


def assert_policy_current(context,state):
    if 'economic_policy' in state and (context.economic_policy_hash!=state['policy_hash']
            or context.economic_policy_revision!=policy_revision(state)):
        raise ValueError('STALE_POLICY_VERSION')


def account_fingerprint(state):
    """Changes to ownership, settlement, orders or session invalidate model authority."""
    value={'account':state['account_id'],'session':state['session']['label'],
        'cash':{k:state['cash'][k] for k in ('settled_usd','unsettled_usd','reserved_usd')},
        'positions':{k:(v['quantity'],v['episode_id']) for k,v in state['positions'].items()},
        'orders':{k:(v['phase'],v['filled_quantity']) for k,v in state['orders'].items() if not v['final']},
        'latches':state['latches']}
    return sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


IMMUTABLE_BOUNDARIES = ('SANDBOX only; PRODUCTION disabled; aggregate exposure <= current equity; '
    'settled cash only; no margin, borrowing, transfers, shorts or unsupported assets; exact owned quantities; '
    'broker idempotency, reconciliation, settlement, fencing, data validity and legal sessions; '
    'no incident/operational-block release; technical capacity 9 positions, 8 simultaneous model calls, '
    '6 billable model calls per rolling 300 seconds with at most 4 non-position calls, hard $275 '
    'model-cost ceiling per America/Denver calendar month '
    'shared across SANDBOX and DRY_RUN. Astra chooses sizing and capital deployment within the supplied '
    'effective limits; no allocation or trade count is required. Owner-released economic preferences are '
    'nonbinding. Daily loss, drawdown and experiment termination remain hard boundaries; no model action '
    'may change them or release safety halts. Aggregate planned downside including pending buys and '
    'exit costs/stress must fit remaining daily loss capacity after realized losses and equity decline; '
    'full settled-capital deployment is permitted only with sufficiently bounded downside. '
    'Model calls still require changed evidence and budget authority.')
