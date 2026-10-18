<#
.SYNOPSIS
  Registers the scheduled jobs for the current user (no password is stored).

.DESCRIPTION
  \hk-estate\daily listings   every day at 08:00          scripts\daily.ps1 (python -m hkestate daily)
  \hk-estate\weekly deals     every Sunday at 07:00       scripts\daily.ps1 -Command update
  \hk-estate\weekly ricacorp  every Sunday at 10:00       scripts\daily.ps1 -Command ricacorp
                              (a pass takes about 7 hours; the task may run up to 14)
  \hk-estate\daily rank       every day at 14:00          scripts\daily.ps1 -Command rank
                              (after the 08:00 listings, which take about 4.5 hours)
  \hk-estate\weekly train     every Sunday at 12:00       scripts\daily.ps1 -Command train
                              (after the 07:00 deal update; before that day's rank)

  All start as soon as possible after a missed start (laptop off or asleep), never run twice
  at once, and run only while the user is logged on. Rerunning this script replaces them.
  Remove with:  Unregister-ScheduledTask -TaskPath '\hk-estate\' -TaskName '<name>'
#>
param([string]$EnvName = 'hk-estate')

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$prefix = (& conda run -n $EnvName python -c "import sys; print(sys.prefix)" | Select-Object -Last 1).Trim()
$python = Join-Path $prefix 'python.exe'
if (-not (Test-Path $python)) { throw "python.exe not found at $python" }
$script = Join-Path $repo 'scripts\daily.ps1'

$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 8)
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive -RunLevel Limited

function Register-Job([string]$Name, [string]$Command, $Trigger, [string]$Description,
                      $JobSettings = $settings) {
    $arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`" " +
                 "-Python `"$python`" -Command $Command"
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arguments `
        -WorkingDirectory $repo
    Register-ScheduledTask -TaskPath '\hk-estate\' -TaskName $Name -Action $action `
        -Trigger $Trigger -Settings $JobSettings -Principal $principal `
        -Description $Description -Force | Out-Null
}

Register-Job 'daily listings' 'daily' (New-ScheduledTaskTrigger -Daily -At '08:00') `
    'HK Estate: snapshot pilot listings (Centanet, 28Hse, Spacious) and prune cached listing pages.'
Register-Job 'weekly deals' 'update' (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At '07:00') `
    'HK Estate: read the newest 28Hse deal-list pages for the pilot estates.'

$longSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 14)
Register-Job 'weekly ricacorp' 'ricacorp' `
    (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At '10:00') `
    'HK Estate: Ricacorp estate pages, previous-deal pairs (one pass, resumable within the week).' `
    $longSettings

Register-Job 'daily rank' 'rank' (New-ScheduledTaskTrigger -Daily -At '14:00') `
    'HK Estate: value active sale listings with the fair-price model (ranked CSV + dashboard data).'
Register-Job 'weekly train' 'train' `
    (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At '12:00') `
    'HK Estate: retrain the fair-price model on the newest data (model_eval.md).'

Get-ScheduledTask -TaskPath '\hk-estate\' |
    Select-Object TaskPath, TaskName, State,
        @{n = 'Trigger'; e = { ($_.Triggers | ForEach-Object { $_.StartBoundary }) -join ', ' } },
        @{n = 'StartWhenAvailable'; e = { $_.Settings.StartWhenAvailable } }
