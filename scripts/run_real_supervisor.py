"""Dormant REAL-only supervisor. Never installed or started by preparation/tests."""
import json
import os
import subprocess
import sys
import time
import hashlib,secrets
import msvcrt
import signal
import argparse
from itertools import count
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'src'))
from autonomous_trader.runtime_storage import data_root
from autonomous_trader.real_trading import RealGate
from autonomous_trader.real_waiting import service_receipt
from scripts.owned_process import spawn, confirm_stop


def _read(path):
    try:return json.loads(path.read_text(encoding='utf-8'))
    except (OSError,ValueError):return None


def _write(path,value):
    temporary=path.with_suffix('.tmp')
    with temporary.open('w',encoding='utf-8') as out:
        json.dump(value,out,sort_keys=True,separators=(',',':'));out.flush();os.fsync(out.fileno())
    # Windows readers can briefly hold the destination without delete sharing.
    # Keep atomic publication, but tolerate a bounded sharing violation.
    for attempt in range(6):
        try:
            os.replace(temporary,path)
            return
        except PermissionError:
            if attempt==5:raise
            time.sleep(min(.05*2**attempt,.4))


def _authority(now):
    """Read-only files only. No lock, process, DB, credential or network touch."""
    try:
        receipt=service_receipt(ROOT,now,gate_type=RealGate);recovery=False
    except ValueError:
        receipt=service_receipt(ROOT,now,recovery=True,gate_type=RealGate);recovery=True
    # Mutable supervisor state is unreadable authority until immutable GO has passed.
    marker=_read(data_root(ROOT)/'.local/real-supervisor/launched.json')
    if recovery and not marker:raise ValueError('REAL_SUPERVISOR_RECOVERY_MARKER_REQUIRED')
    recovery=bool(not receipt.get('service_only') and marker and marker.get('build')==receipt['build_sha256']
        and marker.get('account_binding')==receipt['account_binding']
        and marker.get('authority_session')==receipt['market_session_label'])
    if marker and not recovery and now>datetime.fromisoformat(receipt['expires_at']):
        raise ValueError('REAL_SUPERVISOR_AUTHORITY_MISMATCH')
    return receipt,recovery


def _process_snapshot():
    if os.name!='nt':return {}
    import ctypes
    from ctypes import wintypes
    class PROCESSENTRY32W(ctypes.Structure):
        _fields_=(('dwSize',wintypes.DWORD),('cntUsage',wintypes.DWORD),
            ('th32ProcessID',wintypes.DWORD),('th32DefaultHeapID',ctypes.c_void_p),
            ('th32ModuleID',wintypes.DWORD),('cntThreads',wintypes.DWORD),
            ('th32ParentProcessID',wintypes.DWORD),('pcPriClassBase',wintypes.LONG),
            ('dwFlags',wintypes.DWORD),('szExeFile',wintypes.WCHAR*260))
    handle=ctypes.windll.kernel32.CreateToolhelp32Snapshot(0x00000002,0)
    if handle in (0,-1):return {}
    try:
        entry=PROCESSENTRY32W();entry.dwSize=ctypes.sizeof(entry);result={}
        found=ctypes.windll.kernel32.Process32FirstW(handle,ctypes.byref(entry))
        while found:
            result[int(entry.th32ProcessID)]=(int(entry.th32ParentProcessID),str(entry.szExeFile).lower())
            found=ctypes.windll.kernel32.Process32NextW(handle,ctypes.byref(entry))
        return result
    finally:ctypes.windll.kernel32.CloseHandle(handle)


def _descendant_depth(pid,ancestor,snapshot,max_hops=3):
    if pid==ancestor:return 0
    current=int(pid);seen={current}
    for depth in range(1,max_hops+1):
        row=snapshot.get(current)
        if not row:return None
        parent=row[0]
        if parent==ancestor:return depth
        if not parent or parent in seen:return None
        seen.add(parent);current=parent
    return None


