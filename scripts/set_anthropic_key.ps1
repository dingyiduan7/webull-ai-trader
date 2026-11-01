# Stores the Claude (Anthropic) API key for Auto Trader, encrypted with Windows DPAPI
# for THIS Windows user on THIS computer. No admin rights needed.
# Run in your own PowerShell window at the repo root:
#   powershell -ExecutionPolicy Bypass -File .\scripts\set_anthropic_key.ps1
# Never paste the key into any chat or AI tool.
$ErrorActionPreference = 'Stop'

$taskRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$taskSecrets = Join-Path $taskRoot '.secrets'
$taskPath = Join-Path $taskSecrets 'anthropic.clixml'
New-Item -ItemType Directory -Path $taskSecrets -Force | Out-Null

while ($true) {
    $taskMasked = Read-Host 'Paste the Anthropic API key (starts with sk-ant-), then press Enter' -AsSecureString
    $taskPlain = [System.Net.NetworkCredential]::new('', $taskMasked).Password
    if ($null -ne $taskPlain) { $taskPlain = $taskPlain.Trim() }
    try { Set-Clipboard -Value '' -ErrorAction Stop } catch { Write-Warning 'Clipboard could not be cleared; clear it manually.' }
    if ($taskPlain -match '^sk-ant-[A-Za-z0-9_-]{20,}$') { break }
    $taskPlain = $null
    $taskMasked.Dispose()
    Write-Host 'That does not look like an Anthropic API key (sk-ant-...). Please try again.'
}

$taskValues = @{ ApiKey = (ConvertTo-SecureString -String $taskPlain -AsPlainText -Force) }
$taskPlain = $null
$taskMasked.Dispose()
$taskValues | Export-Clixml -LiteralPath $taskPath -Encoding UTF8
$taskValues.Clear()
Write-Host 'Saved: .secrets\anthropic.clixml (encrypted for your Windows account). No key was printed.'
Write-Host 'Next: .\.venv\Scripts\python scripts\claude_connection_check.py'
