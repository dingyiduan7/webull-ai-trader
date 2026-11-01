# Storage only. This does not enable production trading or broker access.
# Run manually in a separate PowerShell window. Never paste secret values in chat.
$ErrorActionPreference = 'Stop'

function Read-RealCredential {
    [OutputType([System.Security.SecureString])]
    param([Parameter(Mandatory = $true)][string]$Label)

    while ($true) {
        $taskPlain = $null
        $taskMasked = $null
        try {
            $taskMasked = Read-Host "Paste $Label, then press Enter" -AsSecureString
            $taskPlain = [System.Net.NetworkCredential]::new('', $taskMasked).Password
            if ($null -ne $taskPlain) {
                $taskPlain = $taskPlain.Trim()
            }
            if ([string]::IsNullOrWhiteSpace($taskPlain)) {
                Write-Host 'The credential is empty. Please enter it again.'
                continue
            }
            return ConvertTo-SecureString -String $taskPlain -AsPlainText -Force
        } finally {
            $taskPlain = $null
            if ($null -ne $taskMasked) {
                $taskMasked.Dispose()
            }
            try {
                Set-Clipboard -Value '' -ErrorAction Stop
            } catch {
                Write-Warning 'Clipboard could not be cleared. You can clear it manually; credential setup will continue.' -WarningAction Continue
            }
        }
    }
}

$taskRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$taskSecrets = Join-Path $taskRoot '.secrets'
$taskPath = Join-Path $taskSecrets 'webull-real.clixml'
New-Item -ItemType Directory -Path $taskSecrets -Force | Out-Null
if (Test-Path -LiteralPath $taskPath) {
    try {
        $taskValues = Import-Clixml -LiteralPath $taskPath
    } catch {
        throw 'REAL_SECRET_FILE_INVALID'
    }
    if ($taskValues -isnot [hashtable] -or
            $taskValues.AccountId -isnot [System.Security.SecureString] -or
            $taskValues.AccessToken -isnot [System.Security.SecureString]) {
        throw 'REAL_SECRET_SCHEMA_INVALID'
    }
} else {
    $taskValues = @{
        AccountId = [System.Security.SecureString]::new()
        AccessToken = [System.Security.SecureString]::new()
    }
}
$taskValues.AppKey = Read-RealCredential 'Real App Key'
$taskValues.AppSecret = Read-RealCredential 'Real App Secret'
# Windows DPAPI encrypts SecureStrings for this Windows user on this computer.
$taskValues | Export-Clixml -LiteralPath $taskPath -Encoding UTF8
$taskValues.Clear()
Write-Host 'Encrypted local real-account configuration saved for storage only. Production remains disabled. No secret values were printed.'
