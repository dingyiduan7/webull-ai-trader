"""Small in-memory reference model for M1 tests; not a production ledger.

Atomic admission is modeled by a single process lock. No database, allocator,
execution gateway, funding operation, or persistent authority is implemented.
"""
from datetime import date, datetime, timedelta
from decimal import Decimal, localcontext
from threading import RLock
from .contracts import Record, ID, Nonnegative, Positive, Position, OrderIntent, BrokerEvent, utc
from .execution import Order, OrderState, TERMINAL
from .numeric import ZERO, add, mul
from .risk import POLICY, Reason, planned_loss, position_risk


class SettlementLot(Record):
    lot_id: ID
    amount_usd: Nonnegative
    trade_date: date
    settlement_date: date
    confirmed: bool = False


class Episode(Record):
    episode_id: ID
    instrument_id: ID
    admitted_allowance_usd: Positive
    consumed_loss_usd: Nonnegative = ZERO


class Reservation(Record):
    reservation_id: ID
    intent_id: ID
    instrument_id: ID
    episode_id: ID
    cash_usd: Nonnegative
    shares: Nonnegative
    exposure_usd: Nonnegative
    risk_usd: Nonnegative
    fee_allowance_usd: Nonnegative
    future_cost_usd: Nonnegative


class Violation(Record):
    code: Reason
    detail: str


class PortfolioView(Record):
    settled_usd: Nonnegative
    accrued_fees_usd: Nonnegative
    settlement_known: bool
    equity_usd: Positive
    start_of_day_equity_usd: Positive
    positions: tuple[Position, ...]
    reservations: tuple[Reservation, ...]
    episodes: tuple[Episode, ...]
    new_risk_enabled: bool


def invariant_violations(view: PortfolioView) -> tuple[Violation, ...]:
    """Stable reason ordering makes failed runs and replay comparisons deterministic."""
    issues = []
    def check(ok, code, detail):
        if not ok:
            issues.append(Violation(code=code, detail=detail))
    check(view.new_risk_enabled, Reason.STALE_ACCOUNT, "new risk is disabled")
    check(view.settlement_known, Reason.UNKNOWN_SETTLEMENT, "settled eligibility unknown")
    check(all(p.protected_quantity >= p.quantity for p in view.positions), Reason.UNSUPPORTED_CAPABILITY,
          "known entry fills require acknowledged protection before further admission")
    episode_map = {e.episode_id: e.instrument_id for e in view.episodes}
    check(len(episode_map) == len(view.episodes)
          and all(episode_map.get(p.episode_id) == p.instrument_id for p in view.positions)
          and all(episode_map.get(r.episode_id) == r.instrument_id for r in view.reservations),
          Reason.EPISODE_RISK, "missing, duplicated, or mismatched episode identity")
    cash = add(*(r.cash_usd for r in view.reservations), view.accrued_fees_usd)
    check(cash <= view.settled_usd, Reason.INSUFFICIENT_SETTLED_CASH, "cash obligations exceed settled funds")
    instruments = {p.instrument_id for p in view.positions if p.quantity > 0}
    instruments |= {r.instrument_id for r in view.reservations if r.exposure_usd > 0 or r.shares > 0}
    check(len(instruments) <= POLICY.maximum_positions, Reason.POSITION_LIMIT, "positions plus entry commitments exceed five")
    exposure = {}
    for p in view.positions:
        exposure[p.instrument_id] = add(exposure.get(p.instrument_id, ZERO), mul(p.quantity, p.mark_usd))
    for r in view.reservations:
        exposure[r.instrument_id] = add(exposure.get(r.instrument_id, ZERO), r.exposure_usd)
    check(add(*exposure.values()) <= mul(view.equity_usd, POLICY.account_exposure), Reason.EXPOSURE_LIMIT, "account exposure exceeds 100%")
    check(all(x <= mul(view.equity_usd, POLICY.stock_exposure) for x in exposure.values()), Reason.EXPOSURE_LIMIT, "aggregate stock exposure exceeds the per-stock limit")
    basis = min(view.equity_usd, view.start_of_day_equity_usd)
    open_risk = add(*(position_risk(p) for p in view.positions), *(r.risk_usd for r in view.reservations))
    check(open_risk <= mul(basis, POLICY.open_risk), Reason.OPEN_RISK, "aggregate open risk exceeds 5%")
    for episode in view.episodes:
        remaining = add(*(mul(p.quantity, max(ZERO, add(p.average_entry_usd, -p.active_stop_usd))) for p in view.positions if p.episode_id == episode.episode_id),
                        *(add(p.exit_cost_usd, p.stress_slippage_usd) for p in view.positions if p.episode_id == episode.episode_id),
                        *(r.risk_usd for r in view.reservations if r.episode_id == episode.episode_id))
        check(add(episode.consumed_loss_usd, remaining) <= min(episode.admitted_allowance_usd, mul(basis, POLICY.trade_risk)),
              Reason.EPISODE_RISK, "episode consumed plus remaining risk exceeds allowance: " + episode.episode_id)
    for instrument in instruments:
        owned = add(*(p.quantity for p in view.positions if p.instrument_id == instrument))
        reserved = add(*(r.shares for r in view.reservations if r.instrument_id == instrument))
        check(reserved <= owned, Reason.UNKNOWN_ORDER_STATE, "sell reservations exceed owned shares")
    return tuple(issues)


