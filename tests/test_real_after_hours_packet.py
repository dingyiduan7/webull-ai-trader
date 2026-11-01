from datetime import datetime,timezone,timedelta
from decimal import Decimal
import json
import shutil
from types import SimpleNamespace

import pytest

import scripts.webull_real_after_hours_account_packet as subject
import scripts.webull_real_readiness_packet as market_open
from autonomous_trader.real_preflight import RealObservation,Operation
from autonomous_trader.real_readiness import RealReadiness

NOW=datetime(2026,9,29,22,30,tzinfo=timezone.utc)
ACCOUNT='private-account-must-not-escape'
BINDING='real-'+'a'*64
REAL_PROJECT_CHECKPOINT=subject.project_checkpoint


def checkpoint():
    return {'cash':{'settled_usd':'500.00','unsettled_usd':'5.35','equity_usd':'505.35'}}


def state():
    return {'account_binding':BINDING,'entry_authority':False,'overnight_enabled':False,
        'incident_latched':False,'observation_reconciled':True,
        'observed_at':'2026-09-29T07:36:39.949168+00:00',
        'external_quantities':{f'PRE{i}':'1' for i in range(5)},
        'managed_positions':{},'reservations':{}}


class FakeLedger:
    def __init__(self,db):pass
    def verify_replay(self):return state()


class FakeGate:
    def __init__(self,*args):pass
    def checkpoint(self,digest):return checkpoint(),digest


class Reader:
    def __init__(self,settled='505.35',unsettled='0.00',positions=None,orders=None,receipt_base=NOW):
        self.settled=settled;self.unsettled=unsettled;self.positions=[] if positions is None else positions
        self.orders={'data':[]} if orders is None else orders;self.receipt_base=receipt_base
        self._http=SimpleNamespace(contacted=[]);self.index=0
    def read(self,operation,account_id=''):
        paths={Operation.ACCOUNTS:subject.ACCOUNT_PATHS[0],Operation.BALANCE:subject.ACCOUNT_PATHS[1],
            Operation.POSITIONS:subject.ACCOUNT_PATHS[2],Operation.OPEN_ORDERS:subject.ACCOUNT_PATHS[3]}
        self._http.contacted.append(('GET',paths[operation]));self.index+=1
        total=str(Decimal(self.settled)+Decimal(self.unsettled))
        body={Operation.ACCOUNTS:[{'account_id':ACCOUNT,'account_type':'CASH','account_class':'INDIVIDUAL_CASH'}],
            Operation.BALANCE:{'account_currency_assets':[{'currency':'USD','settled_cash':self.settled,
                'unsettled_cash':self.unsettled,'cash_balance':total,'net_liquidation_value':total}]},
            Operation.POSITIONS:self.positions,Operation.OPEN_ORDERS:self.orders}[operation]
        return RealObservation(paths[operation],200,(self.receipt_base+timedelta(milliseconds=self.index)).isoformat(),{},body)


def fresh_clock():
    values=iter((NOW,NOW+timedelta(milliseconds=10)))
    return lambda:next(values)


def packet(reader,tmp_path):
    return subject.account_only_packet(reader,object(),tmp_path,clock=fresh_clock())


@pytest.fixture(autouse=True)
def boundaries(monkeypatch):
    monkeypatch.setattr(subject,'RealObservationLedger',FakeLedger)
    monkeypatch.setattr(subject,'RealGate',FakeGate)
    monkeypatch.setattr(subject,'strategy_schema_present',lambda db:False)
    monkeypatch.setattr(subject,'project_checkpoint',lambda root:None)
    monkeypatch.setattr(subject,'build_digest',lambda root:'build-digest')
    monkeypatch.setattr(subject,'certification_tool_hashes',lambda root:{'after_hours_script_sha256':'a','market_open_script_sha256':'b'})
    monkeypatch.setattr(subject,'binding',lambda account:BINDING)


def test_after_hours_packet_reads_account_only_and_accepts_exact_settlement_evolution(tmp_path):
    value=packet(Reader(),tmp_path)
    assert value['cash']['continuity']=='EXPECTED_SETTLEMENT_EVOLUTION'
    assert value['account']['flat'] and value['account']['open_nonfinal_order_count']==0
    assert value['market_data']=={'requested':False,'discovery_requested':False,
        'quote_freshness':'DEFERRED_TO_CORE_SESSION','nasdaq_basic_entitlement':'DEFERRED_TO_CORE_SESSION'}
    assert value['authority']['broker_mutations']==value['authority']['ledger_mutations']==0
    assert value['authority']['production_enabled'] is False and value['protective_exit']['overnight_authority'] is False
    assert (value['limits']['astra_calls_per_session'],value['limits']['astra_calls_per_exchange_week'])==(None,None)
    assert value['limits']['monthly_model_cost_cap_usd'] is None
    assert value['limits']['monthly_model_cost_cap_enabled'] is False
    assert value['limits']['monthly_model_cost_control']=='OWNER_TEMPORARY_CAP_SUSPENSION_V1'
    assert value['checkpoint_handoff']['prior_observation_external_quantity_count']==5
    assert value['checkpoint_handoff']['observation_journal_mutated'] is False
    text=subject.clean_json(value,(ACCOUNT,'credential-sentinel','credential-token-sentinel'))
    assert ACCOUNT not in text and 'api.sandbox.webull.com' not in text
    path=subject.write_packet(tmp_path,value,(ACCOUNT,'credential-sentinel','credential-token-sentinel'),NOW)
    assert json.loads(path.read_text())['schema']=='REAL_AFTER_HOURS_ACCOUNT_PACKET_V1'


