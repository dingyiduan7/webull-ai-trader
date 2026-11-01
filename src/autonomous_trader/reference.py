"""Injected, bounded calendar fixtures. No guessed holiday schedule or network."""
from datetime import date, datetime, time, timedelta, timezone
from typing import Protocol
from zoneinfo import ZoneInfo
from pydantic import model_validator
from .contracts import Record, UTC

NEW_YORK = ZoneInfo("America/New_York")


class CalendarUnknown(ValueError):
    pass


class Session(Record):
    label: date
    opens_at: UTC
    closes_at: UTC

    @model_validator(mode="after")
    def geometry(self):
        if (self.closes_at - self.opens_at <= timedelta(minutes=5)
                or self.opens_at.astimezone(NEW_YORK).date() != self.label
                or self.closes_at.astimezone(NEW_YORK).date() != self.label):
            raise ValueError("invalid session interval or local label")
        return self

    @property
    def liquidation_at(self) -> datetime:
        return self.closes_at - timedelta(minutes=5)

    def contains(self, now: datetime) -> bool:
        from .contracts import utc
        return self.opens_at <= utc(now) < self.closes_at

    def permits_entry(self, now: datetime) -> bool:
        return self.contains(now) and now < self.liquidation_at


class SessionCalendar(Protocol):
    def session(self, label: date) -> Session | None: ...


class FixtureCalendar:
    def __init__(self, start: date, end: date, *, exchange_holidays=frozenset(),
                 settlement_holidays=frozenset(), early_closes=None):
        if start > end:
            raise ValueError("invalid calendar coverage")
        self.start, self.end = start, end
        self.exchange_holidays = frozenset(exchange_holidays)
        self.settlement_holidays = frozenset(settlement_holidays)
        self.early_closes = dict(early_closes or {})

    def _known(self, label: date):
        if not self.start <= label <= self.end:
            raise CalendarUnknown("date outside supplied calendar coverage")

    def session(self, label: date) -> Session | None:
        self._known(label)
        if label.weekday() >= 5 or label in self.exchange_holidays:
            return None
        return Session(label=label,
                       opens_at=datetime.combine(label, time(9, 30), NEW_YORK),
                       closes_at=datetime.combine(label, self.early_closes.get(label, time(16)), NEW_YORK))

    def settlement_day(self, label: date) -> bool:
        self._known(label)
        return label.weekday() < 5 and label not in self.settlement_holidays

    def next_settlement(self, trade_date: date) -> date:
        self._known(trade_date)
        day = trade_date + timedelta(days=1)
        while not self.settlement_day(day):
            day += timedelta(days=1)
        return day


def exit_due(position, session: Session, now: datetime) -> bool:
    from .contracts import utc
    deadline = min(position.first_fill_at + timedelta(seconds=position.max_holding_seconds),
                   position.latest_exit_at, session.liquidation_at)
    return utc(now) >= deadline
