"""Windows service plumbing. Availability is never trading authorization.

This module has no broker, credential, model, ledger, or trading imports.
The SCM owns this process; the application owns its existing child supervisor.
"""
import threading


def serve(worker, *, service_name='AITrader'):
    import ctypes
    from ctypes import wintypes as w

    STOPPED, START_PENDING, STOP_PENDING, RUNNING = 1, 2, 3, 4
    stop = threading.Event()
    result = {'code': 0}
    api = ctypes.WinDLL('advapi32', use_last_error=True)
    HANDLER = ctypes.WINFUNCTYPE(w.DWORD, w.DWORD, w.DWORD, w.LPVOID, w.LPVOID)
    MAIN = ctypes.WINFUNCTYPE(None, w.DWORD, ctypes.POINTER(w.LPWSTR))

    class Status(ctypes.Structure):
        _fields_ = [(name, w.DWORD) for name in (
            'service_type', 'state', 'accepted', 'win32_exit',
            'service_exit', 'checkpoint', 'wait_hint')]

    class Entry(ctypes.Structure):
        _fields_ = [('name', w.LPWSTR), ('main', MAIN)]

    api.RegisterServiceCtrlHandlerExW.argtypes = [w.LPCWSTR, HANDLER, w.LPVOID]
    api.RegisterServiceCtrlHandlerExW.restype = w.HANDLE
    api.SetServiceStatus.argtypes = [w.HANDLE, ctypes.POINTER(Status)]
    api.SetServiceStatus.restype = w.BOOL
    api.StartServiceCtrlDispatcherW.argtypes = [ctypes.POINTER(Entry)]
    api.StartServiceCtrlDispatcherW.restype = w.BOOL
    handle = None
    lock = threading.Lock()
    status = Status(0x10, START_PENDING, 0, 0, 0, 1, 30000)

    def publish(state, code=0):
        nonlocal status
        with lock:
            if status.state == STOPPED:
                return
            pending = state in (START_PENDING, STOP_PENDING)
            status = Status(0x10, state, 5 if state == RUNNING else 0,
                            1066 if code else 0, code,
                            status.checkpoint + 1 if pending else 0,
                            30000 if pending else 0)
            if not api.SetServiceStatus(handle, ctypes.byref(status)):
                raise ctypes.WinError(ctypes.get_last_error())

    @HANDLER
    def handler(control, event_type, event_data, context):
        if control in (1, 5):  # STOP or SHUTDOWN; cooperative application cleanup.
            try:
                publish(STOP_PENDING)
            except OSError:
                result['code'] = 2
            finally:
                stop.set()
        elif control == 4:  # INTERROGATE
            with lock:
                if status.state != STOPPED:
                    api.SetServiceStatus(handle, ctypes.byref(status))

        return 0

    @MAIN
    def main(argc, argv):
        nonlocal handle
        handle = api.RegisterServiceCtrlHandlerExW(service_name, handler, None)
        if not handle:
            result['code'] = 2
            return
        finished = threading.Event()

        def work():
            try:
                worker(stop)
                if not stop.is_set():
                    result['code'] = 2  # Unexpected return is an SCM failure.
            except BaseException:
                result['code'] = 2  # Never disclose exception/credential text.
            finally:
                finished.set()

        thread = None
        try:
            publish(RUNNING)
            thread = threading.Thread(target=work, name='AITraderResident')
            thread.start()
            finished.wait()  # Only actual transitions advance SCM checkpoints.
            thread.join()
        except BaseException:
            stop.set()
            result['code'] = 2
        finally:
            if thread is not None:
                thread.join()  # No STOPPED report while cleanup is still running.
            try:
                publish(STOPPED, result['code'])
            except OSError:
                result['code'] = 2

    table = (Entry * 2)(Entry(service_name, main), Entry(None, MAIN()))
    if not api.StartServiceCtrlDispatcherW(table):
        raise ctypes.WinError(ctypes.get_last_error())
    return result['code']
