@echo off
:loop
cd /d C:\Users\jorda\projects\reselling-agent
C:\Users\jorda\AppData\Local\Python\pythoncore-3.14-64\python.exe agents\telegram_bot.py
echo Bot exited — restarting in 30 seconds...
timeout /t 30 /nobreak
goto loop
