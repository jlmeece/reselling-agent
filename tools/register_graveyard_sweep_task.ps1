# register_graveyard_sweep_task.ps1 - (re)register the WAT-GraveyardSweep scheduled task.
# Idempotent: -Force replaces any existing task of the same name.
# Runs `scheduler.py --mode graveyard-sweep` once a day (default 11:00 local/Central) as the
# current user, hidden, no overlap. Needs the real Chrome session (Costco cookies), so it only
# works on this PC. Re-checks ~10 un-checked Graveyard items' Costco pages and writes a verdict
# (DEAD / ALIVE / REVIVE) to Graveyard cols P/Q; sends ONE Telegram message only when something
# is worth reviving or a scrape errored. 11:00 sits after WAT-Daily 10:00 and before the 13:00
# active check; the scheduler run-lock SKIPS (not queues) a run that starts while another holds it.
# Add -DryRun to register a task that prints to the log instead of writing/sending.
#
# Usage: powershell -ExecutionPolicy Bypass -File tools\register_graveyard_sweep_task.ps1 [-DryRun] [-At "11:00"]

param(
    [switch]$DryRun,
    [string]$At = "11:00"
)

$root   = Split-Path -Parent $PSScriptRoot
$python = "C:\Users\jorda\AppData\Local\Python\pythoncore-3.14-64\python.exe"
if (-not (Test-Path $python)) { throw "python not found at $python" }

$extra  = if ($DryRun) { " --dry-run" } else { "" }
# cd first: load_dotenv reads .env from the working directory.
$cmd    = "cd /d `"$root`" && `"$python`" agents\scheduler.py --mode graveyard-sweep$extra >> data\logs\graveyard_sweep.log 2>&1"
$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c $cmd"
$trigger = New-ScheduledTaskTrigger -Daily -At $At

$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

Register-ScheduledTask -TaskName "WAT-GraveyardSweep" -Action $action -Trigger $trigger `
    -Settings $settings -User $env:USERNAME -RunLevel Limited -Force `
    -Description "Daily graveyard re-check: verify ~10 removed items' Costco pages (dead/revive/alive)" | Out-Null

Get-ScheduledTask -TaskName "WAT-GraveyardSweep" | Select-Object TaskName, State
(Get-ScheduledTask -TaskName "WAT-GraveyardSweep").Triggers | ForEach-Object { $_.StartBoundary }
