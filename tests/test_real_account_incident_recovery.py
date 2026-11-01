from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import pytest

from test_real_incident_recovery import ACCOUNT, NOW, CHECKPOINT, runner, close
from autonomous_trader.real_trading import RealController


def setup(monkeypatch):
    subject=runner()
    clock=[NOW]
    subject.clock=lambda:clock[0]
    # Receipts carry a validity window (current runtime contract).
    receipt={**subject.authority_receipt,'flat_checkpoint_sha256':'c'*64,
        'daily_authority_sha256':'d'*64,'issued_at':(NOW-timedelta(minutes=1)).isoformat(),
        'expires_at':(NOW+timedelta(hours=2)).isoformat(),
        'recovery_not_after':(NOW+timedelta(days=1)).isoformat()}
    monkeypatch.setattr('autonomous_trader.real_runner.service_receipt',lambda *a,**kw:dict(receipt))
    subject.gate=SimpleNamespace(root='unused',checkpoint=lambda _: (deepcopy(CHECKPOINT),None))
    account={'account_binding':ACCOUNT,'positions':[],'open_client_ids':[],
        'cash':{'settled_cash':'505.44','unsettled_cash':'0','cash_balance':'505.44',
            'net_liquidation_value':'505.44'}}
    subject.source=SimpleNamespace(
        account_observation=lambda:{**deepcopy(account),'observed_at':clock[0],'received_at':clock[0]},
        refresh=lambda *a:pytest.fail('account recovery must not require market data'))
    controller=subject.controller
    controller.clock=subject.clock
    controller.lease=lambda:None
    commands=[]
    def send(command):
        commands.append(command)
        controller.state['reconciliation'].update(ready=True,settlement_known=True)
        for key in command.resolved_incident_ids:controller.state['incidents'].pop(key)
        controller.state['latches']['incident']=bool(controller.state['incidents'])
        return controller.state
    controller.send=send
    controller.refresh_flat_recovery=lambda a:RealController.refresh_flat_recovery(controller,a)
    controller.resolve_flat_reconciliation_incidents=lambda p,ids,**kw:RealController.resolve_flat_reconciliation_incidents(controller,p,ids,**kw)
    return subject,clock,account,receipt,commands


def test_account_only_pair_resolves_via_normal_journal_and_waits_for_market(monkeypatch):
    subject,clock,account,receipt,commands=setup(monkeypatch)
    try:
        assert subject._recover_flat_account_incident()=='RECOVERY_ACCOUNT_CONFIRMATION_PENDING'
        assert subject.controller.state['latches']['incident']
        clock[0]+=timedelta(seconds=1)
        assert subject._recover_flat_account_incident()=='RECOVERY_FLAT_RECONCILED_WAITING_FOR_MARKET_DATA'
        assert subject.controller.waiting_for_market_data
        assert not subject.controller.state['latches']['incident']
        assert [c.kind for c in commands]==['RECONCILE']*3
        assert commands[-1].resolved_incident_ids==('incident-a',)
        assert subject.store.events[-1][1]['evidence_type']=='ACCOUNT_ONLY'
        assert subject.store.events[-1][1]['broker_mutations']==0
    finally:close(subject)


# 'cash' removed 2026-10-18: broker-truth adopts (journals) a cash change instead of refusing it.
@pytest.mark.parametrize('fault',['broker-order','broker-position','local-order','unknown-order',
    'other-incident','daily','writer','stale','authority','checkpoint'])
def test_unsafe_second_observation_never_clears(monkeypatch,fault):
    subject,clock,account,receipt,commands=setup(monkeypatch)
    try:
        subject._recover_flat_account_incident();clock[0]+=timedelta(seconds=1)
        if fault=='cash':account['cash']['settled_cash']='505.43'
        elif fault=='broker-order':account['open_client_ids']=['x']
        elif fault=='broker-position':account['positions']=[{'quantity':'1'}]
        elif fault in ('local-order','unknown-order'):
            subject.controller.state['orders']['x']={'final':fault=='unknown-order','phase':'UNKNOWN'}
        elif fault=='other-incident':subject.controller.state['incidents']['b']={'reason':'OTHER','pending_correction':False}
        elif fault=='daily':subject.controller.state['latches']['daily']=True
        elif fault=='writer':subject._single_writer=lambda:False
        elif fault=='stale':subject.source.account_observation=lambda:{**account,'observed_at':NOW-timedelta(seconds=11),'received_at':clock[0]}
        elif fault=='authority':receipt['build_sha256']='wrong'
        elif fault=='checkpoint':subject.gate.checkpoint=lambda _:({'cash':{'settled_usd':'500','unsettled_usd':'0','equity_usd':'500'}},None)
        subject._recover_flat_account_incident()
        assert subject.controller.state['latches']['incident']
        assert not any(c.resolved_incident_ids for c in commands)
    finally:close(subject)


def test_repeated_observation_cannot_count_twice(monkeypatch):
    subject,clock,account,receipt,commands=setup(monkeypatch)
    try:
        subject._recover_flat_account_incident();subject._recover_flat_account_incident()
        assert subject.controller.state['latches']['incident']
        assert not any(c.resolved_incident_ids for c in commands)
    finally:close(subject)


def test_authority_revocation_during_read_cannot_clear(monkeypatch):
    subject,clock,account,receipt,commands=setup(monkeypatch)
    try:
        subject._recover_flat_account_incident();clock[0]+=timedelta(seconds=1)
        original=subject.source.account_observation
        def read():
            result=original();receipt['build_sha256']='revoked';return result
        subject.source.account_observation=read
        subject._recover_flat_account_incident()
        assert subject.controller.state['latches']['incident']
        assert not any(c.resolved_incident_ids for c in commands)
    finally:close(subject)
