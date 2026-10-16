<#
.SYNOPSIS
  The daily listing job (Task Scheduler runs it at 08:00): `hkestate daily`.

.DESCRIPTION
  Snapshots the pilot estates' listings from Centanet, 28Hse and Spacious, matches them to
  buildings, flags keywords, marks delistings and deletes cached listing pages 7+ days old.
  While a backfill or the weekly update holds the 28Hse lock, 28Hse is skipped for the day
  (28Hse rate-limits hard); the other sites still run. Two daily jobs never overlap.

  Output goes to data\logs\task_daily_<time>.log; the job also writes its own
  data\logs\daily_<time>.log and a row in the run_log table.

.PARAMETER Exe
  Full path to hkestate.exe in the conda env (register_task.ps1 fills it in).
#>
param(
    [string]$Exe = (Join-Path $env:USERPROFILE 'miniconda3\envs\hk-estate\Scripts\hkestate.exe'),
    [string]$Command = 'daily'
)

$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo
$logDir = Join-Path $repo 'data\logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir ('task_{0}_{1:yyyyMMdd_HHmmss}.log' -f $Command, (Get-Date))
$env:PYTHONIOENCODING = 'utf-8'

"$(Get-Date -Format s) start: $Exe $Command" | Out-File -FilePath $log -Encoding utf8
if (-not (Test-Path $Exe)) {
    "hkestate.exe not found at $Exe" | Out-File -FilePath $log -Append -Encoding utf8
    exit 2
}
# cmd /c keeps Python's stderr as plain text in the log (PowerShell 5.1 would wrap it).
cmd /c "`"$Exe`" $Command >> `"$log`" 2>&1"
$code = $LASTEXITCODE
"$(Get-Date -Format s) exit code $code" | Out-File -FilePath $log -Append -Encoding utf8
exit $code
