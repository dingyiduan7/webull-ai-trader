"""A long portfolio refresh must retain its fenced writer lease."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from autonomous_trader.mvp.controller import Controller


def test_large_refresh_renews_writer_before_expiry():
    now=[datetime(2026,9,24,15,tzinfo=timezone.utc)]

    class Ledger:
        def __init__(self):
            self.leases=0
            self.commands=[]
            self.state={'version':0,'instruments':{'x':{}},'marks':{},'leader':{}}

        def snapshot(self,account_id):
            return self.state

        def acquire_leader(self,account_id,holder,*,event_id,at,ttl_seconds):
            self.leases+=1
            self.state['leader']={'holder':holder,'token':str(self.leases),
                'expires_at':(now[0]+timedelta(seconds=ttl_seconds)).isoformat()}
            return self.state

        def execute(self,account_id,command,*,event_id,expected_version,holder,token,at):
            assert holder==self.state['leader']['holder']
            assert token==self.state['leader']['token']
            assert now[0]<datetime.fromisoformat(self.state['leader']['expires_at'])
            self.commands.append(command.kind)
            self.state['version']+=1
            if command.kind=='MARK':
                now[0]+=timedelta(seconds=25)
            return self.state

    item=SimpleNamespace(instrument=SimpleNamespace(instrument_id='x'),instrument_id='x',
        bid=Decimal('2'),quote_at=now[0])
    observation=SimpleNamespace(candidates=(item,item,item,item),evidence_id='e',
        settled=Decimal('1000'),unsettled=Decimal('0'),positions={},open_intents=())
    ledger=Ledger()
    Controller(ledger,'paper',lambda:now[0]).refresh(observation)
    assert ledger.commands==['MARK']*4+['RECONCILE']
    assert ledger.leases>=2
