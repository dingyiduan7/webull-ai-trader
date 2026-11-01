"""Owner-signed REAL authority for Auto Trader.

``verify_owner_authorization(root)`` returns the REAL_PERSISTENT_AUTHORITY_V1
object that every downstream check (daily certificates, receipts, recovery)
already consumes. It is assembled from:

* the owner-signed charter (account, scope incl. capital and risk-policy hash,
  validity window) -- signature, schema and revocation are verified here;
* engineer-maintained deployment evidence (flat checkpoint, market-data
  entitlement evidence);
* the live build digest of the code actually running. Releases therefore need
  no owner signature, but every release still invalidates prior daily
  certificates, forcing fresh readiness evidence for the new build.

``real_daily.validate_unsigned_request`` then enforces scope == code scope,
expiry, account, checkpoint and entitlement exactly as before.
"""
from . import owner_authorization as owner
from .owner_authorization import canonical, decode, encode, parse, record_hash  # noqa: F401  (test helpers)
from .runtime_storage import data_root


def _fail(code):
    raise ValueError(code)


def verify_owner_authorization(root, authority=None):
    # No env/CLI override and no cached approval. Every call re-reads storage,
    # so deletion, revocation, or replacement blocks the next verification.
    from .real_owner_trust import read_protected_authorization
    from .real_trading import build_digest
    trust, record = read_protected_authorization(root)
    charter = owner.verify_record(record, trust)
    evidence = owner.load_deployment_evidence(data_root(root))
    build = build_digest(root)
    entitlement = dict(evidence['entitlement_evidence'])
    entitlement['build_sha256'] = build
    assembled = dict(charter)
    assembled.update(build_sha256=build,
                     flat_checkpoint_sha256=evidence['flat_checkpoint_sha256'],
                     entitlement_evidence=entitlement)
    if authority is not None and canonical(authority) != canonical(assembled):
        _fail('REAL_OWNER_AUTHORITY_MISMATCH')
    return assembled
