"""Validation-only loader for separately stored real-account credentials.

No runtime module imports this file. Loading values grants no network, broker,
order, scheduler, or production authority.
"""
from .runtime_storage import data_root
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import subprocess


class RealSecretError(ValueError):
    """Fixed error codes only; never interpolate decrypted values."""


@dataclass(frozen=True)
class RealCredentials:
    app_key: str = field(repr=False)
    app_secret: str = field(repr=False)
    account_id: str = field(default="", repr=False)
    access_token: str = field(default="", repr=False)

    def __post_init__(self):
        if not self.app_key or not self.app_secret:
            raise RealSecretError("MISSING_REAL_CREDENTIALS")


def load_real_credentials(root: Path) -> RealCredentials:
    """Validate/decrypt only ``.secrets/webull-real.clixml`` using Windows DPAPI."""
    path = data_root(root) / ".secrets" / "webull-real.clixml"
    if not path.is_file():
        raise RealSecretError("MISSING_REAL_CREDENTIALS")
    if os.name != "nt":
        raise RealSecretError("DPAPI_REQUIRES_WINDOWS_OWNER")
    command = """$ErrorActionPreference='Stop';
    $v=Import-Clixml -LiteralPath $env:AITRADER_REAL_DPAPI_PATH;
    if ($v -isnot [hashtable]) { exit 2 };
    $out=@{};
    foreach($n in @('AppKey','AppSecret','AccountId','AccessToken')) {
        if (-not $v.ContainsKey($n) -or $v[$n] -isnot [System.Security.SecureString]) { exit 3 };
        $out[$n]=[System.Net.NetworkCredential]::new('', $v[$n]).Password
    };
    $out | ConvertTo-Json -Compress"""
    env = dict(os.environ, AITRADER_REAL_DPAPI_PATH=str(path))
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW, check=False)
        if result.returncode:
            raise RealSecretError("REAL_SECRET_DECRYPTION_FAILED")
        values = json.loads(result.stdout.decode("utf-8-sig"))
        if not isinstance(values, dict) or set(values) != {"AppKey", "AppSecret", "AccountId", "AccessToken"}:
            raise RealSecretError("REAL_SECRET_SCHEMA_INVALID")
        return RealCredentials(*(values[name] for name in ("AppKey", "AppSecret", "AccountId", "AccessToken")))
    except RealSecretError:
        raise
    except Exception:
        raise RealSecretError("REAL_SECRET_DECRYPTION_FAILED") from None
