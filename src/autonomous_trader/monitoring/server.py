"""Loopback-only GET dashboard. This module has no trading or secret handles."""
import json
import threading
import time
import traceback
from datetime import datetime,timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path

SNAPSHOT_CACHE_SECONDS=1.5
ERROR_LOG_LIMIT_BYTES=500_000


def _json_default(x):
    return str(x) if isinstance(x,Decimal) else x.isoformat()


def degraded_snapshot(code):
    """Same shape the page already renders when the ledger cannot be read; names the real reason."""
    return {'at':datetime.now(timezone.utc).isoformat(),
        'system':{'status':'ERROR','postgres':'UNKNOWN','market':'UNKNOWN','last_error':code,
            'activity':'Dashboard snapshot failed ('+code+'). The trader is not necessarily affected; see dashboard-errors.log.'},
        'account':None,'positions':[],'orders':[],'trades':[],'events':[],'curve':[],'risk':None,'brain':None,'analytics':{},
        'live_panel':{'status':'BLOCKED','blockers':['DASHBOARD_SNAPSHOT_FAILED:'+code]}}


class DashboardServer(ThreadingHTTPServer):
    daemon_threads=True
    def __init__(self,reader,port=8766):
        self.reader=reader;self._lock=threading.Lock();self._cache=(0.0,None)
        super().__init__(('127.0.0.1',port),Handler)

    def record_error(self,exc):
        """Append the exception class and traceback (no request data) so failures are never silent."""
        root=getattr(self.reader,'root',None)
        if root is None:return
        try:
            folder=Path(root)/'.local'/'dashboard';folder.mkdir(parents=True,exist_ok=True)
            path=folder/'dashboard-errors.log'
            if path.exists() and path.stat().st_size>ERROR_LOG_LIMIT_BYTES:path.replace(folder/'dashboard-errors.old.log')
            with path.open('a',encoding='utf-8') as out:
                out.write(datetime.now(timezone.utc).isoformat()+' '+type(exc).__name__+'\n'+''.join(traceback.format_exception(exc))+'\n')
        except Exception:pass

    def snapshot_bytes(self):
        """One snapshot at most every 1.5 s no matter how many tabs poll; a failure becomes a visible reason, not a dropped connection."""
        with self._lock:
            when,body=self._cache
            if body is not None and time.monotonic()-when<SNAPSHOT_CACHE_SECONDS:return body
            try:body=json.dumps(self.reader.snapshot(),default=_json_default).encode()
            except Exception as exc:
                self.record_error(exc);body=json.dumps(degraded_snapshot(type(exc).__name__),default=_json_default).encode()
            self._cache=(time.monotonic(),body);return body


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_POST(self):self.send_error(405)
    do_PUT=do_POST;do_DELETE=do_POST;do_PATCH=do_POST
    def do_GET(self):
        if self.headers.get('Host') not in {f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}'}:
            self.send_error(403);return
        if self.path=='/':
            body=Path(__file__).with_name('dashboard.html').read_bytes();mime='text/html; charset=utf-8'
        elif self.path=='/api/snapshot':
            body=self.server.snapshot_bytes();mime='application/json'
        else:self.send_error(404);return
        self.send_response(200);self.send_header('Content-Type',mime);self.send_header('Content-Length',str(len(body)))
        self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
        self.end_headers();self.wfile.write(body)
