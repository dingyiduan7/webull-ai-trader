param([ValidateSet('init','start','stop','status')][string]$Action = 'status', [string]$DataRoot = '')
$ErrorActionPreference = 'Stop'
$taskRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$taskCodeRoot=$taskRoot
if ($DataRoot) { $taskRoot = [IO.Path]::GetFullPath($DataRoot) }
$taskLocal = [IO.Path]::GetFullPath((Join-Path $taskRoot '.local'))
$taskData = [IO.Path]::GetFullPath((Join-Path $taskLocal 'pgdata-m2'))
$taskBin = if(Test-Path -LiteralPath (Join-Path $taskCodeRoot 'runtime-storage.json')) { Join-Path $taskCodeRoot 'vendor/postgresql/bin' } else { Join-Path $taskLocal 'postgresql/pgsql/bin' }
$taskLog = Join-Path $taskLocal 'pg-m2.log'
if (-not $taskData.StartsWith($taskLocal + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Refusing a database path outside this project .local directory'
}
if (-not (Test-Path -LiteralPath (Join-Path $taskBin 'pg_ctl.exe'))) {
    throw 'PostgreSQL binaries missing. See docs/M2_RUNBOOK.md. This script does not download anything.'
}
switch ($Action) {
    'init' {
        if (Test-Path -LiteralPath $taskData) { throw 'Data directory already exists; it will not be replaced.' }
        & (Join-Path $taskBin 'initdb.exe') -D $taskData -U m2_admin --auth=trust --encoding=UTF8 --no-locale
    }
    'start' {
        & (Join-Path $taskBin 'pg_ctl.exe') -D $taskData -l $taskLog -o '-h 127.0.0.1 -p 55433 -c fsync=on -c synchronous_commit=on' -w start
    }
    'stop' {
        & (Join-Path $taskBin 'pg_ctl.exe') -D $taskData -m fast -w stop
    }
    'status' {
        & (Join-Path $taskBin 'pg_ctl.exe') -D $taskData status
    }
}
if ($LASTEXITCODE -ne 0) { throw "PostgreSQL $Action failed with exit code $LASTEXITCODE" }
