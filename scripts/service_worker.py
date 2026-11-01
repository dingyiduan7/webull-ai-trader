"""Fixed SCM-to-worker boundary; no broker, model, credential or ledger APIs."""
import ctypes as c
from ctypes import wintypes as w
from pathlib import Path
import re
from uuid import uuid4

from scripts.owned_process import spawn, confirm_stop
from scripts.restricted_process import api, check, SIDAttrs, protect_host, suspended

ROOT=Path(__file__).resolve().parents[1]
EVENT_PREFIX='Local\\AITraderResidentStop-'

class SecurityAttributes(c.Structure):
    _fields_=[('length',w.DWORD),('descriptor',w.LPVOID),('inherit',w.BOOL)]

def event_api():
    k,a=api()
    for name,args,result in (
        ('CreateEventW',[c.POINTER(SecurityAttributes),w.BOOL,w.BOOL,w.LPCWSTR],w.HANDLE),
        ('OpenEventW',[w.DWORD,w.BOOL,w.LPCWSTR],w.HANDLE),
        ('SetEvent',[w.HANDLE],w.BOOL)):
        fn=getattr(k,name);fn.argtypes=args;fn.restype=result
    return k,a

class StopEvent:
    def __init__(self,name,*,create=False):
        if not re.fullmatch(r'Local\\AITraderResidentStop-[0-9a-f]{32}',name):
            raise ValueError('INVALID_SERVICE_STOP_EVENT')
        self.k,a=event_api();self.handle=None;self.name=name
        if not create:
            self.handle=self.k.OpenEventW(0x100000,False,name)
            check(self.handle)
            return
        token=w.HANDLE();sd=w.LPVOID();text=w.LPWSTR()
        try:
            check(a.OpenProcessToken(self.k.GetCurrentProcess(),8,c.byref(token)))
            size=w.DWORD();a.GetTokenInformation(token,1,None,0,c.byref(size))
            user=c.create_string_buffer(size.value)
            check(a.GetTokenInformation(token,1,user,size.value,c.byref(size)))
            check(a.ConvertSidToStringSidW(c.cast(user,c.POINTER(SIDAttrs)).contents.sid,c.byref(text)))
            sddl='O:BAG:BAD:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;0x00100000;;;'+text.value+')S:(ML;;NW;;;ME)'
            check(a.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl,1,c.byref(sd),None))
            attributes=SecurityAttributes(c.sizeof(SecurityAttributes),sd,False)
            self.handle=self.k.CreateEventW(c.byref(attributes),True,False,name)
            check(self.handle)
            if c.get_last_error()==183:
                self.close();raise ValueError('SERVICE_STOP_EVENT_ALREADY_EXISTS')
        finally:
            if token:self.k.CloseHandle(token)
            if sd:self.k.LocalFree(sd)
            if text:self.k.LocalFree(c.cast(text,w.HANDLE))
    def wait(self,timeout=None):
        status=self.k.WaitForSingleObject(self.handle,0xFFFFFFFF if timeout is None else max(0,int(timeout*1000)))
        if status==0:return True
        if status==258:return False
        raise c.WinError(c.get_last_error())
    def is_set(self):return self.wait(0)
    def set(self):check(self.k.SetEvent(self.handle))
    def close(self):
        if self.handle:self.k.CloseHandle(self.handle);self.handle=None

def run_owned_worker(stop):
    def command(signal_name):
        return [str(ROOT/'.venv/Scripts/pythonw.exe'),'-I','-B',str(ROOT/'scripts/run_real_daily.py'),'--resident-worker','--stop-event',signal_name]
    return _run_worker(stop,command)


def _run_worker(stop,command_builder):
    # Internal seam for the nontrading Windows integration probe. No external
    # input or service argument can choose a privileged executable/command.
    protect_host()
    signal=StopEvent(EVENT_PREFIX+uuid4().hex,create=True)
    child=None
    try:
        command=command_builder(signal.name)
        child=spawn(command,stop=stop,process_factory=suspended,cwd=str(ROOT))
        while child.poll() is None and not stop.wait(.25):
            pass
        if stop.is_set():
            signal.set()
            child.wait()  # Worker confirms its owned descendants are gone.
        else:
            raise RuntimeError('RESIDENT_WORKER_EXITED')
    finally:
        if child:confirm_stop(child)
        signal.close()