@pytest.mark.parametrize('reader,error',[
    (Reader(settled='500.00',unsettled='5.35'),None),
    (Reader(settled='499.00',unsettled='6.35'),'REAL_SETTLEMENT_TRANSITION_INVALID'),
    (Reader(settled='505.34',unsettled='0.00'),'REAL_FLAT_CHECKPOINT_CASH_CHANGED'),
    (Reader(positions=[{'symbol':'AAPL','quantity':'1','market_value':'1','last_price':'1'}]),'REAL_CURRENT_ACCOUNT_NOT_FLAT'),
    (Reader(orders={'data':[{'client_order_id':'private'}]}),'REAL_CURRENT_ACCOUNT_NOT_FLAT'),
])
def test_after_hours_cash_and_flatness_fail_closed(tmp_path,reader,error):
    if error:
        with pytest.raises(ValueError,match=error):packet(reader,tmp_path)
    else:
        assert packet(reader,tmp_path)['cash']['continuity']=='UNCHANGED'


def test_after_hours_rejects_authority_artifact_or_writer_schema(tmp_path,monkeypatch):
    folder=tmp_path/'.local'/'real-readiness';folder.mkdir(parents=True)
    (folder/'judicator-real-go.json').write_text('{}')
    with pytest.raises(ValueError,match='AUTHORITY_MUST_REMAIN_DISABLED'):
        packet(Reader(),tmp_path)
    (folder/'judicator-real-go.json').unlink();monkeypatch.setattr(subject,'strategy_schema_present',lambda db:True)
    with pytest.raises(ValueError,match='AUTHORITY_MUST_REMAIN_DISABLED'):
        packet(Reader(),tmp_path)


def test_current_reads_must_be_stamped_during_this_invocation(tmp_path):
    stale=Reader(receipt_base=NOW-timedelta(minutes=1))
    with pytest.raises(ValueError,match='REAL_ACCOUNT_OBSERVATION_STALE'):packet(stale,tmp_path)


def test_wrong_current_account_binding_fails(tmp_path,monkeypatch):
    monkeypatch.setattr(subject,'binding',lambda account:'real-'+'b'*64)
    with pytest.raises(ValueError,match='REAL_ACCOUNT_BINDING_MISMATCH'):packet(Reader(),tmp_path)


def test_market_open_cli_reports_after_hours_gate_without_traceback(monkeypatch,capsys):
    monkeypatch.setattr(market_open,'main',lambda:(_ for _ in ()).throw(ValueError('REAL_DISCOVERY_SESSION_REQUIRED')))
    assert market_open.cli()==2
    lines=capsys.readouterr().out.splitlines();assert len(lines)==1
    assert json.loads(lines[0])['status']=='REAL_DISCOVERY_SESSION_REQUIRED'


def test_after_hours_cli_emits_one_sanitized_failure_line(monkeypatch,capsys):
    monkeypatch.setattr(subject,'main',lambda:(_ for _ in ()).throw(ValueError('REAL_AUTHENTICATION_FAILED')))
    assert subject.cli()==2
    lines=capsys.readouterr().out.splitlines();assert len(lines)==1
    assert json.loads(lines[0])=={'status':'REAL_AUTHENTICATION_FAILED'}


def test_project_checkpoint_is_root_relative_when_working_directory_differs(tmp_path,monkeypatch):
    project=tmp_path/'project';folder=project/'.local'/'real-readiness';folder.mkdir(parents=True)
    source=subject.ROOT/'.local'/'real-readiness'/subject.CHECKPOINT_NAME
    shutil.copyfile(source,folder/subject.CHECKPOINT_NAME)
    elsewhere=tmp_path/'normal-user-home';elsewhere.mkdir();monkeypatch.chdir(elsewhere)
    assert REAL_PROJECT_CHECKPOINT(project)==(folder/subject.CHECKPOINT_NAME).resolve()


def test_project_checkpoint_missing_or_tampered_fails_exact_hash(tmp_path):
    with pytest.raises(ValueError,match='REAL_FLAT_CHECKPOINT_REQUIRED'):
        REAL_PROJECT_CHECKPOINT(tmp_path)
    folder=tmp_path/'.local'/'real-readiness';folder.mkdir(parents=True)
    source=subject.ROOT/'.local'/'real-readiness'/subject.CHECKPOINT_NAME
    target=folder/subject.CHECKPOINT_NAME;shutil.copyfile(source,target);target.write_bytes(target.read_bytes()+b' ')
    with pytest.raises(ValueError,match='REAL_FLAT_CHECKPOINT_DIGEST'):
        REAL_PROJECT_CHECKPOINT(tmp_path)
