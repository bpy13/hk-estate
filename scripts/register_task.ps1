<#
.SYNOPSIS
  Registers the scheduled jobs for the current user (no password is stored).

.DESCRIPTION
  \hk-estate\daily listings   every day at 08:00          scripts\daily.ps1 (python -m hkestate daily)
                              (may run up to 23 hours: every due Centanet page is read)
  \hk-estate\weekly deals     every Sunday at 07:00       scripts\daily.ps1 -Command update
  \hk-estate\weekly ricacorp  every Sunday at 10:00       scripts\daily.ps1 -Command ricacorp
                              (a pass takes about 30 hours with restarts; the task may run
                               up to 14, then the watchdog resumes the week's pass)
  \hk-estate\daily rank       every day at 14:00          scripts\daily.ps1 -Command rank
                              (after the 08:00 listings, which take about 4.5 hours)
  \hk-estate\weekly train     every Sunday at 12:00       scripts\daily.ps1 -Command train
                              (after the 07:00 deal update; before that day's rank)

  \hk-estate\monthly blocked-check  the 1st of each month at 09:00  scripts\daily.ps1 -Command check-blocked
                              (robots.txt of Midland, Squarefoot, house730, HKP; a few requests)

  \hk-estate\watchdog         every 15 minutes            scripts\watchdog.ps1 (python -m hkestate watchdog)
                              (restarts long jobs / today's daily run that died; may run 10 min)

  All start as soon as possible after a missed start (laptop off or asleep), never run twice
  at once, and run only while the user is logged on. Rerunning this script replaces them.
  Remove with:  Unregister-ScheduledTask -TaskPath '\hk-estate\' -TaskName '<name>'
#>
param([string]$EnvName = 'hk-estate', [string]$Only = '')  # -Only 'task name' registers just that one

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

# `conhost.exe --headless` runs PowerShell with no window at all. Started directly,
# powershell.exe -WindowStyle Hidden still flashes a window and takes the focus on
# Windows 11, where new consoles open in Windows Terminal (which ignores -WindowStyle).
function New-HiddenAction([string]$PsArguments) {
    New-ScheduledTaskAction -Execute 'conhost.exe' `
        -Argument "--headless powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden $PsArguments" `
        -WorkingDirectory $repo
}

function Register-Job([string]$Name, [string]$Command, $Trigger, [string]$Description,
                      $JobSettings = $settings) {
    if ($Only -and $Only -ne $Name) { return }
    $action = New-HiddenAction "-File `"$script`" -Python `"$python`" -Command $Command"
    Register-ScheduledTask -TaskPath '\hk-estate\' -TaskName $Name -Action $action `
        -Trigger $Trigger -Settings $JobSettings -Principal $principal `
        -Description $Description -Force | Out-Null
}

# The daily run reads every due Centanet page (no cap since 2026-10-20): the first pass is
# ~14 h, so it may run up to 23 h (IgnoreNew: the next day's start is skipped while it runs).
$dailySettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 23)
Register-Job 'daily listings' 'daily' (New-ScheduledTaskTrigger -Daily -At '08:00') `
    'HK Estate: snapshot listings (Centanet, 28Hse, Spacious) and prune cached listing pages.' `
    $dailySettings
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

# New-ScheduledTaskTrigger has no -Monthly in Windows PowerShell 5.1: register with a daily
# trigger, then swap its <ScheduleByDay> for a "day 1 of every month" <ScheduleByMonth>.
Register-Job 'monthly blocked-check' 'check-blocked' (New-ScheduledTaskTrigger -Daily -At '09:00') `
    'HK Estate: request robots.txt of the blocked sites (Midland, Squarefoot, house730, HKP) once; log any that now answer 200.'
if (-not $Only -or $Only -eq 'monthly blocked-check') {
    $xml = Export-ScheduledTask -TaskPath '\hk-estate\' -TaskName 'monthly blocked-check'
    $months = 'January February March April May June July August September October November December'.Split(' ') |
        ForEach-Object { "<$_ />" }
    $monthly = '<ScheduleByMonth><DaysOfMonth><Day>1</Day></DaysOfMonth><Months>' +
        ($months -join '') + '</Months></ScheduleByMonth>'
    $xml = $xml -replace '(?s)<ScheduleByDay>.*?</ScheduleByDay>', $monthly
    Register-ScheduledTask -TaskPath '\hk-estate\' -TaskName 'monthly blocked-check' -Xml $xml -Force | Out-Null
}
if (-not $Only -or $Only -eq 'watchdog') {
    $wdScript = Join-Path $repo 'scripts\watchdog.ps1'
    $wdAction = New-HiddenAction "-File `"$wdScript`" -Python `"$python`""
    # every 15 minutes from midnight today, for 10 years (an open-ended repetition is not
    # accepted by every Windows build)
    $wdTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).Date `
        -RepetitionInterval (New-TimeSpan -Minutes 15) -RepetitionDuration (New-TimeSpan -Days 3650)
    $wdSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
    Register-ScheduledTask -TaskPath '\hk-estate\' -TaskName 'watchdog' -Action $wdAction `
        -Trigger $wdTrigger -Settings $wdSettings -Principal $principal `
        -Description 'HK Estate: restart backfill-territory, cd-history, ricacorp or today''s daily run if it died (log: data\logs\watchdog_<date>.log).' `
        -Force | Out-Null
}
Get-ScheduledTask -TaskPath '\hk-estate\' |
    Select-Object TaskPath, TaskName, State,
        @{n = 'Trigger'; e = { ($_.Triggers | ForEach-Object { $_.StartBoundary }) -join ', ' } },
        @{n = 'StartWhenAvailable'; e = { $_.Settings.StartWhenAvailable } }
