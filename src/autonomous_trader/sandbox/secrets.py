"""Local-only secret loading. Values never appear in representations/errors."""
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import subprocess


class PreflightError(ValueError):
    """Messages must be fixed codes, never broker text or exception interpolation."""


@dataclass(frozen=True)
class Credentials:
    app_key: str = field(repr=False)
    app_secret: str = field(repr=False)
    account_id: str = field(default="", repr=False)
    access_token: str = field(default="", repr=False)

    def __post_init__(self):
        if not self.app_key or not self.app_secret:
            raise PreflightError("MISSING_SANDBOX_CREDENTIALS")

    def sensitive_values(self):
        return tuple(v for v in (self.app_key, self.app_secret, self.account_id, self.access_token) if v)


def load_credentials(root: Path) -> Credentials:
    names = ["WEBULL_SANDBOX_" + n for n in ("APP_KEY", "APP_SECRET", "ACCOUNT_ID", "ACCESS_TOKEN")]
    if any(os.environ.get(n) for n in names[:2]):
        return Credentials(*(os.environ.get(n, "") for n in names))
    secret_path = root.resolve() / ".secrets" / "webull-sandbox.clixml"
    if not secret_path.is_file():
        raise PreflightError("MISSING_SANDBOX_CREDENTIALS")
    if os.name != "nt":
        raise PreflightError("DPAPI_REQUIRES_WINDOWS_OWNER")
    # DPAPI is decrypted by the same Windows owner. Plaintext exists only in the
    # child/parent private pipe and memory, never command arguments or a file.
    command = """$ErrorActionPreference='Stop';
    $v=Import-Clixml -LiteralPath $env:M3A_DPAPI_PATH;
    $out=@{};
    foreach($n in @('AppKey','AppSecret','AccountId','AccessToken')) {
        $out[$n]=[System.Net.NetworkCredential]::new('', $v[$n]).Password
    };
    $out | ConvertTo-Json -Compress"""
    env = dict(os.environ, M3A_DPAPI_PATH=str(secret_path))
    try:
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW, check=False)
        if result.returncode:
            raise PreflightError("LOCAL_SECRET_DECRYPTION_FAILED")
        values = json.loads(result.stdout.decode("utf-8-sig"))
        return Credentials(*(values.get(n, "") for n in ("AppKey", "AppSecret", "AccountId", "AccessToken")))
    except PreflightError:
        raise
    except Exception:
        raise PreflightError("LOCAL_SECRET_DECRYPTION_FAILED") from None
