from pathlib import Path
from types import SimpleNamespace
from datetime import datetime,timezone,timedelta
import hashlib,json
import shutil
import os,msvcrt

import pytest

import scripts.run_real_trader as subject
from autonomous_trader.real_trading import build_digest


class ObservationLedger:
    def __init__(self,db):pass
    def verify_replay(self):return {'account_binding':'real-'+'a'*64}


def capability(receipt,recovery=False):
    value=subject._SupervisorCapability(subject._SUPERVISOR_SENTINEL,receipt,recovery,
        datetime.now(timezone.utc)+timedelta(hours=1))
    subject._ISSUED_SUPERVISOR_CAPABILITIES.add(value)
    return value


@pytest.mark.parametrize('reason',['REAL_OWNER_SIGNATURE_INVALID','REAL_OWNER_AUTHORITY_MISMATCH'])
def test_launcher_stops_at_go_gate_before_secrets_network_writer_or_model(monkeypatch,reason):
    from autonomous_trader import real_owner_trust
    def reject_owner(*args,**kwargs):raise ValueError(reason)
    monkeypatch.setattr(real_owner_trust,'read_protected_authorization',reject_owner)
    class Gate:
        @staticmethod
        def offline_receipt(*args,**kwargs):raise ValueError(reason)
        def __init__(self,*args):pytest.fail('No runtime gate before offline GO')
    monkeypatch.setattr(subject,'RealObservationLedger',ObservationLedger)
    monkeypatch.setattr(subject,'RealGate',Gate)
    for name in ('LocalDatabase','RealObservationLedger','Ledger','load_real_credentials','RealMarketSource',
                 'migrate','grant_writer','load_model_key','provision','ModelBudget','RealRunner'):
        monkeypatch.setattr(subject,name,lambda *args,_name=name,**kwargs:
            pytest.fail(_name+' must remain unreachable before exact GO'))
    receipt={'account_binding':'real-'+'a'*64}
    with pytest.raises(ValueError,match=reason):subject.compose(supervisor_capability=capability(receipt))


def test_direct_compose_without_supervisor_capability_fails_before_go_or_database(monkeypatch):
    monkeypatch.setattr(subject.RealGate,'offline_receipt',lambda *args,**kwargs:pytest.fail('GO read before capability'))
    monkeypatch.setattr(subject,'LocalDatabase',lambda *args,**kwargs:pytest.fail('DB before capability'))
    with pytest.raises(ValueError,match='REAL_SUPERVISOR_CAPABILITY_REQUIRED'):subject.compose()
    forged=subject._SupervisorCapability(subject._SUPERVISOR_SENTINEL,{},False,
        datetime.now(timezone.utc)+timedelta(minutes=1))
    with pytest.raises(ValueError,match='REAL_SUPERVISOR_CAPABILITY_REQUIRED'):
        subject.compose(supervisor_capability=forged)


def test_duplicate_real_writer_lock_fails_closed():
    class Result:
        def fetchone(self):return (False,)
    class Connection:
        autocommit=False
        closed=False
        def execute(self,*args):return Result()
        def close(self):self.closed=True
    class Admin:
        connection=Connection()
        def connect(self):return self.connection
    admin=Admin()
    with pytest.raises(ValueError,match='REAL_RUNNER_ALREADY_ACTIVE'):
        subject.acquire_writer_lock(admin)
    assert admin.connection.closed


def test_real_launcher_is_dedicated_and_does_not_create_scheduler_or_sandbox_mode():
    text=(subject.ROOT/'scripts'/'run_real_trader.py').read_text(encoding='utf-8')
    for required in ('RealRunner','RealExecutionGateway','RealStrategyTransport',"ModelBudget(store,'REAL'",'WRITER_LOCK'):
        assert required in text
    assert 'schtasks' not in text.lower()
    assert "ModelBudget(store,'SANDBOX'" not in text
    assert 'judicator-real-go.json' not in text
    assert subject.WRITER_LOCK==17482026
    assert "'ledger_schema':'real_trader'" in text