def _heartbeat_matches(value,receipt,recovery,now):
    required={'schema','pid','at','status','mode','production_enabled','account_id','ledger_schema','activity',
        'recovery_only','authority_build_sha256','market_session_label','reconciliation_ready','unresolved',
        'last_step_result','last_step_at'}
    try:
        at=datetime.fromisoformat(value['at']);last_step=datetime.fromisoformat(value['last_step_at'])
    except (KeyError,TypeError,ValueError):return False
    allowed={'STARTING','RUNNING','INCIDENT','RECOVERY_RECONCILED','RECOVERY_UNRESOLVED','HUNG','HEARTBEAT_FAILED','STOPPED','WAITING_FOR_MARKET_DATA'}
    daily_downgrade=(receipt.get('daily_authority_sha256') and not recovery
        and value.get('recovery_only') is True and value.get('production_enabled') is False
        and now+timedelta(seconds=60)>=datetime.fromisoformat(receipt['expires_at'])
        and value.get('status') in ('RECOVERY_RECONCILED','RECOVERY_UNRESOLVED','STOPPED','INCIDENT'))
    observation_recovery=(receipt.get('service_only') and not recovery
        and value.get('recovery_only') is True and value.get('production_enabled') is False)
    return (isinstance(value,dict) and set(value)==required and value['schema']=='REAL_CHILD_HEARTBEAT_V1'
        and isinstance(value['pid'],int) and value['pid']>0 and value['status'] in allowed
        and value['mode']=='REAL' and value['ledger_schema']=='real_trader'
        and value['activity']=='Supervised REAL runner' and (value['recovery_only'] is bool(recovery) or daily_downgrade or observation_recovery)
        and not (receipt.get('service_only') and value['production_enabled'] is True)
        and value['production_enabled'] is (value['status']=='RUNNING' and not value['recovery_only'])
        and value['authority_build_sha256']==receipt['build_sha256']
        and value['account_id']==receipt['account_binding']
        and value['market_session_label']==receipt['market_session_label']
        and at.tzinfo is not None and last_step.tzinfo is not None
        and 0<=(now-at).total_seconds()<=20 and last_step<=now)


def normal_resume_receipt(root,previous,now):
    """Independently verify the child's local recovery handoff under the same owner scope."""
    renewed=RealGate.offline_receipt(root,now)
    # The normal receipt itself validates the current session. Recovery receipts
    # may legitimately refer to a previous session under the same owner scope.
    bound=('account_binding','build_sha256','daily_authority_sha256')
    if (not previous.get('daily_authority_sha256') or renewed.get('service_only')
            or renewed.get('verdict')!='GO'
            or any(renewed.get(key)!=previous.get(key) for key in bound)):
        raise ValueError('REAL_SUPERVISOR_AUTHORITY_MISMATCH')
    return renewed


def _owned_heartbeat(value,child_pid,receipt,recovery,now,snapshot=None):
    snapshot=_process_snapshot() if snapshot is None else snapshot
    return (_heartbeat_matches(value,receipt,recovery,now)
        and _descendant_depth(value['pid'],child_pid,snapshot,max_hops=3) is not None)


def _stop_child(child):
    if hasattr(child,'stop_tree'):
        confirm_stop(child)
        return
    snapshot=_process_snapshot()
    descendants=[]
    for pid,(_,name) in snapshot.items():
        depth=_descendant_depth(pid,child.pid,snapshot,max_hops=3)
        if depth and name in ('python.exe','pythonw.exe'):descendants.append((depth,pid))
    for _,pid in sorted(descendants,reverse=True):
        try:os.kill(pid,signal.SIGTERM)
        except OSError:pass
    child.terminate()
    try:child.wait(timeout=15)
    except subprocess.TimeoutExpired:
        child.kill();child.wait(timeout=15)


