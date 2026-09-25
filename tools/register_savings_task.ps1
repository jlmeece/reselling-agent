# register_savings_task.ps1 - (re)register the WAT-Savings scheduled task.
# Idempotent: -Force replaces any existing task of the same name.
# Runs `scheduler.py --mode savings` once a day (default 08:50 local/Central) as the current user,
# hidden, no overlap. Needs the real Chrome session (Costco cookies), so it only works on this PC.
# Scrapes Costco's Member-Only Savings page, updates tracked rows' sale columns (G/X/AW), adds
# in-category new items as PENDING and sends ONE Telegram message for new sales. 08:50 is before the
# 09:30 active check / 09:45 Sale Radar so both see fresh sale data; the scheduler run-lock SKIPS
# (not queues) a run that starts while another holds it. Keep it clear of WAT-Audit 08:00.
# Add -DryRun to register a task that prints to the log instead of writing/sending.
#
# Usage: powershell -ExecutionPolicy Bypass -File tools\register_savings_task.ps1 [-DryRun] [-At "08:50"]

param(
    [switch]$DryRun,
    [string]$At = "08:50"
)

$root   = Split-Path -Parent $PSScriptRoot
$python = "C:\Users\jorda\AppData\Local\Python\pythoncore-3.14-64\python.exe"
if (-not (Test-Path $python)) { throw "python not found at $python" }

$extra  = if ($DryRun) { " --dry-run" } else { "" }
# cd first: load_dotenv reads .env from the working directory.
$cmd    = "cd /d `"$root`" && `"$python`" agents\scheduler.py --mode savings$extra >> data\logs\savings.log 2>&1"
$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c $cmd"
$trigger = New-ScheduledTaskTrigger -Daily -At $At

$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

Register-ScheduledTask -TaskName "WAT-Savings" -Action $action -Trigger $trigger `
    -Settings $settings -User $env:USERNAME -RunLevel Limited -Force `
    -Description "Daily Costco Member-Only Savings scrape: update tracked sales, alert, add new PENDING rows" | Out-Null

Get-ScheduledTask -TaskName "WAT-Savings" | Select-Object TaskName, State
(Get-ScheduledTask -TaskName "WAT-Savings").Triggers | ForEach-Object { $_.StartBoundary }
