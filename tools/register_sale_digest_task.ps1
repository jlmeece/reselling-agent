# register_sale_digest_task.ps1 - (re)register the WAT-SaleDigest scheduled task.
# Idempotent: -Force replaces any existing task of the same name.
# Runs `scheduler.py --mode sale-digest` once a day (default 09:45 local/Central) as the current
# user, hidden, no overlap. Read-only on the sheet; sends ONE Telegram "Sale Radar" message when
# something is really on sale (silent otherwise). 09:45 sits after WAT-Active-930 (~2 min) and
# WAT-Research (~5 min) and before WAT-Daily 10:00, so it reads fresh sale badges; the scheduler
# run-lock SKIPS (not queues) a run that starts while another holds it.
# Add -DryRun to register a task that prints to the log instead of sending.
#
# Usage: powershell -ExecutionPolicy Bypass -File tools\register_sale_digest_task.ps1 [-DryRun] [-At "09:45"]

param(
    [switch]$DryRun,
    [string]$At = "09:45"
)

$root   = Split-Path -Parent $PSScriptRoot
$python = "C:\Users\jorda\AppData\Local\Python\pythoncore-3.14-64\python.exe"
if (-not (Test-Path $python)) { throw "python not found at $python" }

$extra  = if ($DryRun) { " --dry-run" } else { "" }
# cd first: load_dotenv reads .env from the working directory.
$cmd    = "cd /d `"$root`" && `"$python`" agents\scheduler.py --mode sale-digest$extra >> data\logs\sale_digest.log 2>&1"
$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c $cmd"
$trigger = New-ScheduledTaskTrigger -Daily -At $At

$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

Register-ScheduledTask -TaskName "WAT-SaleDigest" -Action $action -Trigger $trigger `
    -Settings $settings -User $env:USERNAME -RunLevel Limited -Force `
    -Description "Daily Sale Radar: one Telegram digest of tracked items that are really on sale" | Out-Null

Get-ScheduledTask -TaskName "WAT-SaleDigest" | Select-Object TaskName, State
(Get-ScheduledTask -TaskName "WAT-SaleDigest").Triggers | ForEach-Object { $_.StartBoundary }
