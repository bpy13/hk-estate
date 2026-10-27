<#
.SYNOPSIS
  Runs the dashboard (Streamlit) on a fixed port and starts it again whenever it stops.

.DESCRIPTION
  The scheduled task \hk-estate\dashboard (register_dashboard_task.ps1) calls this at logon.
  The port is fixed (default 8501) so bookmarks keep working: without it Streamlit moves to
  the next free port when 8501 is taken, e.g. by stock_advisor's dashboard (8502).
  Starts and stops go to data\logs\dashboard.log; Streamlit's own output to dashboard_server.log.
  Stop it for good: Stop-ScheduledTask -TaskPath '\hk-estate\' -TaskName dashboard, then end
  the python.exe listening on the port (Get-NetTCPConnection -LocalPort 8501).
#>
param(
    [string]$Python = (Join-Path $env:USERPROFILE 'miniconda3\envs\hk-estate\python.exe'),
    [int]$Port = 8501
)

$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo
$logDir = Join-Path $repo 'data\logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir 'dashboard.log'
$serverLog = Join-Path $logDir 'dashboard_server.log'
$env:PYTHONIOENCODING = 'utf-8'
function Note([string]$msg) { "$(Get-Date -Format s) $msg" | Out-File -FilePath $log -Append -Encoding utf8 }

while ($true) {
    Note "starting on port $Port"
    # --server.headless: no browser tab and no first-run e-mail prompt (nobody is there to answer).
    # cmd /c keeps Python's stderr as plain text (PowerShell 5.1 would wrap it).
    cmd /c "`"$Python`" -m streamlit run app\dashboard.py --server.port $Port --server.headless true >> `"$serverLog`" 2>&1"
    Note "stopped with code $LASTEXITCODE; restarting in 30 s"
    Start-Sleep -Seconds 30
}
