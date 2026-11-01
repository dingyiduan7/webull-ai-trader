"""Release bundles: verified, transactional, restricted paths, rollback on validation failure."""
from hashlib import sha256
import json

import pytest

import install_reviewed as installer


def h(data):
    return sha256(data).hexdigest()


def bundle(root, release_id, files, *, tamper=None):
    folder = root / '.local/releases/incoming' / release_id
    rows = {}
    for name, (new, base) in files.items():
        target = folder / 'files' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(new if tamper != name else b'tampered')
        rows[name] = {'sha256': h(new), 'base_sha256': None if base is None else h(base)}
    (folder / 'release.json').write_text(json.dumps({'schema': 'AUTOTRADER_RELEASE_V1', 'release_id': release_id,
                                                     'files': rows}))
    return folder


def tree(tmp_path):
    root = tmp_path / 'repo'
    (root / 'src/autonomous_trader').mkdir(parents=True)
    (root / 'src/autonomous_trader/a.py').write_bytes(b'old')
    return root


def test_bundle_applies_and_records_release(tmp_path):
    root = tree(tmp_path)
    bundle(root, 'r6', {'src/autonomous_trader/a.py': (b'new', b'old'), 'scripts/b.py': (b'added', None)})
    results = installer.apply_pending(root, validate=lambda: None)
    assert results == [{'bundle': 'r6', 'status': 'APPLIED', 'release_id': 'r6'}]
    assert (root / 'src/autonomous_trader/a.py').read_bytes() == b'new' and (root / 'scripts/b.py').read_bytes() == b'added'
    assert json.loads((root / '.local/releases/current.json').read_text())['release_id'] == 'r6'
    assert (root / '.local/releases/applied/r6/RESULT.json').is_file()
    assert not list((root / '.local/releases/incoming').iterdir())


@pytest.mark.parametrize('case,reason', [('tamper', 'RELEASE_FILE_HASH_MISMATCH'), ('base', 'ROLLBACK_BASE_CHANGED'),
                                         ('secret', 'RELEASE_PATH_OR_ROW_INVALID'), ('escape', 'RELEASE_PATH_OR_ROW_INVALID'),
                                         ('validate', 'RELEASE_VALIDATION_FAILED')])
def test_rejected_bundles_change_nothing_and_stop_the_queue(tmp_path, case, reason):
    root = tree(tmp_path)
    files = {'src/autonomous_trader/a.py': (b'new', b'old' if case != 'base' else b'other')}
    if case == 'secret':
        files = {'.secrets/webull-real.clixml': (b'x', None)}
    if case == 'escape':
        files = {'src/autonomous_trader/../../outside.py': (b'x', None)}
    bundle(root, 'r6', files, tamper='src/autonomous_trader/a.py' if case == 'tamper' else None)
    bundle(root, 'r7', {'scripts/c.py': (b'later', None)})

    def validate():
        raise ValueError('RELEASE_VALIDATION_FAILED')
    results = installer.apply_pending(root, validate=validate if case == 'validate' else (lambda: None))
    assert results == [{'bundle': 'r6', 'status': 'REJECTED', 'reason': reason}]
    assert (root / 'src/autonomous_trader/a.py').read_bytes() == b'old'
    assert not (root / '.local/releases/current.json').exists()
    assert (root / '.local/releases/rejected/r6/RESULT.json').is_file()
    assert (root / '.local/releases/incoming/r7').is_dir()  # waits until r6 is resolved


def test_real_tree_validation_passes_on_this_repository():
    installer._validate_tree(installer.ROOT)
