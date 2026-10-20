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

  Python runs in its own hidden console (Start-Process), not in this script's console, so a
  Ctrl+C or a closed console window elsewhere can't stop it (2026-10-20: the daily job died
  with exit 0xC000013A). The script waits for it and returns its exit code.

.PARAMETER Python
  Full path to python.exe in the conda env (register_task.ps1 fills it in). The job runs
  `python.exe -m hkestate <command>`: Windows Application Control once blocked hkestate.exe.
#>
param(
    [string]$Python = (Join-Path $env:USERPROFILE 'miniconda3\envs\hk-estate\python.exe'),
    [string]$Command = 'daily'
)

$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo
$logDir = Join-Path $repo 'data\logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir ('task_{0}_{1:yyyyMMdd_HHmmss}.log' -f $Command, (Get-Date))
$out = [IO.Path]::ChangeExtension($log, '.out')
$err = [IO.Path]::ChangeExtension($log, '.err')
$env:PYTHONIOENCODING = 'utf-8'

"$(Get-Date -Format s) start: $Python -m hkestate $Command" | Out-File -FilePath $log -Encoding utf8
if (-not (Test-Path $Python)) {
    "python.exe not found at $Python" | Out-File -FilePath $log -Append -Encoding utf8
    exit 2
}
$arguments = @('-m', 'hkestate') + ($Command -split ' ')
$p = Start-Process -FilePath $Python -ArgumentList $arguments -WorkingDirectory $repo `
    -WindowStyle Hidden -RedirectStandardOutput $out -RedirectStandardError $err -PassThru
$null = $p.Handle  # keeps the handle, so ExitCode is still there after the wait
$p.WaitForExit()
$code = $p.ExitCode
foreach ($f in @($out, $err)) {
    if ((Test-Path $f) -and (Get-Item $f).Length -gt 0) {
        Get-Content -Path $f -Encoding utf8 | Out-File -FilePath $log -Append -Encoding utf8
    }
    Remove-Item -Path $f -ErrorAction SilentlyContinue
}
"$(Get-Date -Format s) exit code $code" | Out-File -FilePath $log -Append -Encoding utf8
exit $code
