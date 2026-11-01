"""Canonical immutable J2 records; all units are explicit in field names.

TradeDecisionV1 retains the exact supplied payload shape. Envelope[T] adds
service-owned provenance without modifying the source JSON Schema.
"""
from datetime import date, datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
from typing import Annotated, Generic, Literal, TypeVar

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, StrictInt, model_validator
from .numeric import decimal, nonnegative, positive


def utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone-aware timestamp required")
    return value.astimezone(timezone.utc)


UTC = Annotated[datetime, BeforeValidator(utc)]
Number = Annotated[Decimal, BeforeValidator(decimal)]
Nonnegative = Annotated[Decimal, BeforeValidator(nonnegative)]
Positive = Annotated[Decimal, BeforeValidator(positive)]
ID = Annotated[str, Field(min_length=1, max_length=128)]
Text = Annotated[str, Field(min_length=1, max_length=2000)]
Fraction = Annotated[Number, Field(ge=0, le=1)]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    @model_validator(mode="before")
    @classmethod
    def reject_floats(cls, values):
        def visit(item):
            if isinstance(item, float):
                raise ValueError("binary floating point is forbidden; use decimal strings")
            if isinstance(item, dict):
                for value in item.values():
                    visit(value)
            elif isinstance(item, (list, tuple)):
                for value in item:
                    visit(value)
        visit(values)
        return values