def test_judicator_build_digest_binds_the_real_launcher(tmp_path):
    package=tmp_path/'src'/'autonomous_trader';package.mkdir(parents=True)
    (package/'durable').mkdir();(package/'mvp').mkdir()
    for path in (package/'durable'/'schema.sql',package/'alpha-decision.schema.json',
                 package/'trade-decision.schema.json',package/'mvp'/'strategy.json'):
        path.write_text('VALUE=1\n',encoding='utf-8')
    for name in ('alpha-decision.schema.json','trade-decision.schema.json'):
        (tmp_path/name).write_text('VALUE=1\n',encoding='utf-8')
    (tmp_path/'pyproject.toml').write_text('VALUE=1\n');(tmp_path/'requirements.txt').write_text('webull-openapi-python-sdk==3.0.0\n')
    scripts=tmp_path/'scripts';scripts.mkdir()
    (package/'kernel.py').write_text('VALUE=1\n',encoding='utf-8')
    launcher=scripts/'run_real_trader.py';launcher.write_text('VALUE=1\n',encoding='utf-8')
    (scripts/'run_real_supervisor.py').write_text('VALUE=1\n',encoding='utf-8')
    (scripts/'webull_real_readiness_packet.py').write_text('VALUE=1\n',encoding='utf-8')
    (scripts/'webull_real_after_hours_account_packet.py').write_text('VALUE=1\n',encoding='utf-8')
    (scripts/'run_ai_trader.py').write_text('VALUE=1\n');(scripts/'run_trading_cycle.py').write_text('VALUE=1\n')
    before=build_digest(tmp_path)
    launcher.write_text('VALUE=2\n',encoding='utf-8')
    assert build_digest(tmp_path)!=before


@pytest.mark.parametrize('name',['durable/schema.sql','alpha-decision.schema.json',
    'trade-decision.schema.json','mvp/strategy.json'])
def test_judicator_build_digest_binds_every_runtime_authority_artifact(tmp_path,name):
    package=tmp_path/'src'/'autonomous_trader';package.mkdir(parents=True)
    (package/'durable').mkdir();(package/'mvp').mkdir();scripts=tmp_path/'scripts';scripts.mkdir()
    (package/'kernel.py').write_text('VALUE=1\n')
    for artifact in ('durable/schema.sql','alpha-decision.schema.json','trade-decision.schema.json','mvp/strategy.json'):
        path=package/artifact;path.write_text('VALUE=1\n')
    for artifact in ('alpha-decision.schema.json','trade-decision.schema.json'):
        (tmp_path/artifact).write_text('VALUE=1\n')
    (tmp_path/'pyproject.toml').write_text('VALUE=1\n');(tmp_path/'requirements.txt').write_text('webull-openapi-python-sdk==3.0.0\n')
    for script in ('run_real_trader.py','run_real_supervisor.py','webull_real_readiness_packet.py',
            'webull_real_after_hours_account_packet.py'):
        (scripts/script).write_text('VALUE=1\n')
    (scripts/'run_ai_trader.py').write_text('VALUE=1\n');(scripts/'run_trading_cycle.py').write_text('VALUE=1\n')
    before=build_digest(tmp_path);(package/name).write_text('VALUE=2\n')
    assert build_digest(tmp_path)!=before


