"""Bind an existing durable account to fresh broker observations."""
from datetime import date
from decimal import Decimal
from uuid import uuid4
from ..durable.models import ConfirmSettlement,NewSession
from ..durable.projection import Rejected,settlement_observation_date
from .config import reviewed_calendar,ExecutionMode
from .gateway import sandbox_ledger_id
from .controller import Controller


def prepare_controller(ledger,config,observation,broker_account_id,clock):
    config.validate()
    aid=sandbox_ledger_id(broker_account_id) if config.mode==ExecutionMode.SANDBOX else 'dryrun-'+uuid4().hex
    try:ledger.snapshot(aid)
    except Rejected as exc:
        # A flat snapshot cannot distinguish a new account from lost journal
        # history. Only an explicitly authorized and evidenced opening can
        # create a durable account; normal runner startup has no such authority.
        if str(exc)=='UNKNOWN_ACCOUNT':
            raise ValueError('ACCOUNT_OPENING_REQUIRES_OWNER_AUTHORIZATION') from exc
        raise
    controller=Controller(ledger,aid,clock);controller.lease()
    refresh_session(controller,observation,clock)
    return controller


def refresh_session(controller,observation,clock,*,exit_observation=False):
    calendar=reviewed_calendar()
    # The only automatic settlement recognition is an exact aggregate match of
    # due recorded lots to actual settled/unsettled deltas, never buying power.
    state=controller.state
    delta=observation.settled-Decimal(state['cash']['settled_usd'])
    outstanding=[(key,lot) for key,lot in state['settlements'].items() if Decimal(lot['outstanding_usd'])>0]
    if delta>0:
        total=sum((Decimal(lot['outstanding_usd']) for _,lot in outstanding),Decimal(0))
        day=settlement_observation_date(state,clock())
        if (delta!=total or observation.unsettled!=Decimal(state['cash']['unsettled_usd'])-total
                or any(date.fromisoformat(lot['settlement_date'])>day for _,lot in outstanding)):
            raise ValueError('SETTLEMENT_CHANGE_UNRECONCILED')
        for xid,lot in outstanding:
            controller.send(ConfirmSettlement(execution_id=xid,amount_usd=lot['amount_usd'],
                effective_on=date.fromisoformat(lot['settlement_date']),evidence_id=observation.evidence_id))
    (controller.refresh_for_exit if exit_observation else controller.refresh)(observation)
    current=calendar.session(clock().astimezone(__import__('zoneinfo').ZoneInfo('America/New_York')).date())
    if current and current.label.isoformat()>controller.state['session']['label'] and current.contains(clock()):
        controller.send(NewSession(session=current))
    return controller
