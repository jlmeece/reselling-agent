# Deployment — current architecture (2026-11-03)

**Everything that writes to the sheet or eBay runs on Windows.**

- Every sheet-writing mode — audit, research, daily, active, discovery, ebay_sync, savings,
  sale-digest, apply_scheduled, graveyard-sweep, rotation, export — runs on Jordan's Windows PC
  via Task Scheduler (`WAT-*` tasks), from `C:\Users\jorda\projects\reselling-agent`.
  The tasks are registered by `tools/register_*_task.ps1`.
- The Telegram bot runs on the same PC (`WAT-TelegramBot` → `start_telegram_bot.bat`, kept
  alive by `WAT-Watchdog` → `watchdog.ps1`).
- **The VPS runs nothing for the reselling agent.** Hermes (the orchestration agent) lives on the
  VPS and only `git pull`s this repo every 5 min into `/opt/data/projects/reselling-agent`.
  It holds no Google credentials for the sheet.
- **There is no VPS cron for the reselling agent.** A stale June-2026 cron on the VPS host once
  ran old code (audit 13:00 / research 14:00 / daily 15:00 UTC)
  and deleted live rows; it and its Google credentials were removed on 2026-11-03. If Run Log rows
  appear stamped exactly on the hour at those times again, something is running old code — stop it.

`Dockerfile`, `docker-compose.yml` and `README.md` in this folder describe an earlier Docker-on-VPS
plan that is not deployed.
