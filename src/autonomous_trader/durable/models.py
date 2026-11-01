"""Internal accounting facts. These do not claim any broker's wire semantics."""
from datetime import date
from typing import Annotated, Literal, Union
from pydantic import Field, StrictInt, TypeAdapter, model_validator
from ..contracts import Record, ID, UTC, Number, Positive, Nonnegative, Instrument, CapabilityManifest, OrderIntent
from ..reference import Session
from ..risk import OwnerRestart
from ..economic_policy import (AuthorizeSandboxEconomics, ReviseSandboxEconomics, ReleaseSandboxPreferences,
    AuthorizeDailyDownsideBudget, AuthorizeModelBudgetCap)


class OpeningStatement(Record):
    """Explicit flat opening checkpoint; retains earlier experiment references.

    Not a cash transfer or a reset. Only accepted as part of first CREATE.
    Outstanding receivable cannot become settled without ConfirmSettlement.
    """
    evidence_id: ID
    unsettled_usd: Nonnegative
    settlement_date: date
    experiment_reference_usd: Positive
    historic_high_water_usd: Positive
    day_start_equity_usd: Positive
    realized_gross_usd: Number
    fees_usd: Nonnegative
    episode_id: ID
    instrument_id: ID
    consumed_loss_usd: Nonnegative


class OpeningReceivable(Record):
    """Observed external cash receivable; never strategy revenue or a deposit."""
    evidence_id: ID
    amount_usd: Positive
    settlement_date: date


class CreateAccount(Record):
    kind: Literal["CREATE"] = "CREATE"
    account_id: ID
    experiment_id: ID
    owner_id: ID
    initial_settled_usd: Positive
    session: Session
    instruments: tuple[Instrument, ...]
    capabilities: CapabilityManifest
    settlement_days: tuple[date, ...]
    # Required explicit fixture settings, not implicit approved J4 defaults.
    mark_max_age_seconds: Annotated[StrictInt, Field(gt=0)]
    reconciliation_max_age_seconds: Annotated[StrictInt, Field(gt=0)]
    opening_statement: OpeningStatement | None = None
    opening_receivables: tuple[OpeningReceivable, ...] = ()
    # REAL ledgers: the owner-signed hard limits in force from the opening (journal-bound policy).
    risk_policy: dict[str, str] | None = None

    @model_validator(mode="after")
    def unique_instruments(self):
        if len({i.instrument_id for i in self.instruments}) != len(self.instruments):
            raise ValueError("duplicate instrument reference")
        if not self.settlement_days or tuple(sorted(set(self.settlement_days))) != self.settlement_days:
            raise ValueError("settlement-day coverage must be explicit, unique and ordered")
        if self.opening_receivables:
            if (self.capabilities.environment!='REAL' or self.opening_statement is not None
                    or len({r.evidence_id for r in self.opening_receivables})!=len(self.opening_receivables)
                    or any(r.settlement_date not in self.settlement_days for r in self.opening_receivables)):
                raise ValueError('invalid REAL opening receivables')
        if self.opening_statement:
            o=self.opening_statement
            equity=self.initial_settled_usd+o.unsettled_usd
            if (o.historic_high_water_usd < max(equity,o.experiment_reference_usd,o.day_start_equity_usd)
                    or o.consumed_loss_usd < max(0,-o.realized_gross_usd)+o.fees_usd
                    or o.instrument_id not in {i.instrument_id for i in self.instruments}
                    or o.settlement_date not in self.settlement_days):
                raise ValueError('invalid opening statement references')
        return self


class Mark(Record):
    kind: Literal["MARK"] = "MARK"
    instrument_id: ID
    bid_usd: Positive
    observed_at: UTC
    source_id: ID
    # REAL direct observation validity; original market timestamp stays observed_at.
    received_at: UTC | None = None
    valid_until: UTC | None = None


class RegisterInstruments(Record):
    kind: Literal["REGISTER_INSTRUMENTS"] = "REGISTER_INSTRUMENTS"
    instruments: Annotated[tuple[Instrument, ...], Field(min_length=1, max_length=100)]
    evidence_id: ID

    @model_validator(mode="after")
    def unique_instruments(self):
        if len({i.instrument_id for i in self.instruments}) != len(self.instruments):
            raise ValueError("duplicate instrument reference")
        return self


class Reconcile(Record):
    kind: Literal["RECONCILE"] = "RECONCILE"
    settled_usd: Nonnegative
    unsettled_usd: Nonnegative
    positions: tuple[tuple[ID, Nonnegative], ...]
    outstanding_intent_ids: tuple[ID, ...]
    evidence_id: ID
    settlement_known: bool
    resolved_incident_ids: tuple[ID, ...] = ()
    reconciliation_protocol: Literal["CANCEL_RECOVERY_V1"] | None = None


class Admit(Record):
    kind: Literal["ADMIT"] = "ADMIT"
    policy_hash: ID | None = None
    intent: OrderIntent
    fee_allowance_usd: Nonnegative
    exit_cost_usd: Nonnegative
    stress_allowance_usd: Nonnegative
    latest_exit_at: UTC
    # Up to the multi-day horizon (carry.MAX_HOLD_SESSIONS sessions, 60 = ~87 calendar days); same-day <= 23400.
    max_holding_seconds: Annotated[StrictInt, Field(gt=0, le=100*86400)]


