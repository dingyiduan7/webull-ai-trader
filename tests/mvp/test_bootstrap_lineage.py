"""Startup must never replace missing financial history with a cash snapshot."""
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from autonomous_trader.durable.projection import Rejected
from autonomous_trader.mvp import bootstrap
from autonomous_trader.mvp.config import ExecutionMode, RuntimeConfig


NOW = datetime(2026, 9, 25, 14, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("positions,open_intents,settled", [
    ({}, (), Decimal('500')),
    ({'stock-a': '1'}, (), Decimal('500')),
    ({}, ('uncertain-order',), Decimal('500')),
    ({}, (), Decimal('999667.24')),
])
def test_unknown_account_never_bootstraps_from_broker_snapshot(positions, open_intents, settled):
    class MissingLedger:
        def snapshot(self, account_id):
            raise Rejected('UNKNOWN_ACCOUNT')

        def create(self, *args, **kwargs):
            pytest.fail('startup must not create an account from a snapshot')

    observation = SimpleNamespace(positions=positions, open_intents=open_intents,
        settled=settled, unsettled=Decimal(0), equity=settled,
        evidence_id='broker-observation')
    with pytest.raises(ValueError, match='ACCOUNT_OPENING_REQUIRES_OWNER_AUTHORIZATION'):
        bootstrap.prepare_controller(MissingLedger(), RuntimeConfig(ExecutionMode.SANDBOX),
            observation, 'designated-cash-account', lambda: NOW)


def test_existing_account_uses_ledger_without_reopening(monkeypatch):
    calls=[]

    class ExistingLedger:
        def snapshot(self, account_id):
            calls.append(('snapshot', account_id))
            return {'account_id': account_id}

        def create(self, *args, **kwargs):
            pytest.fail('existing account must not be recreated')

    class ExistingController:
        def __init__(self, ledger, account_id, clock):
            self.account_id=account_id

        def lease(self):
            calls.append(('lease', self.account_id))

    monkeypatch.setattr(bootstrap, 'Controller', ExistingController)
    monkeypatch.setattr(bootstrap, 'refresh_session',
        lambda controller, observation, clock: calls.append(('refresh', controller.account_id)))
    controller=bootstrap.prepare_controller(ExistingLedger(), RuntimeConfig(ExecutionMode.SANDBOX),
        object(), 'designated-cash-account', lambda: NOW)
    assert calls==[('snapshot', controller.account_id), ('lease', controller.account_id),
        ('refresh', controller.account_id)]


def test_production_still_rejects_before_ledger_access():
    class NoLedgerAccess:
        def snapshot(self, account_id):
            pytest.fail('production must reject before ledger access')

    with pytest.raises(ValueError, match='PRODUCTION_DISABLED'):
        bootstrap.prepare_controller(NoLedgerAccess(), RuntimeConfig(ExecutionMode.PRODUCTION),
            object(), 'real-account', lambda: NOW)
