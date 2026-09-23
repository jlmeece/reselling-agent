# watchdog.ps1 - WAT Framework self-heal watchdog for the Telegram bot.
#
# Run every 5 minutes by the WAT-Watchdog scheduled task
# (register with tools\register_watchdog_task.ps1). Two failure modes:
#
#   DEAD: neither the restart-loop batch file (start_telegram_bot.bat) nor the
#         bot's python.exe is running -> start the WAT-TelegramBot task.
#   HUNG: python.exe is running but data\.telegram_bot.alive has not been
#         touched for -StaleMinutes. The bot touches that file every 60 s from
#         its asyncio event loop, and only while polling is running, so a stuck
#         loop or a silently stopped poller stops the touches. -> kill ONLY that
#         python.exe; the .bat loop relaunches it after 30 s.
#
# It never kills the cmd.exe loop and never starts the task while the loop is
# alive (WAT-TelegramBot is MultipleInstances=IgnoreNew anyway).
#
# Staleness is measured from max(alive-file mtime, process start time), so a
# freshly started bot gets a full grace window, and a bot that hangs on every
# start is killed at most once per -StaleMinutes.
#
# Test switches: -DryRun (log what would happen, change nothing),
# -StaleMinutes, -AliveFile / -ProcessPattern / -LoopPattern (point at a fake
# process), -NoAlert, -AlertTest (send one Telegram test message and exit).

[CmdletBinding()]
param(
    [double]$StaleMinutes = 10,
    [switch]$DryRun,
    [switch]$NoAlert,
    [switch]$AlertTest,
    [string]$AliveFile = (Join-Path $PSScriptRoot "data\.telegram_bot.alive"),
    [string]$ProcessPattern = 'agents\\telegram_bot\.py',
    [string]$LoopPattern = 'start_telegram_bot\.bat',
    [string]$TaskName = "WAT-TelegramBot"
)

$root    = $PSScriptRoot
$logDir  = Join-Path $root "data\logs"
$logFile = Join-Path $logDir "watchdog.log"

function Write-Log($msg) {
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $prefix = ""
    if ($DryRun) { $prefix = "[dry-run] " }
    Add-Content -Path $logFile -Value "$ts $prefix$msg" -Encoding utf8
    Write-Verbose "$prefix$msg"
}

function Limit-LogSize {
    # Keep watchdog.log bounded: over 1 MB -> keep the last 500 lines.
    if ((Test-Path $logFile) -and ((Get-Item $logFile).Length -gt 1MB)) {
        $tail = Get-Content $logFile -Tail 500
        Set-Content -Path $logFile -Value $tail -Encoding utf8
    }
}

function Get-DotEnvValue($name) {
    $envFile = Join-Path $root ".env"
    if (-not (Test-Path $envFile)) { return $null }
    foreach ($line in Get-Content $envFile) {
        if ($line -match "^\s*$name\s*=\s*(.*?)\s*$") {
            return $Matches[1].Trim('"').Trim("'")
        }
    }
    return $null
}

function Send-Alert($text) {
    if ($NoAlert -or $DryRun) { return }
    try {
        $token = Get-DotEnvValue "TELEGRAM_BOT_TOKEN"
        $chat  = Get-DotEnvValue "TELEGRAM_CHAT_ID"
        if (-not $token -or -not $chat) {
            Write-Log "Alert skipped: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not in .env"
            return
        }
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-RestMethod -Uri "https://api.telegram.org/bot$token/sendMessage" -Method Post `
            -Body @{ chat_id = $chat; text = $text } -TimeoutSec 15 | Out-Null
        Write-Log "Telegram alert sent."
    } catch {
        # Never log the exception text verbatim: it can contain the bot-token URL.
        Write-Log "Telegram alert FAILED ($($_.Exception.GetType().Name))."
    }
}

try {
    if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
    Limit-LogSize

    if ($AlertTest) {
        Send-Alert "WAT watchdog: alert test (no action needed)."
        return
    }

    $all = @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='cmd.exe'")
    $bots  = @($all | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -match $ProcessPattern })
    $loops = @($all | Where-Object { $_.Name -eq 'cmd.exe'    -and $_.CommandLine -match $LoopPattern })

    if ($bots.Count -eq 0) {
        if ($loops.Count -gt 0) {
            # Loop is alive and in its 30s/90s restart wait - let it do its job.
            return
        }
        Write-Log "No bot python and no restart loop - starting $TaskName task."
        if (-not $DryRun) { Start-ScheduledTask -TaskName $TaskName }
        return
    }

    foreach ($bot in $bots) {
        $last = $bot.CreationDate
        $source = "process start"
        if (Test-Path $AliveFile) {
            $mtime = (Get-Item $AliveFile).LastWriteTime
            if ($mtime -gt $last) { $last = $mtime; $source = "alive file" }
        }
        $ageMin = ((Get-Date) - $last).TotalMinutes

        if ($ageMin -gt $StaleMinutes) {
            Write-Log ("Bot PID {0} looks hung: no liveness for {1:N1} min (since {2}, from {3}); threshold {4} min. Killing python only - .bat loop will relaunch." -f `
                $bot.ProcessId, $ageMin, $last.ToString("HH:mm:ss"), $source, $StaleMinutes)
            if (-not $DryRun) {
                Stop-Process -Id $bot.ProcessId -Force -ErrorAction Stop
                Write-Log "Killed PID $($bot.ProcessId)."
                Send-Alert ("WAT watchdog: bot (PID {0}) stopped responding for {1:N0} min. Force-restarted; it should be back within ~30s." -f $bot.ProcessId, $ageMin)
            }
        }
    }
} catch {
    Write-Log "Watchdog error: $($_.Exception.Message)"
}
