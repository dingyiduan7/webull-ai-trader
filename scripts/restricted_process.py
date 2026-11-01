"""Candidate-only Windows restricted-process prototype; no financial imports."""
import ctypes as c
from ctypes import wintypes as w
import subprocess

class SIDAttrs(c.Structure):
    _fields_=[('sid',w.LPVOID),('attributes',w.DWORD)]
class Startup(c.Structure):
    _fields_=[('cb',w.DWORD),('reserved',w.LPWSTR),('desktop',w.LPWSTR),('title',w.LPWSTR)]+[(n,w.DWORD) for n in ('x','y','xs','ys','xc','yc','fill','flags')]+[('show',w.WORD),('reserved_size',w.WORD),('reserved_bytes',w.LPVOID),('stdin',w.HANDLE),('stdout',w.HANDLE),('stderr',w.HANDLE)]
class ProcessInfo(c.Structure):
    _fields_=[('process',w.HANDLE),('thread',w.HANDLE),('pid',w.DWORD),('tid',w.DWORD)]

def api():
    k=c.WinDLL('kernel32',use_last_error=True)
    a=c.WinDLL('advapi32',use_last_error=True)
    specs=[(k,'GetCurrentProcess',[],w.HANDLE),(k,'SetErrorMode',[w.UINT],w.UINT),(k,'CloseHandle',[w.HANDLE],w.BOOL),
      (k,'LocalFree',[w.HANDLE],w.HANDLE),(k,'WaitForSingleObject',[w.HANDLE,w.DWORD],w.DWORD),
      (k,'GetExitCodeProcess',[w.HANDLE,c.POINTER(w.DWORD)],w.BOOL),
      (k,'TerminateProcess',[w.HANDLE,w.UINT],w.BOOL),
      (a,'OpenProcessToken',[w.HANDLE,w.DWORD,c.POINTER(w.HANDLE)],w.BOOL),
      (a,'GetTokenInformation',[w.HANDLE,c.c_int,w.LPVOID,w.DWORD,c.POINTER(w.DWORD)],w.BOOL),
      (a,'ConvertSidToStringSidW',[w.LPVOID,c.POINTER(w.LPWSTR)],w.BOOL),
      (a,'ConvertStringSidToSidW',[w.LPCWSTR,c.POINTER(w.LPVOID)],w.BOOL),
      (a,'GetLengthSid',[w.LPVOID],w.DWORD),
      (a,'CreateRestrictedToken',[w.HANDLE,w.DWORD,w.DWORD,c.POINTER(SIDAttrs),w.DWORD,w.LPVOID,w.DWORD,w.LPVOID,c.POINTER(w.HANDLE)],w.BOOL),
      (a,'SetTokenInformation',[w.HANDLE,c.c_int,w.LPVOID,w.DWORD],w.BOOL),
      (a,'CreateProcessAsUserW',[w.HANDLE,w.LPCWSTR,w.LPWSTR,w.LPVOID,w.LPVOID,w.BOOL,w.DWORD,w.LPVOID,w.LPCWSTR,c.POINTER(Startup),c.POINTER(ProcessInfo)],w.BOOL),
      (a,'ConvertStringSecurityDescriptorToSecurityDescriptorW',[w.LPCWSTR,w.DWORD,c.POINTER(w.LPVOID),w.LPVOID],w.BOOL),
      (a,'SetKernelObjectSecurity',[w.HANDLE,w.DWORD,w.LPVOID],w.BOOL)]
    specs.append((a,'GetSecurityDescriptorDacl',[w.LPVOID,c.POINTER(w.BOOL),c.POINTER(w.LPVOID),c.POINTER(w.BOOL)],w.BOOL))
    for dll,name,args,result in specs:
        fn=getattr(dll,name);fn.argtypes=args;fn.restype=result
    return k,a

def check(ok):
    if not ok:raise c.WinError(c.get_last_error())

def protect_host():
    """No worker access to the host's unrestricted process or primary token."""
    k,a=api();k.SetErrorMode(3);sd=w.LPVOID();token=w.HANDLE()
    check(a.ConvertStringSecurityDescriptorToSecurityDescriptorW('O:BAG:BAD:P(A;;GA;;;SY)(A;;GA;;;BA)',1,c.byref(sd),None))
    try:
        check(a.OpenProcessToken(k.GetCurrentProcess(),0xF01FF,c.byref(token)))
        check(a.SetKernelObjectSecurity(token,0x80000005,sd))
        check(a.SetKernelObjectSecurity(k.GetCurrentProcess(),0x80000005,sd))
    finally:
        if token:k.CloseHandle(token)
        k.LocalFree(sd)

