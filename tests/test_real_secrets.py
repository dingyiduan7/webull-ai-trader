import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from autonomous_trader.real_secrets import RealSecretError, load_real_credentials


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "set_webull_real_secrets.ps1"


def test_real_path_is_separate_ignored_and_not_wired_into_runtime():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "webull-real.clixml" in text
    assert "webull-sandbox.clixml" not in text
    assert "set_webull_sandbox_secrets" not in text
    assert ".secrets/" in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    for path in (ROOT / "scripts" / "run_ai_trader.py", ROOT / "scripts" / "run_trading_cycle.py"):
        runtime = path.read_text(encoding="utf-8")
        assert "load_real_credentials" not in runtime and "webull-real.clixml" not in runtime


def test_missing_and_decryption_failures_are_fixed_and_silent(tmp_path, monkeypatch, capsys):
    with pytest.raises(RealSecretError) as missing:
        load_real_credentials(tmp_path)
    assert str(missing.value) == "MISSING_REAL_CREDENTIALS"

    path = tmp_path / ".secrets" / "webull-real.clixml"
    path.parent.mkdir()
    path.write_text("synthetic malformed fixture", encoding="utf-8")
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=3, stdout=b""))
    with pytest.raises(RealSecretError) as invalid:
        load_real_credentials(tmp_path)
    assert str(invalid.value) == "REAL_SECRET_DECRYPTION_FAILED"
    assert capsys.readouterr() == ("", "")


@pytest.mark.skipif(os.name != "nt" or shutil.which("powershell.exe") is None, reason="Windows DPAPI test")
def test_setup_trims_stores_only_real_file_and_never_echoes_secrets(tmp_path):
    project = tmp_path / "project"
    scripts = project / "scripts"
    scripts.mkdir(parents=True)
    copied = scripts / SCRIPT.name
    shutil.copyfile(SCRIPT, copied)
    fake_key = "unit-real-key-SENTINEL"
    fake_secret = "unit-real-secret-SENTINEL"
    wrapper = f"""
    $global:Inputs=[System.Collections.Generic.Queue[string]]::new()
    $global:Inputs.Enqueue('  {fake_key}  ')
    $global:Inputs.Enqueue('  {fake_secret}  ')
    $global:ClipboardCalls=0
    function global:ConvertTo-SecureString {{
        param([string]$String,[switch]$AsPlainText,[switch]$Force)
        $value=[System.Security.SecureString]::new()
        foreach($character in $String.ToCharArray()) {{ $value.AppendChar($character) }}
        $value.MakeReadOnly()
        $value
    }}
    function global:Read-Host {{
        param([Parameter(Position=0)][string]$Prompt,[switch]$AsSecureString)
        ConvertTo-SecureString -String $global:Inputs.Dequeue() -AsPlainText -Force
    }}
    function global:Set-Clipboard {{
        param([string]$Value,[object]$ErrorAction)
        $global:ClipboardCalls++
        if($global:ClipboardCalls -eq 1) {{ throw 'synthetic clipboard failure' }}
    }}
    function global:Export-Clixml {{
        [CmdletBinding()]
        param([Parameter(ValueFromPipeline=$true)]$InputObject,[string]$LiteralPath,[string]$Encoding)
        process {{
            $global:CapturedKey=[System.Net.NetworkCredential]::new('', $InputObject.AppKey).Password
            $global:CapturedSecret=[System.Net.NetworkCredential]::new('', $InputObject.AppSecret).Password
            New-Item -ItemType File -Path $LiteralPath -Force | Out-Null
        }}
    }}
    & '{copied}'
    $p=Join-Path '{project}' '.secrets/webull-real.clixml'
    if($global:CapturedKey -ne '{fake_key}' -or $global:CapturedSecret -ne '{fake_secret}') {{ throw 'SYNTHETIC_SCHEMA_CHECK_FAILED' }}
    if(Test-Path -LiteralPath (Join-Path '{project}' '.secrets/webull-sandbox.clixml')) {{ throw 'SANDBOX_FILE_TOUCHED' }}
    Write-Output "TEST_OK:$global:ClipboardCalls"
    """
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", wrapper],
        text=True, capture_output=True, timeout=30, check=False)
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert fake_key not in output and fake_secret not in output
    assert "TEST_OK:2" in output
    assert (project / ".secrets" / "webull-real.clixml").is_file()
    assert not (project / ".secrets" / "webull-sandbox.clixml").exists()
