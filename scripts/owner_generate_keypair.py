"""OWNER ONLY. Run once, yourself, in your own PowerShell window.

Creates the Ed25519 key you use to sign the trading charter (account, capital,
risk policy). The private key is encrypted with a passphrase you choose and is
written OUTSIDE the repository. Never paste the passphrase or the private key
into any chat or AI tool.

    .\\.venv\\Scripts\\python scripts\\owner_generate_keypair.py

Default private key location: %USERPROFILE%\\.autotrader-owner\\owner_private_key.pem
The public key is copied into the runtime data folder; it is not secret.
Generating a new key later invalidates the current charter until you re-sign.
"""
from getpass import getpass
from pathlib import Path
import argparse
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from autonomous_trader import owner_authorization as owner  # noqa: E402
from autonomous_trader.runtime_storage import data_root  # noqa: E402

DEFAULT_KEY = Path.home() / '.autotrader-owner' / 'owner_private_key.pem'
MIN_PASSPHRASE = 12


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--private-key-path', type=Path, default=DEFAULT_KEY)
    args = parser.parse_args()
    key_path = args.private_key_path.expanduser().resolve()
    if key_path.exists():
        sys.exit(f'A private key already exists at {key_path}. Refusing to overwrite it. '
                 'Delete it yourself only if you intend to rotate keys (you will need to re-sign).')
    if key_path == ROOT or ROOT in key_path.parents:
        sys.exit('The private key must live outside the repository folder.')
    print('Choose a passphrase (at least %d characters). It is never stored anywhere.' % MIN_PASSPHRASE)
    first = getpass('Passphrase: ')
    if len(first) < MIN_PASSPHRASE:
        sys.exit('Passphrase too short.')
    if getpass('Repeat passphrase: ') != first:
        sys.exit('Passphrases do not match.')
    private_key = Ed25519PrivateKey.generate()
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_bytes(private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.BestAvailableEncryption(first.encode('utf-8'))))
    folder = owner.owner_dir(data_root(ROOT))
    folder.mkdir(parents=True, exist_ok=True)
    (folder / owner.PUBLIC_KEY_NAME).write_bytes(owner.public_key_pem(private_key))
    print(f'Private key (encrypted): {key_path}')
    print(f'Public key:              {folder / owner.PUBLIC_KEY_NAME}')
    print('Back up the private key file somewhere safe (it is useless without your passphrase).')
    print('Next: scripts\\owner_sign_authorization.py')


if __name__ == '__main__':
    main()