class Process:
    def __init__(self,k,handle,pid):self.k,self._handle,self.pid=k,handle,pid
    def poll(self):
        state=self.k.WaitForSingleObject(self._handle,0)
        if state==258:return None
        if state!=0:raise c.WinError(c.get_last_error())
        code=w.DWORD();check(self.k.GetExitCodeProcess(self._handle,c.byref(code)));return code.value
    def wait(self,timeout=None):
        state=self.k.WaitForSingleObject(self._handle,0xFFFFFFFF if timeout is None else int(timeout*1000))
        if state==258:raise subprocess.TimeoutExpired('restricted worker',timeout)
        if state!=0:raise c.WinError(c.get_last_error())
        return self.poll()
    def kill(self):check(self.k.TerminateProcess(self._handle,2))
    def __del__(self):
        if getattr(self,'_handle',None):self.k.CloseHandle(self._handle);self._handle=None

def suspended(command,*,creationflags=4,cwd=None,**kwargs):
    if kwargs:raise ValueError('UNSUPPORTED_RESTRICTED_PROCESS_OPTIONS')
    if not creationflags&4:raise ValueError('RESTRICTED_CHILD_MUST_BE_SUSPENDED')
    k,a=api();token=w.HANDLE();restricted=w.HANDLE();admin=w.LPVOID();medium=w.LPVOID()
    info=ProcessInfo()
    try:
        check(a.OpenProcessToken(k.GetCurrentProcess(),0xF01FF,c.byref(token)))
        check(a.ConvertStringSidToSidW('S-1-5-32-544',c.byref(admin)))
        disabled=SIDAttrs(admin,0)
        check(a.CreateRestrictedToken(token,1,1,c.byref(disabled),0,None,0,None,c.byref(restricted)))
        # The child must query its own restricted token; it must NOT inherit
        # the privileged host token object's administrator-only DACL.
        size=w.DWORD()
        a.GetTokenInformation(restricted,1,None,0,c.byref(size))
        user=c.create_string_buffer(size.value)
        check(a.GetTokenInformation(restricted,1,user,size.value,c.byref(size)))
        owner=c.cast(user,c.POINTER(SIDAttrs)).contents.sid
        sid_text=w.LPWSTR();sd=w.LPVOID()
        check(a.ConvertSidToStringSidW(owner,c.byref(sid_text)))
        try:
            sddl='O:'+sid_text.value+'D:P(A;;GA;;;SY)(A;;GA;;;'+sid_text.value+')'
            check(a.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl,1,c.byref(sd),None))
            check(a.SetKernelObjectSecurity(restricted,0x80000005,sd))
            # Elevated tokens normally default new objects to BA/SYSTEM.
            # BA is deny-only in the child: retain the same user as owner and
            # grant that user access to its own process/thread/token objects.
            owner_pointer=w.LPVOID(owner)
            check(a.SetTokenInformation(restricted,4,c.byref(owner_pointer),c.sizeof(owner_pointer)))
            present=w.BOOL();defaulted=w.BOOL();acl=w.LPVOID()
            check(a.GetSecurityDescriptorDacl(sd,c.byref(present),c.byref(acl),c.byref(defaulted)))
            if not present.value or not acl:raise ValueError('RESTRICTED_DEFAULT_DACL_MISSING')
            check(a.SetTokenInformation(restricted,6,c.byref(acl),c.sizeof(acl)))
        finally:
            if sd:k.LocalFree(sd)
            k.LocalFree(c.cast(sid_text,w.HANDLE))
        check(a.ConvertStringSidToSidW('S-1-16-8192',c.byref(medium)))
        label=SIDAttrs(medium,0x20)
        check(a.SetTokenInformation(restricted,25,c.byref(label),c.sizeof(label)+a.GetLengthSid(medium)))
        startup=Startup();startup.cb=c.sizeof(startup);startup.desktop=''
        line=c.create_unicode_buffer(subprocess.list2cmdline(command))
        check(a.CreateProcessAsUserW(restricted,None,line,None,None,False,creationflags|0x08000000,None,cwd,c.byref(startup),c.byref(info)))
        k.CloseHandle(info.thread)
        return Process(k,info.process,info.pid)
    finally:
        for handle in (restricted,token):
            if handle:k.CloseHandle(handle)
        for sid in (admin,medium):
            if sid:k.LocalFree(sid)