@pytest.mark.parametrize('existing',[False,True])
def test_compose_wires_one_real_runner_only_after_go_and_current_flat_packet(monkeypatch,existing):
    events=[];binding='real-'+'a'*64;packet=object();credentials=object();lock=SimpleNamespace(close=lambda:None)
    class Gate:
        @staticmethod
        def offline_receipt(*args,**kwargs):events.append('offline-go');return {'account_binding':binding}
        @staticmethod
        def market_authority(*args):events.append('market-authority');return {'entitlement_evidence_sha256':'a'*64}
        def __init__(self,*args):events.append('gate-init')
        def approved(self,*args):events.append('go')
        def check(self,value,now,initial=False):
            assert value is packet;events.append('current-flat' if initial else 'current-reconciled')
        def read_checked(self,fetch,clock,**kwargs):
            # Current runtime: authority audit, then fetch, then the same current check.
            value=fetch();self.check(value,clock());return value,None
    class Source:
        account_id='broker-account'
        def __init__(self,*args,**kwargs):
            assert args[0] is credentials and args[1]==binding
            assert kwargs['market_authority']['entitlement_evidence_sha256']=='a'*64;events.append('source')
        def discover(self):events.append('discover')
        def bind_account(self):events.append('account-bound')
        def account_observation(self):
            events.append('account-observation');return {'positions':(), 'open_client_ids':(),
                'cash':{'cash_balance':'0'}}
        def refresh(self,*args):events.append('refresh');return packet
    monkeypatch.setattr(subject,'RealObservationLedger',ObservationLedger)
    monkeypatch.setattr(subject,'RealGate',Gate)
    monkeypatch.setattr(subject,'load_real_credentials',lambda root:(events.append('credentials') or credentials))
    monkeypatch.setattr(subject,'RealMarketSource',Source)
    monkeypatch.setattr(subject,'LocalDatabase',lambda *args,**kwargs:SimpleNamespace(schema=kwargs.get('schema')))
    monkeypatch.setattr(subject,'migrate',lambda admin:events.append('migrate'))
    monkeypatch.setattr(subject,'grant_writer',lambda *args:events.append('grant'))
    monkeypatch.setattr(subject,'acquire_writer_lock',lambda admin:(events.append('lock') or lock))
    def absent(*args):raise subject.Rejected('UNKNOWN_ACCOUNT')
    clean={'positions':{},'orders':{},'cash':{'settled_usd':'0','unsettled_usd':'0'}}
    ledger=SimpleNamespace(execution_scope='REAL',snapshot=(lambda *args:clean) if existing else absent,
        rebuild=lambda *args:events.append('replay'))
    ledger.rebuild=lambda *args:(events.append('replay') or clean)
    monkeypatch.setattr(subject,'Ledger',lambda *args,**kwargs:ledger)
    controller=SimpleNamespace(account_id=binding,ledger=ledger,state=clean,
        refresh=lambda value:events.append('controller-refresh'))
    monkeypatch.setattr(subject,'RealController',lambda *args:(events.append('recovered-controller') or controller))
    monkeypatch.setattr(subject,'open_real_ledger',lambda *args,**kwargs:(events.append('ledger') or controller))
    store=object();monkeypatch.setattr(subject,'provision',lambda schema:(events.append('store') or store))
    budget=object();monkeypatch.setattr(subject,'ModelBudget',lambda *args:(events.append('budget') or budget))
    monkeypatch.setattr(subject,'load_model_key',lambda *a:(events.append('model-key') or 'key'))
    transport=object();monkeypatch.setattr(subject,'RealStrategyTransport',lambda *args:(events.append('transport') or transport))
    gateway=object();monkeypatch.setattr(subject,'RealExecutionGateway',lambda *args:(events.append('gateway') or gateway))
    runner=SimpleNamespace();monkeypatch.setattr(subject,'RealRunner',lambda *args:(events.append('runner') or runner))
    receipt={'account_binding':binding}
    assert subject.compose(supervisor_capability=capability(receipt))==(runner,store,lock)
    assert events[0]=='offline-go'
    assert events.index('go')<events.index('credentials')
    if existing:
        assert 'current-flat' not in events
        assert events.index('replay')<events.index('account-observation')<events.index('runner')
        assert controller.recovery_only is False
    else:
        assert events.index('credentials')<events.index('current-flat')<events.index('ledger')
        assert controller.recovery_only is False
    assert events[-3:]==['transport','gateway','runner']


def test_supervisor_cannot_spawn_or_create_state_before_offline_go(monkeypatch,tmp_path):
    import scripts.run_real_supervisor as supervisor
    from autonomous_trader import real_owner_trust
    monkeypatch.setattr(supervisor,'ROOT',tmp_path)
    def denied(*args,**kwargs):raise ValueError('REAL_OWNER_SIGNATURE_INVALID')
    monkeypatch.setattr(real_owner_trust,'read_protected_authorization',denied)
    monkeypatch.setattr(supervisor.RealGate,'offline_receipt',denied)
    monkeypatch.setattr(supervisor.subprocess,'Popen',lambda *args,**kwargs:pytest.fail('spawn before GO'))
    with pytest.raises(ValueError,match='REAL_OWNER_SIGNATURE_INVALID'):supervisor.main()
    assert not (tmp_path/'.local').exists()


def test_supervisor_reads_no_mutable_marker_before_offline_go(monkeypatch,tmp_path):
    import scripts.run_real_supervisor as supervisor
    events=[];monkeypatch.setattr(supervisor,'ROOT',tmp_path)
    monkeypatch.setattr(supervisor,'_read',lambda path:(events.append('marker-read') or None))
    monkeypatch.setattr(supervisor.RealGate,'offline_receipt',lambda *args,**kwargs:
        (_ for _ in ()).throw(ValueError('JUDICATOR_REAL_GO_REQUIRED')))
    with pytest.raises(ValueError):supervisor._authority(datetime.now(timezone.utc))
    assert events==[]


