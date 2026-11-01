"""Pure M1 invariant predicates and independent loss latches (not M2 services)."""
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal, Protocol
from hashlib import sha256
import hmac
import json
from pydantic import model_validator
from .contracts import Record, ID, UTC, Number, Nonnegative, Positive, Position, utc
from .numeric import ZERO, add, mul


class Reason(StrEnum):
    INSUFFICIENT_SETTLED_CASH = "INSUFFICIENT_SETTLED_CASH"
    UNKNOWN_SETTLEMENT = "UNKNOWN_SETTLEMENT"
    STALE_MARKET = "STALE_MARKET"
    STALE_ACCOUNT = "STALE_ACCOUNT"
    PLAN_EXPIRED = "PLAN_EXPIRED"
    UNSUPPORTED_CAPABILITY = "UNSUPPORTED_CAPABILITY"
    POSITION_LIMIT = "POSITION_LIMIT"
    EXPOSURE_LIMIT = "EXPOSURE_LIMIT"
    EPISODE_RISK = "EPISODE_RISK"
    OPEN_RISK = "OPEN_RISK"
    DAILY_LOSS = "DAILY_LOSS"
    DRAWDOWN_HALT = "DRAWDOWN_HALT"
    TERMINATED = "TERMINATED"
    UNKNOWN_ORDER_STATE = "UNKNOWN_ORDER_STATE"
    UNAUTHORIZED_ENVIRONMENT = "UNAUTHORIZED_ENVIRONMENT"


class HardPolicy(Record):
    environment: Literal["OFFLINE"] = "OFFLINE"
    account_exposure: Nonnegative = Decimal("1")
    stock_exposure: Nonnegative = Decimal("0.40")
    maximum_positions: Literal[5] = 5
    # Owner decision 2026-10-16 ("10% for now; tighten as capital grows"): planned loss to the hard stop
    # per position (entry plus adds) up to 10% of equity; all positions together up to 20%.
    trade_risk: Nonnegative = Decimal("0.10")
    open_risk: Nonnegative = Decimal("0.20")
    daily_loss: Nonnegative = Decimal("0.05")
    drawdown: Nonnegative = Decimal("0.20")
    experiment_death: Nonnegative = Decimal("0.90")
    margin: Literal[False] = False
    borrowing: Literal[False] = False
    money_movement: Literal[False] = False
    direction: Literal["LONG_ONLY"] = "LONG_ONLY"
    universe: Literal["US_LISTED_LIQUID_STOCKS"] = "US_LISTED_LIQUID_STOCKS"
    session: Literal["CORE"] = "CORE"
    drawdown_restart: Literal["AUTHENTICATED_OWNER_NEW_SEGMENT"] = "AUTHENTICATED_OWNER_NEW_SEGMENT"

    @model_validator(mode="after")
    def fixed_limits(self):
        expected = {"account_exposure": "1", "stock_exposure": "0.40", "trade_risk": "0.10",
                    "open_risk": "0.20", "daily_loss": "0.05", "drawdown": "0.20", "experiment_death": "0.90"}
        if any(getattr(self, name) != Decimal(value) for name, value in expected.items()):
            raise ValueError("hard policy limits are fixed by the owner mandate")
        return self


POLICY = HardPolicy()


def planned_loss(quantity: Decimal, entry: Decimal, stop: Decimal, costs: Decimal = ZERO) -> Decimal:
    if not 0 < stop < entry or quantity < 0 or costs < 0:
        raise ValueError("invalid initial risk geometry")
    return add(mul(quantity, add(entry, -stop)), costs)


def position_risk(p: Position) -> Decimal:
    # A profitable trailing stop still permits giveback from current equity.
    reference = max(ZERO, add(p.average_entry_usd, -p.active_stop_usd), add(p.mark_usd, -p.active_stop_usd))
    return add(mul(p.quantity, reference), p.exit_cost_usd, p.stress_slippage_usd)


class Mode(StrEnum):
    BOOTSTRAP = "BOOTSTRAP"
    RECONCILING = "RECONCILING"
    READY = "READY"
    ENTRY_BLOCKED = "ENTRY_BLOCKED"
    REDUCE_ONLY = "REDUCE_ONLY"
    DRAWDOWN_HALTED = "DRAWDOWN_HALTED"
    CLOSED = "CLOSED"
    TERMINATED = "TERMINATED"


class EquityObservation(Record):
    at: UTC
    session_label: date
    equity_usd: Number
    realized_net_pnl_usd: Number


class OperatingSegment(Record):
    segment_id: ID
    reference_equity_usd: Positive
    high_water_usd: Positive
    started_at: UTC
    restart_action_id: ID | None


class OwnerRestart(Record):
    action_id: ID
    owner_id: ID
    experiment_id: ID
    segment_id: ID
    expected_equity_usd: Positive
    issued_at: UTC
    expires_at: UTC
    proof: ID


class OwnerAuthenticator(Protocol):
    def verify(self, action: OwnerRestart) -> str | None:
        """Return an authenticated owner principal, or None. Deny by default."""
        ...


class DenyAllOwners:
    def verify(self, action: OwnerRestart) -> None:
        return None


