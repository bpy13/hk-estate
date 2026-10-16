<#
.SYNOPSIS
  Registers the scheduled jobs for the current user (no password is stored).

.DESCRIPTION
  \hk-estate\daily listings   every day at 08:00          scripts\daily.ps1 (hkestate daily)
  \hk-estate\weekly deals     every Sunday at 07:00       scripts\daily.ps1 -Command update

  Both start as soon as possible after a missed start (laptop off or asleep), never run twice
  at once, and run only while the user is logged on. Rerunning this script replaces them.
  Remove with:  Unregister-ScheduledTask -TaskPath '\hk-estate\' -TaskName '<name>'
#>
param([string]$EnvName = 'hk-estate')

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$prefix = (& conda run -n $EnvName python -c "import sys; print(sys.prefix)" | Select-Object -Last 1).Trim()
$exe = Join-Path $prefix 'Scripts\hkestate.exe'
if (-not (Test-Path $exe)) { throw "hkestate.exe not found at $exe (pip install -e . in $EnvName?)" }
$script = Join-Path $repo 'scripts\daily.ps1'

$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 8)
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive -RunLevel Limited

function Register-Job([string]$Name, [string]$Command, $Trigger, [string]$Description) {
    $arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`" " +
                 "-Exe `"$exe`" -Command $Command"
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arguments `
        -WorkingDirectory $repo
    Register-ScheduledTask -TaskPath '\hk-estate\' -TaskName $Name -Action $action `
        -Trigger $Trigger -Settings $settings -Principal $principal `
        -Description $Description -Force | Out-Null
}

Register-Job 'daily listings' 'daily' (New-ScheduledTaskTrigger -Daily -At '08:00') `
    'HK Estate: snapshot pilot listings (Centanet, 28Hse, Spacious) and prune cached listing pages.'
Register-Job 'weekly deals' 'update' (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At '07:00') `
    'HK Estate: read the newest 28Hse deal-list pages for the pilot estates.'

Get-ScheduledTask -TaskPath '\hk-estate\' |
    Select-Object TaskPath, TaskName, State,
        @{n = 'Trigger'; e = { ($_.Triggers | ForEach-Object { $_.StartBoundary }) -join ', ' } },
        @{n = 'StartWhenAvailable'; e = { $_.Settings.StartWhenAvailable } }
