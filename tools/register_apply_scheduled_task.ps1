# register_apply_scheduled_task.ps1 - (re)register the WAT-ApplyScheduled scheduled task.
# Idempotent: -Force replaces any existing task of the same name.
# Runs `scheduler.py --mode apply_scheduled` every 10 minutes, all day, as the current user,
# hidden, no overlap. Each tick (1) sends "Reprice / End at sale end" prompts for ACTIVE
# listings whose Costco sale ends within 24h and (2) applies actions Jay approved whose sale end
# has passed, after a live Costco re-check. A quiet tick (nothing due) is a few seconds, never
# opens Chrome, never takes the scheduler lock and writes no Run Log row. When an action IS due
# it takes the scheduler lock; if another run holds it, it simply retries on the next tick.
#
# Usage: powershell -ExecutionPolicy Bypass -File tools\register_apply_scheduled_task.ps1 [-EveryMinutes 10]

param(
    [int]$EveryMinutes = 10
)

$root   = Split-Path -Parent $PSScriptRoot
$python = "C:\Users\jorda\AppData\Local\Python\pythoncore-3.14-64\python.exe"
if (-not (Test-Path $python)) { throw "python not found at $python" }

# cd first: load_dotenv reads .env from the working directory.
$cmd    = "cd /d `"$root`" && `"$python`" agents\scheduler.py --mode apply_scheduled >> data\logs\apply_scheduled.log 2>&1"
$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c $cmd"

# Start at the next whole 10 minutes, repeat forever.
$now   = Get-Date
$start = $now.Date.AddMinutes([math]::Ceiling($now.TimeOfDay.TotalMinutes / $EveryMinutes) * $EveryMinutes)
$trigger = New-ScheduledTaskTrigger -Once -At $start `
    -RepetitionInterval (New-TimeSpan -Minutes $EveryMinutes)

$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15)

Register-ScheduledTask -TaskName "WAT-ApplyScheduled" -Action $action -Trigger $trigger `
    -Settings $settings -User $env:USERNAME -RunLevel Limited -Force `
    -Description "Pre-stage sale-end reprice/End prompts and apply Jay-approved actions at Costco sale end" | Out-Null

Get-ScheduledTask -TaskName "WAT-ApplyScheduled" | Select-Object TaskName, State
(Get-ScheduledTask -TaskName "WAT-ApplyScheduled").Triggers | ForEach-Object {
    "$($_.StartBoundary)  every $($_.Repetition.Interval)"
}