def _record_attempt(folder, *, attempt, launcher_pid, child_pid, code, forced, reason, heartbeat, at):
    """Best-effort append-only diagnostics, never authority or recovery input."""
    heartbeat=heartbeat if isinstance(heartbeat,dict) else {}
    allowed={'CHILD_EXIT','CHILD_REPORTED_HUNG','CHILD_REPORTED_HEARTBEAT_FAILED',
        'HEARTBEAT_AGE_EXCEEDED','PROGRESS_AGE_EXCEEDED','OWNED_HEARTBEAT_NOT_CONFIRMED',
        'SUPERVISOR_OBSERVATION_FAILED'}
    def age(key):
        try:
            value=(at-datetime.fromisoformat(heartbeat[key])).total_seconds()
            return value if abs(value)<315360000 else None
        except (KeyError,TypeError,ValueError,OverflowError):return None
    status=heartbeat.get('status')
    if not isinstance(status,str) or status not in {'RUNNING','INCIDENT','RECOVERY_RECONCILED','RECOVERY_UNRESOLVED',
        'HUNG','HEARTBEAT_FAILED','STOPPED','WAITING_FOR_MARKET_DATA'}:status=None
    record={'schema':'REAL_SUPERVISOR_ATTEMPT_V1','at':at.isoformat(),'attempt':attempt+1,
        'launcher_pid':launcher_pid,'last_owned_child_pid':child_pid,'exit_code':code,
        'forced_stop':bool(forced),'reason':reason if reason in allowed else 'SUPERVISOR_OBSERVATION_FAILED',
        'heartbeat_status':status,'heartbeat_age_seconds':age('at'),
        'last_progress_age_seconds':age('last_step_at'),
        'heartbeat_is_last_observation_not_current_authority':True}
    try:
        with (folder/'attempt-history.jsonl').open('a',encoding='utf-8') as out:
            out.write(json.dumps(record,sort_keys=True)+'\n');out.flush();os.fsync(out.fileno())
        return True
    except OSError:return False


def advisory_arguments(argv):
    parser=argparse.ArgumentParser()
    parser.add_argument('--sme-advisory-file')
    parser.add_argument('--sme-advisory-sha256')
    parser.add_argument('--sme-authorization-db')
    parser.add_argument('--sme-advisory-provider')
    parser.add_argument('--sme-advisory-provider-sha256')
    args=parser.parse_args(argv)
    if args.sme_advisory_provider or args.sme_advisory_provider_sha256:
        if (not all((args.sme_advisory_provider,args.sme_advisory_provider_sha256))
                or any((args.sme_advisory_file,args.sme_advisory_sha256,args.sme_authorization_db))):return []
        return ['--sme-advisory-provider',args.sme_advisory_provider,'--sme-advisory-provider-sha256',args.sme_advisory_provider_sha256]
    if not any((args.sme_advisory_file,args.sme_advisory_sha256,args.sme_authorization_db)):return []
    if not all((args.sme_advisory_file,args.sme_advisory_sha256,args.sme_authorization_db)):
        return []
    return ['--sme-advisory-file',args.sme_advisory_file,'--sme-advisory-sha256',args.sme_advisory_sha256,
        '--sme-authorization-db',args.sme_authorization_db]


def child_command(recovery,handle,pipe_handle,advisory_args=()):
    command=[sys.executable,'-I','-B',str(ROOT/'scripts/run_real_trader.py')]
    if recovery:command.append('--recovery')
    command.extend(('--supervisor-handle',str(handle),'--supervisor-pipe-handle',str(pipe_handle)))
    command.extend(advisory_arguments(advisory_args))
    return command


