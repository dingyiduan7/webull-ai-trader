"""OWNER ONLY. Sign (or re-sign) the trading charter: the money boundary.

The charter fixes the account, the capital ceiling, the hard risk policy and
an expiry. Code releases do NOT need a new signature; you only re-run this to
change those limits, after the charter expires, or after a revocation.

    .\\.venv\\Scripts\\python scripts\\owner_sign_authorization.py

It shows exactly what you are approving and asks you to type AUTHORIZE, then
your passphrase. Nothing here contacts Webull or places orders.
"""
from datetime import datetime, timedelta, timezone
from getpass import getpass
from pathlib import Path
import argparse
import secrets
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from autonomous_trader import owner_authorization as owner  # noqa: E402
from autonomous_trader.real_daily import scope  # noqa: E402
from autonomous_trader.real_state import binding  # noqa: E402
from autonomous_trader.risk import POLICY  # noqa: E402
from autonomous_trader.runtime_storage import data_root  # noqa: E402

DEFAULT_KEY = Path.home() / '.autotrader-owner' / 'owner_private_key.pem'


def account_id_from_secrets():
    try:
        from autonomous_trader.real_secrets import load_real_credentials
        return load_real_credentials(ROOT).account_id or None
    except Exception:  # noqa: BLE001 - fall back to asking the owner
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--private-key-path', type=Path, default=DEFAULT_KEY)
    parser.add_argument('--valid-days', type=int, default=365)
    args = parser.parse_args()
    if not 1 <= args.valid_days <= 730:
        sys.exit('--valid-days must be between 1 and 730.')
    key_path = args.private_key_path.expanduser()
    if not key_path.is_file():
        sys.exit(f'No private key at {key_path}. Run scripts\\owner_generate_keypair.py first.')

    account_id = account_id_from_secrets()
    if account_id:
        print('Using the Webull account id stored in your encrypted credentials.')
    else:
        account_id = getpass('Webull cash account id (input hidden): ').strip()
    account_binding = binding(account_id)
    code_scope = scope()
    now = datetime.now(timezone.utc)
    charter = dict(schema='REAL_PERSISTENT_AUTHORITY_V1', verdict='GO',
                   authority_id='owner-charter-' + now.strftime('%Y%m%dT%H%M%SZ') + '-' + secrets.token_hex(4),
                   account_binding=account_binding, scope=code_scope,
                   issued_at=now.isoformat(), expires_at=(now + timedelta(days=args.valid_days)).isoformat(),
                   revoked=False)

    masked = account_id[:2] + '*' * max(0, len(account_id) - 4) + account_id[-2:]
    print()
    print('=' * 64)
    print(' YOU ARE APPROVING LIVE TRADING WITHIN THESE LIMITS')
    print('=' * 64)
    print(f' Account:              {masked} ({code_scope["account_type"]}, {code_scope["environment"]})')
    print(f' Capital ceiling:      ${code_scope["capital_usd"]}')
    print(f' Per-stock exposure:   {POLICY.stock_exposure * 100}% of equity')
    print(f' Max positions:        {POLICY.maximum_positions}')
    print(f' Planned loss / trade: {POLICY.trade_risk * 100}% of equity')
    print(f' Daily loss halt:      {POLICY.daily_loss * 100}%   Drawdown halt: {POLICY.drawdown * 100}%'
          '  (halts freeze new buys; holdings are never force-sold, only by their own stops)')
    print(f' Planned loss / all:   {POLICY.open_risk * 100}% of equity across open positions')
    if code_scope.get('overnight') is True:
        from autonomous_trader.carry import MAX_HOLD_SESSIONS
        print(f' Direction / session:  {POLICY.direction}, {POLICY.session} hours trading;'
              f' positions may be HELD OVERNIGHT up to {MAX_HOLD_SESSIONS} sessions')
        print('                       (local stops only: an overnight gap can fill below a stop)')
    else:
        print(f' Direction / session:  {POLICY.direction}, {POLICY.session} hours only, no overnight')
    print(' Margin / borrowing / money movement: none')
    print(f' Valid until:          {charter["expires_at"]}')
    print(f' Charter id:           {charter["authority_id"]}')
    print('=' * 64)
    if input('Type AUTHORIZE to sign, anything else to cancel: ').strip() != 'AUTHORIZE':
        sys.exit('Cancelled. Nothing was signed.')

    passphrase = getpass('Key passphrase: ')
    try:
        private_key = serialization.load_pem_private_key(key_path.read_bytes(), password=passphrase.encode('utf-8'))
    except (ValueError, TypeError):
        sys.exit('Wrong passphrase or unreadable key. Nothing was signed.')
    if not isinstance(private_key, Ed25519PrivateKey):
        sys.exit('That key is not an owner Ed25519 key.')

    record = owner.sign_charter(private_key, charter)
    path = owner.write_record(data_root(ROOT), record, owner.public_key_pem(private_key))
    print(f'Signed charter written to {path}')
    print('To stop trading at any time: scripts\\owner_revoke_authorization.py')


if __name__ == '__main__':
    main()
