# register_ebay_sync_task.ps1 - (re)register the WAT-EbaySync scheduled task.
# Idempotent: -Force replaces any existing task of the same name.
# Runs `scheduler.py --mode ebay_sync` 4x/day (default 10:10, 14:10, 18:10, 22:10 local/Central)
# as the current user, hidden, no overlap. The :10 offset keeps it clear of the other WAT tasks
# (Daily 10:00, Active 9:30/13:00/18:00/21:00) - the scheduler run-lock SKIPS, not queues, a run
# that starts while another holds it. Read-only on eBay; writes only units_sold (col U).
# Add -DryRun to register a report-only task (no sheet writes).
#
# Usage: powershell -ExecutionPolicy Bypass -File tools\register_ebay_sync_task.ps1 [-DryRun] [-Times "10:10","14:10","18:10","22:10"]

param(
    [switch]$DryRun,
    [string[]]$Times = @("10:10", "14:10", "18:10", "22:10")
)

$root   = Split-Path -Parent $PSScriptRoot
$python = "C:\Users\jorda\AppData\Local\Python\pythoncore-3.14-64\python.exe"
if (-not (Test-Path $python)) { throw "python not found at $python" }

$extra  = if ($DryRun) { " --dry-run" } else { "" }
# cd first: load_dotenv reads .env from the working directory.
$cmd    = "cd /d `"$root`" && `"$python`" agents\scheduler.py --mode ebay_sync$extra >> data\logs\ebay_sync.log 2>&1"
$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c $cmd"

$triggers = @($Times | ForEach-Object { New-ScheduledTaskTrigger -Daily -At $_ })

$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15)

Register-ScheduledTask -TaskName "WAT-EbaySync" -Action $action -Trigger $triggers `
    -Settings $settings -User $env:USERNAME -RunLevel Limited -Force `
    -Description "Sync eBay active listings into the Product Tracker (units_sold) and flag margin breaches" | Out-Null

Get-ScheduledTask -TaskName "WAT-EbaySync" | Select-Object TaskName, State
(Get-ScheduledTask -TaskName "WAT-EbaySync").Triggers | ForEach-Object { $_.StartBoundary }