class ConsumePermit(Record):
    kind: Literal["CONSUME"] = "CONSUME"
    nonce: ID


class RefreshPermit(Record):
    kind: Literal["REFRESH_PERMIT"] = "REFRESH_PERMIT"
    intent_id: ID


class OrderObservation(Record):
    kind: Literal["ORDER_OBSERVATION"] = "ORDER_OBSERVATION"
    intent_id: ID
    status: Literal["ACKNOWLEDGED", "PARTIALLY_FILLED", "UNKNOWN", "CANCEL_PENDING", "REPLACE_PENDING"]
    evidence_id: ID


class Execution(Record):
    kind: Literal["EXECUTION"] = "EXECUTION"
    execution_id: ID
    intent_id: ID
    quantity: Positive
    price_usd: Positive
    fee_usd: Nonnegative
    executed_at: UTC
    settlement_date: date | None
    evidence_id: ID


class CorrectedExecution(Record):
    execution_id: ID
    expected_revision: Annotated[StrictInt, Field(ge=0)]
    quantity: Nonnegative  # zero is an explicit bust
    price_usd: Positive
    fee_usd: Nonnegative


class FinalizeOrder(Record):
    kind: Literal["FINALIZE"] = "FINALIZE"
    intent_id: ID
    status: Literal["FILLED", "CANCELED", "REJECTED", "EXPIRED"]
    execution_ids: tuple[ID, ...]
    total_quantity: Nonnegative
    total_fees_usd: Nonnegative
    evidence_id: ID


class CorrectExecutions(Record):
    kind: Literal["CORRECT_EXECUTIONS"] = "CORRECT_EXECUTIONS"
    changes: Annotated[tuple[CorrectedExecution, ...], Field(min_length=1)]
    evidence_id: ID
    resolves_correction_ids: tuple[ID, ...] = ()
    terminal_details: tuple[FinalizeOrder, ...] = ()


class ConfirmSettlement(Record):
    kind: Literal["SETTLE"] = "SETTLE"
    execution_id: ID
    amount_usd: Nonnegative
    effective_on: date
    evidence_id: ID


class CashAdoption(Record):
    """Broker-truth (owner decision 2026-10-18, "use only broker's truth as the true balance"): the broker's
    settled and unsettled cash replace the ledger's whenever they differ. The difference is journaled (never
    hidden), is not trading profit or loss, and requires a quiet account (no open/unknown orders or
    reservations). Positions are never adopted this way."""
    kind: Literal["CASH_ADOPT"] = "CASH_ADOPT"
    settled_delta_usd: Number
    unsettled_delta_usd: Number
    broker_settled_usd: Nonnegative
    broker_unsettled_usd: Nonnegative
    evidence_id: ID

    @model_validator(mode="after")
    def nonzero(self):
        if (not self.settled_delta_usd.is_finite() or not self.unsettled_delta_usd.is_finite()
                or (self.settled_delta_usd == 0 and self.unsettled_delta_usd == 0)):
            raise ValueError("cash adoption must change a bucket by a finite amount")
        return self


class Protection(Record):
    kind: Literal["PROTECTION"] = "PROTECTION"
    instrument_id: ID
    quantity: Nonnegative
    stop_usd: Positive
    evidence_id: ID


class NewSession(Record):
    kind: Literal["NEW_SESSION"] = "NEW_SESSION"
    session: Session
    # Carried positions: first marks of the new session, applied before the session's starting equity is
    # fixed, so an overnight gap is measured as overnight risk (drawdown), not as an intraday daily loss.
    opening_marks: tuple[Mark, ...] = ()


class RealConfiguration(Record):
    """Charter-bound REAL account configuration (owner decisions 2026-10-15).

    Sent only after the owner-signed charter was verified at startup. It records the charter's risk
    policy hash, the authorized multi-day holding horizon (sessions after the current one; 0 means
    same-day only) and extends the settlement calendar, never removing a known future day."""
    kind: Literal["REAL_CONFIGURATION"] = "REAL_CONFIGURATION"
    charter_id: ID
    policy_hash: ID
    carry_sessions: Annotated[StrictInt, Field(ge=0, le=60)]
    settlement_days: tuple[date, ...]
    evidence_id: ID
    # Required when policy_hash changes: the full revised limits, applied from this event onward.
    risk_policy: dict[str, str] | None = None

    @model_validator(mode="after")
    def ordered(self):
        if not self.settlement_days or tuple(sorted(set(self.settlement_days))) != self.settlement_days:
            raise ValueError("settlement days must be unique and ordered")
        return self


class Restart(Record):
    kind: Literal["RESTART"] = "RESTART"
    action: OwnerRestart


class Incident(Record):
    kind: Literal["INCIDENT"] = "INCIDENT"
    reason: ID
    evidence_id: ID


Command = Annotated[Union[Mark, RegisterInstruments, Reconcile, Admit, ConsumePermit, RefreshPermit,
    OrderObservation, Execution, CorrectExecutions, FinalizeOrder, ConfirmSettlement, CashAdoption,
    Protection, NewSession, RealConfiguration, Restart, Incident, AuthorizeSandboxEconomics, ReviseSandboxEconomics,
    ReleaseSandboxPreferences, AuthorizeDailyDownsideBudget, AuthorizeModelBudgetCap], Field(discriminator="kind")]
COMMAND = TypeAdapter(Command)
