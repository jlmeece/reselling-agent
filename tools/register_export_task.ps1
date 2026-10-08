# register_export_task.ps1 - (re)register the WAT-Export scheduled task.
# Idempotent: -Force replaces any existing task of the same name.
# Runs `scheduler.py --mode export` once a day (default 10:30 local/Central) as the current
# user, hidden, no overlap: branded photos -> eBay picture hosting -> Seller Hub CSV of READY
# rows in data\exports\. Sends ONE Telegram message (the CSV attached) only when the export
# differs from the last one sent; silent with 0 READY rows. 10:30 sits 30 min after WAT-Daily
# 10:00 (APPROVED->READY), so today's promotions are included; the scheduler run-lock SKIPS
# (not queues) a run that starts while another holds it. Must run on this PC: the photos,
# data\hosted_photos.json and the CSV are local files.
# Add -DryRun to register a task that only logs counts (no photos, uploads, CSV or Telegram).
#
# Usage: powershell -ExecutionPolicy Bypass -File tools\register_export_task.ps1 [-DryRun] [-At "10:30"]

param(
    [switch]$DryRun,
    [string]$At = "10:30"
)

$root   = Split-Path -Parent $PSScriptRoot
$python = "C:\Users\jorda\AppData\Local\Python\pythoncore-3.14-64\python.exe"
if (-not (Test-Path $python)) { throw "python not found at $python" }

$extra  = if ($DryRun) { " --dry-run" } else { "" }
# cd first: load_dotenv reads .env from the working directory.
$cmd    = "cd /d `"$root`" && `"$python`" agents\scheduler.py --mode export$extra >> data\logs\export.log 2>&1"
$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c $cmd"
$trigger = New-ScheduledTaskTrigger -Daily -At $At

$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 20)

Register-ScheduledTask -TaskName "WAT-Export" -Action $action -Trigger $trigger `
    -Settings $settings -User $env:USERNAME -RunLevel Limited -Force `
    -Description "Daily listing export: branded photos -> eBay hosting -> Seller Hub CSV (Telegram on change)" | Out-Null

Get-ScheduledTask -TaskName "WAT-Export" | Select-Object TaskName, State
(Get-ScheduledTask -TaskName "WAT-Export").Triggers | ForEach-Object { $_.StartBoundary }
