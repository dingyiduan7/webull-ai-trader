"""Owner approval 2026-10-19 ("rate-limited reads are retried instead of logged as incidents"). On Oct 23 and 26 the
first startup read was rate-limited while a position was held; the child exited and restarted as recovery-only,
which cannot trade for the rest of the day. The startup refresh now waits out Webull's retry window (up to 4 tries)."""
from pathlib import Path

SRC = (Path(__file__).resolve().parents[1] / 'scripts' / 'run_real_trader.py').read_text(encoding='utf-8')


def test_startup_refresh_retries_rate_limits_before_failing():
    block = SRC[SRC.index('for attempt in range(4):'):SRC.index('controller.refresh(packet)', SRC.index('for attempt in range(4):'))]
    assert 'except ReadRateLimited as limited:' in block
    assert "if attempt==3:raise" in block                       # still fails loudly after the last try
    assert "startup_progress('STARTUP_READ_RATE_LIMITED_RETRY')" in block   # keeps the startup heartbeat alive
    assert 'time.sleep(min(60,max(5,limited.retry_after)))' in block       # under the 120 s hung-start limit