class AdmissionError(ValueError):
    def __init__(self, violations):
        self.violations = violations
        super().__init__(", ".join(v.code.value for v in violations))


class OfflineAccount:
    def __init__(self, *, account_id: str, settled_usd: Decimal, equity_usd: Decimal,
                 start_of_day_equity_usd: Decimal, calendar):
        from .numeric import nonnegative, positive
        self.account_id = account_id
        self.settled_usd = nonnegative(settled_usd)
        self.equity_usd = positive(equity_usd)
        self.start_of_day_equity_usd = positive(start_of_day_equity_usd)
        self.calendar = calendar
        self.settlement_known = True
        self.new_risk_enabled = True
        self.accrued_fees_usd = ZERO
        self.pending_deposits_usd = ZERO
        self.provisional_buying_power_usd = ZERO
        self.positions: dict[str, Position] = {}
        self.episodes: dict[str, Episode] = {}
        self.reservations: dict[str, Reservation] = {}
        self.orders: dict[str, Order] = {}
        self.settlement_lots: dict[str, SettlementLot] = {}
        self.incidents: list[str] = []
        self.journal: list[BrokerEvent] = []
        self.version = 0
        self._lock = RLock()

    @property
    def available_settled_usd(self):
        return add(self.settled_usd, -self.accrued_fees_usd, *(-r.cash_usd for r in self.reservations.values()))

    def view(self, extra: Reservation | None = None, episodes=None):
        return PortfolioView(settled_usd=self.settled_usd, accrued_fees_usd=self.accrued_fees_usd,
                             settlement_known=self.settlement_known, equity_usd=self.equity_usd,
                             start_of_day_equity_usd=self.start_of_day_equity_usd,
                             positions=tuple(self.positions.values()),
                             reservations=(*self.reservations.values(), *((extra,) if extra else ())),
                             episodes=tuple((episodes if episodes is not None else self.episodes).values()),
                             new_risk_enabled=self.new_risk_enabled)

    def reserve(self, intent: OrderIntent, *, expected_version: int, fee_allowance_usd=ZERO,
                future_cost_usd=ZERO):
        from .numeric import nonnegative
        nonnegative(fee_allowance_usd)
        nonnegative(future_cost_usd)
        with self._lock:
            if intent.account_id != self.account_id:
                raise ValueError("account binding mismatch")
            if expected_version != self.version:
                raise ValueError("STALE_ACCOUNT: re-evaluate state version")
            if intent.intent_id in self.orders or any(o.intent.client_order_id == intent.client_order_id for o in self.orders.values()):
                raise ValueError("duplicate intent or client ID")
            episodes = dict(self.episodes)
            if intent.side == "BUY":
                existing = [e for e in episodes.values() if e.instrument_id == intent.instrument_id]
                if any(e.episode_id != intent.episode_id for e in existing):
                    raise ValueError("episode fragmentation forbidden in offline experiment")
                if intent.episode_id in episodes and episodes[intent.episode_id].instrument_id != intent.instrument_id:
                    raise ValueError("episode instrument mismatch")
                episodes.setdefault(intent.episode_id, Episode(episode_id=intent.episode_id, instrument_id=intent.instrument_id,
                                    admitted_allowance_usd=mul(min(self.equity_usd, self.start_of_day_equity_usd), POLICY.trade_risk)))
                exposure = mul(intent.quantity, intent.maximum_entry_usd)
                reservation = Reservation(reservation_id=intent.intent_id, intent_id=intent.intent_id,
                    instrument_id=intent.instrument_id, episode_id=intent.episode_id, shares=ZERO,
                    cash_usd=add(exposure, fee_allowance_usd), exposure_usd=exposure,
                    risk_usd=planned_loss(intent.quantity, intent.maximum_entry_usd, intent.protection.initial_stop_usd, add(future_cost_usd, fee_allowance_usd)),
                    fee_allowance_usd=fee_allowance_usd, future_cost_usd=future_cost_usd)
                issues = invariant_violations(self.view(reservation, episodes))
                if issues:
                    raise AdmissionError(issues)
            else:
                owned = add(*(p.quantity for p in self.positions.values() if p.instrument_id == intent.instrument_id and p.episode_id == intent.episode_id))
                reserved = add(*(r.shares for r in self.reservations.values() if r.instrument_id == intent.instrument_id))
                if intent.quantity > add(owned, -reserved):
                    raise ValueError("insufficient unreserved owned shares")
                reservation = Reservation(reservation_id=intent.intent_id, intent_id=intent.intent_id,
                    instrument_id=intent.instrument_id, episode_id=intent.episode_id, shares=intent.quantity,
                    cash_usd=ZERO, exposure_usd=ZERO, risk_usd=ZERO,
                    fee_allowance_usd=fee_allowance_usd, future_cost_usd=ZERO)
            self.episodes = episodes
            self.reservations[intent.intent_id] = reservation
            self.orders[intent.intent_id] = Order(intent=intent).transition(OrderState.RESERVED)
            self.version += 1
            return reservation

    def transition(self, intent_id: str, state: OrderState):
        with self._lock:
            self.orders[intent_id] = self.orders[intent_id].transition(state)
            if state == OrderState.UNKNOWN:
                self._incident("UNKNOWN_ORDER_STATE: reconcile before further admission")
            self.version += 1

    def _incident(self, reason: str):
        self.new_risk_enabled = False
        self.incidents.append(reason)

    def apply(self, intent_id: str, event: BrokerEvent):
        """Rollback a failed projection; preserve the observation and obligations."""
        with self._lock:
            fields = ("positions", "episodes", "reservations", "orders", "settlement_lots", "journal", "incidents")
            checkpoint = {name: getattr(self, name).copy() for name in fields}
            checkpoint.update(settled_usd=self.settled_usd, version=self.version, new_risk_enabled=self.new_risk_enabled)
            try:
                self._apply_event(intent_id, event)
            except Exception:
                for name, value in checkpoint.items():
                    setattr(self, name, value)
                self._incident("event projection failed: retain obligations and reconcile observed event")
                if intent_id in self.orders:
                    self.orders[intent_id] = self.orders[intent_id].evolve(state=OrderState.UNKNOWN, final_reconciled=False)
                if event not in self.journal:
                    self.journal.append(event)
                self.version += 1
                raise

    def _apply_event(self, intent_id: str, event: BrokerEvent):
        """Cumulative quantities and cumulative fees, explicitly simulated semantics."""
        from .reference import NEW_YORK
        with self._lock:
            collision = next((x for x in self.journal if x.event_id == event.event_id and x != event), None)
            if collision:
                raise ValueError("global event identity collision")
            old = self.orders[intent_id]
            new = old.apply(event)
            if new == old:
                return
            if new.state == OrderState.UNKNOWN:
                self.orders[intent_id] = new
                self.journal.append(event)
                self._incident("UNKNOWN_ORDER_STATE: explicit reconciliation required")
                self.version += 1
                return
            q = add(new.cumulative_filled, -old.cumulative_filled)
            fees = add(new.cumulative_fees_usd, -old.cumulative_fees_usd)
            intent = old.intent
            reservation = self.reservations[intent_id]
            if q > 0 and event.fill_price_usd is None:
                raise ValueError("incremental fill requires price")
            if q > 0 and intent.limit_price_usd is not None:
                if ((intent.side == "BUY" and event.fill_price_usd > intent.limit_price_usd)
                        or (intent.side == "SELL_OWNED" and event.fill_price_usd < intent.limit_price_usd)):
                    self._incident("fill violates simulated price bound")
                    raise ValueError("fill violates limit")
            trade_date = event.received_at.astimezone(NEW_YORK).date()
            due = self.calendar.next_settlement(trade_date) if q > 0 and intent.side == "SELL_OWNED" else None
            if q > 0 or fees > 0:
                episode = self.episodes[intent.episode_id]
                consumed = add(episode.consumed_loss_usd, fees)
                if intent.side == "BUY":
                    debit = add(mul(q, event.fill_price_usd) if q else ZERO, fees)
                    if debit > self.settled_usd:
                        self._incident("fill/fee exceeds cash; reconciliation required")
                        raise ValueError("negative cash prevented; external discrepancy unresolved")
                    p = self.positions.get(intent.episode_id)
                    if q > 0:
                        quantity = add(p.quantity if p else ZERO, q)
                        # Weighted basis is rounded only in this test projection; retain exact
                        # fill events for lot-level M2 accounting (documented in OPEN_DECISIONS).
                        with localcontext() as ctx:
                            ctx.prec = 80
                            from decimal import ROUND_CEILING
                            basis = (add(mul(p.quantity, p.average_entry_usd) if p else ZERO, mul(q, event.fill_price_usd)) / quantity).quantize(Decimal("0.00000001"), rounding=ROUND_CEILING)
                        self.positions[intent.episode_id] = Position(position_id=intent.episode_id, instrument_id=intent.instrument_id,
                            episode_id=intent.episode_id, plan_id=intent.plan_id, plan_version="1", quantity=quantity,
                            average_entry_usd=basis, mark_usd=event.fill_price_usd,
                            active_stop_usd=intent.protection.initial_stop_usd,
                            first_fill_at=p.first_fill_at if p else event.received_at,
                            latest_exit_at=self.calendar.session(trade_date).liquidation_at,
                            max_holding_seconds=23400, protected_quantity=p.protected_quantity if p else ZERO,
                            exit_cost_usd=reservation.future_cost_usd)
                    self.settled_usd = add(self.settled_usd, -debit)
                else:
                    p = self.positions[intent.episode_id]
                    if q > p.quantity:
                        raise ValueError("short position prevented")
                    proceeds = mul(q, event.fill_price_usd) if q else ZERO
                    # Fees debit settled cash; proceeds remain gross unsettled, charged once.
                    if fees > self.settled_usd:
                        self._incident("sell fee exceeds settled cash")
                        raise ValueError("negative fee balance")
                    self.settled_usd = add(self.settled_usd, -fees)
                    remaining = add(p.quantity, -q)
                    if q:
                        consumed = add(consumed, max(ZERO, mul(q, add(p.average_entry_usd, -event.fill_price_usd))))
                        self.settlement_lots[event.event_id] = SettlementLot(lot_id=event.event_id,
                            amount_usd=proceeds, trade_date=trade_date, settlement_date=due)
                    self.positions[intent.episode_id] = Position(**{**p.model_dump(), "quantity": remaining,
                        "protected_quantity": min(p.protected_quantity, remaining)})
                self.episodes[intent.episode_id] = Episode(**{**episode.model_dump(), "consumed_loss_usd": consumed})
            leaves = add(intent.quantity, -new.cumulative_filled)
            fee_remaining = max(ZERO, add(reservation.fee_allowance_usd, -new.cumulative_fees_usd))
            if intent.side == "BUY":
                exposure = mul(leaves, intent.maximum_entry_usd)
                updates = dict(cash_usd=add(exposure, fee_remaining), exposure_usd=exposure,
                               risk_usd=planned_loss(leaves, intent.maximum_entry_usd, intent.protection.initial_stop_usd,
                                                     add(fee_remaining, reservation.future_cost_usd if not new.cumulative_filled else ZERO)))
            else:
                updates = dict(shares=leaves)
            self.reservations[intent_id] = Reservation(**{**reservation.model_dump(), **updates})
            self.orders[intent_id] = new
            self.journal.append(event)
            if q > 0:
                session = self.calendar.session(trade_date)
                if not session or not session.permits_entry(event.received_at):
                    self._incident("late fill: reconcile residual; regular-session cleanup only")
            self.version += 1

    def confirm_terminal(self, intent_id: str, *, filled: Decimal, fees: Decimal):
        with self._lock:
            self.orders[intent_id] = self.orders[intent_id].reconcile_terminal(cumulative_filled=filled, cumulative_fees_usd=fees)
            self.reservations.pop(intent_id, None)
            self.version += 1

    def confirm_settlement(self, lot_id: str, on: date):
        with self._lock:
            lot = self.settlement_lots[lot_id]
            if on < lot.settlement_date or not self.calendar.settlement_day(on):
                raise ValueError("settlement not due on a settlement business day")
            if not lot.confirmed:
                self.settled_usd = add(self.settled_usd, lot.amount_usd)
                self.settlement_lots[lot_id] = SettlementLot(**{**lot.model_dump(), "confirmed": True})
                self.version += 1

    def acknowledge_protection(self, episode_id: str, quantity: Decimal, *, certified: bool):
        with self._lock:
            p = self.positions[episode_id]
            if not certified or quantity != p.quantity:
                self._incident("PROTECTION_FAILURE: cancel entry remainder, then serialize owned-share exit")
                for key, order in self.orders.items():
                    if order.intent.episode_id == episode_id and order.intent.side == "BUY" and order.state not in TERMINAL:
                        self.orders[key] = order.transition(OrderState.CANCEL_PENDING)
                return
            self.positions[episode_id] = Position(**{**p.model_dump(), "protected_quantity": quantity})
            self.version += 1
