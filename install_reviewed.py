"""Self-applying release installer for Auto Trader (no owner action per release).

Claude delivers a release as a bundle under .local/releases/incoming/<release-id>/:
    release.json   {"schema": "AUTOTRADER_RELEASE_V1", "release_id": ..., "files":
                    {"<relative path>": {"sha256": "<new>", "base_sha256": "<expected current or null>"}}}
    files/<relative path>...   the new file contents
The daily task calls apply_pending() at the only safe point: the first start of a
trading day, before any market read, model call or order (no overnight positions
exist by policy). Each bundle is applied as one transaction: every current file is
checked against its expected base first; old files and the release record are backed
up with a manifest; new files are copied; the installed tree is validated in a fresh
interpreter; any failure restores the exact previous state. Applied and rejected
bundles are moved out of incoming/ with a result file. Paths are restricted to code,
tests and docs: secrets and runtime data can never be replaced.
"""
from hashlib import sha256
from pathlib import Path
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent
RELEASE_SCHEMA = 'AUTOTRADER_RELEASE_V1'
ALLOWED = re.compile(r'(src/autonomous_trader/[A-Za-z0-9_./-]+\.(py|json|sql)|scripts/[A-Za-z0-9_.-]+\.(py|ps1)'
                     r'|tests/[A-Za-z0-9_./-]+\.py|docs/[A-Za-z0-9_.-]+\.md|install_reviewed\.py'
                     r'|pyproject\.toml|requirements\.txt|[a-z-]+\.schema\.json)')


def transactional_replace(root, stage, names, authority, authority_bytes, expected, validate, copy_file=shutil.copyfile):
    """Replace `names` under root with stage copies, write authority, validate; all or nothing."""
    root, stage, authority = Path(root), Path(stage), Path(authority)

    def digest(path):
        return sha256(path.read_bytes()).hexdigest() if path.is_file() else None
    for name in names:
        if digest(root / name) != expected[name]:
            raise ValueError('ROLLBACK_BASE_CHANGED')
    backup = Path(tempfile.mkdtemp(prefix='installed-before-', dir=stage))
    manifest = {'files': {}, 'authority': {'existed': authority.is_file()}}
    for name in names:
        current = root / name
        manifest['files'][name] = {'existed': current.is_file(), 'sha256': digest(current)}
        if current.is_file():
            (backup / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(current, backup / name)
    if authority.is_file():
        shutil.copyfile(authority, backup / 'AUTHORITY.bak')
    (backup / 'ROLLBACK_MANIFEST.json').write_text(json.dumps(manifest, indent=2, sort_keys=True))
    try:
        for name in names:
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            copy_file(stage / name, root / name)
        authority.parent.mkdir(parents=True, exist_ok=True)
        authority.write_bytes(authority_bytes)
        validate()
    except BaseException:
        for name, row in manifest['files'].items():
            target = root / name
            if row['existed']:
                shutil.copyfile(backup / name, target)
            elif target.exists():
                target.unlink()
        if manifest['authority']['existed']:
            shutil.copyfile(backup / 'AUTHORITY.bak', authority)
        elif authority.exists():
            authority.unlink()
        raise
    return backup


def _validate_tree(root):
    """Fresh interpreter: every Python file compiles and the runtime entry modules import."""
    code = ('import sys,compileall,pathlib;r=pathlib.Path(sys.argv[1]);sys.path[:0]=[str(r),str(r/"src")];'
            'ok=compileall.compile_dir(str(r/"src"),quiet=1) and compileall.compile_dir(str(r/"scripts"),quiet=1);'
            'import autonomous_trader.real_daily,autonomous_trader.real_runner,autonomous_trader.decision_api.brain;'
            'from autonomous_trader.real_trading import build_digest;build_digest(r);sys.exit(0 if ok else 3)')
    result = subprocess.run([sys.executable, '-I', '-B', '-c', code, str(root)], cwd=root,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300)
    if result.returncode:
        raise ValueError('RELEASE_VALIDATION_FAILED')


def _load_bundle(bundle):
    release = json.loads((bundle / 'release.json').read_text(encoding='utf-8'))
    if (release.get('schema') != RELEASE_SCHEMA or not re.fullmatch(r'[A-Za-z0-9._-]{1,80}', str(release.get('release_id')))
            or not isinstance(release.get('files'), dict) or not release['files']):
        raise ValueError('RELEASE_MANIFEST_INVALID')
    for name, row in release['files'].items():
        if (not ALLOWED.fullmatch(name) or '..' in name.split('/') or not isinstance(row, dict)
                or set(row) != {'sha256', 'base_sha256'}):
            raise ValueError('RELEASE_PATH_OR_ROW_INVALID')
        staged = bundle / 'files' / name
        if not staged.is_file() or sha256(staged.read_bytes()).hexdigest() != row['sha256']:
            raise ValueError('RELEASE_FILE_HASH_MISMATCH')
    return release


def apply_pending(root=ROOT, validate=None):
    """Apply every incoming bundle in name order; stop at the first failure. Returns results."""
    root = Path(root)
    base = root / '.local' / 'releases'
    incoming = base / 'incoming'
    results = []
    if not incoming.is_dir():
        return results
    for bundle in sorted(p for p in incoming.iterdir() if p.is_dir()):
        result = {'bundle': bundle.name}
        try:
            release = _load_bundle(bundle)
            names = sorted(release['files'])
            record = json.dumps({'release_id': release['release_id'], 'files': release['files']},
                                indent=2, sort_keys=True).encode('utf-8')
            transactional_replace(root, bundle / 'files', names, base / 'current.json', record,
                                  {n: release['files'][n]['base_sha256'] for n in names},
                                  validate or (lambda: _validate_tree(root)))
            result.update(status='APPLIED', release_id=release['release_id'])
            destination = base / 'applied'
        except Exception as exc:
            text = str(exc)
            result.update(status='REJECTED', reason=text if isinstance(exc, ValueError) and text.isupper() else type(exc).__name__)
            destination = base / 'rejected'
        destination.mkdir(parents=True, exist_ok=True)
        target = destination / bundle.name
        if target.exists():
            target = destination / (bundle.name + '-' + os.urandom(4).hex())
        shutil.move(str(bundle), str(target))
        (target / 'RESULT.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
        results.append(result)
        if result['status'] != 'APPLIED':
            break  # later bundles may depend on this one
    return results


if __name__ == '__main__':
    if sys.argv[1:] != ['--apply-pending']:
        raise SystemExit('usage: install_reviewed.py --apply-pending')
    print(json.dumps(apply_pending()))
