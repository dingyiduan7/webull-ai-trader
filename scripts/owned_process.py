"""Contain a child tree before its first instruction; no trading imports."""
import ctypes
from ctypes import wintypes as w
import subprocess
import time


class Limits(ctypes.Structure):
    _fields_ = [('process_time', ctypes.c_longlong), ('job_time', ctypes.c_longlong),
                ('flags', w.DWORD), ('min_ws', ctypes.c_size_t), ('max_ws', ctypes.c_size_t),
                ('process_limit', w.DWORD), ('affinity', ctypes.c_size_t),
                ('priority', w.DWORD), ('scheduling', w.DWORD)]


class IO(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in
                ('read_ops', 'write_ops', 'other_ops', 'read_bytes', 'write_bytes', 'other_bytes')]


class ExtendedLimits(ctypes.Structure):
    _fields_ = [('basic', Limits), ('io', IO), ('process_mem', ctypes.c_size_t),
                ('job_mem', ctypes.c_size_t), ('peak_process', ctypes.c_size_t),
                ('peak_job', ctypes.c_size_t)]


class Accounting(ctypes.Structure):
    _fields_ = [(name, ctypes.c_longlong) for name in ('user', 'kernel', 'period_user', 'period_kernel')] + [
        (name, w.DWORD) for name in ('faults', 'total', 'active', 'terminated')]


class ThreadEntry(ctypes.Structure):
    _fields_ = [(name, w.DWORD) for name in ('size', 'usage', 'tid', 'pid')] + [
        ('base_priority', w.LONG), ('delta_priority', w.LONG), ('flags', w.DWORD)]


def kernel():
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    signatures = {
        'CreateJobObjectW': ([ctypes.c_void_p, w.LPCWSTR], w.HANDLE),
        'SetInformationJobObject': ([w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD], w.BOOL),
        'AssignProcessToJobObject': ([w.HANDLE, w.HANDLE], w.BOOL),
        'TerminateJobObject': ([w.HANDLE, w.UINT], w.BOOL),
        'QueryInformationJobObject': ([w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.c_void_p], w.BOOL),
        'CloseHandle': ([w.HANDLE], w.BOOL),
        'CreateToolhelp32Snapshot': ([w.DWORD, w.DWORD], w.HANDLE),
        'Thread32First': ([w.HANDLE, ctypes.POINTER(ThreadEntry)], w.BOOL),
        'Thread32Next': ([w.HANDLE, ctypes.POINTER(ThreadEntry)], w.BOOL),
        'OpenThread': ([w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
        'ResumeThread': ([w.HANDLE], w.DWORD),
    }
    for name, (args, result) in signatures.items():
        function = getattr(api, name); function.argtypes = args; function.restype = result
    return api


class OwnedProcess:
    def __init__(self, process, job, api):
        self.process, self.job, self.api = process, job, api
        self.pid = process.pid

    def poll(self):
        return self.process.poll()

    def wait(self, *args, **kwargs):
        return self.process.wait(*args, **kwargs)

    def stop_tree(self):
        if self.job is None:
            return
        if not self.api.TerminateJobObject(self.job, 2):
            raise ctypes.WinError(ctypes.get_last_error())
        deadline = time.monotonic() + 15  # Existing owned-child cleanup timeout.
        while True:
            state = Accounting()
            if not self.api.QueryInformationJobObject(self.job, 1, ctypes.byref(state), ctypes.sizeof(state), None):
                raise ctypes.WinError(ctypes.get_last_error())
            if state.active == 0:
                self.process.wait(timeout=15)
                self.api.CloseHandle(self.job); self.job = None
                return
            if time.monotonic() >= deadline:
                raise RuntimeError('OWNED_PROCESS_TERMINATION_UNCONFIRMED')
            time.sleep(.05)


def confirm_stop(child):
    """Remain blocked on ownership uncertainty; never replace an unconfirmed tree."""
    while True:
        try:
            child.stop_tree()
            return
        except Exception:
            time.sleep(2)


def spawn(command, *, stop=None, process_factory=None, **kwargs):
    api = kernel()
    job = api.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    child = None
    assigned = False
    try:
        limits = ExtendedLimits(); limits.basic.flags = 0x2000  # KILL_ON_JOB_CLOSE; no breakaway.
        if not api.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise ctypes.WinError(ctypes.get_last_error())
        kwargs['creationflags'] = kwargs.get('creationflags', 0) | 4  # CREATE_SUSPENDED
        child = (process_factory or subprocess.Popen)(command, **kwargs)
        if not api.AssignProcessToJobObject(job, w.HANDLE(int(child._handle))):
            raise ctypes.WinError(ctypes.get_last_error())
        assigned = True
        snapshot = api.CreateToolhelp32Snapshot(4, 0)
        if snapshot == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            entry = ThreadEntry(); entry.size = ctypes.sizeof(entry); tids = []
            found = api.Thread32First(snapshot, ctypes.byref(entry))
            while found:
                if entry.pid == child.pid:
                    tids.append(entry.tid)
                found = api.Thread32Next(snapshot, ctypes.byref(entry))
        finally:
            api.CloseHandle(snapshot)
        if len(tids) != 1:
            raise RuntimeError('SUSPENDED_CHILD_IDENTITY_UNCONFIRMED')
        thread = api.OpenThread(2, False, tids[0])
        if not thread:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if stop is not None and stop.is_set():
                raise RuntimeError('SERVICE_STOP_REQUESTED')
            if api.ResumeThread(thread) != 1:
                raise RuntimeError('SUSPENDED_CHILD_RESUME_UNCONFIRMED')
        finally:
            api.CloseHandle(thread)
        return OwnedProcess(child, job, api)
    except BaseException:
        # A failed containment setup never releases application code to run.
        if assigned:
            confirm_stop(OwnedProcess(child, job, api))
        else:
            try:
                if child is not None:
                    child.kill(); child.wait(timeout=15)
            finally:
                api.CloseHandle(job)
        raise