def payload_hash(payload: Record) -> str:
    canonical = json.dumps(payload.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return sha256(canonical.encode()).hexdigest()


T = TypeVar("T", bound=Record)


class Envelope(Record, Generic[T]):
    schema_version: Literal["1.0"] = "1.0"
    event_id: ID
    correlation_id: ID
    causation_id: ID | None
    created_at: UTC
    usable_at: UTC
    source_id: ID
    revision: Annotated[StrictInt, Field(ge=0)]
    payload_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    payload: T

    @model_validator(mode="after")
    def verify_hash(self):
        if self.payload_hash != payload_hash(self.payload):
            raise ValueError("payload hash mismatch")
        return self


class Predicate(Record):
    feature_id: ID
    source_id: ID
    operator: Literal["LT", "LTE", "GT", "GTE", "EQ", "NEQ"]
    value: Text
    persistence_ms: Annotated[StrictInt, Field(ge=0, le=600000)]


class ConditionGroup(Record):
    join: Literal["ALL", "ANY"]
    predicates: Annotated[tuple[Predicate, ...], Field(max_length=16)]


class SizingRequest(Record):
    unit: Literal["SHARES", "USD_NOTIONAL"]
    amount: Nonnegative
    minimum_acceptable_quantity: Nonnegative


class Protection(Record):
    initial_stop_usd: Nonnegative
    trigger_reference: Literal["TRADE", "BID", "ASK"]
    trigger_source_id: ID
    execution_policy_id: ID
    mechanism: Literal["BROKER_STOP_MARKET", "BROKER_STOP_LIMIT", "LOCAL_MONITORED_STOP"]
    stop_limit_usd: Nonnegative | None


class Target(Record):
    price_usd: Nonnegative
    fraction_of_initial_quantity: Fraction


class Trailing(Record):
    rule_id: ID
    mode: Literal["NONE", "AMOUNT", "PERCENT", "FEATURE_RULE"]
    activation_rule: ConditionGroup | None
    distance: Nonnegative | None


class EntryPlan(Record):
    instrument_id: ID
    ticker: ID
    direction: Literal["LONG"]
    entry_condition: ConditionGroup
    entry_order_type: Literal["LIMIT", "MARKET", "STOP_LIMIT"]
    session: Literal["CORE"]
    time_in_force: Literal["DAY"]
    maximum_entry_usd: Nonnegative
    limit_price_usd: Nonnegative | None
    trigger_price_usd: Nonnegative | None
    entry_expires_at: UTC
    sizing: SizingRequest
    protection: Protection
    targets: Annotated[tuple[Target, ...], Field(max_length=8)]
    trailing: Trailing
    max_holding_seconds: Annotated[StrictInt, Field(ge=1, le=23400)]
    latest_exit_at: UTC
    invalidation: ConditionGroup
    partial_fill_policy_id: ID
    max_spread_bps: Nonnegative
    strategy_episode_id: ID


class Amendment(Record):
    position_id: ID
    current_plan_id: ID
    current_plan_version: ID
    requested_stop_usd: Nonnegative | None
    requested_targets: Annotated[tuple[Target, ...], Field(max_length=8)] | None
    requested_trailing: Trailing | None
    requested_latest_exit_at: UTC | None
    requested_invalidation: ConditionGroup | None


class ExitRequest(Record):
    position_id: ID
    current_plan_id: ID
    mode: Literal["REDUCE", "CLOSE"]
    requested_quantity: Nonnegative | None
    execution_policy_id: ID
    reason_code: ID


class DataRequest(Record):
    query_type: Literal["QUOTE_REFRESH", "FILING_EXCERPT", "NEWS_REVISION", "EVENT_CALENDAR", "STRATEGY_EVIDENCE"]
    instrument_id: ID | None
    evidence_ids: Annotated[tuple[ID, ...], Field(max_length=32)]
    reason: Text


class Forecast(Record):
    event_definition: Text
    horizon_seconds: Annotated[StrictInt, Field(ge=1, le=23400)]
    raw_probability: Fraction | None
    expected_gross_return_bps: Number | None
    estimated_execution_cost_bps: Nonnegative | None
    net_return_quantile_10_bps: Number | None
    net_return_quantile_50_bps: Number | None
    net_return_quantile_90_bps: Number | None
    calibration_id: ID | None
    estimate_provenance: Literal["MODEL_JUDGMENT", "QUALIFIED_ESTIMATOR", "UNAVAILABLE"]


Action = Literal["ABSTAIN", "REQUEST_DATA", "PROPOSE_ENTRY", "PROPOSE_AMENDMENT", "PROPOSE_EXIT", "HOLD"]


class TradeDecisionV1(Record):
    schema_version: Literal["1.0"]
    decision_id: ID
    context_id: ID
    context_hash: ID
    opportunity_id: ID
    portfolio_state_version: ID
    policy_hash: ID
    capability_version: ID
    strategy_id: ID
    strategy_version: ID
    strategy_artifact_hash: ID
    created_at: UTC
    valid_until: UTC
    action: Action
    entry_plan: EntryPlan | None
    amendment: Amendment | None
    exit_request: ExitRequest | None
    data_request: DataRequest | None
    forecast: Forecast | None
    evidence_ids: Annotated[tuple[ID, ...], Field(max_length=64)]
    contradictions: Annotated[tuple[Text, ...], Field(max_length=8)]
    missing_data: Annotated[tuple[Text, ...], Field(max_length=8)]
    reasoning_summary: Text
    review_triggers: Annotated[tuple[ConditionGroup, ...], Field(max_length=8)]


TradeDecision = TradeDecisionV1


# Compact alpha boundary. These records contain model intent only; durable
# identity, policy, model, timing, token, and cost provenance belongs in the
# service-owned envelope below.
AlphaAction = Literal["ABSTAIN", "OPEN", "HOLD", "MODIFY", "CLOSE"]


class AlphaOpen(Record):
    instrument_id: ID
    symbol: ID
    strategy_label: Annotated[str, Field(min_length=1, max_length=120)]
    thesis: Annotated[str, Field(min_length=1, max_length=1000)]
    desired_notional_usd: Positive | None = None
    desired_quantity: Positive | None = None
    entry_limit_usd: Positive
    entry_expires_at: UTC
    hard_stop_usd: Positive
    latest_exit_at: UTC
    target_usd: Positive | None = None
    next_review_at: UTC

    @model_validator(mode="after")
    def exactly_one_size(self):
        if (self.desired_notional_usd is None) == (self.desired_quantity is None):
            raise ValueError("exactly one desired size is required")
        return self


class AlphaHold(Record):
    position_id: ID
    next_review_at: UTC


class AlphaModify(Record):
    position_id: ID
    requested_stop_usd: Positive | None = None
    target_action: Literal["KEEP", "SET", "REMOVE"] = "KEEP"
    requested_target_usd: Positive | None = None
    requested_next_review_at: UTC | None = None
    requested_latest_exit_at: UTC | None = None

    @model_validator(mode="after")
    def target_shape(self):
        if (self.target_action == "SET") != (self.requested_target_usd is not None):
            raise ValueError("SET requires one target; KEEP/REMOVE require no target")
        if (self.requested_stop_usd is None and self.target_action == "KEEP"
                and self.requested_next_review_at is None and self.requested_latest_exit_at is None):
            raise ValueError("empty modification")
        return self


class AlphaClose(Record):
    position_id: ID
    full_close: Literal[True]


class AlphaDecision(Record):
    schema_version: Literal["1.0"] = "1.0"
    action: AlphaAction
    reason: Annotated[str, Field(min_length=1, max_length=1000)]
    open: AlphaOpen | None = None
    hold: AlphaHold | None = None
    modify: AlphaModify | None = None
    close: AlphaClose | None = None

    @model_validator(mode="after")
    def action_payload(self):
        expected = {"ABSTAIN": None, "OPEN": "open", "HOLD": "hold",
                    "MODIFY": "modify", "CLOSE": "close"}[self.action]
        present = [name for name in ("open", "hold", "modify", "close")
                   if getattr(self, name) is not None]
        if present != ([] if expected is None else [expected]):
            raise ValueError("exactly one matching action payload is required")
        return self


class AlphaBar(Record):
    at: UTC
    open_usd: Positive
    high_usd: Positive
    low_usd: Positive
    close_usd: Positive
    volume: Nonnegative


class DailyMetricObservation(Record):
    metric: ID
    value: Number | None
    unit: Literal["RATIO", "USD", "USD_DOLLAR_VOLUME"]
    window_sessions: Annotated[StrictInt, Field(ge=1, le=60)]
    required_bar_count: Annotated[StrictInt, Field(ge=1, le=61)]
    observed_bar_count: Annotated[StrictInt, Field(ge=0, le=250)]
    start_session: date | None
    end_session: date | None
    source_id: ID
    source_at: UTC | None
    received_at: UTC
    as_of: UTC
    unavailable_reason: ID | None = None

    @model_validator(mode="after")
    def availability(self):
        available = self.value is not None
        if available != (self.unavailable_reason is None):
            raise ValueError("daily metric availability mismatch")
        if available and (self.start_session is None or self.end_session is None or self.source_at is None):
            raise ValueError("available daily metric lacks provenance")
        return self


class DailyResearchContext(Record):
    instrument_id: ID
    symbol: ID
    benchmark_symbol: Literal["SPY"] = "SPY"
    adjustment_basis: Literal["FORWARD_ADJUSTED"]
    source_id: ID
    source_path: Literal["/market-data/stocks/bars/list"]
    received_at: UTC
    as_of: UTC
    completed_bar_count: Annotated[StrictInt, Field(ge=0, le=250)]
    excluded_incomplete_or_future_bars: Annotated[StrictInt, Field(ge=0, le=250)]
    source_payload_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    methodology: Literal["returns=latest_close/prior_close-1; median_dollar_volume=median(adjusted_close*volume); atr_20_ratio=mean_20(max(high-low,abs(high-prior_close),abs(low-prior_close)))/latest_close; distance_sma=latest_close/mean_close-1; relative_strength=candidate_return-SPY_return_on_identical_sessions"]
    metrics: Annotated[tuple[DailyMetricObservation, ...], Field(min_length=14, max_length=14)]


class AlphaInstrumentContext(Record):
    instrument_id: ID
    symbol: ID
    quote_at: UTC
    bid_usd: Positive
    ask_usd: Positive
    price_increment_usd: Positive
    quantity_increment: Positive
    reference_price_usd: Positive | None = None
    spread_bps: Nonnegative | None = None
    bid_size: Nonnegative | None = None
    ask_size: Nonnegative | None = None
    relative_volume_10d: Nonnegative | None = None
    bars: Annotated[tuple[AlphaBar, ...], Field(max_length=240)] = ()
    volume: Nonnegative | None = None
    volatility: Nonnegative | None = None
    price_movement: Number | None = None
    market_context: Text | None = None
    sector_context: Text | None = None
    daily_research: DailyResearchContext | None = None


class AlphaPositionContext(Record):
    position_id: ID
    instrument_id: ID
    symbol: ID
    quantity: Positive
    average_entry_usd: Positive
    mark_usd: Positive
    active_stop_usd: Positive
    target_usd: Positive | None = None
    latest_exit_at: UTC
    unrealized_pnl_usd: Number
    strategy_label: Annotated[str, Field(min_length=1, max_length=120)] | None = None
    original_thesis: Annotated[str, Field(min_length=1, max_length=1000)] | None = None
    entry_limit_usd: Positive | None = None
    original_stop_usd: Positive | None = None
    original_target_usd: Positive | None = None
    next_review_at: UTC | None = None
    plan_version: Annotated[StrictInt, Field(ge=1)] | None = None


class AlphaContext(Record):
    as_of: UTC
    expires_at: UTC
    session_open: UTC
    session_close: UTC
    liquidation_at: UTC
    instruments: Annotated[tuple[AlphaInstrumentContext, ...], Field(min_length=1)]
    positions: Annotated[tuple[AlphaPositionContext, ...], Field(max_length=9)] = ()
    settled_cash_usd: Nonnegative
    remaining_risk_usd: Nonnegative
    reconciliation_ready: bool
    settlement_known: bool
    recent_outcomes: Annotated[tuple[Text, ...], Field(max_length=30)] = ()
    # Compact JSON evidence only; these rows are never executable instruments.
    comparative_opportunities: Annotated[tuple[Text, ...], Field(max_length=8)] = ()
    # Read-only discovery evidence, never executable instrument authority.
    observed_top_runners: Annotated[tuple[Text, ...], Field(max_length=10)] = ()
    execution_cost_evidence: Text | None = None
    # Compact broad-market (SPY) features; context only, never an instrument.
    market_regime: Text | None = None
    focal_instrument_id: ID | None = None
    economic_policy: str | None = None
    economic_policy_hash: ID | None = None
    economic_policy_revision: Annotated[StrictInt, Field(ge=0)] = 0
    immutable_boundaries: str | None = None
    # Latest permitted plan exit when multi-day holding is authorized (carry.exit_horizon); None means
    # same-day only (liquidation_at). Entries themselves always expire within the current session.
    exit_horizon_at: UTC | None = None

    @property
    def exit_horizon(self):
        return self.exit_horizon_at or self.liquidation_at


class PortfolioCandidateEvidence(Record):
    instrument_id: ID
    symbol: ID
    quote_at: UTC
    bid_usd: Positive
    ask_usd: Positive
    quote_source_id: ID
    candidate_evidence_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    market_context_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None
    daily_bar_payload_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None
    unavailable_fields: Annotated[tuple[ID, ...], Field(max_length=40)] = ()


class PortfolioDecisionEvidence(Record):
    record_version: Literal["1.0"] = "1.0"
    decision_id: ID
    action: Literal["ABSTAIN", "OPEN"]
    decision_snapshot_at: UTC
    decision_cutoff_at: UTC
    model_response_at: UTC
    response_within_cutoff: bool
    context_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    candidate_set_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    candidates: Annotated[tuple[PortfolioCandidateEvidence, ...], Field(min_length=1)]
    prompt_version: ID
    prompt_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    model: ID
    reasoning: ID
    decision_schema_version: Literal["1.0"]
    decision_schema_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    model_work_claim_id: ID
    model_work_result_id: ID
    latency_ms: Annotated[StrictInt, Field(ge=0, le=600000)]
    model_cost_usd: Nonnegative | None
    model_cost_status: Literal["FINAL", "UNKNOWN"]
    cost_authority: Literal["REFERENCE_ONLY_MODEL_WORK_RESULT_IS_CANONICAL"] = "REFERENCE_ONLY_MODEL_WORK_RESULT_IS_CANONICAL"
    stated_reason: Text
    stated_thesis: Text | None
    stated_prediction: Text
    regime_label: ID
    stated_horizon_start: UTC | None
    stated_horizon_end: UTC | None
    selected_instrument_id: ID | None
    selected_symbol: ID | None
    entry_exit_plan: AlphaOpen | None
    order_intent_plan_id: ID | None
    outcome_plan_id: ID | None
    episode_id: ID | None

    @model_validator(mode="after")
    def coherent_record(self):
        opened=self.action=="OPEN"
        selected=(self.selected_instrument_id,self.selected_symbol,self.entry_exit_plan,
            self.order_intent_plan_id,self.outcome_plan_id,self.episode_id,self.stated_thesis,
            self.stated_horizon_start,self.stated_horizon_end)
        if opened and any(value is None for value in selected):
            raise ValueError("OPEN decision evidence lacks linkage or plan")
        if not opened and any(value is not None for value in selected):
            raise ValueError("ABSTAIN decision evidence contains OPEN linkage")
        if (self.model_cost_status=="FINAL") != (self.model_cost_usd is not None):
            raise ValueError("model cost status mismatch")
        if self.model_response_at<self.decision_snapshot_at:
            raise ValueError("decision evidence violates temporal causality")
        if self.response_within_cutoff!=(self.model_response_at<=self.decision_cutoff_at):
            raise ValueError("decision cutoff status mismatch")
        if self.model_work_claim_id!=self.model_work_result_id:
            raise ValueError("model work linkage mismatch")
        return self


class AlphaDecisionEnvelope(Record):
    decision_id: ID
    context_hash: ID
    created_at: UTC
    valid_until: UTC
    policy_version: ID
    capability_version: ID
    model: ID
    reasoning: ID
    input_tokens: Annotated[StrictInt, Field(ge=0)]
    output_tokens: Annotated[StrictInt, Field(ge=0)]
    estimated_cost_usd: Nonnegative
    decision: AlphaDecision


class DeepFocusItem(Record):
    instrument_id: ID
    symbol: ID
    rank: Annotated[StrictInt, Field(ge=1, le=20)]
    strategy_label: Annotated[str, Field(min_length=1, max_length=120)]
    thesis: Annotated[str, Field(min_length=1, max_length=1000)]
    horizon: Annotated[str, Field(min_length=1, max_length=120)]


class DeepAlphaPlan(Record):
    regime_label: Annotated[str, Field(min_length=1, max_length=120)]
    regime_view: Annotated[str, Field(min_length=1, max_length=1500)]
    focus: Annotated[tuple[DeepFocusItem, ...], Field(max_length=20)]


class ShadowCandidate(Record):
    instrument_id: ID
    symbol: ID
    probability_positive: Annotated[Decimal, Field(ge=0, le=1)]
    expected_gain_bps: Nonnegative
    expected_loss_bps: Nonnegative
    horizon_minutes: Annotated[StrictInt, Field(ge=1, le=390)]


class ShadowAudit(Record):
    action: Literal["OPEN", "ABSTAIN"]
    preferred: ShadowCandidate | None
    reasoning: Annotated[str, Field(min_length=1, max_length=1000)]

    @model_validator(mode="after")
    def active_payload(self):
        if (self.action == "OPEN") != (self.preferred is not None):
            raise ValueError("shadow action/payload mismatch")
        return self


class AlphaManagedPlan(Record):
    """Service-owned executable interpretation of one validated OPEN intent."""
    plan_id: ID
    instrument_id: ID
    symbol: ID
    episode_id: ID
    strategy_label: Annotated[str, Field(min_length=1, max_length=120)]
    thesis: Annotated[str, Field(min_length=1, max_length=1000)]
    quantity: Positive
    entry_limit_usd: Positive
    original_stop_usd: Positive
    active_stop_usd: Positive
    target_usd: Positive | None = None
    entry_expires_at: UTC
    next_review_at: UTC
    original_latest_exit_at: UTC
    latest_exit_at: UTC
    market_regime: Annotated[str, Field(min_length=1, max_length=120)] = "UNCLASSIFIED"
    initial_model_cost_usd: Nonnegative = Decimal("0")
    shadow_audit_id: ID | None = None
    version: Annotated[StrictInt, Field(ge=1)] = 1
    # A validated decision-model CLOSE is executed as LIMIT sells at the current bid (never below the
    # market); unfilled attempts are cancelled and repeated, then escalate to MARKET (gateway.planned_exit).
    exit_request: Annotated[str, Field(min_length=1, max_length=1000)] | None = None
    exit_attempts: Annotated[StrictInt, Field(ge=0)] = 0

    @model_validator(mode="after")
    def safe_geometry(self):
        # A validated trailing/tightening decision may lock profit by moving the
        # active stop above the original entry. It may never loosen below the
        # original stop; current-mark geometry is checked at amendment time.
        if self.active_stop_usd < self.original_stop_usd:
            raise ValueError("invalid managed stop geometry")
        if self.target_usd is not None and self.target_usd <= self.entry_limit_usd:
            raise ValueError("invalid managed target")
        if not (self.entry_expires_at < self.latest_exit_at <= self.original_latest_exit_at):
            raise ValueError("invalid managed deadlines")
        if self.next_review_at > self.latest_exit_at:
            raise ValueError("review after exit deadline")
        return self


class Instrument(Record):
    instrument_id: ID
    ticker: ID
    country: str
    asset_type: Literal["STOCK", "ETF", "OTHER"]
    listing_exchange: ID
    liquid: bool
    tradable: bool
    halted: bool = False
    quantity_increment: Positive
    price_increment_usd: Positive
    minimum_quantity: Positive


class CapabilityManifest(Record):
    version: ID
    environment: Literal["OFFLINE", "REAL"] = "OFFLINE"
    entry_order_types: tuple[Literal["LIMIT", "STOP_LIMIT"], ...]
    protection_mechanisms: tuple[str, ...]
    fractional_protection: bool = False
    certified_oco: Literal[False] = False


class Feature(Record):
    feature_id: ID
    source_id: ID
    value: Number | str | bool
    value_type: Literal["DECIMAL", "BOOLEAN", "ENUM"]
    allowed_values: tuple[str, ...] = ()
    usable_at: UTC

    @model_validator(mode="before")
    @classmethod
    def typed_value(cls, data):
        if isinstance(data, dict):
            data = dict(data)
            if data.get("value_type") == "DECIMAL":
                data["value"] = decimal(data.get("value"))
            elif data.get("value_type") == "BOOLEAN" and type(data.get("value")) is not bool:
                raise ValueError("BOOLEAN feature requires a boolean value")
            elif data.get("value_type") == "ENUM" and data.get("value") not in data.get("allowed_values", ()):
                raise ValueError("ENUM feature value must be registered")
        return data


class Evidence(Record):
    evidence_id: ID
    source_id: ID
    published_at: UTC
    received_at: UTC
    usable_at: UTC
    revision: ID
    text: str


class MarketEvent(Record):
    instrument_id: ID
    event_type: Literal["QUOTE", "TRADE", "HALT", "CORRECTION"]
    bid_usd: Nonnegative | None
    ask_usd: Nonnegative | None
    trade_price_usd: Nonnegative | None
    quantity: Nonnegative | None
    conditions: tuple[str, ...]
    source_event_at: UTC
    received_at: UTC
    entitled: bool
    delay_ms: Annotated[StrictInt, Field(ge=0)]
    quality_flags: tuple[str, ...]
    raw_reference: ID


class Position(Record):
    position_id: ID
    instrument_id: ID
    episode_id: ID
    plan_id: ID
    plan_version: ID
    quantity: Nonnegative
    average_entry_usd: Positive
    mark_usd: Nonnegative
    active_stop_usd: Positive
    first_fill_at: UTC
    latest_exit_at: UTC
    max_holding_seconds: Annotated[StrictInt, Field(gt=0)]
    protected_quantity: Nonnegative
    exit_cost_usd: Nonnegative = Decimal("0")
    stress_slippage_usd: Nonnegative = Decimal("0")

    @model_validator(mode="after")
    def protection_geometry(self):
        if self.protected_quantity > self.quantity or self.latest_exit_at <= self.first_fill_at:
            raise ValueError("invalid protection quantity or holding deadline")
        return self


class CashBalances(Record):
    settled_usd: Nonnegative
    reserved_usd: Nonnegative
    unsettled_usd: Nonnegative
    pending_deposits_usd: Nonnegative = Decimal("0")
    provisional_buying_power_usd: Nonnegative = Decimal("0")
    accrued_fees_usd: Nonnegative = Decimal("0")
    settlement_known: bool


class AccountSnapshot(Record):
    account_id: ID
    environment: Literal["OFFLINE"]
    account_type: Literal["CASH"]
    balances: CashBalances
    settlement_semantics: Literal["CONFIRMED_SETTLED_ONLY"]
    positions: tuple[Position, ...]
    outstanding_order_ids: tuple[ID, ...]
    observed_at: UTC
    reconciliation_version: ID
    broker_equity_usd: Number
    liquidation_equity_usd: Number
    economic_equity_usd: Number


class Opportunity(Record):
    opportunity_id: ID
    instrument_id: ID
    scanner_version: ID
    features: tuple[Feature, ...]
    applicability: tuple[str, ...]
    score: Number
    evidence_ids: tuple[ID, ...]
    expires_at: UTC
    suppression_history: tuple[str, ...]


class DecisionContext(Record):
    context_id: ID
    decision_id: ID
    opportunity_id: ID
    account: AccountSnapshot
    strategy_id: ID
    strategy_version: ID
    strategy_artifact_hash: ID
    prompt_version: ID
    policy_hash: ID
    as_of: UTC
    expires_at: UTC
    instrument: Instrument
    capabilities: CapabilityManifest
    session_label: date
    session_open: UTC
    session_close: UTC
    liquidation_at: UTC
    market: MarketEvent
    features: tuple[Feature, ...]
    evidence: tuple[Evidence, ...]
    warnings: tuple[str, ...]
    prior_decision_ids: tuple[ID, ...]
    memory_evidence_ids: tuple[ID, ...]
    allowed_actions: tuple[Action, ...]
    evaluation_horizon_seconds: Annotated[StrictInt, Field(gt=0)]
    expected_cost_usd: Nonnegative
    expected_latency_ms: Annotated[StrictInt, Field(ge=0)]
    review_reason: Text


class OrderIntent(Record):
    intent_id: ID
    client_order_id: ID
    account_id: ID
    environment: Literal["OFFLINE", "REAL"]
    instrument_id: ID
    side: Literal["BUY", "SELL_OWNED"]
    quantity: Positive
    order_type: Literal["LIMIT", "STOP_LIMIT", "STOP_MARKET", "MARKET"]
    session: Literal["CORE"]
    time_in_force: Literal["DAY"]
    limit_price_usd: Positive | None
    trigger_price_usd: Positive | None
    maximum_entry_usd: Positive | None
    protection: Protection | None
    plan_id: ID
    episode_id: ID
    deadline: UTC
    capability_version: ID

    @model_validator(mode="after")
    def geometry(self):
        if self.order_type=='MARKET' and (self.environment!='REAL' or self.side!='SELL_OWNED'
                or self.limit_price_usd is not None or self.trigger_price_usd is not None):
            raise ValueError('MARKET requires a REAL owned-share exit')
        if self.order_type == "LIMIT" and (self.limit_price_usd is None or self.trigger_price_usd is not None):
            raise ValueError("LIMIT requires limit and no trigger")
        if self.order_type == "STOP_LIMIT" and (self.limit_price_usd is None or self.trigger_price_usd is None):
            raise ValueError("STOP_LIMIT requires limit and trigger")
        if self.order_type == "STOP_MARKET" and (self.side == "BUY" or self.limit_price_usd is not None or self.trigger_price_usd is None):
            raise ValueError("STOP_MARKET is sell-only, trigger required")
        if self.side == "BUY":
            if self.maximum_entry_usd is None or self.protection is None or self.limit_price_usd > self.maximum_entry_usd:
                raise ValueError("buy requires price bound and protection")
            if not 0 < self.protection.initial_stop_usd < self.limit_price_usd:
                raise ValueError("invalid initial stop")
        return self


class RiskPermit(Record):
    intent_hash: ID
    policy_hash: ID
    reservation_ids: tuple[ID, ...]
    account_state_version: ID
    account_id: ID
    environment: Literal["OFFLINE", "REAL"]
    expires_at: UTC
    nonce: ID
    fencing_token: Annotated[StrictInt, Field(ge=1)]


class BrokerEvent(Record):
    broker_order_id: ID
    client_order_id: ID
    event_id: ID
    kind: Literal["ACK", "FILL", "CANCELED", "REJECTED", "EXPIRED", "UNKNOWN", "CORRECTION"]
    sequence: Annotated[StrictInt, Field(ge=0)]
    cumulative_filled_quantity: Nonnegative | None
    leaves_quantity: Nonnegative | None
    fill_price_usd: Positive | None
    fees_usd: Nonnegative
    received_at: UTC
    raw_reference: ID


class ResearchManifest(Record):
    candidate_id: ID
    parent_ids: tuple[ID, ...]
    version_lineage: tuple[ID, ...]
    trial_family: ID
    complete_trial_history_ids: tuple[ID, ...]
    artifact_hash: ID
    dataset_hash: ID
    window_hash: ID
    registered_endpoint: Text
    cost_scenario_ids: tuple[ID, ...]
    economic_hurdle_usd: Number
    qualification_policy_hash: ID
    registered_at: UTC
    review_at: UTC


class Diagnostic(Record):
    name: ID
    value: Number | None
    explanation: Text


class EvaluationResult(Record):
    manifest_hash: ID
    candidate_id: ID
    trial_family: ID
    complete_trial_history_ids: tuple[ID, ...]
    dataset_hash: ID
    window_hash: ID
    endpoint: ID
    gross_profit_usd: Number
    execution_cost_usd: Nonnegative
    operating_cost_usd: Nonnegative
    net_economic_profit_usd: Number
    diagnostics: tuple[Diagnostic, ...]
    lower_bound_usd: Number | None
    upper_bound_usd: Number | None
    qualification: Literal["INELIGIBLE", "INCONCLUSIVE", "QUALIFIED", "QUARANTINED"]
    reason: Text
