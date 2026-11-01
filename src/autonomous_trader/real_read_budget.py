"""Read-only quotas for the single REAL service; no order retry authority.

Limits: https://developer.webull.com/apis/docs/rate-limits/ (2026-10-05).
All clients in this process sharing an App Key share endpoint counters.
"""
from collections import defaultdict, deque
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
from math import isfinite
from threading import Lock
import time


class ReadRateLimited(ValueError):
    def __init__(self, path, retry_after):
        super().__init__('REAL_READ_RATE_LIMITED')
        self.path = path
        self.retry_after = retry_after


def limits(path):
    if path == '/trading/accounts/list':
        return 10, 30
    if path in ('/trading/assets/balances/get', '/trading/assets/positions/list',
                '/trading/orders/open-orders/list', '/trading/orders/get'):
        return 2, 2
    if path == '/trading/activities/cash-activities/list':
        return 600, 60
    return 60, 60


class ReadBudget:
    def __init__(self, clock=time.monotonic, sleep=time.sleep):
        self.clock, self.sleep = clock, sleep
        self.lock = Lock()
        self.calls = defaultdict(deque)
        self.blocked_until = defaultdict(float)

    def acquire(self, path, *, urgent=False):
        count, window = limits(path)
        # Leave ten snapshot reads for held/selected instruments. This is an
        # allocation of Webull quota, not a limit on symbols or trading decisions.
        if path == '/market-data/stocks/snapshots/list' and not urgent:
            count -= 10
        while True:
            with self.lock:
                now = self.clock()
                history = self.calls[path]
                while history and now >= history[0] + window + .05:
                    history.popleft()
                wait = max(0, self.blocked_until[path]-now)
                if len(history) >= count:
                    wait = max(wait, history[0]+window+.05-now)
                if not wait:
                    history.append(now)
                    return
            # Brief account polling can be paced; never sleep through a long
            # discovery quota window while a position might need supervision.
            if wait > 2.1:
                raise ReadRateLimited(path, wait)
            self.sleep(wait)

    def rejected(self, path, retry_after):
        delay = float(limits(path)[1])
        try:
            try:
                parsed = float(retry_after)
            except (TypeError, ValueError):
                parsed = (parsedate_to_datetime(retry_after)-datetime.now(timezone.utc)).total_seconds()
            if isfinite(parsed):
                delay = max(delay, parsed)
        except (TypeError, ValueError, OverflowError, AttributeError):
            pass
        with self.lock:
            self.blocked_until[path] = max(self.blocked_until[path], self.clock()+delay)
        return ReadRateLimited(path, delay)


_budgets = {}
_lock = Lock()


def shared_budget(app_key):
    # Identity stays in RAM; neither the key nor its fingerprint is logged.
    identity = sha256(app_key.encode()).digest()
    with _lock:
        return _budgets.setdefault(identity, ReadBudget())
