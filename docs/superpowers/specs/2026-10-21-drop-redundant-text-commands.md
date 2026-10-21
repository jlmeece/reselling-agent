# Drop redundant text commands — buttons only (Oct 21 2026)

## Goal

The bot has two parallel ways to do the same thing: slash commands (`/lookup`, `/dashboard`,
`/logs`, `/menu`) and the inline-keyboard buttons, which already cover every one of them. That's
double surface area — Jay has to remember which to use, and `/help` dumps a wall of commands that
mostly duplicate buttons. Collapse to buttons-only for day-to-day use; keep only the slash commands
that have no button equivalent.

## Change (agents/telegram_bot.py)

1. **Remove** these command handlers and their `cmd_*` functions:
   - `/menu` → the Dashboard button is the home screen
   - `/lookup` → the 🔍 Search button
   - `/dashboard` → the 📊 Dashboard button
   - `/logs` → the Logs screen under ⚙️ Run a Mode / Operations
2. **Keep** these (no button equivalent — they're summon/admin/diagnostics, not daily nav):
   - `/start` — shows the persistent button keyboard
   - `/status` — last run time, pass/fail, cookie age
   - `/restart` — reload bot after a code update
   - `/help` — rewrite to list **only** the three kept commands (`/start`, `/status`, `/restart`),
     plus one line: "Everything else is a button below."
3. Verify the persistent keyboard is still shown on `/start` (and after any menu action that might
   dismiss it) so Jay never needs a command to navigate.

## Tests (tests/test_telegram_bot.py, tests/test_menu.py)

- Update any test that asserts a removed command handler exists — drop those assertions.
- Add/keep a test that `/help` lists only `/start`, `/status`, `/restart` (and not `/lookup`).

## Acceptance

- Run the curated safe suite. Commit message:
  `refactor: drop redundant text commands, buttons only (keep /start /status /restart)`,
  push to `master`, reply `DONE — pushed`.