@pytest.mark.parametrize('embedded',[False,True])
def test_real_child_requires_and_consumes_one_use_supervisor_handle(monkeypatch,tmp_path,embedded):
    binding='real-'+'a'*64;now=datetime.now(timezone.utc);nonce='private-supervisor-nonce'
    monkeypatch.setattr(subject,'ROOT',tmp_path)
    monkeypatch.setattr(subject,'_parent_is_supervisor',lambda pid:True)
    monkeypatch.setattr(subject,'_has_supervisor_ancestor',lambda pid:True)
    folder=tmp_path/'.local/real-supervisor';folder.mkdir(parents=True)
    handle=folder/'child-handle-test.json'
    receipt={'build_sha256':'build','account_binding':binding,'market_session_label':'2026-09-29'}
    value={'schema':'REAL_SUPERVISOR_CHILD_HANDLE_V1','build':'build',
        'account_binding':binding,'authority_session':'2026-09-29','recovery':False,
        'expires_at':(now+timedelta(minutes=1)).isoformat(),
        'nonce_sha256':hashlib.sha256(nonce.encode()).hexdigest(),'supervisor_pid':os.getppid()}
    if embedded:value['startup_receipt']=receipt
    handle.write_text(json.dumps(value))
    read_fd,write_fd=os.pipe();os.write(write_fd,nonce.encode());os.close(write_fd)
    subject.consume_supervisor_handle(handle,None if embedded else receipt,recovery=False,now=now,
        pipe_handle=msvcrt.get_osfhandle(read_fd))
    assert not handle.exists() and handle.with_suffix('.consumed').exists()
    with pytest.raises(ValueError,match='REAL_SUPERVISOR_HANDLE_INVALID|REAL_SUPERVISOR_HANDLE_CONSUMED|REAL_SUPERVISOR_PIPE_REQUIRED'):
        subject.consume_supervisor_handle(handle,receipt,recovery=False,now=now,pipe_handle=None)


def test_caller_created_handle_without_supervisor_pipe_cannot_issue_capability(monkeypatch,tmp_path):
    monkeypatch.setattr(subject,'ROOT',tmp_path);folder=tmp_path/'.local/real-supervisor';folder.mkdir(parents=True)
    handle=folder/'child-handle-forged.json';handle.write_text('{}')
    with pytest.raises(ValueError,match='REAL_SUPERVISOR_PIPE_REQUIRED'):
        subject.consume_supervisor_handle(handle,{},recovery=False,now=datetime.now(timezone.utc))


def test_caller_created_pipe_and_nonce_cannot_self_issue(monkeypatch,tmp_path):
    binding='real-'+'a'*64;now=datetime.now(timezone.utc);nonce='forged'
    monkeypatch.setattr(subject,'ROOT',tmp_path);monkeypatch.setattr(subject,'_parent_is_supervisor',lambda pid:False)
    folder=tmp_path/'.local/real-supervisor';folder.mkdir(parents=True);handle=folder/'child-handle-forged.json'
    receipt={'build_sha256':'build','account_binding':binding,'market_session_label':'2026-09-29'}
    handle.write_text(json.dumps({'schema':'REAL_SUPERVISOR_CHILD_HANDLE_V1','build':'build',
        'account_binding':binding,'authority_session':'2026-09-29','recovery':False,
        'expires_at':(now+timedelta(minutes=1)).isoformat(),'nonce_sha256':hashlib.sha256(nonce.encode()).hexdigest(),
        'supervisor_pid':os.getppid()}))
    read_fd,write_fd=os.pipe();os.write(write_fd,nonce.encode());os.close(write_fd)
    with pytest.raises(ValueError,match='REAL_SUPERVISOR_HANDLE_INVALID'):
        subject.consume_supervisor_handle(handle,receipt,recovery=False,now=now,
            pipe_handle=msvcrt.get_osfhandle(read_fd))


def test_venv_redirector_parent_can_consume_nonce_bound_live_supervisor_handle(monkeypatch,tmp_path):
    binding='real-'+'a'*64;now=datetime.now(timezone.utc);nonce='redirected-child-nonce';supervisor_pid=4242
    monkeypatch.setattr(subject,'ROOT',tmp_path)
    monkeypatch.setattr(subject.os,'getppid',lambda:9999)
    monkeypatch.setattr(subject,'_parent_is_supervisor',lambda pid:pid==supervisor_pid)
    monkeypatch.setattr(subject,'_has_supervisor_ancestor',lambda pid:pid==supervisor_pid)
    folder=tmp_path/'.local/real-supervisor';folder.mkdir(parents=True);handle=folder/'child-handle-redirected.json'
    receipt={'build_sha256':'build','account_binding':binding,'market_session_label':'2026-09-30'}
    handle.write_text(json.dumps({'schema':'REAL_SUPERVISOR_CHILD_HANDLE_V1','build':'build',
        'account_binding':binding,'authority_session':'2026-09-30','recovery':False,
        'expires_at':(now+timedelta(minutes=1)).isoformat(),
        'nonce_sha256':hashlib.sha256(nonce.encode()).hexdigest(),'supervisor_pid':supervisor_pid}))
    read_fd,write_fd=os.pipe();os.write(write_fd,nonce.encode());os.close(write_fd)
    capability=subject.consume_supervisor_handle(handle,receipt,recovery=False,now=now,
        pipe_handle=msvcrt.get_osfhandle(read_fd))
    assert capability.receipt==receipt and not handle.exists() and handle.with_suffix('.consumed').exists()


