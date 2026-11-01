"""Ephemeral offline owner key. Real files and real verification; only the key is test-only."""
from copy import deepcopy

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from autonomous_trader import owner_authorization as owner
from autonomous_trader.runtime_storage import data_root


def install_test_authority(root, monkeypatch=None):
    """Return publish(authority): signs the owner charter part and writes the
    engineer deployment evidence, exactly as the owner/engineer tools would."""
    key = Ed25519PrivateKey.generate()
    base = data_root(root)

    def publish(authority):
        charter, evidence = owner.split_authority(deepcopy(authority))
        record = owner.sign_charter(key, charter)
        owner.write_record(base, record, owner.public_key_pem(key))
        owner.write_deployment_evidence(base, evidence)

    publish.key = key
    return publish
