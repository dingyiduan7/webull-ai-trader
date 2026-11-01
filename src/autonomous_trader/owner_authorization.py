"""Auto Trader owner authorization: the money boundary only the owner can sign.

Trust model (owner decision, 2026-10-12)
----------------------------------------
* The OWNER signs a "charter" once with an Ed25519 key whose private half is
  passphrase-encrypted and kept outside the repository. The charter fixes the
  money boundary: which account, how much capital, which hard risk policy, no
  overnight exposure, and an expiry. Changing any of those requires the owner
  to sign again.
* The ENGINEER (Claude) controls code releases. A charter is deliberately NOT
  bound to a code build, so shipping a release never needs the owner. The
  running build is bound live by ``real_owner_authorization`` instead, which
  still forces fresh daily readiness evidence after every release.
* The owner can stop trading at any time without the key by revoking the
  charter (``scripts/owner_revoke_authorization.py``).

What this protects against: forged, stale, expired, revoked, or wrong-account
approvals, and any change to capital or hard risk policy without the owner.
What it does not protect against: an engineer with write access to this
repository editing the code that performs these checks. The owner chose that
trade-off to avoid a per-release elevated install; the release log is the
visibility mechanism. This module never talks to a broker or moves money.
"""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

RECORD_SCHEMA = 'AUTOTRADER_OWNER_RECORD_V1'
PAYLOAD_SCHEMA = 'AUTOTRADER_OWNER_AUTHORIZATION_V1'
REVOCATION_SCHEMA = 'AUTOTRADER_OWNER_REVOCATIONS_V1'
EVIDENCE_SCHEMA = 'AUTOTRADER_DEPLOYMENT_EVIDENCE_V1'
DOMAIN = b'Auto Trader owner charter v1\x00'

PUBLIC_KEY_NAME = 'owner_public_key.pem'
RECORD_NAME = 'owner-authority.json'
REVOCATION_NAME = 'revoked.json'
EVIDENCE_NAME = 'deployment-evidence.json'

# Fields the owner signs. Engineer-maintained fields (build binding, flat
# checkpoint, market-data entitlement evidence) are assembled at verification.
CHARTER_FIELDS = frozenset({'schema', 'verdict', 'authority_id', 'account_binding',
                            'scope', 'issued_at', 'expires_at', 'revoked'})
ENGINEER_FIELDS = frozenset({'build_sha256', 'flat_checkpoint_sha256', 'entitlement_evidence'})
MAX_RECORD_BYTES = 262144


def _fail(code):
    raise ValueError(code)


def owner_dir(data_root: Path) -> Path:
    return Path(data_root) / '.local' / 'owner-authority'


def evidence_path(data_root: Path) -> Path:
    return Path(data_root) / '.local' / 'real-readiness' / EVIDENCE_NAME


def _pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            _fail('REAL_OWNER_DUPLICATE_JSON_KEY')
        value[key] = item
    return value


def parse(raw):
    if isinstance(raw, bytes):
        try:
            raw = raw.decode('utf-8')
        except UnicodeError:
            _fail('REAL_OWNER_INVALID_JSON')
    if len(raw) > MAX_RECORD_BYTES:
        _fail('REAL_OWNER_RECORD_TOO_LARGE')
    try:
        return json.loads(raw, object_pairs_hook=_pairs,
                          parse_constant=lambda _: _fail('REAL_OWNER_INVALID_JSON'))
    except json.JSONDecodeError:
        _fail('REAL_OWNER_INVALID_JSON')


def canonical(value) -> bytes:
    """Deterministic bytes; floats are rejected so money stays in exact strings."""
    def check(item):
        if isinstance(item, float):
            _fail('REAL_OWNER_FLOAT_NOT_ALLOWED')
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                _fail('REAL_OWNER_INVALID_JSON')
            for child in item.values():
                check(child)
        elif isinstance(item, list):
            for child in item:
                check(child)
    check(value)
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=True, allow_nan=False).encode('ascii')


def encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii')


def decode(value) -> bytes:
    try:
        raw = base64.b64decode(value + '=' * (-len(value) % 4), altchars=b'-_', validate=True)
        if encode(raw) != value:
            raise ValueError()
        return raw
    except (TypeError, ValueError):
        _fail('REAL_OWNER_INVALID_ENCODING')


def record_hash(record) -> str:
    return hashlib.sha256(canonical(record)).hexdigest()


def _message(payload) -> bytes:
    return DOMAIN + canonical(payload)


def public_key_pem(private_key: Ed25519PrivateKey) -> bytes:
    return private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo)


def load_public_key(pem) -> Ed25519PublicKey:
    try:
        key = serialization.load_pem_public_key(pem if isinstance(pem, bytes) else pem.encode('ascii'))
    except (ValueError, TypeError):
        _fail('REAL_OWNER_KEY_INVALID')
    if not isinstance(key, Ed25519PublicKey):
        _fail('REAL_OWNER_KEY_INVALID')
    return key


def split_authority(authority: dict):
    """Split a full runtime authority into (owner charter, engineer evidence)."""
    if not isinstance(authority, dict) or set(authority) != CHARTER_FIELDS | ENGINEER_FIELDS:
        _fail('REAL_OWNER_AUTHORITY_SHAPE')
    charter = {k: authority[k] for k in CHARTER_FIELDS}
    evidence = dict(schema=EVIDENCE_SCHEMA,
                    flat_checkpoint_sha256=authority['flat_checkpoint_sha256'],
                    entitlement_evidence=authority['entitlement_evidence'])
    return charter, evidence