@pytest.mark.parametrize('embedded',[False,True])
@pytest.mark.parametrize(('mutation','error'),[
    ('wrong-nonce','REAL_SUPERVISOR_HANDLE_INVALID'),('dead-supervisor','REAL_SUPERVISOR_HANDLE_INVALID'),
    ('wrong-ancestry','REAL_SUPERVISOR_HANDLE_INVALID'),
    ('child-pid','REAL_SUPERVISOR_HANDLE_INVALID'),('expired','REAL_SUPERVISOR_HANDLE_INVALID'),
    ('build','REAL_SUPERVISOR_HANDLE_INVALID'),('account','REAL_SUPERVISOR_HANDLE_INVALID'),
    ('session','REAL_SUPERVISOR_HANDLE_INVALID'),('recovery','REAL_SUPERVISOR_HANDLE_INVALID')])
def test_redirector_does_not_weaken_supervisor_handle_bindings(monkeypatch,tmp_path,mutation,error,embedded):
    binding='real-'+'a'*64;now=datetime.now(timezone.utc);nonce='bound-nonce';supervisor_pid=4242
    monkeypatch.setattr(subject,'ROOT',tmp_path)
    monkeypatch.setattr(subject,'_parent_is_supervisor',lambda pid:pid==supervisor_pid and mutation!='dead-supervisor')
    monkeypatch.setattr(subject,'_has_supervisor_ancestor',lambda pid:pid==supervisor_pid and mutation!='wrong-ancestry')
    folder=tmp_path/'.local/real-supervisor';folder.mkdir(parents=True);handle=folder/'child-handle-bound.json'
    receipt={'build_sha256':'build','account_binding':binding,'market_session_label':'2026-09-30'}
    value={'schema':'REAL_SUPERVISOR_CHILD_HANDLE_V1','build':'build','account_binding':binding,
        'authority_session':'2026-09-30','recovery':False,'expires_at':(now+timedelta(minutes=1)).isoformat(),
        'nonce_sha256':hashlib.sha256(nonce.encode()).hexdigest(),'supervisor_pid':supervisor_pid}
    if mutation=='wrong-nonce':value['nonce_sha256']=hashlib.sha256(b'other').hexdigest()
    elif mutation=='child-pid':value['supervisor_pid']=os.getpid()
    elif mutation=='expired':value['expires_at']=(now-timedelta(seconds=1)).isoformat()
    elif mutation=='build':value['build']='other'
    elif mutation=='account':value['account_binding']='real-'+'b'*64
    elif mutation=='session':value['authority_session']='2026-09-29'
    elif mutation=='recovery':value['recovery']=True
    if embedded:value['startup_receipt']=receipt
    handle.write_text(json.dumps(value))
    read_fd,write_fd=os.pipe();os.write(write_fd,nonce.encode());os.close(write_fd)
    with pytest.raises(ValueError,match=error):
        subject.consume_supervisor_handle(handle,None if embedded else receipt,recovery=False,now=now,
            pipe_handle=msvcrt.get_osfhandle(read_fd))
    assert handle.exists() and not handle.with_suffix('.consumed').exists()


@pytest.mark.parametrize(('parents','expected'),[
    ({9000:4242},True),
    ({9000:8000,8000:4242},True),
    ({9000:8000,8000:7000,7000:4242},True),
    ({9000:8000,8000:7000,7000:6000,6000:4242},False),
    ({9000:8000,8000:9000},False),
    ({9000:0},False)])
def test_supervisor_ancestry_accepts_only_bounded_direct_or_redirector_chain(monkeypatch,parents,expected):
    monkeypatch.setattr(subject,'_process_parent_pid',lambda pid:parents.get(pid))
    assert subject._has_supervisor_ancestor(4242,child_pid=9000)==expected


