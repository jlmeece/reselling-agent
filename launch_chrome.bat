@echo off
:: Launches Chrome with the dedicated agent profile and debug port.
:: Use this when you want Chrome open while the agent runs.
:: Your normal Chrome profile is untouched.
start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" ^
  --remote-debugging-port=9222 ^
  --remote-allow-origins=http://localhost:9222 ^
  --user-data-dir="%LOCALAPPDATA%\CostcoAgentProfile" ^
  --no-first-run ^
  --no-default-browser-check ^
  --disable-extensions ^
  --disable-sync
