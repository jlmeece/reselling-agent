# register_ebay_sync_task.ps1 - (re)register the WAT-EbaySync scheduled task.
# Idempotent: -Force replaces any existing task of the same name.
# Runs `scheduler.py --mode ebay_sync` every 2 hours as the current user, hidden,
# no overlap. Read-only on eBay; writes only units_sold (col U) on the sheet.
# Add -DryRun to register a report-only task (no sheet writes).
#
# Usage: powershell -ExecutionPolicy Bypass -File tools\register_ebay_sync_task.ps1 [-DryRun]

param([switch]$DryRun)

$root   = Split-Path -Parent $PSScriptRoot
$python = "C:\Users\jorda\AppData\Local\Python\pythoncore-3.14-64\python.exe"
if (-not (Test-Path $python)) { throw "python not found at $python" }

$extra  = if ($DryRun) { " --dry-run" } else { "" }
# cd first: load_dotenv reads .env from the working directory.
$cmd    = "cd /d `"$root`" && `"$python`" agents\scheduler.py --mode ebay_sync$extra >> data\logs\ebay_sync.log 2>&1"
$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c $cmd"

$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
    -RepetitionInterval (New-TimeSpan -Hours 2)

$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15)

Register-ScheduledTask -TaskName "WAT-EbaySync" -Action $action -Trigger $trigger `
    -Settings $settings -User $env:USERNAME -RunLevel Limited -Force `
    -Description "Sync eBay active listings into the Product Tracker (units_sold) and flag mismatches" | Out-Null

Get-ScheduledTask -TaskName "WAT-EbaySync" | Select-Object TaskName, State
