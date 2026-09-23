# register_watchdog_task.ps1 - (re)register the WAT-Watchdog scheduled task.
# Idempotent: -Force replaces any existing task of the same name.
# Runs watchdog.ps1 every 5 minutes as the current user, hidden, no overlap.
#
# Usage: powershell -ExecutionPolicy Bypass -File tools\register_watchdog_task.ps1

$root   = Split-Path -Parent $PSScriptRoot
$script = Join-Path $root "watchdog.ps1"
if (-not (Test-Path $script)) { throw "watchdog.ps1 not found at $script" }

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`""

# Start one minute from now, repeat every 5 minutes indefinitely.
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 5)

$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 4)

Register-ScheduledTask -TaskName "WAT-Watchdog" -Action $action -Trigger $trigger `
    -Settings $settings -User $env:USERNAME -RunLevel Limited -Force `
    -Description "Restarts the Telegram bot if dead or hung (see watchdog.ps1)" | Out-Null

Get-ScheduledTask -TaskName "WAT-Watchdog" | Select-Object TaskName, State
