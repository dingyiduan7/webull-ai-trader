"""Storage seam for the owner charter, plus validation-progress observation.

Replaces the original Windows-ACL/Windows-Hello trust boundary. Reads only
local files under the runtime data root; never contacts a broker, never
creates or relaxes authority. See owner_authorization.py for the trust model.
"""
from contextlib import contextmanager
import hashlib
from pathlib import Path
import threading

from . import owner_authorization as owner
from .runtime_storage import data_root

_validation_progress = threading.local()
_CODE_ROOT = Path(__file__).resolve().parents[2]


@contextmanager
def observe_validation_progress(callback):
    previous = getattr(_validation_progress, 'callback', None)
    _validation_progress.callback = callback
    try:
        yield
    finally:
        _validation_progress.callback = previous


def _validated_progress():
    callback = getattr(_validation_progress, 'callback', None)
    if callback is not None:
        callback()


def read_protected_authorization(root):
    """Return (trust, record) for the current owner charter.

    Missing or unreadable files fail closed with REAL_OWNER_TRUST_NOT_ENROLLED;
    nothing here can substitute a default approval.
    """
    folder = owner.owner_dir(data_root(root))
    try:
        public_pem = (folder / owner.PUBLIC_KEY_NAME).read_bytes()
        raw = (folder / owner.RECORD_NAME).read_bytes()
    except OSError:
        raise ValueError('REAL_OWNER_TRUST_NOT_ENROLLED') from None
    record = owner.parse(raw)
    trust = dict(schema='AUTOTRADER_OWNER_TRUST_V1',
                 public_key_pem=public_pem.decode('ascii', errors='strict'),
                 revoked_authority_ids=owner.read_revocations(data_root(root)))
    _validated_progress()
    return trust, record


def authority_revision(root=None):
    """Change detector only: did the charter, revocations, key, or deployment
    evidence change between two points in time? Establishes no authority."""
    base = data_root(root if root is not None else _CODE_ROOT)
    folder = owner.owner_dir(base)
    digest = hashlib.sha256()
    for path in (folder / owner.PUBLIC_KEY_NAME, folder / owner.RECORD_NAME,
                 folder / owner.REVOCATION_NAME, owner.evidence_path(base)):
        digest.update(path.name.encode('utf-8') + b'\x00')
        try:
            digest.update(hashlib.sha256(path.read_bytes()).digest())
        except OSError:
            digest.update(b'ABSENT')
    return digest.hexdigest()
