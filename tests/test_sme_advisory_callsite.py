"""Only synthetic fixtures and mocked transport; no export permission granted."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import sqlite3
from pathlib import Path
import shutil
import pytest

from autonomous_trader.contracts import AlphaContext, AlphaInstrumentContext
from autonomous_trader.decision_api.astra_probe import AstraClient, deep_alpha_request
from autonomous_trader.decision_api.sme_advisory import ApprovedSMEAdvisory, _digest


@pytest.fixture
def alpha():
    now = datetime(2026, 10, 5, 18, tzinfo=timezone.utc)
    return AlphaContext(as_of=now, expires_at=now+timedelta(seconds=30),
        session_open=now-timedelta(hours=4), session_close=now+timedelta(hours=2),
        liquidation_at=now+timedelta(hours=2), settled_cash_usd='100', remaining_risk_usd='1',
        reconciliation_ready=True, settlement_known=True,
        instruments=(AlphaInstrumentContext(instrument_id='fixture', symbol='TEST', quote_at=now,
            bid_usd='10', ask_usd='10.01', price_increment_usd='0.01', quantity_increment='1'),))


def approved_fixture(tmp_path, alpha, change=None):
    # Models the real-advice contract locally. All content is fabricated test data;
    # this helper/approval is never used outside mocked transport tests.
    advice = {'operation': 'advise', 'findings': ['Fixture observation; no trading recommendation.']}
    artifact = {'schema': 'TRADER_SME_ADVISORY_V1', 'authority': 'NONBINDING_ADVICE_ONLY',
        'verification_only': False, 'records': [{'verification_only': False,
            'outbox_id': 'fixture-outbox', 'source_event': 'fixture-event', 'knowledge_id':'fixture-knowledge', 'advice': advice,
            'sha256': _digest(advice), 'created_at': (alpha.as_of-timedelta(days=3)).isoformat()}]}
    if change: change(artifact)
    raw = json.dumps(artifact).encode()
    path = tmp_path/'fixture-only.json'; path.write_bytes(raw)
    db = tmp_path/'fixture-permission.sqlite3'
    with sqlite3.connect(db) as c:
        c.executescript('''CREATE TABLE events(id TEXT,role TEXT,status TEXT,payload TEXT);
            CREATE TABLE outbox(id TEXT,event_id TEXT,destination TEXT,body TEXT,created TEXT);
            CREATE TABLE knowledge(id TEXT,body TEXT,sha256 TEXT,source TEXT);
            CREATE TABLE export_permissions(knowledge_id TEXT);''')
        c.execute('INSERT INTO events VALUES(?,?,?,?)',('fixture-event','trader','COMPLETED',json.dumps({'knowledge_id':'fixture-knowledge'})))
        c.execute('INSERT INTO outbox VALUES(?,?,?,?,?)',('fixture-outbox','fixture-event','astra_advisory',json.dumps(advice),(alpha.as_of-timedelta(days=3)).isoformat()))
        c.execute('INSERT INTO knowledge VALUES(?,?,?,?)',('fixture-knowledge',json.dumps({'fixture':'only'}),_digest({'fixture':'only'}),'fixture'))
        c.execute('INSERT INTO export_permissions VALUES(?)',('fixture-knowledge',))
    return ApprovedSMEAdvisory(path, sha256(raw).hexdigest(), db)


def call(alpha, approval=None):
    client = AstraClient('synthetic-unused', approved_sme_advisory=approval)
    captured = []
    def transport(body):
        captured.append(deepcopy(body))
        return json.dumps({'regime_label': 'FIXTURE', 'regime_view': 'Fixture only', 'focus': []}),0,{'input_tokens':0,'output_tokens':0},'0'
    client._structured_call = transport
    client.deep_plan(alpha, {'maximum_positions':5})
    assert len(captured) == 1
    return client, captured[0]


def test_actual_deep_plan_attaches_only_nonbinding_evidence_without_changing_prompt(tmp_path, alpha):
    approval = approved_fixture(tmp_path, alpha)
    client, body = call(alpha, approval)
    expected = deep_alpha_request(alpha, {'maximum_positions':5})
    payload = json.loads(body['input'][1]['content'])
    advisory = payload.pop('trader_sme_advisory')
    assert advisory['authority'] == 'NONBINDING_ADVICE_ONLY'
    assert advisory['artifact_sha256'] == approval.sha256
    row = advisory['records'][0]
    assert row['source_event'] == 'fixture-event'
    assert row['assessment_age_seconds'] == 259200 and row['temporal_status'] == 'VALIDITY_UNKNOWN'
    assert row['market_evidence_as_of'] is None
    assert json.loads(expected['input'][1]['content']) == payload
    body['input'][1]['content'] = expected['input'][1]['content']
    assert body == expected
    assert client.last_sme_advisory_status == 'ATTACHED_APPROVED_NONBINDING'


@pytest.mark.parametrize('case',['absent','missing','hash','record-hash','synthetic-top','synthetic-row',
    'synthetic-source','malformed','future','no-approval','bad-hash-type','nested','oversize'])
def test_optional_advice_failure_never_blocks_or_changes_main_request(tmp_path, alpha, case):
    changes = {
        'record-hash': lambda a:a['records'][0].update(sha256='0'*64),
        'synthetic-top': lambda a:a.update(verification_only=True),
        'synthetic-row': lambda a:a['records'][0].update(verification_only=True),
        'synthetic-source': lambda a:a['records'][0].update(source_event='synthetic-test'),
        'future': lambda a:a['records'][0].update(created_at=(alpha.as_of+timedelta(seconds=1)).isoformat())}
    approved = approved_fixture(tmp_path, alpha, changes.get(case))
    if case == 'absent': approved=None
    elif case == 'missing': approved.path.unlink()
    elif case == 'hash': approved=ApprovedSMEAdvisory(approved.path,'0'*64,approved.authorization_db)
    elif case == 'no-approval':
        with sqlite3.connect(approved.authorization_db) as c: c.execute('DELETE FROM export_permissions')
    elif case == 'bad-hash-type': approved=ApprovedSMEAdvisory(approved.path,None,approved.authorization_db)
    elif case in ('malformed','nested','oversize'):
        raw = b'{' if case == 'malformed' else b'['*2000+b'0'+b']'*2000 if case == 'nested' else b' '*32769
        approved.path.write_bytes(raw)
        approved=ApprovedSMEAdvisory(approved.path,sha256(raw).hexdigest(),approved.authorization_db)
    client, body = call(alpha, approved)
    assert body == deep_alpha_request(alpha, {'maximum_positions':5})
    assert client.last_sme_advisory_status != 'ATTACHED_APPROVED_NONBINDING'


def test_explicit_cli_wiring_defaults_disabled_and_requires_all_three_fields(tmp_path):
    from scripts.run_ai_trader import parse_args, advisory_configuration
    assert advisory_configuration(parse_args([])) is None
    assert advisory_configuration(parse_args(['--sme-advisory-file',str(tmp_path/'x')])) is None
    args=parse_args(['--sme-advisory-file',str(tmp_path/'x'),'--sme-advisory-sha256','a'*64,
                     '--sme-authorization-db',str(tmp_path/'permission.db')])
    assert advisory_configuration(args) == ApprovedSMEAdvisory(tmp_path/'x','a'*64,tmp_path/'permission.db')


def test_provider_failure_is_not_hidden_by_optional_advice(alpha):
    client = AstraClient('synthetic-unused')
    client._structured_call = lambda _: (_ for _ in ()).throw(ValueError('PROVIDER_ERROR'))
    with pytest.raises(ValueError, match='PROVIDER_ERROR'): client.deep_plan(alpha, {})


@pytest.mark.parametrize('mode',['ENTRY','POSITION'])
def test_real_composition_brain_attaches_advice_to_existing_fast_call(tmp_path, alpha, mode):
    from scripts.run_real_trader import configured_brain
    from autonomous_trader.decision_api.astra_probe import fast_alpha_request
    approval = approved_fixture(tmp_path, alpha)
    budget = object()
    client = configured_brain('unused-fixture', budget, approval)
    captured = []
    def transport(body):
        captured.append(body)
        raise ValueError('MOCK_TRANSPORT_STOP')
    client._structured_call = transport
    with pytest.raises(ValueError,match='MOCK_TRANSPORT_STOP'):
        client.fast_decide(alpha,mode=mode,hard_policy={'maximum_positions':5},clock=lambda:alpha.as_of)
    assert client.budget is budget and len(captured) == 1
    body = captured[0]
    payload = json.loads(body['input'][1]['content'])
    assert payload.pop('trader_sme_advisory')['records'][0]['source_event'] == 'fixture-event'
    from autonomous_trader.decision_api.brain import DECISION_MODEL
    expected = fast_alpha_request(alpha,mode=mode,hard_policy={'maximum_positions':5},model=DECISION_MODEL)
    assert payload == json.loads(expected['input'][1]['content'])
    body['input'][1]['content'] = expected['input'][1]['content']
    assert body == expected


def test_existing_permissions_exporter_provider_and_revocation(tmp_path, alpha, monkeypatch):
    parents = Path(__file__).resolve().parents
    if len(parents) <= 4 or not (parents[4]/'agent_layer').is_dir():
        pytest.skip('external agent_layer repository is not part of this Auto Trader copy')
    agent_source = Path(__file__).resolve().parents[4]/'agent_layer'
    monkeypatch.syspath_prepend(str(agent_source))
    import agents
    from advisory_export import export_approved_advisory
    root=tmp_path/'agent-fixture';root.mkdir()
    shutil.copy(agent_source/'config.json',root/'config.json')
    monkeypatch.setattr(agents,'now',lambda:(alpha.as_of-timedelta(seconds=1)).isoformat())
    layer=agents.Agents(root)
    eid=layer.ingest('economics',{'fixture_only':'fabricated test data'},'fixture',alpha.as_of.isoformat(),allow_model=False)
    layer.authorize_export(eid)  # TEST database only; no actual evidence authorized.
    def transport(request):
        result={'operation':'advise','objective':'Fixture','expected_result':'Fixture','findings':['Fixture'],
            'limitations':['No actual evidence'],'memory':'Fixture','verdict':'NOT_APPLICABLE','next_action':'Fixture'}
        return {'id':'fixture','model':request['model'],'status':'completed','usage':{'input_tokens':0,'output_tokens':0},
            'output':[{'type':'message','content':[{'type':'output_text','text':json.dumps(result)}]}]}
    layer.run(eid,transport)
    exported=export_approved_advisory(layer,tmp_path/'approved-fixture.json')
    approval=ApprovedSMEAdvisory(Path(exported['path']),exported['sha256'],Path(exported['authorization_db']))
    client,body=call(alpha,approval)
    assert client.last_sme_advisory_status=='ATTACHED_APPROVED_NONBINDING'
    assert json.loads(body['input'][1]['content'])['trader_sme_advisory']['records'][0]['source_event']==eid
    with layer.db() as c:c.execute('DELETE FROM export_permissions')
    client,body=call(alpha,approval)
    assert client.last_sme_advisory_status=='IGNORED_EXPORT_PERMISSION'
    assert body==deep_alpha_request(alpha,{'maximum_positions':5})
    with pytest.raises(ValueError,match='NO_APPROVED'):export_approved_advisory(layer,tmp_path/'must-not-write.json')
    sid=layer.ingest('economics',{'verification_only':True},'synthetic-fixture',alpha.as_of.isoformat(),allow_model=True)
    layer.run(sid,transport)
    with pytest.raises(ValueError,match='NO_APPROVED'):export_approved_advisory(layer,tmp_path/'must-not-write.json')
    assert not (tmp_path/'must-not-write.json').exists()


def test_created_at_and_knowledge_source_cannot_misrepresent_provenance(tmp_path,alpha):
    approved=approved_fixture(tmp_path,alpha)
    with sqlite3.connect(approved.authorization_db) as c:c.execute("UPDATE knowledge SET source='synthetic-proof'")
    client,body=call(alpha,approved)
    assert client.last_sme_advisory_status=='IGNORED_EXPORT_PERMISSION'
    with sqlite3.connect(approved.authorization_db) as c:
        c.execute("UPDATE knowledge SET source='fixture'")
        c.execute("UPDATE outbox SET created='2026-09-01T00:00:00+00:00'")
    client,body=call(alpha,approved)
    assert client.last_sme_advisory_status=='IGNORED_EXPORT_PERMISSION'


def test_supervisor_forwards_only_explicit_advisory_configuration_without_starting(tmp_path):
    import subprocess
    from scripts import run_real_supervisor as supervisor, run_real_trader as trader
    flags=['--sme-advisory-file',str(tmp_path/'advice.json'),'--sme-advisory-sha256','a'*64,
           '--sme-authorization-db',str(tmp_path/'permissions.db')]
    default=supervisor.child_command(False,'fixture-handle',123)
    configured=supervisor.child_command(True,'fixture-handle',123,flags)
    assert not any(x.startswith('--sme-') for x in default)
    assert configured[-6:]==flags and '--recovery' in configured
    assert trader._is_supervisor_command_line(subprocess.list2cmdline(
        [supervisor.sys.executable,'-I','-B',str(supervisor.ROOT/'scripts/run_real_supervisor.py'),*flags]))
    assert not trader._is_supervisor_command_line(subprocess.list2cmdline(
        [supervisor.sys.executable,'-I','-B',str(supervisor.ROOT/'scripts/run_real_supervisor.py'),'--recovery']))
    assert supervisor.advisory_arguments(flags[:2])==[]
    assert supervisor.child_command(False,'fixture-handle',123,flags[:2])==default
    assert trader._is_supervisor_command_line(subprocess.list2cmdline(
        [supervisor.sys.executable,'-I','-B',str(supervisor.ROOT/'scripts/run_real_supervisor.py'),*flags[:2]]))


def test_optional_advice_delay_cannot_spend_against_expired_context(tmp_path,alpha,monkeypatch):
    from autonomous_trader.decision_api import sme_advisory
    from autonomous_trader.sandbox.secrets import PreflightError
    current=[alpha.as_of]
    def slow(body, context, approved):
        current[0]=context.expires_at
        return body,'IGNORED_INVALID_OR_UNAVAILABLE'
    monkeypatch.setattr(sme_advisory,'attach_approved_advisory',slow)
    client=AstraClient('unused',approved_sme_advisory=approved_fixture(tmp_path,alpha))
    client._structured_call=lambda _:pytest.fail('expired context must not spend')
    with pytest.raises(PreflightError,match='STALE_CONTEXT'):
        client.fast_decide(alpha,mode='ENTRY',hard_policy={},clock=lambda:current[0])