@pytest.mark.skipif(os.name!='nt',reason='Windows command-line normalization')
@pytest.mark.parametrize('script',[
    r'scripts\run_real_supervisor.py',
    str((subject.ROOT/'scripts'/'run_real_supervisor.py').resolve())])
def test_supervisor_parent_accepts_exact_relative_or_absolute_script_path(script):
    command=__import__('subprocess').list2cmdline((subject.sys.executable,'-I','-B',script))
    assert subject._is_supervisor_command_line(command)


@pytest.mark.skipif(os.name!='nt',reason='Windows command-line normalization')
@pytest.mark.parametrize('parts',[
    (subject.sys.executable,r'scripts\run_real_trader.py'),
    (subject.sys.executable,r'scripts\run_real_supervisor.py','--recovery'),
    (subject.sys.executable,'-c','print(1)'),
    (r'C:\Windows\py.exe',r'scripts\run_real_supervisor.py'),
    (subject.sys.executable,r'..\run_real_supervisor.py')])
def test_supervisor_parent_rejects_wrong_ambiguous_or_extra_command(parts):
    command=__import__('subprocess').list2cmdline((parts[0],'-I','-B',*parts[1:]))
    assert not subject._is_supervisor_command_line(command)


@pytest.mark.skipif(os.name!='nt',reason='Windows command-line normalization')
def test_supervisor_parent_rejects_empty_or_malformed_command():
    assert not subject._is_supervisor_command_line('')
    assert not subject._is_supervisor_command_line('"')


def test_recovery_step_precedes_dashboard_and_heartbeat_start():
    import inspect
    text=inspect.getsource(subject.main)
    step="progress['result']=step()" if "progress['result']=step()" in text else "progress['result']=runner.step()"
    assert text.index(step) < text.index('heartbeat(runtime_status(runner,progress))')
    assert text.index('heartbeat(runtime_status(runner,progress))') < text.index('server=start_optional_dashboard')


@pytest.mark.skipif(os.name!='nt',reason='Windows command-line normalization')
@pytest.mark.parametrize('flags',[(),('-I',),('-B',),('-B','-I')])
def test_supervisor_parent_requires_exact_isolation_flags(flags):
    import subprocess
    command=subprocess.list2cmdline((subject.sys.executable,*flags,str(subject.ROOT/'scripts/run_real_supervisor.py')))
    assert not subject._is_supervisor_command_line(command)


def test_initial_uncertain_recovery_is_never_published_running():
    runner=SimpleNamespace(controller=SimpleNamespace(recovery_only=True,state={
        'positions':{'x':{}},'orders':{},'latches':{},'incidents':{}}))
    assert subject.runtime_status(runner,{'result':'REAL_ORDER_TRANSPORT_UNCERTAIN'})=='RECOVERY_UNRESOLVED'
    runner.controller.state['positions']={}
    assert subject.runtime_status(runner,{'result':'RECOVERY_FLAT_RECONCILED'})=='RECOVERY_RECONCILED'


def test_runtime_distribution_version_drift_changes_build(monkeypatch):
    import autonomous_trader.real_trading as trading
    root=subject.ROOT;before=trading.build_digest(root)
    original=trading.distribution_version
    monkeypatch.setattr(trading,'distribution_version',lambda name:'999.0.0' if name=='webull-openapi-python-sdk' else original(name))
    with pytest.raises(ValueError,match='REAL_RUNTIME_DISTRIBUTION_MISMATCH'):trading.build_digest(root)


@pytest.mark.parametrize('relative',['pyproject.toml','requirements.txt','scripts/run_ai_trader.py',
    'scripts/run_trading_cycle.py','scripts/webull_real_liquidation.py','scripts/review_real_liquidation.ps1'])
def test_manifest_and_every_executable_script_are_build_bound(tmp_path,relative):
    root=subject.ROOT
    shutil.copytree(root/'src'/'autonomous_trader',tmp_path/'src'/'autonomous_trader')
    (tmp_path/'scripts').mkdir()
    for suffix in ('*.py','*.ps1','*.cmd'):
        for path in (root/'scripts').glob(suffix):shutil.copy2(path,tmp_path/'scripts'/path.name)
    for name in ('pyproject.toml','requirements.txt','alpha-decision.schema.json','trade-decision.schema.json'):
        shutil.copy2(root/name,tmp_path/name)
    before=build_digest(tmp_path);path=tmp_path/relative
    path.write_text(path.read_text(encoding='utf-8')+'\n# mutation\n',encoding='utf-8')
    assert build_digest(tmp_path)!=before


