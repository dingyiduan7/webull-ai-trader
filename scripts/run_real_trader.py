"""Supervised REAL composition root. Absent exact Judicator GO, nothing is initialized."""
import argparse,hashlib,json,os,sys,threading,queue,time,traceback,warnings
warnings.filterwarnings('ignore',message='Pydantic serializer warnings',category=UserWarning)
import msvcrt
from datetime import datetime,timezone,timedelta
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'src'))

from autonomous_trader.runtime_storage import data_root
from autonomous_trader.real_owner_trust import observe_validation_progress
from autonomous_trader.decision_api.brain import DECISION_MODEL,load_model_key,make_brain
from autonomous_trader.durable.ledger import LocalDatabase,Ledger,migrate,grant_writer
from autonomous_trader.monitoring.model_budget import ModelBudget
from autonomous_trader.monitoring.server import DashboardServer
from autonomous_trader.monitoring.store import provision
from autonomous_trader.monitoring.views import DashboardReader
from autonomous_trader.real_market import RealMarketSource
from autonomous_trader.real_waiting import service_receipt,market_unavailable,clean_flat,recoverable_flat_incident,market_authority as observation_market_authority
from autonomous_trader.real_runner import RealRunner
from autonomous_trader.real_read_budget import ReadRateLimited
from autonomous_trader.real_secrets import load_real_credentials
from autonomous_trader.real_state import RealObservationLedger
from autonomous_trader.real_strategy_broker import RealStrategyTransport
from autonomous_trader.real_strategy_gateway import RealExecutionGateway
from autonomous_trader.real_trading import RealGate,RealController,open_real_ledger
from autonomous_trader.durable.projection import Rejected

# Shared with the SANDBOX runner: REAL and SANDBOX may never both own the
# process-wide trading writer on the same database cluster.
WRITER_LOCK=17482026
_SUPERVISOR_SENTINEL=object()
_ISSUED_SUPERVISOR_CAPABILITIES=set()


