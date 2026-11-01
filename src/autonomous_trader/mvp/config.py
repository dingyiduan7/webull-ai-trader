from dataclasses import dataclass
from datetime import date, time
from enum import StrEnum
import os
from ..reference import FixtureCalendar


class ExecutionMode(StrEnum):
    DRY_RUN='DRY_RUN'
    SANDBOX='SANDBOX'
    PRODUCTION='PRODUCTION'


@dataclass(frozen=True)
class RuntimeConfig:
    mode: ExecutionMode=ExecutionMode.DRY_RUN
    model: str='claude-opus-5-5'
    reasoning: str='medium'
    opportunity_workers: int=2
    opportunity_calls_per_window: int=3

    @classmethod
    def from_env(cls):
        return cls(ExecutionMode(os.getenv('AI_TRADER_MODE','DRY_RUN')),
            os.getenv('AI_TRADER_MODEL','claude-opus-5-5'),os.getenv('AI_TRADER_REASONING','medium'),
            int(os.getenv('AI_TRADER_OPPORTUNITY_WORKERS','4')),
            int(os.getenv('AI_TRADER_OPPORTUNITY_CALLS_PER_WINDOW','6')))

    def validate(self):
        if self.mode==ExecutionMode.PRODUCTION:
            raise ValueError('PRODUCTION_DISABLED')
        if not 1<=self.opportunity_workers<=4 or not 1<=self.opportunity_calls_per_window<=120:
            raise ValueError('INVALID_OPPORTUNITY_BOUNDS')


def reviewed_calendar():
    # Official NYSE 2026 table verified 2026-09-11; NYSE 2027 table (press release 2025-12-23) verified
    # 2026-10-16. Bounded, never a weekday-only guess: dates past 2027-12-31 are unknown and fail closed.
    holidays={date.fromisoformat(d) for d in ('2026-01-01','2026-01-19','2026-02-16','2026-04-03',
        '2026-05-25','2026-06-19','2026-07-03','2026-09-07','2026-11-26','2026-12-25',
        '2027-01-01','2027-01-18','2027-02-15','2027-03-26','2027-05-31','2027-06-18','2027-07-05',
        '2027-09-06','2027-11-25','2027-12-24')}
    # Settlement (bank) holidays: exchange holidays plus Columbus and Veterans Day. 2027-06-18 and 2027-12-24
    # are exchange holidays whose underlying date falls on a Saturday; they are kept as settlement holidays
    # here (conservative) -- RE-VERIFY against the DTC/Fed 2027 schedule before June 2027.
    return FixtureCalendar(date(2026,1,1),date(2027,12,31),exchange_holidays=holidays,
        settlement_holidays=holidays|{date(2026,10,12),date(2026,11,11),date(2027,10,11),date(2027,11,11)},
        early_closes={date(2026,11,27):time(13),date(2026,12,24):time(13),date(2027,11,26):time(13)})