class HMACOwnerAuthenticator:
    """Offline authentication fixture; no credentials are loaded or generated.

    A trusted test caller supplies a local key and owner identity. This is not
    a deployed owner login/signing service or a broker/API credential facility.
    """
    def __init__(self, owner_id: str, key: bytes):
        if not isinstance(key, bytes) or len(key) < 32:
            raise ValueError("offline authentication key requires at least 32 bytes")
        self._owner_id = owner_id
        self._key = key

    def verify(self, action: OwnerRestart) -> str | None:
        body = json.dumps(action.model_dump(mode="json", exclude={"proof"}), sort_keys=True, separators=(",", ":")).encode()
        expected = hmac.new(self._key, body, sha256).hexdigest()
        if action.owner_id == self._owner_id and hmac.compare_digest(expected, action.proof):
            return self._owner_id
        return None


class SafetyState(Record):
    experiment_id: ID
    owner_id: ID
    initial_equity_usd: Positive
    historic_high_water_usd: Positive
    current_equity_usd: Number
    day_start_equity_usd: Positive
    session_label: date
    segments: tuple[OperatingSegment, ...]
    observations: tuple[EquityObservation, ...] = ()
    used_restart_ids: tuple[ID, ...] = ()
    daily_halted: bool = False
    drawdown_halted: bool = False
    terminated: bool = False
    incident_halted: bool = False
    reconciling: bool = True
    entry_blocked: bool = False
    scheduled_liquidation: bool = False
    session_closed: bool = True

    @model_validator(mode="after")
    def history_geometry(self):
        if not self.segments or self.historic_high_water_usd < max(self.initial_equity_usd, self.current_equity_usd):
            raise ValueError("invalid high-water history or missing segment")
        if len({s.segment_id for s in self.segments}) != len(self.segments):
            raise ValueError("duplicate segment identity")
        if any(s.high_water_usd < s.reference_equity_usd for s in self.segments):
            raise ValueError("segment high-water below reference")
        return self

    @property
    def mode(self) -> Mode:
        if self.terminated:
            return Mode.TERMINATED
        if self.drawdown_halted:
            return Mode.DRAWDOWN_HALTED
        if self.session_closed:
            return Mode.CLOSED
        if self.incident_halted or self.daily_halted or self.scheduled_liquidation:
            return Mode.REDUCE_ONLY
        if self.reconciling:
            return Mode.RECONCILING
        if self.entry_blocked:
            return Mode.ENTRY_BLOCKED
        return Mode.READY

    def evolve(self, **updates):
        return type(self).model_validate({**self.model_dump(), **updates})

    def observe(self, observation: EquityObservation):
        if observation.session_label != self.session_label:
            raise ValueError("explicit new_session required")
        if self.observations and observation.at <= self.observations[-1].at:
            raise ValueError("equity observations must be strictly chronological")
        equity = observation.equity_usd
        historic = max(self.historic_high_water_usd, equity)
        last = self.segments[-1]
        high = max(last.high_water_usd, equity)
        segment = OperatingSegment(**{**last.model_dump(), "high_water_usd": high})
        loss = mul(self.day_start_equity_usd, POLICY.daily_loss)
        return self.evolve(current_equity_usd=equity, historic_high_water_usd=historic,
                           segments=(*self.segments[:-1], segment), observations=(*self.observations, observation),
                           daily_halted=self.daily_halted or observation.realized_net_pnl_usd <= -loss or add(equity, -self.day_start_equity_usd) <= -loss,
                           drawdown_halted=self.drawdown_halted or equity <= mul(high, Decimal("0.8")),
                           terminated=self.terminated or equity <= mul(historic, Decimal("0.1")))

    def new_session(self, label: date, *, reconciled: bool, ready: bool):
        if label <= self.session_label or not reconciled or not ready or self.current_equity_usd <= 0:
            raise ValueError("new session requires later date and clean readiness")
        return self.evolve(session_label=label, day_start_equity_usd=self.current_equity_usd,
                           daily_halted=False, scheduled_liquidation=False)

    def restart(self, action: OwnerRestart, now: datetime, authenticator: OwnerAuthenticator | None = None):
        now = utc(now)
        auth = authenticator or DenyAllOwners()
        if auth.verify(action) != self.owner_id or action.owner_id != self.owner_id:
            raise PermissionError("authenticated owner action required")
        if self.terminated or not self.drawdown_halted:
            raise ValueError("restart requires drawdown halt and nonterminated experiment")
        if (action.experiment_id != self.experiment_id or action.segment_id != self.segments[-1].segment_id
                or action.expected_equity_usd != self.current_equity_usd or action.action_id in self.used_restart_ids
                or action.action_id in {s.segment_id for s in self.segments}
                or not action.issued_at <= now < action.expires_at):
            raise ValueError("stale, replayed, expired, or mismatched restart action")
        segment = OperatingSegment(segment_id=action.action_id, reference_equity_usd=self.current_equity_usd,
                                   high_water_usd=self.current_equity_usd, started_at=now, restart_action_id=action.action_id)
        return self.evolve(segments=(*self.segments, segment), drawdown_halted=False,
                           used_restart_ids=(*self.used_restart_ids, action.action_id))
