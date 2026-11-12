# register_backfill_task.ps1 - (re)register the WAT-PhotoBackfill scheduled task.
# Idempotent: -Force replaces any existing task of the same name.
# Runs backfill_active_photos.py once a day (default 11:00 local/Central) to stamp the branded
# main photo onto every live eBay listing (col Q item id) that doesn't have it yet — catches
# listings created manually / outside the export pipeline. Idempotent: already-branded listings
# are skipped with one GetItem each. Windows-only (eBay OAuth token + Photoshop template local).

param(
    [switch]$DryRun,
    [string]$At = "11:00"
)

$root   = Split-Path -Parent $PSScriptRoot
$python = "C:\Users\jorda\AppData\Local\Python\pythoncore-3.14-64\python.exe"
if (-not (Test-Path $python)) { throw "python not found at $python" }

$extra  = if ($DryRun) { " --dry-run" } else { "" }
$cmd    = "cd /d `"$root`" && `"$python`" tools\backfill_active_photos.py$extra >> data\logs\backfill_photos.log 2>&1"
$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c $cmd"
$trigger = New-ScheduledTaskTrigger -Daily -At $At

$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

Register-ScheduledTask -TaskName "WAT-PhotoBackfill" -Action $action -Trigger $trigger `
    -Settings $settings -User $env:USERNAME -RunLevel Limited -Force `
    -Description "Daily branded-photo backfill onto live eBay listings missing it (idempotent)" | Out-Null

Get-ScheduledTask -TaskName "WAT-PhotoBackfill" | Select-Object TaskName, State
