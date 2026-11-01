"""Charter-bound REAL ledger configuration (risk policy hash, multi-day horizon, settlement calendar).

The owner-signed charter (verified by ``real_daily.load_authority`` before any caller reaches this
module) is the only source of these values; code cannot enable overnight holding by itself."""
from datetime import timedelta
from uuid import uuid4

from .durable.models import RealConfiguration
from .mvp.config import reviewed_calendar
from .reference import CalendarUnknown

CALENDAR_DAYS_AHEAD = 100   # covers 60 sessions plus settlement (owner-approved hold horizon 2026-10-20)


def current_limits():
    """The running code's hard limits as journal values (CREATE.risk_policy / REAL_CONFIGURATION.risk_policy)."""
    from .risk import POLICY
    return {k: str(v) for k, v in POLICY.model_dump(mode='json').items()
            if k in ('account_exposure', 'stock_exposure', 'trade_risk', 'open_risk', 'daily_loss', 'drawdown',
                     'experiment_death')}


def settlement_days_from(label, days=CALENDAR_DAYS_AHEAD):
    calendar, out = reviewed_calendar(), []
    for offset in range(days):
        day = label + timedelta(days=offset)
        try:
            if calendar.settlement_day(day):
                out.append(day)
        except CalendarUnknown:
            break
    return tuple(out)


def desired_configuration(authority, state, now):
    from datetime import date
    from .carry import MAX_HOLD_SESSIONS
    scope = authority['scope']
    label = date.fromisoformat(state['session']['label'])
    from .risk import POLICY
    from .contracts import payload_hash
    limits = current_limits() if scope['risk_policy_sha256'] == payload_hash(POLICY) else None
    return RealConfiguration(charter_id=authority['authority_id'], policy_hash=scope['risk_policy_sha256'], risk_policy=limits,
                             carry_sessions=MAX_HOLD_SESSIONS if scope.get('overnight') is True else 0,
                             settlement_days=settlement_days_from(label), evidence_id='real-config-' + uuid4().hex)


def configuration_needed(state, command):
    config = state['config']
    known = set(config['settlement_days'])
    return (state['policy_hash'] != command.policy_hash
            or (config.get('carry_sessions') or 0) != command.carry_sessions
            or any(day.isoformat() not in known for day in command.settlement_days))


def ensure_configuration(controller, authority, now, *, extend_only=False):
    """Journal the configuration when the ledger differs; never changes the policy hash here.
    extend_only (recovery / protection starts): only extend the settlement calendar, keeping the ledger's
    current horizon, so exits never run out of known settlement days."""
    command = desired_configuration(authority, controller.state, now)
    if extend_only:
        command = command.model_copy(update={'policy_hash': controller.state['policy_hash'], 'risk_policy': None,
                                             'carry_sessions': controller.state['config'].get('carry_sessions') or 0})
    if command.policy_hash != controller.state['policy_hash']:
        # A revision is journaled only for the owner-signed policy that the running code implements.
        from .contracts import payload_hash
        from .risk import POLICY
        if command.policy_hash != payload_hash(POLICY):raise ValueError('REAL_DAILY_POLICY_MISMATCH')
    if configuration_needed(controller.state, command):
        controller.lease()
        controller.send(command)
    return controller.state