def _parent_is_supervisor(pid):
    """Bind the inherited pipe to the actual supervisor process command line."""
    try:
        import ctypes
        from ctypes import wintypes
        process=ctypes.windll.kernel32.OpenProcess(0x1000|0x0010,False,int(pid))
        if not process:return False
        try:
            needed=wintypes.ULONG()
            ctypes.windll.ntdll.NtQueryInformationProcess(process,60,None,0,ctypes.byref(needed))
            if not needed.value:return False
            buffer=ctypes.create_string_buffer(needed.value)
            if ctypes.windll.ntdll.NtQueryInformationProcess(process,60,buffer,needed,ctypes.byref(needed))!=0:return False
            class UNICODE_STRING(ctypes.Structure):
                _fields_=(('Length',wintypes.USHORT),('MaximumLength',wintypes.USHORT),('Buffer',ctypes.c_void_p))
            value=ctypes.cast(buffer,ctypes.POINTER(UNICODE_STRING)).contents
            command=ctypes.wstring_at(value.Buffer,value.Length//2)
            return _is_supervisor_command_line(command)
        finally:ctypes.windll.kernel32.CloseHandle(process)
    except Exception:return False


def _is_supervisor_command_line(command):
    """Accept only this environment's Python invoking the one supervisor script."""
    if not isinstance(command,str) or not command.strip() or os.name!='nt':return False
    import ctypes
    argc=ctypes.c_int()
    ctypes.windll.shell32.CommandLineToArgvW.restype=ctypes.POINTER(ctypes.c_wchar_p)
    argv=ctypes.windll.shell32.CommandLineToArgvW(command,ctypes.byref(argc))
    if not argv:return False
    try:parts=[argv[index] for index in range(argc.value)]
    finally:ctypes.windll.kernel32.LocalFree(argv)
    if parts[1:3]!=['-I','-B']:return False
    parts=[parts[0],*parts[3:]]
    if len(parts)!=2:
        if len(parts) not in (4,6,8,10,12):return False
        options=dict(zip(parts[2::2],parts[3::2]))
        if (len(options)!=(len(parts)-2)//2 or not set(options)<={'--sme-advisory-file','--sme-advisory-sha256','--sme-authorization-db','--sme-advisory-provider','--sme-advisory-provider-sha256'}
                or any(not value or value.startswith('--') for value in options.values())):return False
    def normalized(value):
        path=Path(value)
        if not path.is_absolute():path=ROOT/path
        return os.path.normcase(str(path.resolve(strict=False)))
    return (normalized(parts[0])==normalized(sys.executable)
        and normalized(parts[1])==normalized(ROOT/'scripts/run_real_supervisor.py'))


def _process_parent_pid(pid):
    """Read one Windows process-tree edge without trusting mutable command text."""
    if os.name!='nt':return None
    import ctypes
    from ctypes import wintypes
    class PROCESSENTRY32W(ctypes.Structure):
        _fields_=(('dwSize',wintypes.DWORD),('cntUsage',wintypes.DWORD),
            ('th32ProcessID',wintypes.DWORD),('th32DefaultHeapID',ctypes.c_void_p),
            ('th32ModuleID',wintypes.DWORD),('cntThreads',wintypes.DWORD),
            ('th32ParentProcessID',wintypes.DWORD),('pcPriClassBase',wintypes.LONG),
            ('dwFlags',wintypes.DWORD),('szExeFile',wintypes.WCHAR*260))
    snapshot=ctypes.windll.kernel32.CreateToolhelp32Snapshot(0x00000002,0)
    if snapshot in (0,-1):return None
    try:
        entry=PROCESSENTRY32W();entry.dwSize=ctypes.sizeof(entry)
        found=ctypes.windll.kernel32.Process32FirstW(snapshot,ctypes.byref(entry))
        while found:
            if entry.th32ProcessID==int(pid):return int(entry.th32ParentProcessID)
            found=ctypes.windll.kernel32.Process32NextW(snapshot,ctypes.byref(entry))
        return None
    finally:ctypes.windll.kernel32.CloseHandle(snapshot)


def _has_supervisor_ancestor(supervisor_pid,child_pid=None,max_hops=3):
    """Permit only the direct parent or the bounded Windows venv redirector chain."""
    current=os.getpid() if child_pid is None else int(child_pid);seen={current}
    for _ in range(max_hops):
        parent=_process_parent_pid(current)
        if not parent or parent in seen:return False
        if parent==supervisor_pid:return True
        seen.add(parent);current=parent
    return False


class _SupervisorCapability:
    def __init__(self,sentinel,receipt,recovery,expires):
        if sentinel is not _SUPERVISOR_SENTINEL:raise ValueError('REAL_SUPERVISOR_CAPABILITY_REQUIRED')
        self.receipt=dict(receipt);self.recovery=bool(recovery);self.expires=expires;self._consumed=False

    def consume(self,now,recovery):
        if self._consumed or self.recovery is not bool(recovery) or now>self.expires:
            raise ValueError('REAL_SUPERVISOR_CAPABILITY_INVALID')
        self._consumed=True
        return self.receipt


def consume_supervisor_handle(path,receipt,*,recovery,now,pipe_handle=None):
    if not path:raise ValueError('REAL_SUPERVISOR_HANDLE_REQUIRED')
    folder=(data_root(ROOT)/'.local/real-supervisor').resolve();candidate=Path(path).resolve()
    if candidate.parent!=folder or not candidate.name.startswith('child-handle-') or candidate.suffix!='.json':
        raise ValueError('REAL_SUPERVISOR_HANDLE_INVALID')
    if pipe_handle is None:raise ValueError('REAL_SUPERVISOR_PIPE_REQUIRED')
    try:
        descriptor=msvcrt.open_osfhandle(int(pipe_handle),os.O_RDONLY)
        with os.fdopen(descriptor,'rb',closefd=True) as source:nonce=source.read(256).decode('ascii')
    except (OSError,ValueError,UnicodeError):raise ValueError('REAL_SUPERVISOR_PIPE_INVALID') from None
    if not nonce:raise ValueError('REAL_SUPERVISOR_PIPE_INVALID')
    try:value=json.loads(candidate.read_text(encoding='utf-8'));expires=datetime.fromisoformat(value['expires_at'])
    except (OSError,ValueError,KeyError):raise ValueError('REAL_SUPERVISOR_HANDLE_INVALID') from None
    required={'schema','build','account_binding','authority_session','recovery','expires_at','nonce_sha256','supervisor_pid'}
    if 'startup_receipt' in value:required.add('startup_receipt')
    if receipt is None:receipt=value.get('startup_receipt')
    if not isinstance(receipt,dict) or not {'build_sha256','account_binding','market_session_label'}<=set(receipt):
        raise ValueError('REAL_SUPERVISOR_HANDLE_INVALID')
    supervisor_pid=value.get('supervisor_pid')
    if (set(value)!=required or value['schema']!='REAL_SUPERVISOR_CHILD_HANDLE_V1'
            or value['build']!=receipt['build_sha256'] or value['account_binding']!=receipt['account_binding']
            or value['authority_session']!=receipt['market_session_label'] or value['recovery'] is not bool(recovery)
            or not isinstance(supervisor_pid,int) or supervisor_pid<=0 or supervisor_pid==os.getpid()
            or not _parent_is_supervisor(supervisor_pid) or not _has_supervisor_ancestor(supervisor_pid)
            or expires.tzinfo is None or not now<=expires<=now+__import__('datetime').timedelta(minutes=2)
            or value['nonce_sha256']!=hashlib.sha256(nonce.encode()).hexdigest()):
        raise ValueError('REAL_SUPERVISOR_HANDLE_INVALID')
    consumed=candidate.with_suffix('.consumed')
    if consumed.exists():raise ValueError('REAL_SUPERVISOR_HANDLE_CONSUMED')
    try:os.replace(candidate,consumed)
    except OSError:raise ValueError('REAL_SUPERVISOR_HANDLE_CONSUMED') from None
    capability=_SupervisorCapability(_SUPERVISOR_SENTINEL,receipt,recovery,expires)
    _ISSUED_SUPERVISOR_CAPABILITIES.add(capability)
    return capability


_HEARTBEAT_WRITE_LOCK=threading.Lock()

def supervisor_heartbeat(payload):
    with _HEARTBEAT_WRITE_LOCK:_publish_supervisor_heartbeat(payload)

def _publish_supervisor_heartbeat(payload):
    folder=data_root(ROOT)/'.local/real-supervisor';folder.mkdir(parents=True,exist_ok=True)
    path=folder/'child-heartbeat.json';temporary=path.with_suffix('.tmp')
    with temporary.open('w',encoding='utf-8') as out:
        json.dump(payload,out,sort_keys=True,separators=(',',':'));out.flush();os.fsync(out.fileno())
    # Windows refuses to replace a file another process (dashboard, scanner) has open for a moment.
    # That is transient: retry briefly. Any other failure, or a persistent one, still raises.
    for attempt in range(40):
        try:os.replace(temporary,path);return
        except PermissionError:
            if attempt==39:raise
            time.sleep(0.05)


def record_pulse_failure(exc,where='pulse'):
    """Leave a trace of why the process ended (class and frames, never the message)."""
    try:
        frames=[f'{Path(f.filename).name}:{f.lineno}:{f.name}' for f in traceback.extract_tb(exc.__traceback__)[-6:]]
        folder=data_root(ROOT)/'.local/real-supervisor';folder.mkdir(parents=True,exist_ok=True)
        with (folder/'child-failure.log').open('a',encoding='utf-8') as out:
            out.write(json.dumps({'at':datetime.now(timezone.utc).isoformat(),'pid':os.getpid(),'where':where,
                'error_class':type(exc).__name__,'code':str(exc) if isinstance(exc,ValueError) and str(exc).isupper() else None,
                'frames':frames})+'\n')
    except Exception:pass


_LAST_STEP=[None]

def record_step(result):
    """Append one line whenever the step result changes, so every day can be explained afterwards."""
    if result==_LAST_STEP[0]:return
    _LAST_STEP[0]=result
    try:
        folder=data_root(ROOT)/'.local/real-supervisor';folder.mkdir(parents=True,exist_ok=True)
        with (folder/'step-history.jsonl').open('a',encoding='utf-8') as out:
            out.write(json.dumps({'at':datetime.now(timezone.utc).isoformat(),'pid':os.getpid(),'result':str(result)})+'\n')
    except Exception:pass

def acquire_writer_lock(admin):
    lock=admin.connect();lock.autocommit=True
    if not lock.execute('SELECT pg_try_advisory_lock(%s)',(WRITER_LOCK,)).fetchone()[0]:
        lock.close();raise ValueError('REAL_RUNNER_ALREADY_ACTIVE')
    return lock


CLOSED_DAY_RESULTS=('CLOSED_RECONCILED_WITH_INCIDENTS','CLOSED_UNRECONCILED')


def launch_lab_after_close(result,spawn=None):
    """Owner-approved 2026-10-20: the strategy lab runs automatically after each close (read-only daily bars,
    paper tracking, tomorrow's picks). Detached and best effort: it can never affect the trader or its exit."""
    if result not in ('CLOSED_RECONCILED',)+CLOSED_DAY_RESULTS:return False
    try:
        if spawn is None:
            import subprocess
            flags=(subprocess.CREATE_NO_WINDOW|subprocess.CREATE_NEW_PROCESS_GROUP|subprocess.DETACHED_PROCESS) if os.name=='nt' else 0
            spawn=lambda argv:subprocess.Popen(argv,cwd=str(ROOT),stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,creationflags=flags,close_fds=True)
        spawn([sys.executable,'-B',str(ROOT/'scripts'/'strategy_lab.py'),'--after-close'])
        return True
    except Exception:
        return False


def exposure_unresolved(state):
    """Open orders, or positions the signed charter does not allow to be carried."""
    from autonomous_trader.carry import carry_enabled
    return bool((state['positions'] and not carry_enabled())
        or any(not row['final'] for row in state['orders'].values()))


def runtime_status(runner,progress):
    state=runner.controller.state
    unsafe=bool(exposure_unresolved(state) or any(state.get('latches',{}).values()) or state.get('incidents'))
    if runner.controller.recovery_only:
        return 'RECOVERY_RECONCILED' if progress['result'] in ('RECOVERY_FLAT_RECONCILED','ENTRY_WINDOW_CLOSED_RECONCILED') and not unsafe else 'RECOVERY_UNRESOLVED'
    if getattr(runner.controller,'waiting_for_market_data',False) and not unsafe:return 'WAITING_FOR_MARKET_DATA'
    return 'INCIDENT' if unsafe else 'RUNNING'


def configured_brain(key,budget,approved_sme_advisory=None):
    brain=make_brain(key,model=DECISION_MODEL,reasoning='medium',
        approved_sme_advisory=approved_sme_advisory)
    brain.budget=budget
    return brain


def compose(clock=lambda:datetime.now(timezone.utc),*,recovery=False,supervisor_capability=None,
            approved_sme_advisory=None,startup_progress=lambda phase:None):
    now=clock()
    if (type(supervisor_capability) is not _SupervisorCapability
            or supervisor_capability not in _ISSUED_SUPERVISOR_CAPABILITIES):
        raise ValueError('REAL_SUPERVISOR_CAPABILITY_REQUIRED')
    _ISSUED_SUPERVISOR_CAPABILITIES.remove(supervisor_capability)
    capability_receipt=supervisor_capability.consume(now,recovery)
    startup_progress('AUTHORITY')
    receipt=service_receipt(ROOT,now,recovery=recovery,gate_type=RealGate)
    if receipt!=capability_receipt:raise ValueError('REAL_SUPERVISOR_CAPABILITY_INVALID')
    startup_progress('MARKET_AUTHORITY')
    market_authority=(observation_market_authority(ROOT,receipt,now) if receipt.get('service_only') else RealGate.market_authority(ROOT,receipt))
    startup_progress('OBSERVATION_REPLAY')
    observation_db=LocalDatabase('postgres','m2_admin',schema='real_readiness')
    observation=RealObservationLedger(observation_db).verify_replay()
    if not observation or not observation.get('account_binding'):raise ValueError('REAL_OBSERVATION_BINDING_REQUIRED')
    gate=RealGate(ROOT,observation['account_binding'],observation_db)
    if observation['account_binding']!=receipt['account_binding']:raise ValueError('REAL_ACCOUNT_BINDING_MISMATCH')
    if receipt.get('service_only'):gate.checkpoint(receipt['flat_checkpoint_sha256'])
    else:gate.approved(receipt,clock())
    startup_progress('WRITER_INITIALIZATION')
    admin=LocalDatabase('postgres','m2_admin',schema='real_trader');migrate(admin);grant_writer(admin,'m2_writer')
    lock=acquire_writer_lock(admin)
    try:
        ledger=Ledger(LocalDatabase('postgres','m2_writer',schema='real_trader'),execution_scope='REAL')
        try:state=ledger.snapshot(receipt['account_binding'])
        except Rejected as exc:
            if str(exc)!='UNKNOWN_ACCOUNT':raise
            if recovery or receipt.get('service_only'):raise ValueError('REAL_RECOVERY_LEDGER_REQUIRED') from None
            credentials=load_real_credentials(ROOT)
            source=RealMarketSource(credentials,observation['account_binding'],clock,market_authority=market_authority)
            source.enrich_context=True
            source.discover();packet=source.refresh();gate.check(packet,clock(),initial=True)
            controller=open_real_ledger(ledger,gate,packet,clock)
        else:
            state=ledger.rebuild(receipt['account_binding'])
            startup_progress('POLICY_AUTHORITY')
            if receipt.get('daily_authority_sha256'):
                from autonomous_trader.real_daily import load_authority,policy_authorized
                if not policy_authorized(state,load_authority(ROOT,clock())):
                    raise ValueError('REAL_DAILY_POLICY_MISMATCH')
            controller=RealController(ledger,receipt['account_binding'],clock)
            from autonomous_trader.real_configuration import ensure_configuration
            from autonomous_trader.real_daily import load_authority as _owner_authority
            # Charter-bound horizon and settlement calendar on normal starts; calendar extension only otherwise.
            extend_only=bool(receipt.get('service_only') or recovery or not receipt.get('daily_authority_sha256'))
            try:ensure_configuration(controller,_owner_authority(ROOT,clock()),clock(),extend_only=extend_only)
            except Exception:
                if not extend_only:raise      # a normal start must carry the charter-bound configuration
            state=controller.state
            startup_progress('ACCOUNT_RECONCILIATION')
            from autonomous_trader.carry import carry_enabled,carry_account_values
            # Charter-authorized carried positions are a normal start, not a crash recovery.
            unresolved=bool((state['positions'] and not carry_enabled())
                or any(not row['final'] for row in state['orders'].values()))
            recovery=bool(recovery or unresolved)
            credentials=load_real_credentials(ROOT)
            source=RealMarketSource(credentials,observation['account_binding'],clock,market_authority=market_authority)
            source.enrich_context=True
            from autonomous_trader.cash_precision import make_adjuster
            source.cash_adjuster=make_adjuster(controller,source,clock)
            if recovery:
                # Never reconcile pre-crash fills against an old projection.
                # The runner queries every known client order first.
                source.bind_account()
            else:
                account=source.account_observation()
                # Broker-truth (owner 2026-10-18): adopt the broker's cash before the clean-restart comparisons.
                from autonomous_trader.real_trading import confirm_flat_settlements
                try:confirm_flat_settlements(controller,{**account,'evidence_id':__import__('uuid').uuid4().hex})
                except Exception:pass      # never blocks start-up; the exact comparisons below still apply
                state=controller.state
                if state['positions']:carry_account_values(account,state,controller.account_id,clock())
                elif account['positions'] or account['open_client_ids']:
                    raise ValueError('REAL_CLEAN_RESTART_ACCOUNT_NOT_FLAT')
                broker_total=__import__('decimal').Decimal(account['cash']['cash_balance'])
                local_total=(__import__('decimal').Decimal(state['cash']['settled_usd'])
                    +__import__('decimal').Decimal(state['cash']['unsettled_usd']))
                if broker_total!=local_total:raise ValueError('REAL_CLEAN_RESTART_CASH_CHANGED')
                from autonomous_trader.real_trading import flat_recovery_values
                if receipt.get('service_only') and state['positions']:
                    # Carried positions without today's entry certificate: protect them (stops, deadlines and
                    # planned exits run from the narrow held-instrument path); no entries (reduce-only).
                    controller.refresh_carry_recovery({**account,'evidence_id':__import__('uuid').uuid4().hex})
                    controller.waiting_for_market_data=False
                    controller.protection_only=True
                    recovery=True
                elif receipt.get('service_only'):
                    flat_recovery_values(account,state,controller.account_id,clock())
                    if not (clean_flat(state) or recoverable_flat_incident(state)):raise ValueError('REAL_WAITING_ACCOUNT_NOT_CLEAN')
                    controller.waiting_for_market_data=clean_flat(state)
                    controller.refresh_flat_recovery({**account,'evidence_id':__import__('uuid').uuid4().hex})
                else:
                    try:
                        # A rate-limited first read waits Webull's retry window and tries again (owner approval
                        # 2026-10-19: rate-limited reads are retried, not incidents). Failing here would restart the
                        # child as recovery-only, which cannot trade for the rest of the day while holding a position.
                        for attempt in range(4):
                            try:
                                source.discover()
                                packet,_=gate.read_checked(lambda:source.refresh(controller.state),clock)
                                break
                            except ReadRateLimited as limited:
                                if attempt==3:raise
                                startup_progress('STARTUP_READ_RATE_LIMITED_RETRY')
                                time.sleep(min(60,max(5,limited.retry_after)))
                        controller.refresh(packet)
                    except Exception as exc:
                        if not (market_unavailable(exc) or isinstance(exc,ReadRateLimited)) or not clean_flat(controller.state):raise
                        controller.waiting_for_market_data=True
                        controller.refresh_flat_recovery({**source.account_observation(),'evidence_id':__import__('uuid').uuid4().hex})
        controller.recovery_only=recovery
        startup_progress('RUNTIME_COMPOSITION')
        store=provision('real_trader')
        budget=ModelBudget(store,'REAL',clock)
        key=None
        def brain_factory():
            nonlocal key
            if key is None:key=load_model_key(DECISION_MODEL)
            return configured_brain(key,budget,approved_sme_advisory)
        transport=RealStrategyTransport(credentials,source.account_id)
        gateway=RealExecutionGateway(gate,controller,source,transport,store,clock)
        runner=RealRunner(controller,source,gateway,gate,store,brain_factory,clock)
        runner.authority_receipt=receipt
        def prepare_waiting_market():
            from scripts.run_real_daily import prepare_market
            from autonomous_trader.real_daily import load_authority,persist_certificate,current_certificate,decode_packet,session_entitlement,digest
            from autonomous_trader.reference import NEW_YORK
            authority=load_authority(ROOT,clock())
            # A recovery receipt can describe an older session. Fresh readiness
            # must use today's independently validated market entitlement.
            source.market_authority=dict(session_label=clock().astimezone(NEW_YORK).date().isoformat(),
                entitlement_evidence_sha256=digest(session_entitlement(authority,clock(),ROOT)))
            try:
                current_certificate(ROOT,authority,clock())
            except ValueError as exc:
                if str(exc) not in {'REAL_DAILY_EVIDENCE_UNAVAILABLE','REAL_DAILY_CERTIFICATE_INVALID'}:raise
                value=prepare_market(ROOT,authority,clock,source,controller.state)
                persist_certificate(ROOT,value)
                current_certificate(ROOT,authority,clock())
            ready_receipt=RealGate.offline_receipt(ROOT,clock())
            # Session authority is reused; the runner acquires fresh prices.
            # The immediate runner handoff performs the current gate and ledger
            # reconciliation once; repeating them here consumes observation TTL.
            runner.authority_receipt=ready_receipt
            if not source.symbols:source.discover()
            return None
        runner.prepare_waiting_market=prepare_waiting_market
        return runner,store,lock
    except Exception:
        lock.close();raise


def start_optional_dashboard(port):
    """The independent dashboard service owns availability; this view is optional."""
    server=None
    try:
        server=DashboardServer(DashboardReader(ROOT),port)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        return server
    except Exception:
        if server is not None:
            try:server.server_close()
            except Exception:pass
        print('REAL_OPTIONAL_DASHBOARD_UNAVAILABLE',flush=True)
        return None


class MonitoringHeartbeat:
    """One bounded optional writer; stalled monitoring cannot stall supervision."""
    def __init__(self,store):
        self.store=store;self.pending=queue.Queue(maxsize=1)
        self.guard=threading.Lock();self.closed=False
        self.thread=threading.Thread(target=self._run,daemon=True)
        self.thread.start()
    def submit(self,payload):
        with self.guard:
            if not self.closed:self._enqueue(payload)
    def _enqueue(self,payload):
        try:self.pending.put_nowait(payload)
        except queue.Full:
            try:self.pending.get_nowait()
            except queue.Empty:pass
            try:self.pending.put_nowait(payload)
            except queue.Full:pass
    def _run(self):
        while True:
            payload=self.pending.get()
            if payload is None:return
            try:self.store.heartbeat(payload)
            except Exception:print('REAL_MONITORING_HEARTBEAT_UNAVAILABLE',flush=True)
            with self.guard:
                if self.closed and self.pending.empty():return
    def close(self):
        with self.guard:
            self.closed=True
            if self.pending.empty():self._enqueue(None)


def compose_with_heartbeat(receipt,recovery,**kwargs):
    """Report actual startup phase progress without claiming trading readiness."""
    stop=threading.Event()
    progress={'at':datetime.now(timezone.utc),'phase':'COMPOSITION'}
    def advance(phase):
        progress.update(at=datetime.now(timezone.utc),phase=phase)
    def publish():
        now=datetime.now(timezone.utc)
        hung=(now-progress['at']).total_seconds()>120
        supervisor_heartbeat(dict(schema='REAL_CHILD_HEARTBEAT_V1',pid=os.getpid(),at=now.isoformat(),
            status='HUNG' if hung else 'STARTING',mode='REAL',production_enabled=False,
            account_id=receipt['account_binding'],ledger_schema='real_trader',activity='Supervised REAL runner',
            recovery_only=recovery,authority_build_sha256=receipt['build_sha256'],
            market_session_label=receipt['market_session_label'],reconciliation_ready=False,unresolved=True,
            last_step_result=progress['phase'],last_step_at=progress['at'].isoformat()))
        if hung:os._exit(1)
    def pulse():
        while not stop.wait(3):
            try:publish()
            except Exception:os._exit(1)
    publish()
    thread=threading.Thread(target=pulse,daemon=True);thread.start()
    try:
        with observe_validation_progress(lambda:advance('AUTHORITY_VALIDATED')):
            return compose(recovery=recovery,startup_progress=advance,**kwargs)
    finally:stop.set();thread.join()


def progress_observation(progress,now,poll_seconds):
    # Scheduled waiting is healthy; once the scheduled wait has elapsed the
    # existing work deadline applies, even if the main thread never wakes.
    return (min(now,progress['at']+timedelta(seconds=poll_seconds))
            if progress.get('idle') else progress['at'])


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,default=8766)
    parser.add_argument('--poll-seconds',type=int,default=30)
    parser.add_argument('--recovery',action='store_true')
    parser.add_argument('--sme-advisory-file',type=Path)
    parser.add_argument('--sme-advisory-sha256')
    parser.add_argument('--sme-authorization-db',type=Path)
    parser.add_argument('--sme-advisory-provider',type=Path)
    parser.add_argument('--sme-advisory-provider-sha256')
    parser.add_argument('--supervisor-handle');parser.add_argument('--supervisor-pipe-handle');args=parser.parse_args()
    if not 5<=args.poll_seconds<=300:raise ValueError('REAL_POLL_INTERVAL_INVALID')
    # The inherited one-use nonce proves this is the verified supervisor's
    # child. Its receipt permits STARTING liveness only. compose independently
    # verifies current signed authority before any database or broker operation.
    now=datetime.now(timezone.utc)
    capability=consume_supervisor_handle(args.supervisor_handle,None,recovery=args.recovery,now=now,
        pipe_handle=args.supervisor_pipe_handle)
    receipt=capability.receipt
    advisory=None
    if args.sme_advisory_provider or args.sme_advisory_provider_sha256:
        if not any((args.sme_advisory_file,args.sme_advisory_sha256,args.sme_authorization_db)):
            from autonomous_trader.decision_api.sme_advisory import rotating_configuration
            advisory=rotating_configuration(args.sme_advisory_provider,args.sme_advisory_provider_sha256)
    elif args.sme_advisory_file and args.sme_advisory_sha256 and args.sme_authorization_db:
        from autonomous_trader.decision_api.sme_advisory import ApprovedSMEAdvisory
        advisory=ApprovedSMEAdvisory(args.sme_advisory_file,args.sme_advisory_sha256,args.sme_authorization_db)
    runner,store,lock=compose_with_heartbeat(receipt,args.recovery,supervisor_capability=capability,
        approved_sme_advisory=advisory);stop=threading.Event()
    progress={'at':datetime.now(timezone.utc),'result':'STARTING','idle':False}
    try:monitoring=MonitoringHeartbeat(store)
    except Exception:monitoring=None
    server=None
    heartbeat_guard=threading.RLock();terminal=[False]
    def heartbeat(status=None,*,final=False):
        with heartbeat_guard:
            if terminal[0] and not final:return False
            publish_heartbeat(status)
            return True
    def publish_heartbeat(status=None):
        status=status or runtime_status(runner,progress);state=runner.controller.state
        # 'daily' resets at the next session (NEW_SESSION); it alone never makes a closed day unresolved.
        unresolved=bool(exposure_unresolved(state) or state.get('incidents')
            or any(v for k,v in state.get('latches',{}).items() if k!='daily'))
        payload={'status':status,'mode':'REAL','production_enabled':status=='RUNNING' and not runner.controller.recovery_only,
            'account_id':runner.controller.account_id,'ledger_schema':'real_trader',
            'activity':'Supervised REAL runner','recovery_only':runner.controller.recovery_only,
            'authority_build_sha256':runner.authority_receipt['build_sha256'],
            'market_session_label':runner.authority_receipt['market_session_label'],
            'reconciliation_ready':bool(state['reconciliation']['ready']),'unresolved':unresolved,
            'last_step_result':progress['result'],
            'last_step_at':progress_observation(progress,datetime.now(timezone.utc),args.poll_seconds).isoformat()}
        supervisor_heartbeat({'schema':'REAL_CHILD_HEARTBEAT_V1','pid':os.getpid(),
            'at':datetime.now(timezone.utc).isoformat(),**payload})
        if monitoring is not None:monitoring.submit(payload)
    def pulse():
        while not stop.wait(3):
            try:
                lock.execute('SELECT 1')
                now=datetime.now(timezone.utc)
                status=('HUNG' if (now-progress_observation(progress,now,args.poll_seconds)).total_seconds()>120
                    else 'STARTING' if progress['result']=='STARTING' else runtime_status(runner,progress))
                if not heartbeat(status):return
                if status=='HUNG' and not stop.is_set():os._exit(1)
            except Exception as exc:
                if stop.is_set():return
                record_pulse_failure(exc)
                try:supervisor_heartbeat({'schema':'REAL_CHILD_HEARTBEAT_V1','pid':os.getpid(),
                    'at':datetime.now(timezone.utc).isoformat(),'status':'HEARTBEAT_FAILED','mode':'REAL'})
                finally:os._exit(1)
    def step():
        with observe_validation_progress(lambda:progress.update(at=datetime.now(timezone.utc))):
            return runner.step()
    try:
        heartbeat('STARTING')
        threading.Thread(target=pulse,daemon=True).start()
        if runner.controller.recovery_only:
            progress['result']=step();progress['at']=datetime.now(timezone.utc);record_step(progress['result'])
        else:progress['result']='INITIALIZED'
        progress['idle']=True
        heartbeat(runtime_status(runner,progress))
        server=start_optional_dashboard(args.port)
        while not stop.wait(args.poll_seconds):
            progress.update(at=datetime.now(timezone.utc),idle=False)
            progress['result']=step();progress['at']=datetime.now(timezone.utc);record_step(progress['result'])
            progress['idle']=True
            # The daily-loss latch resets at the next session and must not keep a closed day's child alive.
            if progress['result']=='CLOSED_RECONCILED' and not any(v for k,v in runner.controller.state['latches'].items() if k!='daily'):break
            # After the close with an incident journaled: stop (the next preparation decides), never loop all night.
            if progress['result'] in CLOSED_DAY_RESULTS:break
    except KeyboardInterrupt:pass
    finally:
        stop.set()
        with heartbeat_guard:terminal[0]=True
        state=runner.controller.state
        unresolved=exposure_unresolved(state)
        try:
            if unresolved:
                runner.controller.incident('REAL_SHUTDOWN_WITH_UNRESOLVED_EXPOSURE')
                heartbeat('INCIDENT',final=True)
            else:heartbeat('STOPPED',final=True)
        finally:
            if monitoring is not None:monitoring.close()
            try:
                runner.worker.shutdown(wait=False,cancel_futures=True)
                if getattr(runner,'shadow_worker',None) is not None:runner.shadow_worker.shutdown(wait=False,cancel_futures=True)
            finally:
                try:
                    if server is not None:
                        try:server.shutdown()
                        finally:server.server_close()
                finally:
                    lock.close()
                    launch_lab_after_close(progress.get('result'))


if __name__=='__main__':
    try:main()
    except Exception as exc:
        code=str(exc) if isinstance(exc,ValueError) and str(exc).isupper() else 'REAL_STARTUP_FAILED'
        record_pulse_failure(exc,'startup')
        print(code,flush=True);raise SystemExit(2)
