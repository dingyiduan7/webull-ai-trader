"""Owner charter: real Ed25519 signatures, real files, no network."""
from datetime import datetime, timedelta, timezone
import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from autonomous_trader import owner_authorization as owner
from autonomous_trader import real_owner_authorization as auth
from autonomous_trader import real_owner_trust as trust
from autonomous_trader import real_trading
from owner_authority_fixture import install_test_authority

NOW = datetime(2026, 10, 12, 15, 0, tzinfo=timezone.utc)


def authority():
    return dict(schema='REAL_PERSISTENT_AUTHORITY_V1', verdict='GO', authority_id='owner-charter-test',
                account_binding='real-test', build_sha256='build-1',
                scope=dict(capital_usd='500', risk_policy_sha256='policy', overnight=False),
                flat_checkpoint_sha256='A' * 64,
                entitlement_evidence=dict(account_binding='real-test', build_sha256='build-1', status='VERIFIED'),
                issued_at=(NOW - timedelta(days=1)).isoformat(),
                expires_at=(NOW + timedelta(days=30)).isoformat(), revoked=False)


@pytest.fixture
def published(tmp_path, monkeypatch):
    monkeypatch.setattr(real_trading, 'build_digest', lambda root: 'build-1')
    publish = install_test_authority(tmp_path)
    value = authority()
    publish(value)
    return tmp_path, publish, value


def test_valid_charter_assembles_runtime_authority(published):
    root, _, value = published
    assert auth.verify_owner_authorization(root) == value


def test_release_needs_no_owner_signature_but_rebinds_build(published, monkeypatch):
    root, _, value = published
    monkeypatch.setattr(real_trading, 'build_digest', lambda root: 'build-2')
    result = auth.verify_owner_authorization(root)
    assert result['build_sha256'] == 'build-2'
    assert result['entitlement_evidence']['build_sha256'] == 'build-2'
    # The signed money boundary itself is untouched by a release.
    assert {k: result[k] for k in owner.CHARTER_FIELDS} == {k: value[k] for k in owner.CHARTER_FIELDS}


def test_capital_tampering_after_signing_fails(published):
    root, _, _ = published
    path = owner.owner_dir(root) / owner.RECORD_NAME
    record = json.loads(path.read_text())
    record['payload']['charter']['scope']['capital_usd'] = '5000'
    path.write_text(json.dumps(record))
    with pytest.raises(ValueError, match='REAL_OWNER_SIGNATURE_INVALID'):
        auth.verify_owner_authorization(root)


def test_charter_signed_by_another_key_fails(published):
    root, _, value = published
    charter, _ = owner.split_authority(value)
    record = owner.sign_charter(Ed25519PrivateKey.generate(), charter)
    (owner.owner_dir(root) / owner.RECORD_NAME).write_text(json.dumps(record))
    with pytest.raises(ValueError, match='REAL_OWNER_SIGNATURE_INVALID'):
        auth.verify_owner_authorization(root)


def test_swapping_public_key_alone_fails(published):
    root, _, _ = published
    (owner.owner_dir(root) / owner.PUBLIC_KEY_NAME).write_bytes(
        owner.public_key_pem(Ed25519PrivateKey.generate()))
    with pytest.raises(ValueError, match='REAL_OWNER_SIGNATURE_INVALID'):
        auth.verify_owner_authorization(root)


def test_kill_switch_revocation_blocks(published):
    root, _, value = published
    owner.write_revocation(root, value['authority_id'])
    with pytest.raises(ValueError, match='REAL_OWNER_AUTHORIZATION_REVOKED'):
        auth.verify_owner_authorization(root)


def test_unrelated_revocation_does_not_block(published):
    root, _, value = published
    owner.write_revocation(root, 'some-older-charter')
    assert auth.verify_owner_authorization(root) == value


@pytest.mark.parametrize('name', [owner.RECORD_NAME, owner.PUBLIC_KEY_NAME])
def test_missing_owner_files_fail_closed(published, name):
    root, _, _ = published
    (owner.owner_dir(root) / name).unlink()
    with pytest.raises(ValueError, match='REAL_OWNER_TRUST_NOT_ENROLLED'):
        auth.verify_owner_authorization(root)


def test_missing_deployment_evidence_fails_closed(published):
    root, _, _ = published
    owner.evidence_path(root).unlink()
    with pytest.raises(ValueError, match='REAL_DEPLOYMENT_EVIDENCE_REQUIRED'):
        auth.verify_owner_authorization(root)


def test_expected_authority_mismatch_is_rejected(published):
    root, _, value = published
    other = dict(value, authority_id='someone-else')
    with pytest.raises(ValueError, match='REAL_OWNER_AUTHORITY_MISMATCH'):
        auth.verify_owner_authorization(root, other)


def test_duplicate_json_keys_and_floats_are_rejected():
    with pytest.raises(ValueError, match='REAL_OWNER_DUPLICATE_JSON_KEY'):
        owner.parse('{"a":1,"a":2}')
    with pytest.raises(ValueError, match='REAL_OWNER_FLOAT_NOT_ALLOWED'):
        owner.canonical({'capital_usd': 500.0})


def test_revision_detects_charter_revocation_and_evidence_changes(published):
    root, publish, value = published
    first = trust.authority_revision(root)
    assert trust.authority_revision(root) == first
    owner.write_revocation(root, 'x')
    second = trust.authority_revision(root)
    assert second != first
    publish(dict(value, flat_checkpoint_sha256='B' * 64))
    assert trust.authority_revision(root) != second


def test_storage_read_reports_validation_progress(published):
    root, _, _ = published
    events = []
    with trust.observe_validation_progress(lambda: events.append('ok')):
        trust.read_protected_authorization(root)
    assert events == ['ok']


def test_private_key_is_useless_without_passphrase():
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.BestAvailableEncryption(b'correct horse battery'))
    with pytest.raises((TypeError, ValueError)):
        serialization.load_pem_private_key(pem, password=None)
    with pytest.raises(ValueError):
        serialization.load_pem_private_key(pem, password=b'wrong passphrase!!')
    assert isinstance(serialization.load_pem_private_key(pem, password=b'correct horse battery'),
                      Ed25519PrivateKey)
