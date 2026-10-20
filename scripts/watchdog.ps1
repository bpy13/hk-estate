<#
.SYNOPSIS
  The watchdog (Task Scheduler runs it every 15 minutes): `hkestate watchdog`.

.DESCRIPTION
  Restarts backfill-territory, cd-history or ricacorp if they died, and today's daily
  listing run if it started but did not finish (08:00-20:00 only). It never opens the
  database. Rules and limits: src\hkestate\watchdog.py and docs\operations.md "Watchdog".
  Decisions go to data\logs\watchdog_<date>.log; anything the script itself prints (a
  crash) to data\logs\watchdog_<date>.err.

  Register the scheduled task (\hk-estate\watchdog, every 15 minutes, only while logged on,
  never two at once):

      powershell -NoProfile -ExecutionPolicy Bypass -File scripts\watchdog.ps1 -Register

  which is the same as  scripts\register_task.ps1 -Only watchdog

.PARAMETER Python
  Full path to python.exe in the conda env (register_task.ps1 fills it in).
.PARAMETER DryRun
  Log what it would do, start nothing.
#>
param(
    [string]$Python = (Join-Path $env:USERPROFILE 'miniconda3\envs\hk-estate\python.exe'),
    [switch]$Register,
    [switch]$DryRun,
    [string]$EnvName = 'hk-estate'
)

if ($Register) {
    & (Join-Path $PSScriptRoot 'register_task.ps1') -EnvName $EnvName -Only 'watchdog'
    exit $LASTEXITCODE
}

$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo
$logDir = Join-Path $repo 'data\logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$err = Join-Path $logDir ('watchdog_{0:yyyyMMdd}.err' -f (Get-Date))
$env:PYTHONIOENCODING = 'utf-8'
if (-not (Test-Path $Python)) {
    "$(Get-Date -Format s) python.exe not found at $Python" | Out-File -FilePath $err -Append -Encoding utf8
    exit 2
}
$dry = ''
if ($DryRun) { $dry = ' --dry-run' }
# cmd /c keeps Python's stderr as plain text (PowerShell 5.1 would wrap it)
cmd /c "`"$Python`" -m hkestate watchdog$dry >> `"$err`" 2>&1"
$code = $LASTEXITCODE
if ((Test-Path $err) -and (Get-Item $err).Length -eq 0) { Remove-Item $err -ErrorAction SilentlyContinue }
exit $code