def sign_charter(private_key: Ed25519PrivateKey, charter: dict) -> dict:
    # Shape only. Semantic validity (GO verdict, not revoked, scope == code
    # scope, validity window) is enforced at use by real_daily.
    if not isinstance(charter, dict) or set(charter) != CHARTER_FIELDS:
        _fail('REAL_OWNER_CHARTER_SHAPE')
    payload = dict(schema=PAYLOAD_SCHEMA, action='AUTHORIZE', charter=charter)
    return dict(schema=RECORD_SCHEMA, payload=payload,
                signature=encode(private_key.sign(_message(payload))))


def verify_record(record, trust) -> dict:
    """Return the signed charter, or raise a fixed REAL_OWNER_* code."""
    try:
        if not isinstance(trust, dict) or trust.get('schema') != 'AUTOTRADER_OWNER_TRUST_V1':
            _fail('REAL_OWNER_TRUST_INVALID')
        if not isinstance(record, dict) or set(record) != {'schema', 'payload', 'signature'}:
            _fail('REAL_OWNER_RECORD_INVALID')
        if record['schema'] != RECORD_SCHEMA:
            _fail('REAL_OWNER_RECORD_INVALID')
        payload = record['payload']
        if (not isinstance(payload, dict) or set(payload) != {'schema', 'action', 'charter'}
                or payload['schema'] != PAYLOAD_SCHEMA or payload['action'] != 'AUTHORIZE'):
            _fail('REAL_OWNER_PAYLOAD_INVALID')
        charter = payload['charter']
        if not isinstance(charter, dict) or set(charter) != CHARTER_FIELDS:
            _fail('REAL_OWNER_PAYLOAD_INVALID')
        key = load_public_key(trust['public_key_pem'])
        key.verify(decode(record['signature']), _message(payload))
        revoked = trust.get('revoked_authority_ids', [])
        if not isinstance(revoked, list):
            _fail('REAL_OWNER_REVOCATIONS_INVALID')
        if charter['authority_id'] in revoked:
            _fail('REAL_OWNER_AUTHORIZATION_REVOKED')
        return charter
    except InvalidSignature:
        _fail('REAL_OWNER_SIGNATURE_INVALID')
    except (KeyError, TypeError, AttributeError):
        _fail('REAL_OWNER_RECORD_INVALID')


def read_revocations(data_root: Path) -> list:
    path = owner_dir(data_root) / REVOCATION_NAME
    if not path.exists():
        return []
    try:
        value = parse(path.read_bytes())
    except OSError:
        _fail('REAL_OWNER_REVOCATIONS_INVALID')
    if (not isinstance(value, dict) or value.get('schema') != REVOCATION_SCHEMA
            or not isinstance(value.get('revoked_authority_ids'), list)
            or not all(isinstance(x, str) for x in value['revoked_authority_ids'])):
        _fail('REAL_OWNER_REVOCATIONS_INVALID')
    return value['revoked_authority_ids']


def write_revocation(data_root: Path, authority_id: str) -> list:
    ids = set(read_revocations(data_root))
    ids.add(authority_id)
    folder = owner_dir(data_root)
    folder.mkdir(parents=True, exist_ok=True)
    value = dict(schema=REVOCATION_SCHEMA, revoked_authority_ids=sorted(ids))
    _atomic_write(folder / REVOCATION_NAME, json.dumps(value, indent=2).encode('utf-8'))
    return value['revoked_authority_ids']


def load_deployment_evidence(data_root: Path) -> dict:
    path = evidence_path(data_root)
    try:
        value = parse(path.read_bytes())
    except OSError:
        _fail('REAL_DEPLOYMENT_EVIDENCE_REQUIRED')
    if (not isinstance(value, dict)
            or set(value) != {'schema', 'flat_checkpoint_sha256', 'entitlement_evidence'}
            or value['schema'] != EVIDENCE_SCHEMA
            or not isinstance(value['flat_checkpoint_sha256'], str)
            or not isinstance(value['entitlement_evidence'], dict)):
        _fail('REAL_DEPLOYMENT_EVIDENCE_INVALID')
    return value


def write_deployment_evidence(data_root: Path, evidence: dict) -> Path:
    if set(evidence) != {'schema', 'flat_checkpoint_sha256', 'entitlement_evidence'}:
        _fail('REAL_DEPLOYMENT_EVIDENCE_INVALID')
    path = evidence_path(data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, json.dumps(evidence, indent=2, sort_keys=True).encode('utf-8'))
    return path


def write_record(data_root: Path, record: dict, public_pem: bytes) -> Path:
    folder = owner_dir(data_root)
    folder.mkdir(parents=True, exist_ok=True)
    _atomic_write(folder / PUBLIC_KEY_NAME, public_pem)
    _atomic_write(folder / RECORD_NAME, json.dumps(record, indent=2, sort_keys=True).encode('utf-8'))
    return folder / RECORD_NAME


def _atomic_write(path: Path, data: bytes):
    temp = path.with_name(path.name + '.tmp')
    temp.write_bytes(data)
    temp.replace(path)