def test_sandbox_and_real_writer_locks_exclude_each_other_both_directions():
    import scripts.run_trading_cycle as sandbox
    assert sandbox.WRITER_LOCK==subject.WRITER_LOCK
    class Shared:
        held=False
    class Result:
        def __init__(self,value):self.value=value
        def fetchone(self):return (self.value,)
    class Connection:
        autocommit=False
        def __init__(self):self.owns=False
        def execute(self,query,args):
            value=not Shared.held
            if value:Shared.held=True;self.owns=True
            return Result(value)
        def close(self):
            if self.owns:Shared.held=False;self.owns=False
    admin=SimpleNamespace(connect=lambda:Connection())
    real=subject.acquire_writer_lock(admin)
    with pytest.raises(ValueError,match='RUNNER_ALREADY_ACTIVE'):sandbox.acquire_writer_lock(admin)
    real.close();sand=sandbox.acquire_writer_lock(admin)
    with pytest.raises(ValueError,match='REAL_RUNNER_ALREADY_ACTIVE'):subject.acquire_writer_lock(admin)
    sand.close()


def test_sandbox_one_cycle_locks_before_credentials_or_transport():
    text=(subject.ROOT/'scripts'/'run_trading_cycle.py').read_text(encoding='utf-8')
    assert text.index('lock=acquire_writer_lock(admin)')<text.index('credentials=load_credentials(ROOT)')
    assert text.index('lock=acquire_writer_lock(admin)')<text.index('SandboxTransport(credentials)')


@pytest.mark.parametrize('name',['webull_m3b_first_order.py','webull_m3b_round_trip.py'])
def test_legacy_order_harness_locks_before_credentials_and_network(name):
    text=(subject.ROOT/'scripts'/name).read_text(encoding='utf-8')
    assert 'WRITER_LOCK=17482026' in text
    assert text.index('writer_lock=acquire_writer_lock()')<text.index('credentials = load_credentials(ROOT)')


def test_recovery_controller_cannot_admit_entry():
    from autonomous_trader.real_trading import RealController
    controller=object.__new__(RealController);controller.recovery_only=True
    with pytest.raises(ValueError,match='REAL_RECOVERY_REDUCE_ONLY'):
        controller.admit(SimpleNamespace(side='BUY'),None)


def test_supervisor_restart_is_reduce_only_and_can_continue_after_close(monkeypatch,tmp_path):
    import scripts.run_real_supervisor as supervisor
    binding='real-'+'a'*64
    receipt={'build_sha256':'build','account_binding':binding,'market_session_label':'2026-09-29',
        'expires_at':'2026-09-29T20:00:00+00:00','recovery_not_after':'2026-10-06T15:00:00+00:00'}
    monkeypatch.setattr(supervisor,'ROOT',tmp_path)
    marker=tmp_path/'.local/real-supervisor/launched.json';marker.parent.mkdir(parents=True)
    marker.write_text(__import__('json').dumps({'build':'build','account_binding':binding,
        'authority_session':'2026-09-29'}))
    calls=[]
    def offline(root,now,**kwargs):
        calls.append(kwargs.get('recovery',False))
        if not kwargs.get('recovery'):raise ValueError('REGULAR_SESSION_REQUIRED')
        return receipt
    monkeypatch.setattr(supervisor.RealGate,'offline_receipt',offline)
    value,recovery=supervisor._authority(__import__('datetime').datetime(2026,9,30,14,tzinfo=__import__('datetime').timezone.utc))
    assert value==receipt and recovery is True and calls==[False,True]


