"""OWNER KILL SWITCH. Stops live trading immediately; no key or passphrase needed.

    .\\.venv\\Scripts\\python scripts\\owner_revoke_authorization.py

Revokes the current charter. The trader re-verifies the charter before every
market read, so after revocation it can no longer pass that check. To resume,
sign a new charter with scripts\\owner_sign_authorization.py.
"""
from pathlib import Path
import argparse
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from autonomous_trader import owner_authorization as owner  # noqa: E402
from autonomous_trader.runtime_storage import data_root  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--authority-id', help='Revoke a specific charter id instead of the current one.')
    args = parser.parse_args()
    base = data_root(ROOT)
    authority_id = args.authority_id
    if not authority_id:
        path = owner.owner_dir(base) / owner.RECORD_NAME
        try:
            authority_id = owner.parse(path.read_bytes())['payload']['charter']['authority_id']
        except (OSError, KeyError, TypeError, ValueError):
            sys.exit(f'No readable current charter at {path}. Nothing to revoke.')
    ids = owner.write_revocation(base, authority_id)
    print(f'REVOKED: {authority_id}')
    print(f'Revocation list now holds {len(ids)} id(s). Live entries stop at the next authority check.')


if __name__ == '__main__':
    main()
