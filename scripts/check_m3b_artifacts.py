"""Owner-local M3B security check. Only counts/booleans leave this process."""
from pathlib import Path
import hashlib
import json
import sqlite3
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from autonomous_trader.sandbox.secrets import load_credentials, PreflightError
from autonomous_trader.sandbox.sanitize import Sanitizer, SECRET, normalize


def main():
    credentials=load_credentials(ROOT)
    sanitizer=Sanitizer(credentials.sensitive_values())
    files=[p for folder in ('src','tests','scripts','docs') for p in (ROOT/folder).rglob('*')
           if p.is_file() and p.suffix in ('.py','.json','.md','.toml','.txt','.xml','.ps1','.sql')]
    files += [ROOT/n for n in ('README.md','OPEN_DECISIONS.md','pyproject.toml','requirements.txt','.gitignore')]
    for path in files: sanitizer.assert_clean(path.read_text(encoding='utf-8-sig'))
    def no_credentials(value):
        if isinstance(value,dict):
            if any(SECRET.search(normalize(k)) for k in value):
                raise PreflightError('CREDENTIAL_FIELD_IN_EVIDENCE')
            for v in value.values(): no_credentials(v)
        elif isinstance(value,list):
            for v in value: no_credentials(v)
    def walk(value):
        if isinstance(value,dict):
            for name in ('body','headers','payload'):
                if name in value: no_credentials(value[name])
            for v in value.values(): walk(v)
        elif isinstance(value,list):
            for v in value: walk(v)
    fixtures=list((ROOT/'tests/fixtures/webull_m3b').glob('*/*.json'))
    for path in fixtures: walk(json.loads(path.read_text()))
    private_db=ROOT/'.local/m3b/certification.sqlite3'
    with sqlite3.connect(private_db.as_uri()+'?mode=ro',uri=True) as db:
        sanitizer.assert_clean('\n'.join(db.iterdump()))
        journal_events=db.execute('SELECT COUNT(*) FROM events').fetchone()[0]
        claims=dict(db.execute('SELECT kind,COUNT(*) FROM intents WHERE claimed=1 GROUP BY kind').fetchall())
    if list(ROOT.glob('webull*_sdk.log*')) or (ROOT/'token.json').exists() or (ROOT/'conf/token.txt').exists():
        raise PreflightError('UNEXPECTED_SDK_LOG_OR_TOKEN_CACHE')
    source_hashes={n:hashlib.sha256((ROOT/n).read_bytes()).hexdigest() for n in
                   ('autonomous-trader-architecture.md','trade-decision.schema.json')}
    expected=json.loads((ROOT/'docs/m3a-security-checks.json').read_text())['source_hashes']
    if source_hashes != expected: raise PreflightError('SOURCE_OF_TRUTH_CHANGED')
    result={'result':'PASSED','files_scanned':len(files),'m3b_fixture_files_checked':len(fixtures),
        'current_known_secret_or_account_id_matches':0,'credential_fields_in_fixture_payloads':0,
        'journal_events_scanned':journal_events,'persistent_claim_counts':claims,'source_hashes':source_hashes,
        'sdk_log_or_token_cache_found':False,'credentials_in_postgresql':False,
        'limits':'Exact current-value and credential-field scan, not a universal secret detector. M3B has no PostgreSQL write path; runtime credentials remain memory/DPAPI only.'}
    (ROOT/'docs/m3b-security-checks.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({'security_check':'PASSED','files_scanned':len(files),'m3b_fixtures_checked':len(fixtures)}))


if __name__=='__main__':
    try: main()
    except Exception:
        print('{"security_check":"FAILED_DETAILS_SUPPRESSED"}')
        sys.exit(1)