def test_supervisor_never_restarts_crashed_child_with_entry_authority(monkeypatch,tmp_path):
    import scripts.run_real_supervisor as supervisor
    receipt={'build_sha256':'build','account_binding':'real-'+'a'*64,'market_session_label':'2026-09-29',
        'expires_at':'2026-09-29T20:00:00+00:00','recovery_not_after':'2026-10-06T15:00:00+00:00'}
    monkeypatch.setattr(supervisor,'ROOT',tmp_path);monkeypatch.setattr(supervisor,'_authority',lambda now:(receipt,False))
    monkeypatch.setattr(supervisor.RealGate,'offline_receipt',lambda *args,**kwargs:receipt)
    monkeypatch.setattr(supervisor.time,'sleep',lambda seconds:None)
    commands=[]
    class Child:
        def __init__(self,polls,code,pid):self.polls,self.code,self.pid=list(polls),code,pid
        def poll(self):return self.polls.pop(0) if self.polls else self.code
        def wait(self,timeout=None):return self.code
    children=[Child((),2,101),Child((None,),0,102)]
    def popen(command,**kwargs):commands.append(command);return children.pop(0)
    monkeypatch.setattr(supervisor.subprocess,'Popen',popen)
    # The supervisor launches through the Windows job-object wrapper; this test
    # covers restart policy, so the wrapper itself is replaced.
    monkeypatch.setattr(supervisor,'spawn',lambda command,**kwargs:popen(command,**kwargs))
    monkeypatch.setattr(supervisor,'confirm_stop',lambda child:None)
    original_read=supervisor._read
    stopped=supervisor_heartbeat(datetime.now(timezone.utc),pid=102,status='STOPPED',production_enabled=False,
        market_session_label='2026-09-29')
    monkeypatch.setattr(supervisor,'_read',lambda path:(stopped
        if path.name=='child-heartbeat.json' else original_read(path)))
    monkeypatch.setattr(supervisor,'_owned_heartbeat',lambda value,pid,*args,**kwargs:value.get('pid')==pid)
    monkeypatch.setattr(supervisor,'_heartbeat_matches',lambda *args,**kwargs:True)
    supervisor.main()
    assert '--recovery' not in commands[0] and '--recovery' in commands[1]


def supervisor_heartbeat(now,**changes):
    value={'schema':'REAL_CHILD_HEARTBEAT_V1','pid':202,'at':now.isoformat(),'status':'RUNNING','mode':'REAL',
        'production_enabled':True,'account_id':'real-'+'a'*64,'ledger_schema':'real_trader',
        'activity':'Supervised REAL runner','recovery_only':False,'authority_build_sha256':'build',
        'market_session_label':'2026-09-30','reconciliation_ready':True,'unresolved':False,
        'last_step_result':'STARTING','last_step_at':now.isoformat()}
    value.update(changes);return value


@pytest.mark.parametrize(('snapshot','expected'),[
    ({202:(101,'python.exe')},True),
    ({202:(150,'python.exe'),150:(101,'python.exe')},True),
    ({202:(150,'python.exe'),150:(140,'python.exe'),140:(101,'python.exe')},True),
    ({202:(150,'python.exe'),150:(140,'python.exe'),140:(130,'python.exe'),130:(101,'python.exe')},False),
    ({202:(999,'python.exe')},False),
    ({202:(150,'python.exe'),150:(202,'python.exe')},False)])
def test_supervisor_heartbeat_accepts_only_bounded_live_child_chain(snapshot,expected):
    import scripts.run_real_supervisor as supervisor
    now=datetime.now(timezone.utc);receipt={'build_sha256':'build','account_binding':'real-'+'a'*64,
        'market_session_label':'2026-09-30'}
    assert supervisor._owned_heartbeat(supervisor_heartbeat(now),101,receipt,False,now,snapshot)==expected


@pytest.mark.parametrize(('change','value'),[
    ('authority_build_sha256','other'),('account_id','real-'+'b'*64),('market_session_label','2026-09-29'),
    ('mode','SANDBOX'),('activity','Other'),('recovery_only',True),('production_enabled',False),
    ('at','STALE'),('extra','unexpected')])
def test_supervisor_heartbeat_rejects_wrong_role_authority_or_stale_evidence(change,value):
    import scripts.run_real_supervisor as supervisor
    now=datetime.now(timezone.utc);receipt={'build_sha256':'build','account_binding':'real-'+'a'*64,
        'market_session_label':'2026-09-30'};heartbeat=supervisor_heartbeat(now)
    if change=='at':heartbeat['at']=(now-timedelta(seconds=21)).isoformat()
    else:heartbeat[change]=value
    assert not supervisor._owned_heartbeat(heartbeat,101,receipt,False,now,{202:(101,'python.exe')})


def test_supervisor_cleanup_terminates_only_bounded_python_descendants(monkeypatch):
    import scripts.run_real_supervisor as supervisor
    snapshot={202:(150,'python.exe'),150:(101,'python.exe'),303:(202,'helper.exe'),
        404:(303,'python.exe'),505:(999,'python.exe')}
    killed=[]
    monkeypatch.setattr(supervisor,'_process_snapshot',lambda:snapshot)
    monkeypatch.setattr(supervisor.os,'kill',lambda pid,signal:killed.append(pid))
    child=SimpleNamespace(pid=101,terminate=lambda:killed.append(101),wait=lambda timeout=None:0,
        kill=lambda:killed.append('force'))
    supervisor._stop_child(child)
    assert killed==[202,150,101]
