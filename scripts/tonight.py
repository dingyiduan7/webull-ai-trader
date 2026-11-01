"""One command for tonight (after 2:30 PM MT): lock diagnostic, SEC research data, then fund and insider-stock bars.
Read-only throughout; never places orders.   .venv\\Scripts\\python.exe scripts\\tonight.py"""
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
steps = [
    ('Lock diagnostic', ['scripts/diag_incidents.py']),
    ('SEC insider and earnings data', ['scripts/research_data.py', '--sec']),
    ('Fund and insider-stock prices', ['scripts/research_data.py', '--etf-bars', '--insider-bars']),
]
for name, args in steps:
    print('\n=== %s ===' % name, flush=True)
    code = subprocess.call([PY] + args, cwd=ROOT)
    print('=== %s: %s ===' % (name, 'done' if code == 0 else 'failed (exit %d), continuing' % code), flush=True)
print('\nAll done. Tell Claude "done".')
