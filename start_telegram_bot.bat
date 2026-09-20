@echo off
:loop
cd /d C:\Users\jorda\projects\reselling-agent
C:\Users\jorda\AppData\Local\Python\pythoncore-3.14-64\python.exe agents\telegram_bot.py
set code=%ERRORLEVEL%
if "%code%"=="3" (
    echo Conflict detected — waiting 90s for Telegram to release the session...
    timeout /t 90 /nobreak
) else (
    echo Bot exited with code %code% — restarting in 30s...
    timeout /t 30 /nobreak
)
goto loop