def main(advisory_args=()):
    # File-only validation MUST precede locks, process state and child creation.
    receipt,recovery=_authority(datetime.now(timezone.utc))
    advisory_args=advisory_arguments(advisory_args)
    import msvcrt
    folder=data_root(ROOT)/'.local/real-supervisor'
    folder.mkdir(parents=True,exist_ok=True)
    with (folder/'owner.lock').open('a+b') as lock:
        lock.seek(0);lock.write(b'0');lock.flush();lock.seek(0)
        msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
        marker=folder/'launched.json';heartbeat=folder/'child-heartbeat.json'
        # Persist before spawning: a supervisor crash must not regain first-start authority.
        _write(marker,{'schema':'REAL_SUPERVISOR_LAUNCH_V1','build':receipt['build_sha256'],
            'account_binding':receipt['account_binding'],'authority_session':receipt['market_session_label'],
            'recovery_not_after':receipt['recovery_not_after']})
        for attempt in count():
            child_recovery=(recovery or bool(attempt)) and not receipt.get('service_only',False)
            validated=service_receipt(ROOT,datetime.now(timezone.utc),recovery=child_recovery)
            if any(validated.get(key)!=receipt.get(key) for key in
                   ('account_binding','build_sha256','daily_authority_sha256','market_session_label')):
                raise ValueError('REAL_SUPERVISOR_AUTHORITY_MISMATCH')
            # A failed child never regains first-start privileges because an
            # observation-only receipt became a normal receipt between polls.
            if (recovery or attempt) and not validated.get('service_only') and not child_recovery:
                child_recovery=True
                validated=service_receipt(ROOT,datetime.now(timezone.utc),recovery=True)
            if any(validated.get(key)!=receipt.get(key) for key in
                   ('account_binding','build_sha256','daily_authority_sha256','market_session_label')):
                raise ValueError('REAL_SUPERVISOR_AUTHORITY_MISMATCH')
            receipt=validated
            nonce=secrets.token_urlsafe(32);handle=folder/('child-handle-'+secrets.token_hex(16)+'.json')
            _write(handle,{'schema':'REAL_SUPERVISOR_CHILD_HANDLE_V1','build':receipt['build_sha256'],
                'account_binding':receipt['account_binding'],'authority_session':receipt['market_session_label'],
                'recovery':child_recovery,'expires_at':(datetime.now(timezone.utc)+timedelta(minutes=2)).isoformat(),
                'startup_receipt':receipt,
                'nonce_sha256':hashlib.sha256(nonce.encode()).hexdigest(),'supervisor_pid':os.getpid()})
            read_fd,write_fd=os.pipe();os.set_inheritable(read_fd,True)
            pipe_handle=msvcrt.get_osfhandle(read_fd)
            os.write(write_fd,nonce.encode('ascii'));os.close(write_fd)
            command=child_command(child_recovery,handle,pipe_handle,advisory_args)
            try:
                child=spawn(command,cwd=ROOT,creationflags=subprocess.CREATE_NO_WINDOW,close_fds=False)
            finally:os.close(read_fd)
            started=time.monotonic();forced=False;last_owned_pid=None;last_owned_at=None
            stop_reason='CHILD_EXIT';observer_failed=False;value={}
            try:
                while child.poll() is None:
                    value=_read(heartbeat)
                    now=datetime.now(timezone.utc)
                    if ((child_recovery or receipt.get('service_only'))
                            and isinstance(value,dict) and isinstance(value.get('pid'),int)
                            and value.get('recovery_only') is False and value.get('production_enabled') is True
                            and _descendant_depth(value.get('pid',0),child.pid,_process_snapshot()) is not None):
                        try:renewed=normal_resume_receipt(ROOT,receipt,now)
                        except (ValueError,OSError):pass
                        else:
                            # Verification may take time; inspect a new heartbeat,
                            # never freshen the observation that triggered it.
                            value=_read(heartbeat);now=datetime.now(timezone.utc)
                            if _owned_heartbeat(value,child.pid,renewed,False,now):
                                receipt=renewed;child_recovery=False
                    if _owned_heartbeat(value,child.pid,receipt,child_recovery,now):
                        last_owned_pid=value['pid']
                        last_owned_at=datetime.fromisoformat(value['at'])
                        try:
                            age=(now-datetime.fromisoformat(value['at'])).total_seconds()
                            progress_age=(now-datetime.fromisoformat(value['last_step_at'])).total_seconds()
                        except (KeyError,TypeError,ValueError):age=progress_age=999
                        if value.get('status') in ('HUNG','HEARTBEAT_FAILED') or age>20 or progress_age>125:
                            stop_reason=('CHILD_REPORTED_'+value['status'] if value.get('status') in ('HUNG','HEARTBEAT_FAILED')
                                else 'HEARTBEAT_AGE_EXCEEDED' if age>20 else 'PROGRESS_AGE_EXCEEDED')
                            _stop_child(child);forced=True;break
                        _write(folder/'supervisor-status.json',{'schema':'REAL_SUPERVISOR_STATUS_V1','status':'HEALTHY',
                            'child_pid':value['pid'],'launcher_pid':child.pid,'build':receipt['build_sha256'],
                            'account_binding':receipt['account_binding'],
                            'authority_session':receipt['market_session_label'],'recovery_only':value['recovery_only'],
                            'unresolved':value.get('unresolved'),'at':datetime.now(timezone.utc).isoformat()})
                    elif ((last_owned_at is None and time.monotonic()-started>90)
                            or (last_owned_at is not None and (now-last_owned_at).total_seconds()>20)):
                        stop_reason='OWNED_HEARTBEAT_NOT_CONFIRMED'
                        _stop_child(child);forced=True;break
                    time.sleep(2)
            except BaseException:
                observer_failed=True;stop_reason='SUPERVISOR_OBSERVATION_FAILED'
                raise
            finally:
                # A status/observer failure must never abandon a live child.
                try:
                    confirm_stop(child)
                finally:
                    if observer_failed:
                        _record_attempt(folder,attempt=attempt,launcher_pid=child.pid,child_pid=last_owned_pid,
                            code=child.poll(),forced=True,reason=stop_reason,heartbeat=value or {},at=datetime.now(timezone.utc))
            code=child.wait()
            value=_read(heartbeat) or {}
            _record_attempt(folder,attempt=attempt,launcher_pid=child.pid,child_pid=last_owned_pid,
                code=code,forced=forced,reason=stop_reason,heartbeat=value,at=datetime.now(timezone.utc))
            if (code==0 and not forced and value.get('pid')==last_owned_pid and value.get('status')=='STOPPED'
                    and value.get('unresolved') is False
                    and _heartbeat_matches(value,receipt,child_recovery,datetime.now(timezone.utc))):
                return
            # A closed day whose child stopped after journaling an incident is over: restarting a child
            # all night only repeats the same post-close checks and blocks the next day's start (2026-10-15).
            if (code==0 and not forced and value.get('pid')==last_owned_pid
                    # STOPPED (never INCIDENT): the child confirmed no unresolved exposure (no open order, no
                    # position the charter does not allow to carry), so nothing needs a live process tonight.
                    and value.get('status')=='STOPPED'
                    and value.get('last_step_result') in ('CLOSED_RECONCILED_WITH_INCIDENTS','CLOSED_UNRECONCILED')
                    and _heartbeat_matches(value,receipt,child_recovery,datetime.now(timezone.utc))):
                return
            recovery=True
            _write(folder/'supervisor-status.json',{'schema':'REAL_SUPERVISOR_STATUS_V1','status':'RECOVERING',
                'reason':'CHILD_RECOVERY_PENDING','attempts':attempt+1,'build':receipt['build_sha256'],
                'account_binding':receipt['account_binding'],'at':datetime.now(timezone.utc).isoformat()})
            # Availability retries are not trading permission. Every next child
            # starts through fresh authority, singleton and recovery checks.
            time.sleep(min(5*2**min(attempt,2),20))


if __name__=='__main__':
    try:main(sys.argv[1:])
    except Exception:
        # Do not expose child configuration, broker responses or credentials.
        print('REAL_SUPERVISOR_BLOCKED',flush=True)
        raise SystemExit(2)
