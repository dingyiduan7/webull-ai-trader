"""Exercise only extracted pure file transaction; installer top-level never runs."""
import ast
from hashlib import sha256
import json
from pathlib import Path
import shutil
import tempfile
import pytest


def transaction():
    source=Path(__file__).resolve().parents[1]/'install_reviewed.py'
    function=next(n for n in ast.parse(source.read_text()).body if isinstance(n,ast.FunctionDef) and n.name=='transactional_replace')
    namespace=dict(Path=Path,sha256=sha256,json=json,shutil=shutil,tempfile=tempfile)
    exec(compile(ast.Module(body=[function],type_ignores=[]),str(source),'exec'),namespace)
    return namespace['transactional_replace']


@pytest.mark.parametrize('failure',['mid-copy','post-authority'])
def test_failure_restores_base_and_authority_ignoring_stale_absent_file_backup(tmp_path,failure):
    root=tmp_path/'installed';stage=tmp_path/'staged';root.mkdir();stage.mkdir()
    (root/'existing.py').write_bytes(b'base');(stage/'existing.py').write_bytes(b'new')
    (stage/'added.py').write_bytes(b'new-file')
    stale=stage/'installed-before';stale.mkdir();(stale/'added.py').write_bytes(b'unrelated stale backup')
    authority=root/'authority.json';authority.write_bytes(b'old-authority')
    def copy(a,b):
        shutil.copyfile(a,b)
        if failure=='mid-copy' and a.name=='added.py':raise OSError('injected-copy-failure')
    def validate():
        assert authority.read_bytes()==b'new-authority'
        raise ValueError('injected-final-validation-failure')
    with pytest.raises((OSError,ValueError)):
        transaction()(root,stage,['existing.py','added.py'],authority,b'new-authority',
            {'existing.py':sha256(b'base').hexdigest(),'added.py':None},validate,copy_file=copy)
    assert (root/'existing.py').read_bytes()==b'base'
    assert not (root/'added.py').exists()
    assert authority.read_bytes()==b'old-authority'
    assert (stale/'added.py').read_bytes()==b'unrelated stale backup'
    backups=list(stage.glob('installed-before-*'));assert len(backups)==1
    manifest=json.loads((backups[0]/'ROLLBACK_MANIFEST.json').read_text())
    assert manifest['files']['added.py']=={'existed':False,'sha256':None}


def test_changed_base_rejects_before_any_replacement(tmp_path):
    root=tmp_path/'installed';stage=tmp_path/'staged';root.mkdir();stage.mkdir()
    (root/'one.py').write_bytes(b'changed');(stage/'one.py').write_bytes(b'candidate')
    authority=root/'authority';authority.write_bytes(b'authority')
    with pytest.raises(ValueError,match='ROLLBACK_BASE_CHANGED'):
        transaction()(root,stage,['one.py'],authority,b'new',{'one.py':'wrong'},lambda:None)
    assert (root/'one.py').read_bytes()==b'changed' and authority.read_bytes()==b'authority'
