<#
.SYNOPSIS
  Registers \hk-estate\dashboard: the dashboard starts when Windows starts, before anyone logs on.

.DESCRIPTION
  Run once in an elevated (Run as administrator) PowerShell; S4U logon (no password stored)
  needs it. The task runs scripts\run_dashboard.ps1 headless (no window), which restarts
  Streamlit when it stops. Every 10 minutes Task Scheduler starts the task again if it is not
  running. Rerunning this script replaces the task.
  Remove with:  Unregister-ScheduledTask -TaskPath '\hk-estate\' -TaskName dashboard -Confirm:$false
#>
param([string]$EnvName = 'hk-estate', [int]$Port = 8501)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$python = Join-Path $env:USERPROFILE "miniconda3\envs\$EnvName\python.exe"
if (-not (Test-Path $python)) { throw "python.exe not found at $python" }
$runner = Join-Path $repo 'scripts\run_dashboard.ps1'

$action = New-ScheduledTaskAction -Execute 'conhost.exe' `
    -Argument "--headless powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$runner`" -Python `"$python`" -Port $Port" `
    -WorkingDirectory $repo
$atBoot = New-ScheduledTaskTrigger -AtStartup
$watchdog = New-ScheduledTaskTrigger -Once -At (Get-Date).Date `
    -RepetitionInterval (New-TimeSpan -Minutes 10) -RepetitionDuration (New-TimeSpan -Days 3650)
# Priority 5 = normal (Task Scheduler's default 7 lets the long scrapes slow the pages down).
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -Priority 5 `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType S4U -RunLevel Limited

Register-ScheduledTask -TaskPath '\hk-estate\' -TaskName 'dashboard' -Action $action `
    -Trigger @($atBoot, $watchdog) -Settings $settings -Principal $principal `
    -Description "HK Estate: the dashboard on http://localhost:$Port, started at boot (log: data\logs\dashboard.log)." `
    -Force | Out-Null
Write-Output "Registered \hk-estate\dashboard (at boot, port $Port) using $python"
Write-Output "Start now: Start-ScheduledTask -TaskPath '\hk-estate\' -TaskName dashboard"
